#!/usr/bin/env python3
"""Update repair incident state on a Script Health Registry row."""
from __future__ import annotations

import argparse
import json
import os
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
ALLOWED = {"Open", "Repairing", "Awaiting Approval", "Approved", "Rejected", "Resolved", "Retry"}


def key() -> str:
    for line in (HOME / ".env").read_text(errors="ignore").splitlines():
        if line.startswith("NOTION_API_KEY="):
            return line.split("=", 1)[1].strip()
    return os.environ.get("NOTION_API_KEY", "")


def rt(value: str) -> dict:
    return {"rich_text": [{"type": "text", "text": {"content": value[:1900]}}] if value else []}


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--row-id", required=True)
    p.add_argument("--status", required=True, choices=sorted(ALLOWED))
    p.add_argument("--pr-url", default="")
    p.add_argument("--instructions", default="")
    a = p.parse_args()
    props = {
        "Incident Status": {"select": {"name": a.status}},
        "Last Repair Attempt": {"date": {"start": datetime.now(timezone.utc).isoformat()}},
    }
    if a.pr_url:
        props["Fix PR"] = {"url": a.pr_url}
    if a.instructions:
        props["Approval Instructions"] = rt(a.instructions)
    body = json.dumps({"properties": props}).encode()
    req = urllib.request.Request(
        f"https://api.notion.com/v1/pages/{a.row_id}", method="PATCH", data=body,
        headers={"Authorization": f"Bearer {key()}", "Notion-Version": "2025-09-03",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=45) as r:
        result = json.load(r)
    print(f"Repair incident {a.row_id}: {a.status}")
    if a.pr_url:
        print(a.pr_url)
    return 0 if result.get("id") else 1


if __name__ == "__main__":
    raise SystemExit(main())
