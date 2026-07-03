import json
from types import SimpleNamespace
from unittest.mock import patch

from app.llm_review import (
    MAX_DIFF_CHUNK_BYTES,
    MAX_REQUEST_INPUT_BYTES,
    MAX_STATIC_SUMMARY_BYTES,
    MAX_FILES_PER_CHUNK,
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
    # Scale large_line to be large enough to trigger chunking based on the current MAX_DIFF_CHUNK_BYTES
    multiplier = max(600, int((MAX_DIFF_CHUNK_BYTES * 1.5) / 16))
    large_line = "+" + ("value = call(); " * multiplier)
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
    # Scale number of files to exceed MAX_FILES_PER_CHUNK
    num_files = MAX_FILES_PER_CHUNK + 2
    for index in range(num_files):
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


def test_skip_non_code_files():
    diff = (
        "diff --git a/README.md b/README.md\n"
        "--- a/README.md\n"
        "+++ b/README.md\n"
        "@@ -1 +1 @@\n"
        "+This is a README\n"
        "diff --git a/app.py b/app.py\n"
        "--- a/app.py\n"
        "+++ b/app.py\n"
        "@@ -1 +1 @@\n"
        "+print('hello')\n"
    )
    chunks = _chunk_diff(diff)
    assert "README.md" not in "".join(chunks)
    assert "app.py" in "".join(chunks)


def test_findings_for_chunk_only_returns_relevant():
    chunk = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1 +1 @@\n"
        "+x = 1\n"
    )
    findings = [
        Finding(file="a.py", line=1, tool="bandit", rule="r1", message="m1", severity="error"),
        Finding(file="b.py", line=2, tool="bandit", rule="r2", message="m2", severity="warning"),
    ]
    from app.llm_review import _findings_for_chunk
    filtered = _findings_for_chunk(chunk, findings)
    assert len(filtered) == 1
    assert filtered[0].file == "a.py"


def test_call_groq_exponential_backoff():
    calls = []
    
    import openai
    import httpx
    from types import SimpleNamespace
    from app.llm_review import _call_groq_with_backoff
    
    mock_response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content="Mocked success response"))]
    )
    
    def mock_create(*args, **kwargs):
        calls.append(1)
        mock_request = httpx.Request("POST", "https://api.groq.com")
        if len(calls) == 1:
            mock_http_resp = httpx.Response(status_code=429, request=mock_request)
            raise openai.RateLimitError(
                message="Rate limit exceeded",
                response=mock_http_resp,
                body=None
            )
        elif len(calls) == 2:
            mock_http_resp = httpx.Response(status_code=429, request=mock_request)
            raise openai.APIStatusError(
                message="HTTP 429 Status Error",
                response=mock_http_resp,
                body=None
            )
        return mock_response

    with patch("app.llm_review.client.chat.completions.create", side_effect=mock_create):
        with patch("time.sleep") as mock_sleep:
            res = _call_groq_with_backoff(
                messages=[{"role": "user", "content": "hi"}],
                max_tokens=100,
                chunk_number=1,
                chunk_count=1
            )
            assert res == "Mocked success response"
            assert len(calls) == 3
            assert mock_sleep.call_count == 2


def test_chunk_diff_accounts_for_static_findings():
    diff = (
        "diff --git a/a.py b/a.py\n"
        "--- a/a.py\n"
        "+++ b/a.py\n"
        "@@ -1 +1 @@\n"
        "+x = 1\n"
        "diff --git a/b.py b/b.py\n"
        "--- a/b.py\n"
        "+++ b/b.py\n"
        "@@ -1 +1 @@\n"
        "+y = 2\n"
    )
    
    chunks_without = _chunk_diff(diff, [])
    assert len(chunks_without) == 1
    
    findings = [
        Finding(
            file="a.py",
            line=index,
            tool="bandit",
            rule=f"rule_{index}",
            message="A very long message to bloat the formatted static findings size " * 10,
            severity="error",
        )
        for index in range(40)
    ] + [
        Finding(
            file="b.py",
            line=index,
            tool="bandit",
            rule=f"rule_{index}",
            message="A very long message to bloat the formatted static findings size " * 10,
            severity="error",
        )
        for index in range(40)
    ]
    
    with patch("app.llm_review.MAX_REQUEST_INPUT_BYTES", 2000):
        chunks_with = _chunk_diff(diff, findings)
        assert len(chunks_with) > 1
