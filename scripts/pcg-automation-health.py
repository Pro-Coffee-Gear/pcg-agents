#!/usr/bin/env python3
"""PCG automation health monitor.

Runs as a no-agent cron job. It inventories /opt/data/scripts, performs safe
syntax/dependency/cron checks, updates the Notion Script Health Registry and
Business Automations & Deliverables catalog, and prints only state changes or
periodic failure reminders. Empty stdout means healthy/no change.
"""
from __future__ import annotations

import ipaddress
import json
import os
import py_compile
import re
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

HERMES_HOME = Path(os.environ.get("HERMES_HOME", "/opt/data"))
sys.path.insert(0, str(HERMES_HOME / "scripts"))
from pcg_automation_core import (incident_transition, is_safe_repo_restore,
                                 parse_health_toml, policy_for)  # noqa: E402

HERMES_BIN = os.environ.get("HERMES_BIN", "/opt/hermes/.venv/bin/hermes")
SCRIPTS_DIR = HERMES_HOME / "scripts"
REGISTRY_FILE = HERMES_HOME / ".pcg_automation_registry.json"
DEFAULT_DELIVERABLE_DS = "d27ab37d-f4f0-4303-a87e-c0edc890cd66"
DEFAULT_SCRIPT_DS = "c9290a04-1c19-41fc-b76e-35d23532c510"
DEFAULT_DELIVERABLE_URL = "https://app.notion.com/p/a6c0a72f550a4f97a5990fc07a44a49e"
DEFAULT_SCRIPT_URL = "https://app.notion.com/p/10381e9695214aab80cc04644564cb64"
MEMBER_EMAIL_FILE = HERMES_HOME / ".pcg_member_email"
INSTANCE = MEMBER_EMAIL_FILE.read_text().strip() if MEMBER_EMAIL_FILE.exists() else "exec-default"
IS_CONTROL_PLANE = INSTANCE == "exec-default"
STATE_FILE = HERMES_HOME / ".pcg_automation_health_state.json"
JOBS_FILE = HERMES_HOME / "cron" / "jobs.json"
OUTPUT_DIR = HERMES_HOME / "cron" / "output"
NOTION_VERSION = "2025-09-03"
REMINDER_HOURS = 6
NOW = datetime.now(timezone.utc)

CRITICAL = {
    "front_context_build.py", "front_mcp_watchdog.py", "pcg_onboard.py",
    "pcg_sync.py", "profile_sync.py", "pcg-automation-health.py",
}
HIGH = {
    "front_draft_gate.py", "front_model_scan.py", "front_review_gate.py",
    "run_bridges.py", "ingest_intake_docs.py", "roster_sync_check.py",
}
UTILITY = {
    "colorize_roster.py", "scaffold_pcg_repo.py", "update_onboard_doc.py",
    "front_oauth.py", "pcg-automation-registry.py",
}
FRONT_FILES = {p.name for p in SCRIPTS_DIR.glob("front_*.py")}
NOTION_FILES = {
    "pcg_onboard.py", "pcg_sync.py", "profile_sync.py", "roster_sync_check.py",
    "gen_onboard_cmd.py", "ingest_intake_docs.py", "update_onboard_doc.py",
    "colorize_roster.py", "pcg-automation-registry.py", "pcg-automation-health.py",
}
GITHUB_FILES = {"pcg_sync.py", "scaffold_pcg_repo.py", "gen_onboard_cmd.py", "pcg_onboard.py"}

SUPPORTS = {
    "front_context_build.py": "Front drafting and inbox automation suite",
    "front_draft_gate.py": "Front Single Draft",
    "front_mcp_watchdog.py": "Front MCP Watchdog; all Front automations",
    "front_model_scan.py": "Front @model Draft Trigger",
    "front_review_gate.py": "Front Draft Review & Feedback Loop",
    "front_oauth.py": "Front MCP authentication",
    "pcg_onboard.py": "Agent Team Roster & Onboarding",
    "profile_sync.py": "Agent Team Roster & Onboarding",
    "roster_sync_check.py": "Agent Team Roster & Onboarding",
    "gen_onboard_cmd.py": "Agent Team Roster & Onboarding",
    "pcg_sync.py": "Fleet Skills, Scripts & Jobs Distribution; Shared Function Skills Library",
    "run_bridges.py": "Function-to-LT Context Bridges",
    "ingest_intake_docs.py": "Function standing-context ingestion",
    "honcho_bootstrap_peers.py": "Agent memory bootstrap",
    "pcg-automation-registry.py": "Business Automations & Deliverables; Script Health Registry",
    "pcg-automation-health.py": "Script Health Registry; Business Automations & Deliverables",
}


