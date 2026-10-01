#!/usr/bin/env python3
"""Notify the local script owner when a tested repair PR needs approval."""
from __future__ import annotations

import json
import os
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
EMAIL_FILE = HOME / ".pcg_member_email"
STATE_FILE = HOME / ".pcg_repair_notifications.json"
DS_ID = "c9290a04-1c19-41fc-b76e-35d23532c510"
MAX_ATTEMPTS = 4
RETRY_BASE_DELAY_SECONDS = 2
sys.path.insert(0, str(HOME / "scripts"))
from pcg_automation_core import script_row_to_incident, select_owner_notifications  # noqa: E402


def env_key(name: str) -> str:
    for line in (HOME / ".env").read_text(errors="ignore").splitlines():
        if line.startswith(name + "="):
            return line.split("=", 1)[1].strip()
    return os.environ.get(name, "")


def fetch_page(body: dict) -> dict:
    """POST one Notion query page, retrying transient failures with backoff.

    Notion intermittently returns 5xx; a single blip must not fail the run
    and page a repair incident. Persistent failures still raise.
    """
    req = urllib.request.Request(
        f"https://api.notion.com/v1/data_sources/{DS_ID}/query", method="POST",
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {env_key('NOTION_API_KEY')}",
                 "Notion-Version": "2025-09-03", "Content-Type": "application/json"},
    )
    last_error: Exception | None = None
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            with urllib.request.urlopen(req, timeout=45) as r:
                return json.load(r)
        except urllib.error.HTTPError as e:
            last_error = e
            transient = e.code >= 500 or e.code == 429
        except (urllib.error.URLError, TimeoutError, OSError) as e:
            last_error = e
            transient = True
        if not transient or attempt == MAX_ATTEMPTS:
            raise last_error
        time.sleep(RETRY_BASE_DELAY_SECONDS * (2 ** (attempt - 1)))
    raise last_error  # unreachable, satisfies the type checker


def owner_email() -> str:
    if EMAIL_FILE.exists() and EMAIL_FILE.read_text().strip():
        return EMAIL_FILE.read_text().strip()
    return os.environ.get("PCG_OWNER_EMAIL", "wes@procoffeegear.com")


def main() -> int:
    try:
        seen = set(json.loads(STATE_FILE.read_text()).get("seen", []))
    except Exception:
        seen = set()
    rows, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        page = fetch_page(body)
        rows.extend(script_row_to_incident(x) for x in page.get("results", []))
        if not page.get("has_more"):
            break
        cursor = page.get("next_cursor")
    selected = select_owner_notifications(rows, owner_email(), seen)
    if not selected:
        return 0
    for incident in selected:
        instances = ", ".join(incident.get("instances", [])) or incident.get("instance", "")
        print(f"[REPAIR APPROVAL NEEDED] {incident['name']} on {instances}")
        print(f"Failure: {incident['failure_detail']}")
        print(f"Repair PR: {incident['fix_pr']}")
        if incident.get("test_command"):
            print(f"Required test: {incident['test_command']}")
        if incident.get("rollback_method"):
            print(f"Rollback: {incident['rollback_method']}")
        print("Approval: review and merge the PR in GitHub. Alternatively, tell your agent:")
        print(f'  review and approve {incident["fix_pr"]}')
        print("The agent must re-check CI and ask for explicit confirmation before merging.")
        print()
        seen.add(incident["notification_marker"])
    STATE_FILE.write_text(json.dumps({"seen": sorted(seen)}, indent=2) + "\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
