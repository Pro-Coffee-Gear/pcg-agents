#!/usr/bin/env python3
"""Stable monitor source for the central PCG repair agent.

Outputs only eligible, deduplicated repair-pr incidents. With Hermes monitor mode,
unchanged output suppresses the agent entirely.
"""
from __future__ import annotations

import json
import os
import sys
import urllib.request
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
DS_ID = "c9290a04-1c19-41fc-b76e-35d23532c510"
sys.path.insert(0, str(HOME / "scripts"))
from pcg_automation_core import script_row_to_incident, group_repair_candidates  # noqa: E402


def key() -> str:
    for line in (HOME / ".env").read_text(errors="ignore").splitlines():
        if line.startswith("NOTION_API_KEY="):
            return line.split("=", 1)[1].strip()
    return os.environ.get("NOTION_API_KEY", "")


def main() -> int:
    rows, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        req = urllib.request.Request(
            f"https://api.notion.com/v1/data_sources/{DS_ID}/query", method="POST",
            data=json.dumps(body).encode(),
            headers={"Authorization": f"Bearer {key()}", "Notion-Version": "2025-09-03",
                     "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(req, timeout=45) as r:
            page = json.load(r)
        rows.extend(page.get("results", []))
        if not page.get("has_more"):
            break
        cursor = page.get("next_cursor")
    incidents = group_repair_candidates([script_row_to_incident(row) for row in rows])
    # Stable ordering and stable fields are essential for monitor hash suppression.
    payload = [{k: row.get(k, "") for k in (
        "repair_group_key", "row_ids", "instances", "incident_keys", "name", "failure_detail",
        "owner_email", "source_repo", "test_command", "deployment_method", "rollback_method")}
        for row in sorted(incidents, key=lambda x: x.get("repair_group_key", ""))]
    print(json.dumps({"repair_incidents": payload}, sort_keys=True, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
