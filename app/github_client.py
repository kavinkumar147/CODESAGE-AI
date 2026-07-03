"""
GitHub App client: handles JWT-based app authentication, installation
access tokens, and fetching PR diff / changed-file content via the
GitHub REST API.

Auth flow (GitHub Apps):
  1. Sign a short-lived JWT with the App's private key (RS256), using the
     App ID as the issuer.
  2. Exchange that JWT for an installation access token, scoped to the
     specific repo installation that triggered the webhook.
  3. Use the installation token as a normal Bearer/token credential for
     REST calls (fetching diffs, file contents, etc).

Design notes:
  - Per-file content fetches are wrapped individually so one bad/huge/
    binary file can't take down the whole review.
  - Lockfiles and binary-looking extensions are skipped up front — they
    add no review value and can be very large.
  - Pagination is handled for the PR files list (GitHub caps at 100/page).
"""
from __future__ import annotations

import logging
import time
from pathlib import Path
from typing import Optional

import httpx
import jwt

from app.config import settings

logger = logging.getLogger("codesage.github_client")

GITHUB_API = "https://api.github.com"
HTTP_TIMEOUT = 20.0

# Files that add no review value / are frequently huge — skip outright.
SKIP_FILENAMES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "Cargo.lock",
    "Gemfile.lock",
    "composer.lock",
}

BINARY_EXTENSIONS = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".svg",
    ".pdf", ".zip", ".tar", ".gz", ".woff", ".woff2", ".ttf", ".eot",
    ".mp4", ".mp3", ".mov", ".exe", ".dll", ".so", ".class", ".jar",
}


class GitHubAuthError(RuntimeError):
    """Raised when JWT signing or installation token exchange fails."""


class GitHubAPIError(RuntimeError):
    """Raised when a GitHub REST call fails unrecoverably."""


class GitHubClient:
    def __init__(self) -> None:
        if not settings.github_app_id or not settings.github_private_key:
            raise GitHubAuthError(
                "GITHUB_APP_ID and GITHUB_PRIVATE_KEY must be set to use GitHubClient."
            )
        self.app_id = settings.github_app_id
        self.private_key = settings.github_private_key.replace("\\n", "\n")
        print("----- PRIVATE KEY DEBUG -----")
        print(repr(self.private_key[:100]))
        print("-----------------------------")

    # ------------------------------------------------------------------ #
    # Auth
    # ------------------------------------------------------------------ #

    def _generate_app_jwt(self) -> str:
        """Signs a short-lived JWT identifying the GitHub App itself
        (not a specific installation)."""
        now = int(time.time())
        payload = {
            "iat": now - 60,       # allow for clock drift
            "exp": now + 540,      # GitHub caps this at 10 minutes
            "iss": self.app_id,
        }
        try:
            return jwt.encode(payload, self.private_key, algorithm="RS256")
        except Exception as exc:  # noqa: BLE001
            raise GitHubAuthError(f"Failed to sign App JWT: {exc}") from exc

    def get_installation_token(self, installation_id: int) -> str:
        """Exchanges the App JWT for an installation access token, which
        is what's actually used to read/write on the installed repo(s)."""
        app_jwt = self._generate_app_jwt()
        headers = {
            "Authorization": f"Bearer {app_jwt}",
            "Accept": "application/vnd.github+json",
        }
        url = f"{GITHUB_API}/app/installations/{installation_id}/access_tokens"
        try:
            resp = httpx.post(url, headers=headers, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise GitHubAuthError(
                f"Failed to obtain installation token: {exc}"
            ) from exc
        return resp.json()["token"]

    # ------------------------------------------------------------------ #
    # PR data fetching
    # ------------------------------------------------------------------ #

    def fetch_pr_diff(self, owner: str, repo: str, pr_number: int, token: str) -> str:
        """Fetches the full unified diff for a PR using GitHub's diff media type."""
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.v3.diff",
        }
        url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}"
        try:
            resp = httpx.get(url, headers=headers, timeout=HTTP_TIMEOUT)
            resp.raise_for_status()
        except httpx.HTTPError as exc:
            raise GitHubAPIError(f"Failed to fetch PR diff: {exc}") from exc
        return resp.text

    def fetch_pr_files(self, owner: str, repo: str, pr_number: int, token: str) -> list[dict]:
        """Fetches the list of changed files (metadata, not content) for a PR,
        handling pagination."""
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github+json",
        }
        url = f"{GITHUB_API}/repos/{owner}/{repo}/pulls/{pr_number}/files"
        files: list[dict] = []
        page = 1
        while True:
            try:
                resp = httpx.get(
                    url,
                    headers=headers,
                    params={"per_page": 100, "page": page},
                    timeout=HTTP_TIMEOUT,
                )
                resp.raise_for_status()
            except httpx.HTTPError as exc:
                raise GitHubAPIError(f"Failed to fetch PR files: {exc}") from exc

            batch = resp.json()
            if not batch:
                break
            files.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        return files

    def fetch_file_content(
        self, owner: str, repo: str, path: str, ref: str, token: str
    ) -> Optional[str]:
        """Fetches the full raw content of a file at a given ref (commit SHA).
        Returns None if the file doesn't exist at that ref (e.g. it was deleted)
        or can't be decoded as text."""
        headers = {
            "Authorization": f"token {token}",
            "Accept": "application/vnd.github.raw",
        }
        url = f"{GITHUB_API}/repos/{owner}/{repo}/contents/{path}"
        try:
            resp = httpx.get(
                url, headers=headers, params={"ref": ref}, timeout=HTTP_TIMEOUT
            )
        except httpx.HTTPError as exc:
            logger.warning("Network error fetching content for %s: %s", path, exc)
            return None

        if resp.status_code == 404:
            return None
        if resp.status_code != 200:
            logger.warning(
                "Unexpected status %d fetching content for %s", resp.status_code, path
            )
            return None

        try:
            return resp.text
        except UnicodeDecodeError:
            logger.info("Skipping non-text file: %s", path)
            return None

    def _should_skip_file(self, filename: str) -> bool:
        if filename in SKIP_FILENAMES:
            return True
        if Path(filename).suffix.lower() in BINARY_EXTENSIONS:
            return True
        return False

    def fetch_changed_files_content(
        self, owner: str, repo: str, pr_number: int, head_sha: str, token: str
    ) -> dict[str, str]:
        """
        Builds the {file_path: full_content} mapping that review_diff() expects,
        by listing changed files on the PR and fetching each one's full content
        at the PR's head commit. Removed files, binary files, and lockfiles are
        skipped. Individual fetch failures are logged and skipped rather than
        aborting the whole batch.
        """
        pr_files = self.fetch_pr_files(owner, repo, pr_number, token)
        changed_files: dict[str, str] = {}

        for file_meta in pr_files[: settings.max_files_per_review]:
            filename = file_meta.get("filename", "")
            status = file_meta.get("status", "")

            if status == "removed":
                continue
            if self._should_skip_file(filename):
                logger.info("Skipping non-reviewable file: %s", filename)
                continue

            try:
                content = self.fetch_file_content(owner, repo, filename, head_sha, token)
            except Exception:  # noqa: BLE001 — one bad file must not break the batch
                logger.exception("Failed to fetch content for %s", filename)
                continue

            if content is None:
                continue

            max_chars = settings.max_diff_chars_per_file * 5
            if len(content) > max_chars:
                logger.info("Truncating large file %s (%d chars)", filename, len(content))
                content = content[:max_chars]

            changed_files[filename] = content

        return changed_files
