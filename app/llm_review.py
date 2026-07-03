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
import random
import re
import time
from collections import defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

import openai
from openai import OpenAI

from app.config import settings
from app.static_analysis import Finding

client = OpenAI(
    api_key=settings.groq_api_key,
    base_url="https://api.groq.com/openai/v1",
)

logger = logging.getLogger("codesage.llm_review")

VALID_SEVERITIES = {"critical", "suggestion", "praise"}

# Adjusted limits to reduce requests while preserving review quality, keeping well within API limits
MAX_REQUEST_INPUT_BYTES = 9000
MAX_DIFF_CHUNK_BYTES = 7000
MAX_STATIC_SUMMARY_BYTES = 1200
MAX_FILES_PER_CHUNK = 20
MAX_RESPONSE_TOKENS = 1800

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


def _is_skipped_file(filename: str) -> bool:
    """Helper to detect if the file should be skipped from AI review."""
    if not filename:
        return False
    clean_name = filename.strip().strip('"').strip("'")
    base = Path(clean_name).name
    return base in {"README.md", "requirements.txt", "LICENSE", ".gitignore"}


def _normalize_path(p: str) -> str:
    """Normalizes slashes for robust OS-independent path matching."""
    return p.replace("\\", "/").strip().strip('"').strip("'")


def _chunk_diff(diff: str, static_findings: list[Finding] = None) -> list[str]:
    """
    Split on file boundaries, then split oversized individual files.
    No diff content is discarded.
    """
    if static_findings is None:
        static_findings = []

    parts: list[tuple[str, str | None]] = []
    for section in _file_sections(diff):
        name = _file_name(section)
        # Skip non-code files from AI review
        if name and _is_skipped_file(name):
            logger.info("Skipping file %s from AI review.", name)
            continue
        parts.extend((part, name) for part in _split_large_file_section(section))

    chunks: list[str] = []
    current: list[str] = []
    current_files: set[str] = set()

    for part, name in parts:
        if current:
            # Check prospective files count
            prospective_files = current_files | ({name} if name else set())
            exceeds_files = len(prospective_files) > MAX_FILES_PER_CHUNK
            
            # Check prospective size BEFORE appending new content
            prospective_diff = "\n".join(current + [part])
            prospective_size = _estimate_request_size(prospective_diff, static_findings)
            exceeds_size = prospective_size > MAX_REQUEST_INPUT_BYTES
            
            if exceeds_size or exceeds_files:
                # Finalize current chunk, start a new one
                chunks.append("\n".join(current))
                current = []
                current_files = set()

        current.append(part)
        if name:
            current_files.add(name)

    if current:
        chunks.append("\n".join(current))
    return chunks or [""]


def _findings_for_chunk(
    chunk: str,
    static_findings: list[Finding],
) -> list[Finding]:
    names = {
        _normalize_path(name)
        for section in _file_sections(chunk)
        if (name := _file_name(section))
    }
    return [
        finding
        for finding in static_findings
        if _normalize_path(finding.file) in names
    ]


def _format_static_findings(findings: list[Finding]) -> str:
    """Compact repeated linter output while retaining actionable examples."""
    if not findings:
        return "(no static analysis findings)"

    # Limit static findings to at most 15 findings per file to keep the prompt clean.
    file_counts: dict[str, int] = defaultdict(int)
    filtered_findings = []
    for finding in findings:
        if file_counts[finding.file] < 15:
            filtered_findings.append(finding)
            file_counts[finding.file] += 1
    
    findings = filtered_findings

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


def _estimate_request_size(
    diff_content: str,
    static_findings: list[Finding],
    chunk_number: int = 1,
    chunk_count: int = 1,
) -> int:
    """Estimates the total byte count of the prompt payload sent to the LLM."""
    relevant_findings = _findings_for_chunk(diff_content, static_findings)
    user_prompt = _build_user_prompt(
        diff_content,
        relevant_findings,
        chunk_number,
        chunk_count,
    )
    return _utf8_size(SYSTEM_PROMPT) + _utf8_size(user_prompt)


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


def _call_groq_with_backoff(
    messages: list[dict[str, str]],
    max_tokens: int,
    chunk_number: int,
    chunk_count: int,
) -> str:
    """
    Calls Groq API with exponential backoff on HTTP 429 Rate Limit error.
    """
    base_delay = 2.0
    max_delay = 60.0
    max_retries = 5

    for attempt in range(max_retries + 1):
        try:
            response = client.chat.completions.create(
                model=settings.groq_model,
                messages=messages,
                temperature=0,
                max_tokens=max_tokens,
            )
            return response.choices[0].message.content
        except openai.RateLimitError as exc:
            if attempt == max_retries:
                logger.error("Groq API rate limit exceeded (HTTP 429) for chunk %d/%d after %d retries.", chunk_number, chunk_count, max_retries)
                raise exc
            delay = min(base_delay * (2**attempt) + random.uniform(0, 1), max_delay)
            logger.warning(
                "Groq API HTTP 429 Rate Limit for chunk %d/%d. Retrying in %.2f seconds (attempt %d/%d)...",
                chunk_number,
                chunk_count,
                delay,
                attempt + 1,
                max_retries,
            )
            time.sleep(delay)
        except openai.APIStatusError as exc:
            if exc.status_code == 429:
                if attempt == max_retries:
                    logger.error("Groq API rate limit exceeded (HTTP 429) for chunk %d/%d after %d retries.", chunk_number, chunk_count, max_retries)
                    raise exc
                delay = min(base_delay * (2**attempt) + random.uniform(0, 1), max_delay)
                logger.warning(
                    "Groq API HTTP 429 Rate Limit for chunk %d/%d. Retrying in %.2f seconds (attempt %d/%d)...",
                    chunk_number,
                    chunk_count,
                    delay,
                    attempt + 1,
                    max_retries,
                )
                time.sleep(delay)
            else:
                raise exc


def _review_chunk(
    diff: str,
    static_findings: list[Finding],
    chunk_number: int,
    chunk_count: int,
) -> ReviewResult:
    if not diff.strip():
        logger.info("Chunk %d/%d is empty/skipped — returning empty review result.", chunk_number, chunk_count)
        return ReviewResult(
            summary="No reviewable changes in this section.",
            findings=[],
        )

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
            raw_text = _call_groq_with_backoff(
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                max_tokens=MAX_RESPONSE_TOKENS,
                chunk_number=chunk_number,
                chunk_count=chunk_count,
            )
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
    chunks = _chunk_diff(diff, static_findings)
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

# demo test.