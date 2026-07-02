from app.formatter import format_review_comment
from app.llm_review import ReviewFinding, ReviewResult


def test_format_empty_findings():
    result = ReviewResult(summary="Looks clean.", findings=[])
    comment = format_review_comment(result)
    assert "No issues found" in comment
    assert "Looks clean." in comment


def test_format_groups_by_severity():
    result = ReviewResult(
        summary="Found some issues.",
        findings=[
            ReviewFinding(file="a.py", line=1, severity="critical", issue="SQLi", suggestion="Use params"),
            ReviewFinding(file="b.py", line=None, severity="praise", issue="Good naming", suggestion=""),
        ],
    )
    comment = format_review_comment(result)
    assert "🔴 Critical" in comment
    assert "🟢 Praise" in comment
    assert "a.py" in comment
    assert "b.py" in comment


def test_format_parse_failed_shows_warning():
    result = ReviewResult(summary="", findings=[], parse_failed=True)
    comment = format_review_comment(result)
    assert "trouble formatting" in comment
