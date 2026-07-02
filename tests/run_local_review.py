"""
Manual smoke test for Stage 1: runs the full pipeline (static analysis +
LLM review + formatting) against the sample fixture diff and prints the
resulting Markdown comment to the console.

Run with:
    python -m tests.run_local_review

Requires ANTHROPIC_API_KEY to be set (via .env or environment).
"""
import logging
from pathlib import Path

from app.pipeline import review_diff

logging.basicConfig(level=logging.INFO)

FIXTURES_DIR = Path(__file__).parent / "fixtures"


def main():
    diff_text = (FIXTURES_DIR / "sample_pr.diff").read_text()
    file_content = (FIXTURES_DIR / "user_service.py").read_text()

    changed_files = {
        "app/user_service.py": file_content,
    }

    output = review_diff(diff=diff_text, changed_files=changed_files)

    print("\n" + "=" * 70)
    print("STRUCTURED REVIEW")
    print("=" * 70)
    print(f"Summary: {output.review.summary}")
    print(f"Parse failed: {output.review.parse_failed}")
    print(f"Findings: {len(output.review.findings)}")
    for f in output.review.findings:
        print(f"  [{f.severity}] {f.file}:{f.line} — {f.issue}")

    print("\n" + "=" * 70)
    print("RENDERED MARKDOWN COMMENT (what would be posted to the PR)")
    print("=" * 70)
    print(output.comment_markdown)


if __name__ == "__main__":
    main()