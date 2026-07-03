"""
Stage 2: FastAPI webhook server.

Receives GitHub `pull_request` webhook events, verifies the webhook
signature, fetches the PR diff + changed file content via the GitHub App,
runs the existing review_diff() pipeline, and returns the generated review
as a JSON response.

IMPORTANT: This stage does NOT post anything back to GitHub. The review is
computed and returned in the HTTP response only, so you can inspect output
(e.g. via the GitHub App's webhook redelivery + response viewer, or by
pointing the webhook at this server and checking logs/response) before
Stage 3 wires up actually commenting on the PR.

Run locally with:
    uvicorn app.main:app --reload --port 8000
"""
from __future__ import annotations

import hashlib
import hmac
import json
import logging

from fastapi import FastAPI, Header, HTTPException, Request

from app.config import settings
from app.github_client import GitHubAPIError, GitHubAuthError, GitHubClient
from app.pipeline import review_diff

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("codesage.main")

app = FastAPI(title="CodeSage AI", version="0.2.0")

# Only these PR actions produce a meaningful new diff to review.
SUPPORTED_ACTIONS = {"opened", "synchronize", "reopened"}


def verify_signature(payload_body: bytes, signature_header: str | None) -> bool:
    """
    Verifies the X-Hub-Signature-256 header GitHub sends with every webhook
    delivery, using HMAC-SHA256 over the raw request body and the app's
    webhook secret. Uses constant-time comparison to avoid timing attacks.
    """
    if not signature_header or not signature_header.startswith("sha256="):
        return False
    if not settings.github_webhook_secret:
        logger.error("GITHUB_WEBHOOK_SECRET is not configured — rejecting all webhooks.")
        return False

    expected_signature = hmac.new(
        key=settings.github_webhook_secret.encode("utf-8"),
        msg=payload_body,
        digestmod=hashlib.sha256,
    ).hexdigest()
    expected_header = f"sha256={expected_signature}"

    return hmac.compare_digest(expected_header, signature_header)


@app.get("/health")
def health() -> dict:
    return {"status": "ok"}


@app.post("/webhook")
async def handle_webhook(
    request: Request,
    x_hub_signature_256: str | None = Header(default=None),
    x_github_event: str | None = Header(default=None),
) -> dict:
    raw_body = await request.body()

    # 1. Verify signature BEFORE parsing/trusting anything in the payload.
    if not verify_signature(raw_body, x_hub_signature_256):
        logger.warning("Rejected webhook with invalid or missing signature.")
        raise HTTPException(status_code=401, detail="Invalid webhook signature")

    # 2. Only handle pull_request events; acknowledge everything else politely.
    if x_github_event != "pull_request":
        return {"status": "ignored", "reason": f"unsupported event type: {x_github_event}"}

    try:
        payload = json.loads(raw_body)
    except json.JSONDecodeError as exc:
        raise HTTPException(status_code=400, detail="Malformed JSON payload") from exc

    action = payload.get("action")
    if action not in SUPPORTED_ACTIONS:
        return {"status": "ignored", "reason": f"unsupported action: {action}"}

    pr = payload.get("pull_request", {})
    repository = payload.get("repository", {})
    installation = payload.get("installation", {})

    owner = (repository.get("owner") or {}).get("login")
    repo_name = repository.get("name")
    pr_number = pr.get("number")
    head_sha = (pr.get("head") or {}).get("sha")
    installation_id = installation.get("id")

    missing = [
        name
        for name, value in [
            ("repository.owner.login", owner),
            ("repository.name", repo_name),
            ("pull_request.number", pr_number),
            ("pull_request.head.sha", head_sha),
            ("installation.id", installation_id),
        ]
        if not value
    ]
    if missing:
        raise HTTPException(
            status_code=400,
            detail=f"Payload missing required fields: {', '.join(missing)}",
        )

    logger.info("Processing PR #%s on %s/%s (action=%s)", pr_number, owner, repo_name, action)

    # 3. Authenticate as the GitHub App installation and fetch PR data.
    try:
        client = GitHubClient()
        installation_token = client.get_installation_token(installation_id)
        diff_text = client.fetch_pr_diff(owner, repo_name, pr_number, installation_token)
        changed_files = client.fetch_changed_files_content(
            owner, repo_name, pr_number, head_sha, installation_token
        )
    except (GitHubAuthError, GitHubAPIError) as exc:
        logger.exception("GitHub API interaction failed")
        raise HTTPException(status_code=502, detail=str(exc)) from exc

    if not changed_files:
        logger.info("No reviewable files in PR #%s — skipping review.", pr_number)
        return {
            "status": "skipped",
            "reason": "no reviewable files (all removed, binary, or lockfiles)",
            "repo": f"{owner}/{repo_name}",
            "pr_number": pr_number,
        }

    # 4. Run the existing pipeline (static analysis + LLM review + formatting).
    #    This is unchanged from Stage 1 — still using your Groq-backed
    #    review_diff() implementation under the hood.
    output = review_diff(diff=diff_text, changed_files=changed_files)

    # 5. Post the review comment back to GitHub.
    posted_to_github = False
    comment_url = None
    post_error = None

    try:
        comment = client.post_issue_comment(
            owner=owner,
            repo=repo_name,
            pr_number=pr_number,
            token=installation_token,
            body=output.comment_markdown,
        )

        posted_to_github = True
        comment_url = comment.get("html_url")

        logger.info(
        "Posted review comment to PR #%s: %s",
        pr_number,
        comment_url,
    )

    except GitHubAPIError as exc:
        post_error = str(exc)
        logger.exception("Failed to post review comment to GitHub")

    # 5. Return the review locally. No comment is posted to GitHub in this stage.
    return {
        "status": "reviewed",
        "posted_to_github": posted_to_github,
        "comment_url": comment_url,
        "post_error": post_error,
        "repo": f"{owner}/{repo_name}",
        "pr_number": pr_number,
        "files_reviewed": list(changed_files.keys()),
        "summary": output.review.summary,
        "findings": [
            {
                "file": f.file,
                "line": f.line,
                "severity": f.severity,
                "issue": f.issue,
                "suggestion": f.suggestion,
            }
            for f in output.review.findings
        ],
        "comment_markdown": output.comment_markdown,
    }