def env_key(*names: str) -> str:
    envp = HERMES_HOME / ".env"
    if envp.exists():
        for line in envp.read_text(errors="ignore").splitlines():
            for name in names:
                if line.startswith(name + "="):
                    return line.split("=", 1)[1].strip()
    for name in names:
        if os.environ.get(name):
            return os.environ[name]
    return ""


def notion(path: str, method: str = "GET", body: dict | None = None) -> dict:
    token = env_key("NOTION_API_KEY", "NOTION_TOKEN")
    req = urllib.request.Request(
        "https://api.notion.com/v1" + path,
        method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Notion-Version": NOTION_VERSION,
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=45) as r:
        return json.load(r)


def load_health_policies() -> dict[str, dict]:
    local = HERMES_HOME / "health.toml"
    if local.exists():
        try:
            return parse_health_toml(local.read_text())
        except Exception:
            pass
    token = env_key("GITHUB_SYNC_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")
    if not token:
        return {}
    req = urllib.request.Request(
        "https://api.github.com/repos/WWWPCG/pcg-agents/contents/health.toml",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.raw"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return parse_health_toml(r.read().decode())
    except Exception:
        return {}


def safe_restore_script(name: str, policy: dict) -> bool:
    """Restore a missing pcg-agents script only after validating path and syntax."""
    if not is_safe_repo_restore(name, policy):
        return False
    token = env_key("GITHUB_SYNC_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")
    if not token:
        return False
    source_path = policy["source_path"]
    req = urllib.request.Request(
        f"https://api.github.com/repos/WWWPCG/pcg-agents/contents/{source_path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.raw"},
    )
    tmp = SCRIPTS_DIR / f".{name}.health-restore.tmp"
    dest = SCRIPTS_DIR / name
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            data = r.read()
        tmp.write_bytes(data)
        py_compile.compile(str(tmp), doraise=True)
        os.replace(tmp, dest)
        return True
    except Exception:
        if tmp.exists():
            tmp.unlink()
        return False


def gh_probe() -> tuple[bool, str]:
    token = env_key("GITHUB_SYNC_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")
    if not token:
        return False, "GitHub sync token missing"
    req = urllib.request.Request(
        "https://api.github.com/repos/WWWPCG/pcg-agents",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"},
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return (r.status == 200, "")
    except Exception as e:
        return False, f"pcg-agents repo probe failed: {type(e).__name__}"


def notion_probe() -> tuple[bool, str]:
    try:
        data = notion("/users/me")
        return (bool(data.get("id")), "" if data.get("id") else "Notion /users/me returned no id")
    except Exception as e:
        return False, f"Notion API probe failed: {type(e).__name__}"


def front_probe() -> tuple[bool, str]:
    script = SCRIPTS_DIR / "front_mcp_watchdog.py"
    if not script.exists():
        return False, "Front watchdog script missing"
    try:
        r = subprocess.run([sys.executable, str(script)], cwd=HERMES_HOME, capture_output=True,
                           text=True, timeout=75, env={**os.environ, "HERMES_HOME": str(HERMES_HOME)})
        out = (r.stdout + "\n" + r.stderr).strip()
        return (r.returncode == 0 and not out, out[:500] if out else "")
    except Exception as e:
        return False, f"Front live probe failed: {type(e).__name__}"


def url_probe(url: str) -> tuple[bool, str]:
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "PCG-Automation-Health/1.0"})
        with urllib.request.urlopen(req, timeout=30) as r:
            return (200 <= r.status < 400, "")
    except Exception as e:
        return False, f"URL probe failed: {type(e).__name__}"


def parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc)
    except Exception:
        return None


def load_jobs() -> list[dict]:
    try:
        data = json.loads(JOBS_FILE.read_text())
        return data.get("jobs", []) if isinstance(data, dict) else data
    except Exception:
        return []


