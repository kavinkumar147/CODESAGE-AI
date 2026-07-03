import json
from types import SimpleNamespace
from unittest.mock import patch

from app.llm_review import (
    MAX_DIFF_CHUNK_BYTES,
    MAX_REQUEST_INPUT_BYTES,
    MAX_STATIC_SUMMARY_BYTES,
    SYSTEM_PROMPT,
    _chunk_diff,
    _format_static_findings,
    _parse_response,
    run_review,
)
from app.static_analysis import Finding


def test_parse_valid_json():
    raw = '''{
        "summary": "Test summary",
        "findings": [
            {"file": "a.py", "line": 5, "severity": "critical", "issue": "bug", "suggestion": "fix it"}
        ]
    }'''
    result = _parse_response(raw)
    assert result is not None
    assert result.summary == "Test summary"
    assert len(result.findings) == 1
    assert result.findings[0].severity == "critical"


def test_parse_strips_markdown_fences():
    raw = '```json\n{"summary": "ok", "findings": []}\n```'
    result = _parse_response(raw)
    assert result is not None
    assert result.summary == "ok"


def test_parse_invalid_severity_defaults_to_suggestion():
    raw = '{"summary": "x", "findings": [{"file": "a.py", "line": 1, "severity": "weird", "issue": "y", "suggestion": "z"}]}'
    result = _parse_response(raw)
    assert result.findings[0].severity == "suggestion"


def test_parse_garbage_returns_none():
    raw = "This is not JSON at all."
    result = _parse_response(raw)
    assert result is None


def test_large_diff_is_chunked_without_dropping_content():
    large_line = "+" + ("value = call(); " * 600)
    diff = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1 +1 @@\n"
        f"{large_line}\n"
        "TAIL_MARKER_A\n"
        "diff --git a/b.py b/b.py\n"
        "--- a/b.py\n"
        "+++ b/b.py\n"
        "@@ -1 +1 @@\n"
        f"{large_line}\n"
        "TAIL_MARKER_B\n"
    )

    chunks = _chunk_diff(diff)

    assert len(chunks) > 2
    assert all(len(chunk.encode("utf-8")) <= MAX_DIFF_CHUNK_BYTES for chunk in chunks)
    assert "TAIL_MARKER_A" in "".join(chunks)
    assert "TAIL_MARKER_B" in "".join(chunks)


def test_static_analysis_summary_is_bounded():
    findings = [
        Finding(
            file=f"module_{index}.py",
            line=index,
            tool="pylint",
            rule="long-rule",
            message="A detailed static analysis message " * 8,
            severity="warning",
        )
        for index in range(100)
    ]

    summary = _format_static_findings(findings)

    assert len(summary.encode("utf-8")) <= MAX_STATIC_SUMMARY_BYTES
    assert "Additional static findings omitted" in summary


def test_large_review_uses_bounded_requests_and_merges_findings():
    sections = []
    for index in range(6):
        sections.append(
            f"diff --git a/file_{index}.py b/file_{index}.py\n"
            f"--- a/file_{index}.py\n"
            f"+++ b/file_{index}.py\n"
            "@@ -1 +1 @@\n"
            + ("+changed_value = calculate_result()\n" * 80)
        )
    diff = "".join(sections)
    expected_chunks = len(_chunk_diff(diff))

    def bounded_response(**kwargs):
        messages = kwargs["messages"]
        input_bytes = sum(
            len(message["content"].encode("utf-8")) for message in messages
        )
        assert input_bytes <= MAX_REQUEST_INPUT_BYTES
        assert kwargs["max_tokens"] == 1_800

        payload = json.dumps(
            {
                "summary": "The shown changes were reviewed.",
                "findings": [
                    {
                        "file": "shared.py",
                        "line": 7,
                        "severity": "suggestion",
                        "issue": "Repeated issue",
                        "suggestion": "Apply the fix once.",
                    }
                ],
            }
        )
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=payload))]
        )

    with patch(
        "app.llm_review.client.chat.completions.create",
        side_effect=bounded_response,
    ) as create:
        result = run_review(diff=diff, static_findings=[])

    assert create.call_count == expected_chunks
    assert len(result.findings) == 1
    assert not result.parse_failed
    assert len(SYSTEM_PROMPT.encode("utf-8")) < MAX_REQUEST_INPUT_BYTES
