"""
Stage 1 core pipeline: given a raw diff and the changed files' content,
run static analysis, feed everything to the LLM, and produce the final
Markdown comment. No GitHub or web server involved — pure functions,
easy to unit test.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass

from app.formatter import format_review_comment
from app.llm_review import ReviewResult, run_review
from app.static_analysis import analyze_files

logger = logging.getLogger("codesage.pipeline")


@dataclass
class PipelineOutput:
    review: ReviewResult
    comment_markdown: str


def review_diff(diff: str, changed_files: dict[str, str]) -> PipelineOutput:
    """
    Main entry point for Stage 1.

    Args:
        diff: unified diff text covering all changed files.
        changed_files: mapping of file_path -> full post-change file content.
            Linters need real files, not diff fragments, so we pass full
            content here rather than just the diff hunks.

    Returns:
        PipelineOutput with the structured review and the ready-to-post
        Markdown comment.
    """
    if not diff.strip() or not changed_files:
        logger.info("Empty diff or no changed files — skipping review.")
        result = ReviewResult(summary="No reviewable changes detected.", findings=[])
        return PipelineOutput(review=result, comment_markdown=format_review_comment(result))

    static_findings = analyze_files(changed_files)
    logger.info("Static analysis produced %d findings.", len(static_findings))

    review = run_review(diff=diff, static_findings=static_findings)
    logger.info(
        "LLM review produced %d findings (parse_failed=%s).",
        len(review.findings),
        review.parse_failed,
    )

    comment = format_review_comment(review)
    return PipelineOutput(review=review, comment_markdown=comment)