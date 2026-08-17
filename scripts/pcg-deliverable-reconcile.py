#!/usr/bin/env python3
"""Version Approved Notion deliverables into pcg-agents/deliverables.toml."""
from __future__ import annotations

import base64
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
DS_ID = "d27ab37d-f4f0-4303-a87e-c0edc890cd66"
REPO = "WWWPCG/pcg-agents"
REMOTE_PATH = "deliverables.toml"
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


def notion_rows() -> list[dict]:
    rows, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        req = urllib.request.Request(
            f"https://api.notion.com/v1/data_sources/{DS_ID}/query", method="POST",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {env_key('NOTION_API_KEY')}",
                     "Notion-Version": "2025-09-03", "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=45) as r:
            page = json.load(r)
        rows.extend(page.get("results", []))
        if not page.get("has_more"):
            return rows
        cursor = page.get("next_cursor")


def main() -> int:
    content = render_deliverables_toml(approved_deliverables_from_rows(notion_rows()))
    token = env_key("GITHUB_TOKEN", "GH_TOKEN")
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json",
               "X-GitHub-Api-Version": "2022-11-28"}
    url = f"https://api.github.com/repos/{REPO}/contents/{REMOTE_PATH}"
    sha, existing = None, ""
    try:
        with urllib.request.urlopen(urllib.request.Request(url, headers=headers), timeout=30) as r:
            current = json.load(r)
        sha = current.get("sha")
        existing = base64.b64decode(current.get("content", "")).decode()
    except urllib.error.HTTPError as e:
        if e.code != 404:
            raise
    if existing == content:
        return 0
    body = {
        "message": "chore: snapshot approved business deliverables",
        "content": base64.b64encode(content.encode()).decode(),
        "branch": "main",
    }
    if sha:
        body["sha"] = sha
    req = urllib.request.Request(url, method="PUT", data=json.dumps(body).encode(),
                                 headers={**headers, "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=45) as r:
        result = json.load(r)
    print(f"Deliverables snapshot updated: {len(approved_deliverables_from_rows(notion_rows()))} approved rows")
    print(result["commit"]["html_url"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