def latest_output(job_id: str) -> str:
    d = OUTPUT_DIR / job_id
    if not d.is_dir():
        return ""
    # Filenames are ISO-like timestamps and sort chronologically. Use names, not
    # mtimes: migrated output files can retain copy-time mtimes that misorder runs.
    files = sorted(d.glob("*.md"), key=lambda p: p.name, reverse=True)
    if not files:
        return ""
    text = files[0].read_text(errors="replace")
    # Agent outputs include the prompt. Scan only the final response to avoid
    # matching failure words that appear inside instructions.
    if "## Response" in text:
        text = text.rsplit("## Response", 1)[1]
    return text[-8000:]


def job_issues(job: dict) -> list[str]:
    issues: list[str] = []
    name = job.get("name") or job.get("id") or "unknown job"
    if not job.get("enabled", True):
        return []
    if (job.get("last_status") or "").lower() not in ("", "ok", "success"):
        issues.append(f"{name}: last_status={job.get('last_status')}")
    workdir = str(job.get("workdir") or "")
    if re.match(r"^[A-Za-z]:\\", workdir):
        issues.append(f"{name}: stale Windows workdir {workdir}")
    prompt = str(job.get("prompt") or "")
    if "C:\\Users\\" in prompt:
        issues.append(f"{name}: prompt contains stale Windows path")
    nxt = parse_dt(job.get("next_run_at"))
    if nxt and nxt < NOW - timedelta(minutes=10):
        issues.append(f"{name}: scheduler overdue; next_run_at={nxt.isoformat()}")
    response = latest_output(job.get("id") or job.get("job_id") or "")
    bad_patterns = [
        r"Traceback \(most recent call last\)", r"\[WATCHDOG ALERT\]",
        r"Front tools (?:are|were) not available", r"no Front tools",
        r"MCP server ['\"]front['\"] is unreachable", r"No such file or directory",
    ]
    for pat in bad_patterns:
        if re.search(pat, response, re.I):
            issues.append(f"{name}: latest output matched failure pattern")
            break
    return issues


def text_value(prop: dict) -> str:
    typ = prop.get("type")
    vals = prop.get(typ, []) if typ else []
    if isinstance(vals, list):
        return "".join(x.get("plain_text", "") for x in vals)
    return ""


def title_value(row: dict) -> str:
    for p in row.get("properties", {}).values():
        if p.get("type") == "title":
            return "".join(x.get("plain_text", "") for x in p.get("title", []))
    return ""


def query_rows(ds_id: str) -> list[dict]:
    rows, cursor = [], None
    while True:
        body = {"page_size": 100}
        if cursor:
            body["start_cursor"] = cursor
        data = notion(f"/data_sources/{ds_id}/query", "POST", body)
        rows.extend(data.get("results", []))
        if not data.get("has_more"):
            return rows
        cursor = data.get("next_cursor")


def rich(value: str) -> dict:
    return {"rich_text": [{"type": "text", "text": {"content": value[:1900]}}] if value else []}


def select(value: str) -> dict:
    return {"select": {"name": value}}


def multi(values: list[str]) -> dict:
    return {"multi_select": [{"name": v} for v in values]}


def date(value: str | None) -> dict:
    return {"date": {"start": value}} if value else {"date": None}


def checkbox(value: bool) -> dict:
    return {"checkbox": value}


def title(name: str) -> dict:
    return {"title": [{"type": "text", "text": {"content": name}}]}


def source_url(name: str) -> str:
    return f"https://github.com/WWWPCG/pcg-agents/blob/main/scripts/{name}"


def script_functions(name: str) -> list[str]:
    if name.startswith("front_"):
        return ["Company"]
    if name == "run_bridges.py":
        return ["LT", "Company"]
    if name.startswith(("pcg", "profile", "roster", "gen_onboard", "ingest", "honcho", "colorize", "update_onboard", "scaffold")):
        return ["Company"]
    return ["Company"]


def criticality(name: str) -> str:
    if name in CRITICAL:
        return "Critical"
    if name in HIGH:
        return "High"
    if name in UTILITY:
        return "Utility"
    return "Normal"


