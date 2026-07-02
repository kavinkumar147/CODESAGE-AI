from app.llm_review import _parse_response


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
