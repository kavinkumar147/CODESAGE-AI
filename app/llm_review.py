"""
Sends bounded diff chunks and summarized static-analysis context to Groq,
then merges the structured results into one review.

Every request is kept under a conservative byte ceiling. Llama's tokenizer
cannot produce more input tokens than the UTF-8 bytes supplied, so the byte
ceiling plus framing reserve guarantees fewer than 10,000 input tokens.
"""
from __future__ import annotations

import json
import logging
import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Optional

from openai import OpenAI

from app.config import settings
from app.static_analysis import Finding

client = OpenAI(
    api_key=settings.groq_api_key,
    base_url="https://api.groq.com/openai/v1",
)

logger = logging.getLogger("codesage.llm_review")

VALID_SEVERITIES = {"critical", "suggestion", "praise"}

# The total content sent in the system + user messages stays below this.
# A further ~1,500-token margin remains below the requested 10,000-token cap
# for chat-message framing and special tokens.
MAX_REQUEST_INPUT_BYTES = 8_500
MAX_DIFF_CHUNK_BYTES = 4_500
MAX_STATIC_SUMMARY_BYTES = 900
MAX_FILES_PER_CHUNK = 8
MAX_RESPONSE_TOKENS = 1_800

RETRY_SUFFIX = (
    "\n\nIMPORTANT: Your previous response was not valid JSON. "
    "Respond with ONLY the JSON object, nothing else."
)

SYSTEM_PROMPT = """You are an expert senior software engineer performing a pull \
request code review. You review diffs the way a careful, pragmatic senior \
engineer would: focused on real bugs, security issues, and meaningful \
improvements — not nitpicking style that a linter already caught.

You will be given:
1. A bounded chunk of a unified diff
2. A compact summary of relevant static analysis findings (may be empty)

Your job: find logic errors, edge cases, security vulnerabilities (e.g. SQL \
injection, hardcoded secrets, missing input validation), race conditions, and \
best-practice violations that go beyond what the static analysis already \
flagged. Also call out genuinely good patterns worth praising.

Respond with STRICT JSON ONLY. No markdown fences, no prose before or after \
the JSON. The JSON must match this exact schema:

{
  "summary": "1-3 sentence overview of the shown changes and their quality",
  "findings": [
    {
      "file": "path/to/file.py",
      "line": 42,
      "severity": "critical | suggestion | praise",
      "issue": "concise description of the problem or positive pattern",
      "suggestion": "concrete fix or acknowledgement"
    }
  ]
}

Rules:
- "line" may be null if the issue applies to the whole file.
- "severity" must be exactly one of: critical, suggestion, praise.
- Review only the changes shown; do not assume an omitted chunk is safe.
- Do not repeat static-analysis findings verbatim.
- If the shown changes have no issues, return an empty findings list.
"""


@dataclass
class ReviewFinding:
    file: str
    line: Optional[int]
    severity: str
    issue: str
    suggestion: str


@dataclass
class ReviewResult:
    summary: str
    findings: list[ReviewFinding] = field(default_factory=list)
    parse_failed: bool = False


def _utf8_size(text: str) -> int:
    return len(text.encode("utf-8"))


def _split_text_by_bytes(text: str, max_bytes: int) -> list[str]:
    """Split text without dropping characters or breaking UTF-8 boundaries."""
    if not text:
        return [""]
    if _utf8_size(text) <= max_bytes:
        return [text]

    chunks: list[str] = []
    current: list[str] = []
    current_size = 0

    for character in text:
        character_size = _utf8_size(character)
        if current and current_size + character_size > max_bytes:
            chunks.append("".join(current))
            current = []
            current_size = 0
        current.append(character)
        current_size += character_size

    if current:
        chunks.append("".join(current))
    return chunks


def _file_sections(diff: str) -> list[str]:
    starts = [match.start() for match in re.finditer(r"(?m)^diff --git ", diff)]
    if not starts:
        return [diff]

    starts.append(len(diff))
    return [
        diff[starts[index] : starts[index + 1]].strip()
        for index in range(len(starts) - 1)
    ]


def _file_name(section: str) -> str | None:
    match = re.search(r"(?m)^diff --git a/(.+?) b/(.+)$", section)
    if match:
        return match.group(2).strip('"')

    match = re.search(r"(?m)^\+\+\+ b/(.+)$", section)
    return match.group(1).strip('"') if match else None