def upsert_script(ds_id: str, rows_by_name: dict[str, dict], result: dict,
                  policies: dict[str, dict]) -> None:
    name = result["name"]
    display_name = name if IS_CONTROL_PLANE else f"{name} — {INSTANCE}"
    existing = rows_by_name.get(display_name)
    old_last_success = old_last_failure = None
    old_health, existing_key, existing_status, existing_pr, existing_approval = "Unknown", "", "None", "", ""
    if existing:
        old_props = existing.get("properties", {})
        old_last_success = ((old_props.get("Last Success", {}).get("date") or {}).get("start"))
        old_last_failure = ((old_props.get("Last Failure", {}).get("date") or {}).get("start"))
        old_health = (old_props.get("Health", {}).get("select") or {}).get("name") or "Unknown"
        existing_key = text_value(old_props.get("Incident Key", {}))
        existing_status = (old_props.get("Incident Status", {}).get("select") or {}).get("name") or "None"
        existing_pr = old_props.get("Fix PR", {}).get("url") or ""
        existing_approval = text_value(old_props.get("Approval Instructions", {}))
    failure_detail = "; ".join(result["issues"])
    transition = incident_transition(old_health, result["health"], INSTANCE, name,
                                     failure_detail, existing_key, existing_status)
    if transition["incident_key"] != existing_key and transition["incident_status"] == "Open":
        existing_pr, existing_approval = "", ""
    fallback_owner = INSTANCE if "@" in INSTANCE else "wes@procoffeegear.com"
    policy = policy_for(name, policies, fallback_owner)
    if transition["incident_status"] == "Open" and policy["repair_policy"] == "repair-pr":
        existing_approval = "Repair agent may open a tested PR, but the owner must approve/merge it."
    last_success = NOW.isoformat() if result["health"] == "Healthy" else old_last_success
    last_failure = NOW.isoformat() if result["health"] == "Failing" else old_last_failure
    props = {
        "Name": title(display_name), "Script File": rich(name), "Instance": rich(INSTANCE),
        "Path": rich(result["path"]), "Function": multi(result["functions"]),
        "Criticality": select(result["criticality"]), "Health": select(result["health"]),
        "Test Coverage": multi(result["coverage"]), "Last Checked": date(NOW.isoformat()),
        "Last Success": date(last_success), "Last Failure": date(last_failure),
        "Failure Detail": rich(failure_detail), "Cron Jobs": rich(result["cron_names"]),
        "Supports": rich(result["supports"]),
        "Source URL": {"url": source_url(name) if policy["source_repo"] else None},
        "Owner Email": {"email": policy["owner_email"] or None},
        "Source Repository": {"url": policy["source_repo"] or None},
        "Repair Policy": select(policy["repair_policy"]),
        "Test Command": rich(policy["test_command"]),
        "Deployment Method": rich(policy["deployment_method"]),
        "Rollback Method": rich(policy["rollback_method"]),
        "Alert Target": rich(policy["alert_target"]),
        "Incident Status": select(transition["incident_status"]),
        "Incident Key": rich(transition["incident_key"]), "Fix PR": {"url": existing_pr or None},
        "Approval Instructions": rich(existing_approval),
        "Repository Managed": checkbox(bool(policy["source_repo"])), "Notes": rich(result["notes"]),
    }
    if existing:
        notion(f"/pages/{existing['id']}", "PATCH", {"properties": props})
    else:
        notion("/pages", "POST", {"parent": {"type": "data_source_id", "data_source_id": ds_id}, "properties": props})


def test_prompt_jobs(jobs: list[dict]) -> list[dict]:
    """Register prompt-only cron jobs (no script file) as first-class registry rows.

    A cron job whose work is a bare prompt (no --script) is invisible to the
    file-scan in test_scripts — but it's still a running automation that can
    fail. Track it under a "job: <name>" row; health comes from schedule,
    last-run status, and output-pattern checks only (coverage: Cron).
    """
    results = []
    for job in jobs:
        if job.get("script"):
            continue  # script-backed jobs are tracked via their script file
        name = job.get("name") or job.get("id") or "unnamed-job"
        reg_name = f"job: {name}"
        enabled = bool(job.get("enabled", True))
        issues = job_issues(job) if enabled else []
        health = ("Failing" if issues else "Healthy") if enabled else "Disabled"
        results.append({
            "name": reg_name,
            "path": f"cron job (prompt-only): {name}",
            "functions": ["Company"],
            "criticality": criticality(name),
            "health": health,
            "coverage": ["Cron"],
            "issues": issues,
            "cron_names": name,
            "supports": "",
            "notes": "Prompt-only cron job (no script file). Health from schedule, last-run status, and output checks.",
        })
    return results


