#!/usr/bin/env python3
"""Version Approved Notion deliverables into pcg-agents/deliverables.toml.

GitHub access is via the Composio CLI (the exec box's OAuth'd GitHub connection,
account Pro-Coffee-Gear) — no personal access token. Notion access is unchanged
(NOTION_API_KEY). Deterministic: read -> render -> compare -> push-if-changed.
"""
from __future__ import annotations

import base64
import json
import os
import re
import subprocess
import sys
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
DS_ID = "d27ab37d-f4f0-4303-a87e-c0edc890cd66"
OWNER = "Pro-Coffee-Gear"
REPO = "pcg-agents"
REMOTE_PATH = "deliverables.toml"
BRANCH = "main"
COMPOSIO = "/opt/data/.composio/composio"
sys.path.insert(0, str(HOME / "scripts"))
from pcg_automation_core import approved_deliverables_from_rows, render_deliverables_toml  # noqa: E402


def env_key(*names: str) -> str:
    values = {}
    for line in (HOME / ".env").read_text(errors="ignore").splitlines():
        if "=" in line and not line.lstrip().startswith("#"):
            k, v = line.split("=", 1)
            values[k.strip()] = v.strip()
    for name in names:
        if values.get(name):
            return values[name]
        if os.environ.get(name):
            return os.environ[name]
    return ""


def composio_exec(slug: str, payload: dict) -> dict:
    r = subprocess.run(
        [COMPOSIO, "execute", slug, "-d", json.dumps(payload)],
        capture_output=True, text=True, timeout=180,
    )
    out = r.stdout
    start = out.find("{")
    if start < 0:
        return {"error": (r.stderr or out)[:400]}
    try:
        return json.loads(out[start:])
    except json.JSONDecodeError:
        return {"error": out[:500]}


def notion_rows() -> list[dict]:
    rows, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        req = urllib_request(
            f"https://api.notion.com/v1/data_sources/{DS_ID}/query",
            method="POST", body=body,
            headers={"Authorization": f"Bearer {env_key('NOTION_API_KEY')}",
                     "Notion-Version": "2025-09-03"},
        )
        page = json.loads(req)
        rows.extend(page.get("results", []))
        if not page.get("has_more"):
            return rows
        cursor = page.get("next_cursor")


def urllib_request(url, *, method="GET", body=None, headers=None):
    import urllib.request
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, method=method, data=data, headers=headers or {})
    with urllib.request.urlopen(req, timeout=45) as r:
        return r.read()


def remote_state() -> tuple[str | None, str]:
    """Return (sha, decoded_text) of the current remote file. sha None on 404."""
    res = composio_exec("GITHUB_GET_REPOSITORY_CONTENT", {
        "owner": OWNER, "repo": REPO, "path": REMOTE_PATH, "ref": BRANCH,
    })
    data = res.get("data") or {}
    inner = data.get("content")
    if isinstance(inner, dict) and "message" in inner and inner.get("message") == "Not Found":
        return None, ""
    if not isinstance(inner, dict):
        # could be a 404 shaped differently
        if "Not Found" in json.dumps(res):
            return None, ""
        return None, ""
    sha = inner.get("sha")
    b64 = re.sub(r"\s+", "", str(inner.get("content", "")))
    b64 += "=" * ((-len(b64)) % 4)
    try:
        text = base64.b64decode(b64).decode("utf-8", errors="replace")
    except Exception:
        text = ""
    return sha, text


def push(content: str, sha: str | None) -> dict:
    payload = {
        "owner": OWNER, "repo": REPO, "path": REMOTE_PATH,
        "branch": BRANCH,
        "message": "chore: snapshot approved business deliverables",
        "content": content,
    }
    if sha:
        payload["sha"] = sha
    return composio_exec("GITHUB_CREATE_OR_UPDATE_FILE_CONTENTS", payload)


def main() -> int:
    content = render_deliverables_toml(approved_deliverables_from_rows(notion_rows()))
    sha, existing = remote_state()
    if existing == content:
        print("Deliverables unchanged — nothing to push.")
        return 0
    result = push(content, sha)
    err = result.get("error") if isinstance(result, dict) else None
    ok = result.get("successful") if isinstance(result, dict) else None
    if err or ok is False:
        print(f"PUSH FAILED: {err or result}")
        return 1
    print(f"Deliverables snapshot pushed ({len(content)} bytes).")
    commit_url = ""
    try:
        commit_url = (result.get("data") or {}).get("content", {}).get("commit", {}).get("html_url", "")
    except Exception:
        pass
    if commit_url:
        print(commit_url)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
