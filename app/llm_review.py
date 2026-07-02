"""
Sends the diff + static analysis findings to Claude for deeper reasoning
(logic bugs, edge cases, security issues, best practices) and returns a
validated, structured result.

Design notes:
- The prompt forces strict JSON output via explicit schema + instructions.
- If parsing fails, we retry once with a stricter "JSON ONLY" nudge.
- If it still fails, we fall back to a minimal summary-only result so the
  pipeline never crashes just because the model added stray prose.
"""
from __future__ import annotations

import json
import logging
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

SYSTEM_PROMPT = """You are an expert senior software engineer performing a pull \
request code review. You review diffs the way a careful, pragmatic senior \
engineer would: focused on real bugs, security issues, and meaningful \
improvements — not nitpicking style that a linter already caught.

You will be given:
1. A unified diff of changed files
2. Findings from static analysis tools (may be empty)

Your job: find logic errors, edge cases, security vulnerabilities (e.g. SQL \
injection, hardcoded secrets, missing input validation), race conditions, and \
best-practice violations that go beyond what the static analysis already \
flagged. Also call out genuinely good patterns worth praising — reviews that \
are 100% criticism are less useful and less honest.

Respond with STRICT JSON ONLY. No markdown fences, no prose before or after \
the JSON. The JSON must match this exact schema:

{
  "summary": "1-3 sentence overview of the PR's changes and overall quality",
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
- Do not repeat findings the static analysis already reported verbatim — \
  reference them briefly if relevant, but focus on what a linter can't catch.
- If the diff is trivial or has no issues, return an empty findings list and \
  say so in the summary.
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


def _format_static_findings(findings: list[Finding]) -> str:
    if not findings:
        return "(no static analysis findings)"
    lines = []
    for f in findings:
        loc = f"{f.file}:{f.line}" if f.line else f.file
        rule = f.rule or ""
        lines.append(f"- [{f.tool}/{f.severity}] {loc} ({rule}): {f.message}")
    return "\n".join(lines)


def _build_user_prompt(diff: str, static_findings: list[Finding]) -> str:
    return f"""## Diff

```diff
{diff}
```

## Static analysis findings

{_format_static_findings(static_findings)}

Return the JSON review now."""


def _parse_response(raw_text: str) -> Optional[ReviewResult]:
    text = raw_text.strip()
    # Defensive: strip accidental markdown fences even though we asked for none
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


def run_review(diff: str, static_findings: list[Finding]) -> ReviewResult:
    user_prompt = _build_user_prompt(diff, static_findings)

    for attempt in range(2):
        prompt = user_prompt
        if attempt == 1:
            prompt += (
                "\n\nIMPORTANT: Your previous response was not valid JSON. "
                "Respond with ONLY the JSON object, nothing else."
            )

        try:
            response = client.chat.completions.create(
                model=settings.groq_model,
                messages=[
                    {"role": "system", "content": SYSTEM_PROMPT},
                    {"role": "user", "content": prompt},
                ],
                temperature=0,
            )

            raw_text = response.choices[0].message.content

        except Exception:
            logger.exception("Groq API call failed on attempt %d", attempt)
            continue

        result = _parse_response(raw_text)
        if result is not None:
            return result

        logger.warning("Failed to parse LLM JSON on attempt %d", attempt)

    return ReviewResult(
        summary="Automated review could not be generated due to a formatting error in the model response. Please review this PR manually.",
        findings=[],
        parse_failed=True,
    )