def test_scripts(jobs: list[dict], deps: dict[str, tuple[bool, str]]) -> list[dict]:
    by_script: dict[str, list[dict]] = {}
    for job in jobs:
        script = os.path.basename(str(job.get("script") or ""))
        if script:
            by_script.setdefault(script, []).append(job)
    results = []
    for path in sorted(SCRIPTS_DIR.glob("*.py")):
        if path.name.startswith("_"):
            continue  # local build/test scratch files are not production registry items
        name = path.name
        issues, coverage = [], ["Syntax"]
        try:
            py_compile.compile(str(path), doraise=True)
        except Exception as e:
            issues.append(f"syntax: {type(e).__name__}: {e}")
        linked = by_script.get(name, [])
        if linked:
            coverage.append("Cron")
            for job in linked:
                issues.extend(job_issues(job))
        if name in FRONT_FILES:
            coverage.extend(["Dependency", "Live Probe"])
            if not deps["front"][0]:
                issues.append(deps["front"][1] or "Front live probe failed")
        if name in NOTION_FILES:
            coverage.append("Dependency")
            if not deps["notion"][0]:
                issues.append(deps["notion"][1])
        if name in GITHUB_FILES:
            coverage.append("Dependency")
            if not deps["github"][0]:
                issues.append(deps["github"][1])
        coverage = list(dict.fromkeys(coverage))
        health = "Failing" if issues else "Healthy"
        notes = "Safe checks only; scripts with external writes are not executed during health monitoring."
        if coverage == ["Syntax"]:
            notes = "Syntax check only; add a dependency or live probe if this becomes business-critical."
        results.append({
            "name": name, "path": str(path), "functions": script_functions(name),
            "criticality": criticality(name), "health": health, "coverage": coverage,
            "issues": list(dict.fromkeys(issues)),
            "cron_names": ", ".join(j.get("name", "") for j in linked),
            "supports": SUPPORTS.get(name, ""), "notes": notes,
        })
    return results


def is_public_https_url(url: str) -> bool:
    parsed = urllib.parse.urlsplit(url)
    hostname = (parsed.hostname or "").lower()
    if parsed.scheme != "https" or not hostname:
        return False
    if hostname == "localhost" or hostname.endswith((".local", ".internal")):
        return False
    try:
        address = ipaddress.ip_address(hostname)
    except ValueError:
        return True
    return address.is_global


def deliverable_health(
    row: dict,
    jobs_by_id: dict[str, dict],
    deps: dict[str, tuple[bool, str]],
    script_health_by_id: dict[str, str] | None = None,
) -> tuple[str, list[str]]:
    props = row.get("properties", {})
    name = title_value(row)
    status = (props.get("Status", {}).get("select") or {}).get("name")
    if status == "Paused":
        return "Paused", []
    issues = []
    observed = False
    job_text = text_value(props.get("Job ID", {}))
    ids = re.findall(r"[0-9a-f]{12}", job_text)
    for jid in ids:
        observed = True
        job = jobs_by_id.get(jid)
        if not job:
            issues.append(f"job {jid} not found")
        else:
            issues.extend(job_issues(job))

    script_health_by_id = script_health_by_id or {}
    for relation in props.get("Scripts", {}).get("relation", []):
        observed = True
        related_health = script_health_by_id.get(relation.get("id"), "Unknown")
        if related_health != "Healthy":
            issues.append(f"related script is {related_health}")

    if name.startswith("Front "):
        observed = True
        if not deps["front"][0]:
            issues.append(deps["front"][1] or "Front live probe failed")
    if name in {"Agent Team Roster & Onboarding"}:
        observed = True
        if not deps["notion"][0]:
            issues.append(deps["notion"][1])
    if name in {"Agent Team Roster & Onboarding", "Fleet Skills, Scripts & Jobs Distribution", "Shared Function Skills Library"}:
        observed = True
        if not deps["github"][0]:
            issues.append(deps["github"][1])
    if name == "Open-box Returns Dashboard":
        observed = True
        if not deps["dashboard"][0]:
            issues.append(deps["dashboard"][1])

    kind = (props.get("Type", {}).get("select") or {}).get("name")
    url = props.get("URL", {}).get("url") or ""
    if url and kind == "App / Dashboard" and name != "Open-box Returns Dashboard":
        observed = True
        if not is_public_https_url(url):
            issues.append("URL health check must use a public HTTPS endpoint")
        else:
            ok, detail = url_probe(url)
            if not ok:
                issues.append(detail or "URL probe failed")

    if issues:
        # Configuration drift is a warning; execution/dependency failures are failing.
        only_config = all("stale Windows path" in x for x in issues)
        return ("Warning" if only_config else "Failing"), list(dict.fromkeys(issues))
    if not observed:
        return "Unknown", ["No health check configured"]
    return "Healthy", []


