#!/usr/bin/env python3
"""Version Approved Notion deliverables into pcg-agents/deliverables.toml."""
from __future__ import annotations

import base64
import binascii
import hashlib
import json
import os
import re
import sys
import urllib.request
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
DS_ID = "d27ab37d-f4f0-4303-a87e-c0edc890cd66"
REMOTE_PATH = "deliverables.toml"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")
BUNDLE_SCRIPTS = Path(__file__).resolve().parent
sys.path.insert(0, str(BUNDLE_SCRIPTS))
from pcg_automation_core import approved_deliverables_from_rows, render_deliverables_toml  # noqa: E402
from pcg_github import ComposioGitHub, GitHubError, REPO_API, SNAPSHOT_API  # noqa: E402


class ReconcileError(RuntimeError):
    """A safe reconcile failure suitable for operator output."""


def env_key(*names: str) -> str:
    values: dict[str, str] = {}
    path = HOME / ".env"
    if path.exists():
        try:
            for raw in path.read_text(errors="ignore").splitlines():
                if "=" in raw and not raw.lstrip().startswith("#"):
                    key, value = raw.split("=", 1)
                    values[key.strip()] = value.strip()
        except OSError:
            values = {}
    for name in names:
        if values.get(name):
            return values[name]
        if os.environ.get(name):
            return os.environ[name]
    return ""


def notion_rows() -> list[dict]:
    token = env_key("NOTION_API_KEY")
    if not token:
        raise ReconcileError("NOTION_API_KEY is missing")
    rows, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        request = urllib.request.Request(
            f"https://api.notion.com/v1/data_sources/{DS_ID}/query",
            method="POST",
            data=json.dumps(body).encode(),
            headers={
                "Authorization": f"Bearer {token}",
                "Notion-Version": "2025-09-03",
                "Content-Type": "application/json",
            },
        )
        with urllib.request.urlopen(request, timeout=45) as response:
            page = json.load(response)
        results = page.get("results")
        if not isinstance(results, list):
            raise ReconcileError("Notion deliverables response was malformed")
        rows.extend(results)
        if not page.get("has_more"):
            return rows
        cursor = page.get("next_cursor")
        if not cursor:
            raise ReconcileError("Notion deliverables pagination was malformed")


def _git_blob_sha(raw: bytes) -> str:
    return hashlib.sha1(b"blob %d\0%s" % (len(raw), raw)).hexdigest()


def _decode_file(response: object) -> tuple[str, str]:
    if not isinstance(response, dict) or response.get("type") != "file":
        raise ReconcileError("GitHub deliverables response was malformed")
    if response.get("encoding") != "base64":
        raise ReconcileError("GitHub deliverables content was unavailable")
    if "truncated" in response and response["truncated"] is not False:
        raise ReconcileError("GitHub deliverables content was unavailable")
    sha = response.get("sha")
    encoded = response.get("content")
    if not isinstance(sha, str) or not SHA_RE.fullmatch(sha) or not isinstance(encoded, str):
        raise ReconcileError("GitHub deliverables response was malformed")
    try:
        raw = base64.b64decode(encoded.replace("\n", "").replace("\r", ""), validate=True)
        text = raw.decode("utf-8")
    except (binascii.Error, UnicodeError, ValueError):
        raise ReconcileError("GitHub deliverables content was malformed") from None
    size = response.get("size")
    if (
        not isinstance(size, int)
        or isinstance(size, bool)
        or size < 0
        or len(raw) != size
    ):
        raise ReconcileError("GitHub deliverables response had invalid size metadata")
    if _git_blob_sha(raw) != sha:
        raise ReconcileError("GitHub deliverables content did not match its blob SHA")
    return sha, text


def reconcile(adapter: ComposioGitHub, rows: list[dict]) -> tuple[bool, str | None, int]:
    """Reconcile once; a PUT is never retried and is verified at its commit."""
    repo = adapter.get(REPO_API)
    if not isinstance(repo, dict) or repo.get("full_name") != "WWWPCG/pcg-agents":
        raise ReconcileError("GitHub repository probe was malformed")

    approved = approved_deliverables_from_rows(rows)
    desired = render_deliverables_toml(approved)
    current_sha = None
    existing = ""
    try:
        current_sha, existing = _decode_file(adapter.get_contents(REMOTE_PATH, ref="main"))
    except GitHubError as exc:
        # A missing snapshot is meaningful only after the repository probe above.
        if exc.status != 404:
            raise
    if existing == desired:
        return False, None, len(approved)

    result = adapter.put_contents(
        SNAPSHOT_API,
        desired,
        branch="main",
        message="chore: snapshot approved business deliverables",
        sha=current_sha,
    )
    if not isinstance(result, dict):
        raise ReconcileError("GitHub write response was malformed; outcome requires review")
    try:
        commit_sha = result["commit"]["sha"]
        written_sha = result["content"]["sha"]
    except (KeyError, TypeError):
        raise ReconcileError("GitHub write response was malformed; outcome requires review") from None
    if not all(
        isinstance(value, str) and SHA_RE.fullmatch(value)
        for value in (commit_sha, written_sha)
    ):
        raise ReconcileError("GitHub write response was malformed; outcome requires review")
    desired_bytes = desired.encode("utf-8")
    if _git_blob_sha(desired_bytes) != written_sha:
        raise ReconcileError("GitHub write returned an unexpected blob; outcome requires review")

    verified_sha, verified = _decode_file(adapter.get_contents(REMOTE_PATH, ref=commit_sha))
    if verified != desired or verified_sha != written_sha:
        raise ReconcileError("GitHub write could not be verified; outcome requires review")
    commit_url = f"https://github.com/WWWPCG/pcg-agents/commit/{commit_sha}"
    return True, commit_url, len(approved)


def main() -> int:
    try:
        adapter = ComposioGitHub.from_environment(home=HOME)
        rows = notion_rows()
        changed, commit_url, approved_count = reconcile(adapter, rows)
    except (GitHubError, ReconcileError, ValueError):
        print("Deliverables snapshot reconcile failed", file=sys.stderr)
        return 1
    except Exception:
        print("Deliverables snapshot reconcile failed", file=sys.stderr)
        return 1
    if changed:
        print(f"Deliverables snapshot updated: {approved_count} approved rows")
        print(commit_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
