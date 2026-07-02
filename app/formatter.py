"""
Converts a ReviewResult into a clean, readable Markdown comment suitable for
posting on a GitHub PR.
"""
from app.llm_review import ReviewFinding, ReviewResult

SEVERITY_ORDER = ["critical", "suggestion", "praise"]
SEVERITY_LABELS = {
    "critical": "🔴 Critical",
    "suggestion": "🟡 Suggestion",
    "praise": "🟢 Praise",
}


def _format_finding(f: ReviewFinding) -> str:
    location = f"`{f.file}`" + (f" (line {f.line})" if f.line else "")
    lines = [f"- **{location}** — {f.issue}"]
    if f.suggestion:
        lines.append(f"  - *Suggestion:* {f.suggestion}")
    return "\n".join(lines)


def format_review_comment(result: ReviewResult) -> str:
    parts = ["## 🤖 AI Code Review\n", result.summary.strip(), ""]

    if result.parse_failed:
        parts.append(
            "\n> ⚠️ The automated reviewer had trouble formatting its findings "
            "this time. Please treat this PR as manually-reviewed for now."
        )
        return "\n".join(parts)

    if not result.findings:
        parts.append("\n✅ No issues found — nice work!")
        return "\n".join(parts)

    grouped: dict[str, list[ReviewFinding]] = {s: [] for s in SEVERITY_ORDER}
    for f in result.findings:
        grouped.setdefault(f.severity, []).append(f)

    counts = {s: len(grouped.get(s, [])) for s in SEVERITY_ORDER}
    summary_line = " · ".join(
        f"{SEVERITY_LABELS[s]}: {counts[s]}" for s in SEVERITY_ORDER if counts[s]
    )
    if summary_line:
        parts.append(f"**{summary_line}**\n")

    for severity in SEVERITY_ORDER:
        items = grouped.get(severity, [])
        if not items:
            continue
        parts.append(f"### {SEVERITY_LABELS[severity]}")
        for f in items:
            parts.append(_format_finding(f))
        parts.append("")

    parts.append(
        "\n<sub>Generated automatically by CodeSage AI. "
        "Review history is logged for trend tracking.</sub>"
    )

    return "\n".join(parts)
