"""
Runs static analysis tools against changed files and normalizes their
output into a common structure so the LLM prompt doesn't need to know
which tool produced which finding.

Supported today:
  - Python  -> Pylint + Bandit
  - JS/TS   -> ESLint (requires a local ESLint config in the target repo)

Design notes:
  - Each linter runs as a subprocess with a timeout so one broken tool
    (or an infinite loop) can't hang the whole review.
  - Failures are caught and logged, never raised, so a missing/broken
    linter degrades the review instead of crashing the pipeline.
"""
from __future__ import annotations

import json
import logging
import subprocess
import tempfile
from dataclasses import dataclass
from pathlib import Path

from app.config import settings

logger = logging.getLogger("codesage.static_analysis")

PYTHON_EXTENSIONS = {".py"}
JS_TS_EXTENSIONS = {".js", ".jsx", ".ts", ".tsx"}


@dataclass
class Finding:
    file: str
    line: int | None
    tool: str
    rule: str | None
    message: str
    severity: str  # "error" | "warning" | "info"

    def to_dict(self) -> dict:
        return {
            "file": self.file,
            "line": self.line,
            "tool": self.tool,
            "rule": self.rule,
            "message": self.message,
            "severity": self.severity,
        }


def _run_subprocess(cmd: list[str], cwd: str | None = None) -> tuple[str, str, int]:
    """Run a subprocess with a timeout. Never raises on non-zero exit
    (linters commonly exit non-zero when they find issues -- that's expected)."""
    try:
        result = subprocess.run(
            cmd,
            cwd=cwd,
            capture_output=True,
            text=True,
            timeout=settings.linter_timeout_seconds,
        )
        return result.stdout, result.stderr, result.returncode
    except subprocess.TimeoutExpired:
        logger.warning("Linter timed out: %s", " ".join(cmd))
        return "", "timeout", -1
    except FileNotFoundError:
        logger.warning("Linter not installed / not on PATH: %s", cmd[0])
        return "", "not_installed", -1
    except Exception as exc:  # noqa: BLE001 - deliberately broad, must never crash pipeline
        logger.exception("Unexpected error running linter %s: %s", cmd[0], exc)
        return "", str(exc), -1


def _write_temp_file(content: str, suffix: str) -> Path:
    tmp = tempfile.NamedTemporaryFile(
        mode="w", suffix=suffix, delete=False, encoding="utf-8"
    )
    tmp.write(content)
    tmp.close()
    return Path(tmp.name)


def run_pylint(file_path: str, content: str) -> list[Finding]:
    tmp_path = _write_temp_file(content, ".py")
    findings: list[Finding] = []
    try:
        stdout, _stderr, _code = _run_subprocess(
            ["pylint", "--output-format=json", str(tmp_path)]
        )
        if not stdout.strip():
            return findings
        try:
            issues = json.loads(stdout)
        except json.JSONDecodeError:
            logger.warning("Could not parse pylint JSON output for %s", file_path)
            return findings

        for issue in issues:
            findings.append(
                Finding(
                    file=file_path,
                    line=issue.get("line"),
                    tool="pylint",
                    rule=issue.get("symbol"),
                    message=issue.get("message", ""),
                    severity=_pylint_severity(issue.get("type", "")),
                )
            )
    finally:
        tmp_path.unlink(missing_ok=True)
    return findings


def _pylint_severity(pylint_type: str) -> str:
    return {
        "error": "error",
        "fatal": "error",
        "warning": "warning",
    }.get(pylint_type, "info")


def run_bandit(file_path: str, content: str) -> list[Finding]:
    tmp_path = _write_temp_file(content, ".py")
    findings: list[Finding] = []
    try:
        stdout, _stderr, _code = _run_subprocess(
            ["bandit", "-f", "json", str(tmp_path)]
        )
        if not stdout.strip():
            return findings
        try:
            data = json.loads(stdout)
        except json.JSONDecodeError:
            logger.warning("Could not parse bandit JSON output for %s", file_path)
            return findings

        for issue in data.get("results", []):
            findings.append(
                Finding(
                    file=file_path,
                    line=issue.get("line_number"),
                    tool="bandit",
                    rule=issue.get("test_id"),
                    message=issue.get("issue_text", ""),
                    severity=_bandit_severity(issue.get("issue_severity", "")),
                )
            )
    finally:
        tmp_path.unlink(missing_ok=True)
    return findings


def _bandit_severity(bandit_severity: str) -> str:
    return {"HIGH": "error", "MEDIUM": "warning", "LOW": "info"}.get(
        bandit_severity.upper(), "info"
    )


def run_eslint(file_path: str, content: str) -> list[Finding]:
    suffix = Path(file_path).suffix or ".js"
    tmp_path = _write_temp_file(content, suffix)
    findings: list[Finding] = []
    try:
        stdout, _stderr, _code = _run_subprocess(
            ["eslint", "--format", "json", str(tmp_path)]
        )
        if not stdout.strip():
            return findings
        try:
            results = json.loads(stdout)
        except json.JSONDecodeError:
            logger.warning("Could not parse eslint JSON output for %s", file_path)
            return findings

        for file_result in results:
            for msg in file_result.get("messages", []):
                findings.append(
                    Finding(
                        file=file_path,
                        line=msg.get("line"),
                        tool="eslint",
                        rule=msg.get("ruleId"),
                        message=msg.get("message", ""),
                        severity="error" if msg.get("severity") == 2 else "warning",
                    )
                )
    finally:
        tmp_path.unlink(missing_ok=True)
    return findings


def analyze_file(file_path: str, content: str) -> list[Finding]:
    """Dispatch a single file to the right linter(s) based on extension.
    Returns an empty list (never raises) if no linter matches or all fail."""
    ext = Path(file_path).suffix.lower()

    if ext in PYTHON_EXTENSIONS:
        findings = []
        findings.extend(run_pylint(file_path, content))
        findings.extend(run_bandit(file_path, content))
        return findings

    if ext in JS_TS_EXTENSIONS:
        return run_eslint(file_path, content)

    logger.info("No linter configured for extension '%s' (%s) - skipping", ext, file_path)
    return []


def analyze_files(files: dict[str, str]) -> list[Finding]:
    """files: mapping of file_path -> full file content (post-change version)."""
    all_findings: list[Finding] = []
    for file_path, content in files.items():
        all_findings.extend(analyze_file(file_path, content))
    return all_findings