def _split_large_file_section(section: str) -> list[str]:
    if _utf8_size(section) <= MAX_DIFF_CHUNK_BYTES:
        return [section]

    lines = section.splitlines(keepends=True)
    hunk_start = next(
        (index for index, line in enumerate(lines) if line.startswith("@@")),
        None,
    )
    if hunk_start is None:
        return _split_text_by_bytes(section, MAX_DIFF_CHUNK_BYTES)

    header = "".join(lines[:hunk_start])

    # Repeating a small file header keeps each partial hunk understandable.
    header_parts = _split_text_by_bytes(header, MAX_DIFF_CHUNK_BYTES // 3)
    safe_header = header_parts[0]
    body_budget = MAX_DIFF_CHUNK_BYTES - _utf8_size(safe_header) - 1
    body = "".join(header_parts[1:]) + "".join(lines[hunk_start:])
    body_parts = _split_text_by_bytes(body, max(body_budget, 512))

    if not body_parts:
        return _split_text_by_bytes(section, MAX_DIFF_CHUNK_BYTES)
    return [f"{safe_header}{part}" for part in body_parts]


def _chunk_diff(diff: str) -> list[str]:
    """
    Split on file boundaries, then split oversized individual files.
    No diff content is discarded.
    """
    parts: list[tuple[str, str | None]] = []
    for section in _file_sections(diff):
        name = _file_name(section)
        parts.extend((part, name) for part in _split_large_file_section(section))

    chunks: list[str] = []
    current: list[str] = []
    current_files: set[str] = set()
    current_size = 0

    for part, name in parts:
        part_size = _utf8_size(part)
        prospective_files = current_files | ({name} if name else set())
        exceeds_size = current and current_size + part_size + 1 > MAX_DIFF_CHUNK_BYTES
        exceeds_files = current and len(prospective_files) > MAX_FILES_PER_CHUNK

        if exceeds_size or exceeds_files:
            chunks.append("\n".join(current))
            current = []
            current_files = set()
            current_size = 0

        current.append(part)
        if name:
            current_files.add(name)
        current_size += part_size + (1 if current_size else 0)

    if current:
        chunks.append("\n".join(current))
    return chunks or [""]


def _findings_for_chunk(
    chunk: str,
    static_findings: list[Finding],
) -> list[Finding]:
    names = {
        name
        for section in _file_sections(chunk)
        if (name := _file_name(section))
    }
    if not names:
        return static_findings
    return [finding for finding in static_findings if finding.file in names]


def _format_static_findings(findings: list[Finding]) -> str:
    """Compact repeated linter output while retaining actionable examples."""
    if not findings:
        return "(no static analysis findings)"

    grouped: dict[tuple[str, str, str, str], list[Finding]] = defaultdict(list)
    for finding in findings:
        key = (
            finding.file,
            finding.tool,
            finding.severity,
            finding.rule or "no-rule",
        )
        grouped[key].append(finding)

    lines: list[str] = []
    for (file_name, tool, severity, rule), group in grouped.items():
        line_numbers = [str(item.line) for item in group if item.line is not None]
        locations = ", ".join(line_numbers[:5]) or "file-level"
        if len(line_numbers) > 5:
            locations += f", +{len(line_numbers) - 5} more"
        sample = group[0].message.strip()
        lines.append(
            f"- {file_name} [{tool}/{severity}/{rule}]: "
            f"{len(group)} finding(s), lines {locations}; example: {sample}"
        )

    summary = "\n".join(lines)
    if _utf8_size(summary) <= MAX_STATIC_SUMMARY_BYTES:
        return summary

    marker = "\n- Additional static findings omitted from prompt summary."
    available = MAX_STATIC_SUMMARY_BYTES - _utf8_size(marker)
    return _split_text_by_bytes(summary, available)[0].rstrip() + marker


def _build_user_prompt(
    diff: str,
    static_findings: list[Finding],
    chunk_number: int = 1,
    chunk_count: int = 1,
) -> str:
    return f"""## Review scope

Chunk {chunk_number} of {chunk_count}. Review every shown change.

## Diff

```diff
{diff}
```

## Static analysis summary

{_format_static_findings(static_findings)}

Return the JSON review now."""


def _parse_response(raw_text: str) -> Optional[ReviewResult]:
    text = raw_text.strip()
    if text.startswith("```"):
        text = text.strip("`")
        if text.lower().startswith("json"):
            text = text[4:]
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        return None

    summary = data.get("summary", "")
    raw_findings = data.get("findings", [])
    findings = []
    for item in raw_findings:
        severity = str(item.get("severity", "")).lower()
        if severity not in VALID_SEVERITIES:
            severity = "suggestion"
        findings.append(
            ReviewFinding(
                file=item.get("file", "unknown"),
                line=item.get("line"),
                severity=severity,
                issue=item.get("issue", ""),
                suggestion=item.get("suggestion", ""),
            )
        )
    return ReviewResult(summary=summary, findings=findings)


def _review_chunk(
    diff: str,
    static_findings: list[Finding],
    chunk_number: int,
    chunk_count: int,
) -> ReviewResult:
    user_prompt = _build_user_prompt(
        diff,
        static_findings,
        chunk_number,
        chunk_count,
    )

    for attempt in range(2):
        prompt = user_prompt + (RETRY_SUFFIX if attempt == 1 else "")
        input_bytes = _utf8_size(SYSTEM_PROMPT) + _utf8_size(prompt)

        # Fail closed instead of ever sending a request above the hard budget.
        if input_bytes > MAX_REQUEST_INPUT_BYTES:
            logger.error(
                "Refusing oversized Groq input for chunk %d/%d: %d bytes",
                chunk_number,
                chunk_count,
                input_bytes,
            )
            break

        try:
            response = client.chat.completions.create(
                model=settings.groq_model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
                max_tokens=MAX_RESPONSE_TOKENS,
            )
            raw_text = response.choices[0].message.content
        except Exception:
            logger.exception(
                "Groq API call failed for chunk %d/%d on attempt %d",
                chunk_number,
                chunk_count,
                attempt,
            )
            continue

        result = _parse_response(raw_text)
        if result is not None:
            return result

        logger.warning(
            "Failed to parse Groq JSON for chunk %d/%d on attempt %d",
            chunk_number,
            chunk_count,
            attempt,
        )

    return ReviewResult(
        summary=f"Automated review could not be generated for section {chunk_number}.",
        findings=[],
        parse_failed=True,
    )


def _merge_results(results: list[ReviewResult]) -> ReviewResult:
    successful = [result for result in results if not result.parse_failed]
    if not successful:
        return ReviewResult(
            summary=(
                "Automated review could not be generated due to a formatting "
                "or API error. Please review this PR manually."
            ),
            findings=[],
            parse_failed=True,
        )

    summaries = [result.summary.strip() for result in successful if result.summary.strip()]
    if len(results) == 1:
        summary = summaries[0] if summaries else "Review completed."
    else:
        summary = (
            f"CodeSage reviewed this pull request in {len(results)} bounded sections. "
            + " ".join(summaries)
        ).strip()
        if len(summary) > 1_500:
            summary = summary[:1_497].rstrip() + "..."

    findings: list[ReviewFinding] = []
    seen: set[tuple[str, Optional[int], str, str]] = set()
    for result in successful:
        for finding in result.findings:
            key = (
                finding.file,
                finding.line,
                finding.severity,
                finding.issue.strip().casefold(),
            )
            if key not in seen:
                seen.add(key)
                findings.append(finding)

    failed_count = len(results) - len(successful)
    if failed_count:
        logger.warning(
            "%d of %d review chunks failed; preserving successful findings.",
            failed_count,
            len(results),
        )

    return ReviewResult(summary=summary, findings=findings)


def run_review(diff: str, static_findings: list[Finding]) -> ReviewResult:
    chunks = _chunk_diff(diff)
    logger.info(
        "Reviewing diff in %d bounded Groq request(s), max %d bytes each.",
        len(chunks),
        MAX_REQUEST_INPUT_BYTES,
    )

    results = [
        _review_chunk(
            chunk,
            _findings_for_chunk(chunk, static_findings),
            index,
            len(chunks),
        )
        for index, chunk in enumerate(chunks, start=1)
    ]
    return _merge_results(results)