def update_deliverables(
    ds_id: str,
    script_ds_id: str,
    jobs: list[dict],
    deps: dict[str, tuple[bool, str]],
) -> dict[str, dict]:
    results = {}
    jobs_by_id = {
        key: job
        for job in jobs
        if (key := job.get("id") or job.get("job_id"))
    }
    script_health_by_id = {
        row["id"]: ((row.get("properties", {}).get("Health", {}).get("select") or {}).get("name") or "Unknown")
        for row in query_rows(script_ds_id)
    }
    for row in query_rows(ds_id):
        name = title_value(row)
        props = row.get("properties", {})
        health, issues = deliverable_health(row, jobs_by_id, deps, script_health_by_id)
        updates = {
            "Health": select(health),
            "Last Verified": date(NOW.isoformat() if health in {"Healthy", "Warning", "Failing"} else None),
        }
        alert_target = text_value(props.get("Alert Target", {})).strip()
        owner_email = props.get("Owner Email", {}).get("email") or ""
        if not alert_target and owner_email:
            updates["Alert Target"] = rich(owner_email)
        notion(f"/pages/{row['id']}", "PATCH", {"properties": updates})
        results[name] = {"health": health, "issues": issues}
    return results


def sync_links(deliverable_ds: str, script_ds: str) -> int:
    """Keep Deliverables.Scripts <-> Registry.Deliverable links current.

    Two signals, unioned: registry 'Supports' text matched to deliverable names,
    and deliverable 'Script Paths' filenames matched to registry script names.
    Idempotent — only PATCHes a deliverable whose link set actually changed.
    Returns the number of deliverables updated.
    """
    delivs = {title_value(d): d for d in query_rows(deliverable_ds)}
    scripts = {title_value(s): s for s in query_rows(script_ds)}
    links: dict[str, set[str]] = {}
    for sname, s in scripts.items():
        sup = text_value(s.get("properties", {}).get("Supports", {}))
        for dname in delivs:
            if dname and dname in sup:
                links.setdefault(dname, set()).add(sname)
    for dname, d in delivs.items():
        sp = text_value(d.get("properties", {}).get("Script Paths", {}))
        for token in sp.replace(";", ",").split(","):
            token = token.strip()
            if token in scripts:
                links.setdefault(dname, set()).add(token)
    updated = 0
    for dname, snames in links.items():
        d = delivs[dname]
        have_ids = {r["id"] for r in d.get("properties", {}).get("Scripts", {}).get("relation", [])}
        want_ids = {scripts[s]["id"] for s in sorted(snames)}
        if want_ids != have_ids:
            notion(f"/pages/{d['id']}", "PATCH",
                   {"properties": {"Scripts": {"relation": [{"id": i} for i in sorted(want_ids)]}}})
            updated += 1
    return updated


def load_state() -> dict:
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return {"items": {}, "last_alert_at": None}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def alerts(script_results: list[dict], deliverable_results: dict[str, dict]) -> list[str]:
    previous = load_state()
    old_items = previous.get("items", {})
    now_items = {}
    lines = []
    combined = {}
    for r in script_results:
        combined[f"script:{r['name']}"] = {"label": f"Script {r['name']}", "health": r["health"], "issues": r["issues"]}
    for name, r in deliverable_results.items():
        combined[f"deliverable:{name}"] = {"label": name, **r}
    for key, item in combined.items():
        now_items[key] = item["health"]
        old = old_items.get(key)
        if item["health"] in ("Failing", "Warning") and old != item["health"]:
            detail = "; ".join(item.get("issues", [])) or item["health"]
            lines.append(f"{item['label']}: {item['health']} — {detail}")
        elif item["health"] == "Healthy" and old in ("Failing", "Warning"):
            lines.append(f"{item['label']}: RECOVERED")
    last_alert = parse_dt(previous.get("last_alert_at"))
    failing = [i for i in combined.values() if i["health"] == "Failing"]
    if failing and not lines and (not last_alert or NOW - last_alert > timedelta(hours=REMINDER_HOURS)):
        lines.append(f"Reminder: {len(failing)} automation health item(s) still failing. See the Notion Script Health Registry.")
    state = {"items": now_items, "last_alert_at": NOW.isoformat() if lines else previous.get("last_alert_at")}
    save_state(state)
    return lines


