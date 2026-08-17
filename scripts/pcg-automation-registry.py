#!/usr/bin/env python3
"""Create and seed PCG's Notion automation catalog and script-health registry.

Idempotent: finds existing databases/rows by title, creates missing schema, and
updates seeded business deliverables without duplicating them.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

NOTION_VERSION = "2025-09-03"
COMPANY_HOME = "f27ce5da-5497-82ca-9f0a-0141d10a6b78"
DELIVERABLES_TITLE = "Business Automations & Deliverables"
SCRIPTS_TITLE = "Script Health Registry"
HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
STATE_FILE = HERMES_HOME / ".pcg_automation_registry.json"


def env_key(name: str) -> str:
    envp = HERMES_HOME / ".env"
    if envp.exists():
        for line in envp.read_text(errors="ignore").splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip()
    return os.environ.get(name, "")


def notion(path: str, method: str = "GET", body: dict | None = None) -> dict:
    token = env_key("NOTION_API_KEY") or env_key("NOTION_TOKEN")
    req = urllib.request.Request(
        "https://api.notion.com/v1" + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={
            "Authorization": f"Bearer {token}",
            "Notion-Version": NOTION_VERSION,
            "Content-Type": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            return json.load(r)
    except urllib.error.HTTPError as e:
        detail = e.read().decode(errors="replace")[:1000]
        raise RuntimeError(f"Notion {method} {path} failed: HTTP {e.code}: {detail}") from e


def plain(rt: list[dict] | None) -> str:
    return "".join(x.get("plain_text", "") for x in (rt or []))


def search_title(title: str) -> dict | None:
    data = notion("/search", "POST", {"query": title, "page_size": 100})
    for obj in data.get("results", []):
        if obj.get("object") not in ("database", "data_source"):
            continue
        if plain(obj.get("title")) == title:
            return obj
    return None


def create_database(title: str) -> dict:
    # On this workspace/API version, create the database title first; add schema
    # through its data source afterward (POST /databases can drop properties).
    return notion(
        "/databases",
        "POST",
        {
            "parent": {"type": "page_id", "page_id": COMPANY_HOME},
            "title": [{"type": "text", "text": {"content": title}}],
            "is_inline": False,
        },
    )


def database_ids(obj: dict) -> tuple[str, str]:
    if obj.get("object") == "data_source":
        ds_id = obj["id"]
        ds = notion(f"/data_sources/{ds_id}")
        db_id = (ds.get("parent") or {}).get("database_id")
        if not db_id:
            raise RuntimeError(f"Could not resolve database parent for data source {ds_id}")
        return db_id, ds_id
    db_id = obj["id"]
    db = notion(f"/databases/{db_id}")
    sources = db.get("data_sources") or []
    if not sources:
        raise RuntimeError(f"Database {db_id} has no data source")
    return db_id, sources[0]["id"]


def ensure_database(title: str) -> tuple[str, str, str]:
    obj = search_title(title)
    if not obj:
        obj = create_database(title)
    db_id, ds_id = database_ids(obj)
    url = obj.get("url") or notion(f"/databases/{db_id}").get("url", "")
    return db_id, ds_id, url


def ensure_schema(ds_id: str, properties: dict) -> None:
    current = notion(f"/data_sources/{ds_id}").get("properties", {})
    missing = {name: spec for name, spec in properties.items() if name not in current}
    if missing:
        notion(f"/data_sources/{ds_id}", "PATCH", {"properties": missing})


def set_description(db_id: str, text: str) -> None:
    notion(
        f"/databases/{db_id}",
        "PATCH",
        {"description": [{"type": "text", "text": {"content": text}}]},
    )


def title_prop(name: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": name}}]}


def rt(value: str) -> dict:
    return {"rich_text": [{"type": "text", "text": {"content": value[:1900]}}] if value else []}


def select(value: str | None) -> dict:
    return {"select": {"name": value}} if value else {"select": None}


def multi(values: list[str]) -> dict:
    return {"multi_select": [{"name": v} for v in values]}


def date_prop(value: str | None) -> dict:
    return {"date": {"start": value}} if value else {"date": None}


def url_prop(value: str | None) -> dict:
    return {"url": value or None}


def query_rows(ds_id: str) -> list[dict]:
    rows: list[dict] = []
    cursor = None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = notion(f"/data_sources/{ds_id}/query", "POST", body)
        rows.extend(data.get("results", []))
        if not data.get("has_more"):
            return rows
        cursor = data.get("next_cursor")


def row_title(row: dict) -> str:
    for prop in row.get("properties", {}).values():
        if prop.get("type") == "title":
            return plain(prop.get("title"))
    return ""


def upsert(ds_id: str, name: str, properties: dict) -> str:
    existing = next((r for r in query_rows(ds_id) if row_title(r) == name), None)
    payload = {"properties": {"Name": title_prop(name), **properties}}
    if existing:
        notion(f"/pages/{existing['id']}", "PATCH", payload)
        return existing["id"]
    created = notion("/pages", "POST", {"parent": {"type": "data_source_id", "data_source_id": ds_id}, **payload})
    return created["id"]


DELIVERABLE_SCHEMA = {
    "Name": {"title": {}},
    "Type": {"select": {"options": [
        {"name": "Scheduled Automation", "color": "blue"},
        {"name": "App / Dashboard", "color": "purple"},
        {"name": "Agent Platform", "color": "green"},
        {"name": "Integration", "color": "orange"},
        {"name": "Shared Artifact", "color": "gray"},
    ]}},
    "Status": {"select": {"options": [
        {"name": "Live", "color": "green"}, {"name": "Pilot", "color": "yellow"},
        {"name": "Building", "color": "blue"}, {"name": "Paused", "color": "orange"},
        {"name": "Deprecated", "color": "gray"},
    ]}},
    "Health": {"select": {"options": [
        {"name": "Healthy", "color": "green"}, {"name": "Warning", "color": "yellow"},
        {"name": "Failing", "color": "red"}, {"name": "Unknown", "color": "gray"},
        {"name": "Paused", "color": "orange"},
    ]}},
    "Functions": {"multi_select": {"options": []}},
    "Audience": {"select": {"options": [
        {"name": "Company", "color": "blue"}, {"name": "LT", "color": "purple"},
        {"name": "Function", "color": "green"}, {"name": "Wes", "color": "gray"},
    ]}},
    "Owner": {"rich_text": {}},
    "Owner Email": {"email": {}},
    "Submitted By": {"email": {}},
    "Curation Status": {"select": {"options": [
        {"name": "Proposed", "color": "yellow"}, {"name": "Approved", "color": "green"},
        {"name": "Rejected", "color": "red"}, {"name": "Needs Changes", "color": "orange"},
    ]}},
    "Source Repository": {"url": {}},
    "Repair Policy": {"select": {"options": [
        {"name": "detect-only", "color": "gray"}, {"name": "safe-auto-heal", "color": "blue"},
        {"name": "repair-pr", "color": "green"}, {"name": "critical-approval", "color": "red"},
        {"name": "manual", "color": "orange"},
    ]}},
    "Test Command": {"rich_text": {}},
    "Deployment Method": {"rich_text": {}},
    "Rollback Method": {"rich_text": {}},
    "Alert Target": {"rich_text": {}},
    "Last Changed": {"date": {}},
    "Business Purpose": {"rich_text": {}},
    "Schedule": {"rich_text": {}},
    "URL": {"url": {}},
    "Job ID": {"rich_text": {}},
    "Script Paths": {"rich_text": {}},
    "Last Verified": {"date": {}},
    "Visibility": {"select": {"options": [
        {"name": "Team", "color": "green"}, {"name": "Function", "color": "blue"},
        {"name": "Private", "color": "gray"},
    ]}},
    "Notes": {"rich_text": {}},
}

SCRIPT_SCHEMA = {
    "Name": {"title": {}},
    "Script File": {"rich_text": {}},
    "Instance": {"rich_text": {}},
    "Path": {"rich_text": {}},
    "Function": {"multi_select": {"options": []}},
    "Criticality": {"select": {"options": [
        {"name": "Critical", "color": "red"}, {"name": "High", "color": "orange"},
        {"name": "Normal", "color": "blue"}, {"name": "Utility", "color": "gray"},
    ]}},
    "Health": {"select": {"options": [
        {"name": "Healthy", "color": "green"}, {"name": "Warning", "color": "yellow"},
        {"name": "Failing", "color": "red"}, {"name": "Unknown", "color": "gray"},
        {"name": "Disabled", "color": "orange"},
    ]}},
    "Test Coverage": {"multi_select": {"options": [
        {"name": "Syntax", "color": "gray"}, {"name": "Cron", "color": "blue"},
        {"name": "Dependency", "color": "purple"}, {"name": "Live Probe", "color": "green"},
    ]}},
    "Last Checked": {"date": {}},
    "Last Success": {"date": {}},
    "Last Failure": {"date": {}},
    "Failure Detail": {"rich_text": {}},
    "Cron Jobs": {"rich_text": {}},
    "Supports": {"rich_text": {}},
    "Owner Email": {"email": {}},
    "Source Repository": {"url": {}},
    "Repair Policy": {"select": {"options": [
        {"name": "detect-only", "color": "gray"}, {"name": "safe-auto-heal", "color": "blue"},
        {"name": "repair-pr", "color": "green"}, {"name": "critical-approval", "color": "red"},
        {"name": "manual", "color": "orange"},
    ]}},
    "Test Command": {"rich_text": {}},
    "Deployment Method": {"rich_text": {}},
    "Rollback Method": {"rich_text": {}},
    "Alert Target": {"rich_text": {}},
    "Incident Status": {"select": {"options": [
        {"name": "None", "color": "gray"}, {"name": "Open", "color": "red"},
        {"name": "Repairing", "color": "blue"}, {"name": "Awaiting Approval", "color": "yellow"},
        {"name": "Approved", "color": "green"}, {"name": "Rejected", "color": "orange"},
        {"name": "Resolved", "color": "green"}, {"name": "Retry", "color": "purple"},
    ]}},
    "Incident Key": {"rich_text": {}},
    "Fix PR": {"url": {}},
    "Last Repair Attempt": {"date": {}},
    "Approval Instructions": {"rich_text": {}},
    "Source URL": {"url": {}},
    "Repository Managed": {"checkbox": {}},
    "Notes": {"rich_text": {}},
}


DELIVERABLES = [
    dict(name="Open-box Returns Dashboard", type="App / Dashboard", status="Live", health="Healthy", functions=["Operations", "Finance"], audience="Company", owner="Wes / Operations", purpose="Shared operational dashboard for open-box returns, pricing, credits, and vendor recovery.", schedule="On demand", url="https://open-box-dashboard.wes-34f.workers.dev", job="", scripts="External repo: WWWPCG/open-box-dashboard", visibility="Team", notes="Cloudflare Worker; source in private GitHub repo."),
    dict(name="Alfred Slack Agent", type="Integration", status="Live", health="Healthy", functions=["LT"], audience="LT", owner="Wes", purpose="Slack access to the Exec/LT Hermes agent.", schedule="Always on", url="", job="", scripts="Hermes Slack integration", visibility="Team", notes="Functional deployment complete; Slack UI rename and DM test may still be pending."),
    dict(name="Agent Team Roster & Onboarding", type="Agent Platform", status="Live", health="Healthy", functions=["Company"], audience="Company", owner="Wes / LT", purpose="Notion control plane for Primary, Adjacent, LT access, onboarding, revocation, and live profile state.", schedule="Profile sync every 15 minutes", url="https://app.notion.com/p/Company-Home-f27ce5da549782ca9f0a0141d10a6b78", job="pcg-profile-sync (per member box)", scripts="pcg_onboard.py, profile_sync.py, roster_sync_check.py, gen_onboard_cmd.py", visibility="Team", notes="Fresh-box acceptance test will use wes@foresightequity.com later."),
    dict(name="Fleet Skills, Scripts & Jobs Distribution", type="Agent Platform", status="Live", health="Healthy", functions=["Company"], audience="Company", owner="Wes / LT", purpose="Distributes common and function-scoped skills, shared scripts, and managed jobs from the private pcg-agents repo.", schedule="Every 30 minutes per member box", url="https://github.com/WWWPCG/pcg-agents", job="pcg-fleet-sync (per member box)", scripts="pcg_sync.py", visibility="Team", notes="Read-only fine-grained GitHub token on member boxes; repo-managed assets use pcg- prefix."),
    dict(name="Function-to-LT Context Bridges", type="Scheduled Automation", status="Live", health="Healthy", functions=["LT", "Company"], audience="LT", owner="LT", purpose="Summarizes material changes from each function into LT context without dual-writing raw conversations.", schedule="Daily at 06:30 UTC", url="", job="dbc8e33efdd5", scripts="run_bridges.py", visibility="Team", notes="Seven function-to-exec edges plus existing cross-function edges."),
    dict(name="Front Inbox Auto-filter", type="Scheduled Automation", status="Live", health="Healthy", functions=["Company"], audience="Wes", owner="Wes", purpose="Moves messages out of Pro Coffee Gear and Personal source inboxes into the correct destination inboxes.", schedule="08:00, 11:00, 14:00, 17:00, 20:00, 23:00 UTC", url="https://app.frontapp.com", job="30d663e8f0b4", scripts="filter_prompt.txt; Front MCP", visibility="Private", notes="Meaningful business workflow; MCP health is monitored separately."),
    dict(name="Front Single Draft", type="Scheduled Automation", status="Live", health="Healthy", functions=["Company"], audience="Wes", owner="Wes", purpose="Creates one high-quality email draft from eligible Focus conversations.", schedule="15 minutes after each filter cycle", url="https://app.frontapp.com", job="88012bfd43f6", scripts="front_draft_gate.py, front_context_build.py", visibility="Private", notes="Sonnet drafting with a cheap gate."),
    dict(name="Front @model Draft Trigger", type="Scheduled Automation", status="Live", health="Healthy", functions=["Company"], audience="Wes", owner="Wes", purpose="Turns @sonnet/@opus/@haiku/@fable comments in Front into ready-to-send drafts.", schedule="Every minute", url="https://app.frontapp.com", job="d56a2a1bf3d1", scripts="front_model_scan.py, front_context_build.py", visibility="Private", notes="Comment scanner is deterministic because Front search does not index comments."),
    dict(name="Front Draft Review & Feedback Loop", type="Scheduled Automation", status="Live", health="Healthy", functions=["Company"], audience="Wes", owner="Wes", purpose="Reviews drafting outcomes and processes explicit feedback to improve the drafting system.", schedule="Review daily 04:00 UTC; feedback daily 07:00 UTC", url="https://app.frontapp.com", job="044971ce4142, 34514464edce", scripts="front_review_gate.py, front_context_build.py", visibility="Private", notes="Opus review is gated to avoid expensive no-op runs."),
    dict(name="Front Context Builder", type="Scheduled Automation", status="Live", health="Healthy", functions=["Company"], audience="Wes", owner="Wes", purpose="Builds compact drafting context from recent Front activity.", schedule="Daily 04:30 UTC", url="https://app.frontapp.com", job="29663fe90897", scripts="front_context_build.py", visibility="Private", notes="Feeds the drafting system; local delivery."),
    dict(name="Front MCP Watchdog", type="Scheduled Automation", status="Live", health="Healthy", functions=["Company"], audience="Wes", owner="Wes", purpose="Detects dead Front OAuth/MCP connections so business workflows cannot fail silently.", schedule="Every 5 minutes", url="", job="f33e58116721", scripts="front_mcp_watchdog.py, front_context_build.py", visibility="Private", notes="Alerts on failure; healthy runs are silent."),
    dict(name="Shopify to Attio Reconciliation", type="Scheduled Automation", status="Paused", health="Paused", functions=["Sales", "Operations"], audience="Function", owner="Sales / Operations", purpose="Reconciles Shopify B2B data into existing Attio CRM records.", schedule="Previously daily 02:00", url="", job="c5c9ea40ad42, 4703628162ed", scripts="Legacy Windows workdir", visibility="Function", notes="Disabled after migration; needs an explicit migrate-or-retire decision."),
    dict(name="Shared Function Skills Library", type="Shared Artifact", status="Live", health="Healthy", functions=["Company"], audience="Company", owner="Function owners", purpose="Common and function-specific procedural knowledge distributed to the right agent profiles.", schedule="Synced every 30 minutes", url="https://github.com/WWWPCG/pcg-agents/tree/main/skills", job="pcg-fleet-sync", scripts="pcg_sync.py", visibility="Team", notes="Common skills stay small; functional skills only load in matching profiles."),
    dict(name="Automation Catalog & Script Health Monitoring", type="Scheduled Automation", status="Live", health="Healthy", functions=["Company"], audience="Company", owner="LT / system owners", purpose="Makes significant business deliverables visible and continuously checks script syntax, dependencies, cron state, live integrations, and missing files.", schedule="Every 15 minutes on each managed box", url="https://app.notion.com/p/10381e9695214aab80cc04644564cb64", job="3a6d9653c521 (exec); pcg-automation-health (fleet)", scripts="pcg-automation-health.py, pcg-automation-registry.py", visibility="Team", notes="Healthy runs are silent; failures and recoveries alert through connected channels. Member boxes write instance-scoped rows."),
]


def deliverable_props(item: dict) -> dict:
    owner_email = item.get("owner_email") or ("wes@procoffeegear.com" if "Wes" in item.get("owner", "") else "")
    source_repo = item.get("source_repo") or (item.get("url", "") if "github.com/" in item.get("url", "") else "")
    repair_policy = item.get("repair_policy") or ("manual" if item.get("status") == "Paused" else "repair-pr")
    now = datetime.now(timezone.utc).isoformat()
    return {
        "Type": select(item["type"]), "Status": select(item["status"]), "Health": select(item["health"]),
        "Functions": multi(item["functions"]), "Audience": select(item["audience"]), "Owner": rt(item["owner"]),
        "Owner Email": {"email": owner_email or None}, "Submitted By": {"email": owner_email or None},
        "Curation Status": select(item.get("curation_status", "Approved")),
        "Source Repository": url_prop(source_repo), "Repair Policy": select(repair_policy),
        "Test Command": rt(item.get("test_command", "")),
        "Deployment Method": rt(item.get("deployment_method", "")),
        "Rollback Method": rt(item.get("rollback_method", "Revert the source change and redeploy.")),
        "Alert Target": rt(item.get("alert_target", owner_email)), "Last Changed": date_prop(now),
        "Business Purpose": rt(item["purpose"]), "Schedule": rt(item["schedule"]), "URL": url_prop(item["url"]),
        "Job ID": rt(item["job"]), "Script Paths": rt(item["scripts"]),
        "Last Verified": date_prop(now),
        "Visibility": select(item["visibility"]), "Notes": rt(item["notes"]),
    }


def main() -> int:
    if not env_key("NOTION_API_KEY") and not env_key("NOTION_TOKEN"):
        raise SystemExit("NOTION_API_KEY is missing")

    d_db, d_ds, d_url = ensure_database(DELIVERABLES_TITLE)
    s_db, s_ds, s_url = ensure_database(SCRIPTS_TITLE)
    ensure_schema(d_ds, DELIVERABLE_SCHEMA)
    ensure_schema(s_ds, SCRIPT_SCHEMA)
    set_description(d_db, "Team-facing catalog of significant business automations, scheduled jobs, apps, dashboards, and shared artifacts. MANUAL: purpose, owner, audience, visibility, notes. DYNAMIC: health and last verified are maintained by automation where possible. Create a Board view grouped by Status or Type in the Notion UI.")
    set_description(s_db, "Operational registry for scripts on the PCG Hermes fleet. DYNAMIC: Health, Last Checked, Last Success, Last Failure, Failure Detail, and Cron Jobs are maintained by pcg-automation-health.py. Do not edit those fields manually. Create a Board view grouped by Health in the Notion UI.")

    delivery_ids = {}
    for item in DELIVERABLES:
        delivery_ids[item["name"]] = upsert(d_ds, item["name"], deliverable_props(item))

    state = {
        "deliverables": {"database_id": d_db, "data_source_id": d_ds, "url": d_url},
        "scripts": {"database_id": s_db, "data_source_id": s_ds, "url": s_url},
        "seeded_at": datetime.now(timezone.utc).isoformat(),
    }
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")
    print(json.dumps(state, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
