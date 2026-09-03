#!/usr/bin/env python3
"""Publish a completed PCG work product as a Proposed Notion deliverable."""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
EMAIL_FILE = HERMES_HOME / ".pcg_member_email"
DELIVERABLE_DS = "d27ab37d-f4f0-4303-a87e-c0edc890cd66"
NOTION_VERSION = "2025-09-03"
sys.path.insert(0, str(HERMES_HOME / "scripts"))
from pcg_automation_core import classify_deliverable  # noqa: E402


def env_key(name: str) -> str:
    envp = HERMES_HOME / ".env"
    if envp.exists():
        for line in envp.read_text(errors="ignore").splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip()
    return os.environ.get(name, "")


def notion(path: str, method: str = "GET", body: dict | None = None) -> dict:
    req = urllib.request.Request(
        "https://api.notion.com/v1" + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {env_key('NOTION_API_KEY')}",
                 "Notion-Version": NOTION_VERSION, "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.load(r)


def rt(value: str) -> dict:
    return {"rich_text": [{"type": "text", "text": {"content": value[:1900]}}] if value else []}


def select(value: str) -> dict:
    return {"select": {"name": value}}


def title(value: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": value}}]}


def multi(values: list[str]) -> dict:
    return {"multi_select": [{"name": value} for value in values]}


def row_title(row: dict) -> str:
    for prop in row.get("properties", {}).values():
        if prop.get("type") == "title":
            return "".join(x.get("plain_text", "") for x in prop.get("title", []))
    return ""


def find_existing(name: str, owner_email: str) -> dict | None:
    data = notion(f"/data_sources/{DELIVERABLE_DS}/query", "POST", {"page_size": 100})
    for row in data.get("results", []):
        email = row.get("properties", {}).get("Owner Email", {}).get("email") or ""
        if row_title(row) == name and email.lower() == owner_email.lower():
            return row
    return None


def _existing_select(existing: dict | None, name: str) -> str:
    prop = (existing or {}).get("properties", {}).get(name, {})
    return ((prop.get("select") or {}).get("name") or "").strip()


def _existing_text(existing: dict | None, name: str) -> str:
    prop = (existing or {}).get("properties", {}).get(name, {})
    values = prop.get("rich_text") or []
    return "".join(
        item.get("plain_text") or ((item.get("text") or {}).get("content") or "")
        for item in values
    ).strip()


def _existing_url(existing: dict | None, name: str) -> str:
    return str((existing or {}).get("properties", {}).get(name, {}).get("url") or "").strip()


def _existing_date(existing: dict | None, name: str) -> str:
    value = (existing or {}).get("properties", {}).get(name, {}).get("date") or {}
    return str(value.get("start") or "").strip()


def build_properties(p: dict, existing: dict | None = None) -> dict:
    current = _existing_select(existing, "Curation Status")
    curation = current if current in {"Approved", "Rejected"} else "Proposed"
    now = datetime.now(timezone.utc).isoformat()
    status = p.get("status") or _existing_select(existing, "Status") or "Building"
    health = p.get("health") or _existing_select(existing, "Health") or "Unknown"
    audience = p.get("audience") or _existing_select(existing, "Audience") or "Function"
    visibility = p.get("visibility") or _existing_select(existing, "Visibility") or "Function"
    schedule = p.get("schedule") or _existing_text(existing, "Schedule") or "On demand"
    url = p.get("url") or _existing_url(existing, "URL")
    source_repo = p.get("source_repo") or _existing_url(existing, "Source Repository")
    jobs = p.get("jobs") or _existing_text(existing, "Job ID")
    scripts = p.get("scripts") or _existing_text(existing, "Script Paths")
    repair_policy = p.get("repair_policy") or _existing_select(existing, "Repair Policy") or "detect-only"
    test_command = p.get("test_command") or _existing_text(existing, "Test Command")
    deployment_method = p.get("deployment_method") or _existing_text(existing, "Deployment Method")
    rollback_method = p.get("rollback_method") or _existing_text(existing, "Rollback Method")
    alert_target = p.get("alert_target") or _existing_text(existing, "Alert Target") or p["owner_email"]
    last_verified = p.get("last_verified") or _existing_date(existing, "Last Verified")
    return {
        "Name": title(p["name"]), "Type": select(p["type"]),
        "Status": select(status), "Health": select(health),
        "Functions": multi(p["function"]), "Audience": select(audience),
        "Owner": rt(p.get("owner", p["owner_email"])), "Owner Email": {"email": p["owner_email"]},
        "Submitted By": {"email": p["submitted_by"]}, "Curation Status": select(curation),
        "Business Purpose": rt(p["purpose"]), "Schedule": rt(schedule),
        "URL": {"url": url or None}, "Source Repository": {"url": source_repo or None},
        "Job ID": rt(jobs), "Script Paths": rt(scripts),
        "Repair Policy": select(repair_policy),
        "Test Command": rt(test_command),
        "Deployment Method": rt(deployment_method),
        "Rollback Method": rt(rollback_method),
        "Alert Target": rt(alert_target),
        "Visibility": select(visibility),
        "Last Changed": {"date": {"start": now}},
        "Last Verified": {"date": {"start": last_verified}} if last_verified else {"date": None},
        "Notes": rt("Automatically cataloged so completed PCG work is not hidden. "
                    "A function owner must approve it before it becomes official."),
    }


def default_submitter() -> str:
    if EMAIL_FILE.exists() and EMAIL_FILE.read_text().strip():
        return EMAIL_FILE.read_text().strip()
    return os.environ.get("PCG_OWNER_EMAIL", "wes@procoffeegear.com")


def registration_reasons(proposal: dict) -> list[str]:
    """Return useful classification labels without rejecting completed work."""
    _meaningful, reasons = classify_deliverable(proposal)
    return reasons or ["created work product"]


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--name", required=True)
    p.add_argument("--purpose", required=True)
    p.add_argument("--owner-email", required=True)
    p.add_argument("--function", action="append", required=True)
    p.add_argument("--type", required=True)
    p.add_argument("--audience", default=None)
    p.add_argument("--visibility", default=None)
    p.add_argument("--schedule", default=None)
    p.add_argument("--url", default="")
    p.add_argument("--source-repo", default="")
    p.add_argument("--scripts", default="")
    p.add_argument("--jobs", default="")
    p.add_argument("--repair-policy", default=None,
                   choices=["detect-only", "safe-auto-heal", "repair-pr", "critical-approval", "manual"])
    p.add_argument("--test-command", default="")
    p.add_argument("--deployment-method", default="")
    p.add_argument("--rollback-method", default="")
    p.add_argument("--alert-target", default="")
    p.add_argument("--submitted-by", default="")
    return p.parse_args()


def main() -> int:
    proposal = vars(parse_args())
    proposal["submitted_by"] = proposal.get("submitted_by") or default_submitter()
    reasons = registration_reasons(proposal)
    existing = find_existing(proposal["name"], proposal["owner_email"])
    props = build_properties(proposal, existing)
    if existing:
        page = notion(f"/pages/{existing['id']}", "PATCH", {"properties": props})
        action = "updated"
    else:
        page = notion("/pages", "POST", {"parent": {"type": "data_source_id", "data_source_id": DELIVERABLE_DS},
                                           "properties": props})
        action = "proposed"
    print(f"Deliverable {action}: {proposal['name']} ({', '.join(reasons)}).")
    print(page.get("url", "https://app.notion.com/p/a6c0a72f550a4f97a5990fc07a44a49e"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