def main() -> int:
    if REGISTRY_FILE.exists():
        reg = json.loads(REGISTRY_FILE.read_text())
    else:
        # Member boxes do not run the database bootstrap; the shared IDs are
        # stable and safe to distribute. Their own Notion token still gates access.
        reg = {
            "scripts": {"data_source_id": DEFAULT_SCRIPT_DS, "url": DEFAULT_SCRIPT_URL},
            "deliverables": {"data_source_id": DEFAULT_DELIVERABLE_DS, "url": DEFAULT_DELIVERABLE_URL},
        }
    script_ds = reg["scripts"]["data_source_id"]
    deliverable_ds = reg["deliverables"]["data_source_id"]
    jobs = load_jobs()
    policies = load_health_policies()
    deps = {
        "front": front_probe(), "notion": notion_probe(), "github": gh_probe(),
        "dashboard": url_probe("https://open-box-dashboard.wes-34f.workers.dev/api/diagnostics"),
    }
    script_rows = {title_value(r): r for r in query_rows(script_ds)}
    script_results = test_scripts(jobs, deps)
    script_results.extend(test_prompt_jobs(jobs))
    present = {r["name"] for r in script_results}
    # A previously registered script disappearing is itself a failure. This
    # catches broken syncs and accidental deletions rather than letting the row
    # silently retain its last green status.
    for display_name, row in script_rows.items():
        props = row.get("properties", {})
        row_instance = text_value(props.get("Instance", {})) or "exec-default"
        script_file = text_value(props.get("Script File", {})) or display_name
        if row_instance != INSTANCE or script_file in present or script_file.startswith("_"):
            continue
        fallback_owner = INSTANCE if "@" in INSTANCE else "wes@procoffeegear.com"
        if script_file.startswith("job:"):
            # Prompt-only job removed from the cron schedule — no file to restore.
            # Mark Disabled so the row goes quiet instead of paging as a missing script.
            if (props.get("Health", {}).get("select") or {}).get("name") != "Disabled":
                notion(f"/pages/{row['id']}", "PATCH", {"properties": {
                    "Health": select("Disabled"),
                    "Notes": rich("Cron job no longer present in the schedule (deleted or renamed). Remove this registry row manually if intentional."),
                    "Last Checked": date(NOW.isoformat()),
                }})
            continue
        policy = policy_for(script_file, policies, fallback_owner)
        if safe_restore_script(script_file, policy):
            script_results.append({
                "name": script_file, "path": str(SCRIPTS_DIR / script_file),
                "functions": script_functions(script_file), "criticality": criticality(script_file),
                "health": "Healthy", "coverage": ["Syntax", "Dependency"], "issues": [],
                "cron_names": text_value(props.get("Cron Jobs", {})),
                "supports": text_value(props.get("Supports", {})),
                "notes": "Safe auto-heal restored the missing script from pcg-agents after validating syntax.",
            })
            continue
        script_results.append({
            "name": script_file,
            "path": text_value(props.get("Path", {})),
            "functions": script_functions(script_file),
            "criticality": (props.get("Criticality", {}).get("select") or {}).get("name") or criticality(script_file),
            "health": "Failing", "coverage": ["Syntax"],
            "issues": [f"script missing from instance {INSTANCE}"],
            "cron_names": text_value(props.get("Cron Jobs", {})),
            "supports": text_value(props.get("Supports", {})),
            "notes": "Previously registered script is no longer present on disk.",
        })
    for result in script_results:
        upsert_script(script_ds, script_rows, result, policies)
    if IS_CONTROL_PLANE:
        sync_links(deliverable_ds, script_ds)
        deliverable_results = update_deliverables(deliverable_ds, script_ds, jobs, deps)
    else:
        deliverable_results = {}
    changed = alerts(script_results, deliverable_results)
    if changed:
        print("[AUTOMATION HEALTH UPDATE]")
        for line in changed[:25]:
            print("- " + line)
        print(f"Script board: {reg['scripts']['url']}")
        print(f"Deliverables board: {reg['deliverables']['url']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
