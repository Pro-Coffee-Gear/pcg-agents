#!/usr/bin/env python3
"""Pure helpers shared by PCG deliverable publishing and repair automation."""
from __future__ import annotations

import hashlib
import json
import re
import tomllib
from typing import Any


def parse_deliverables_toml(text: str) -> list[dict[str, Any]]:
    data = tomllib.loads(text)
    return list(data.get("deliverable", []))


def parse_health_toml(text: str) -> dict[str, dict[str, Any]]:
    data = tomllib.loads(text)
    return {row["name"]: row for row in data.get("script", []) if row.get("name")}


def classify_deliverable(proposal: dict[str, Any]) -> tuple[bool, list[str]]:
    """Return whether a build belongs on the business board and why.

    This is deliberately conservative: utilities and one-off local work do not
    become business deliverables unless another material signal is present.
    """
    kind = str(proposal.get("type", "")).lower()
    purpose = str(proposal.get("purpose", "")).lower()
    schedule = str(proposal.get("schedule", "")).strip().lower()
    audience = str(proposal.get("audience", "")).lower()
    visibility = str(proposal.get("visibility", "")).lower()
    reasons: list[str] = []

    if schedule and schedule not in {"on demand", "one-time", "one time", "none", "n/a"}:
        reasons.append("recurring")
    if any(word in kind for word in ("app", "dashboard", "integration", "scheduled automation", "agent platform")):
        reasons.append("operational system")
    if audience in {"company", "function", "lt"} or visibility in {"team", "function"}:
        reasons.append("shared use")
    if any(word in purpose for word in ("customer", "revenue", "payment", "refund", "inventory", "compliance", "financial", "vendor", "daily", "recurring")):
        reasons.append("material business impact")

    if "utility" in kind and not any(r in reasons for r in ("recurring", "shared use", "material business impact")):
        return False, []
    return bool(reasons), list(dict.fromkeys(reasons))


def normalize_failure(detail: str) -> str:
    text = re.sub(r"\bline \d+\b", "line #", detail.lower())
    text = re.sub(r"0x[0-9a-f]+", "0x#", text)
    text = re.sub(r"\b\d{4}-\d{2}-\d{2}t[^\s]+", "<timestamp>", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:1000]


def incident_key(instance: str, script: str, failure_detail: str) -> str:
    raw = json.dumps([instance, script, normalize_failure(failure_detail)], separators=(",", ":"))
    return hashlib.sha256(raw.encode()).hexdigest()[:20]


def incident_transition(old_health: str, new_health: str, instance: str, script: str,
                        failure_detail: str, existing_key: str, existing_status: str) -> dict[str, str]:
    key = existing_key
    status = existing_status or "None"
    if new_health == "Failing":
        if old_health != "Failing" or not key:
            key = incident_key(instance, script, failure_detail)
            status = "Open"
    elif old_health == "Failing" and new_health == "Healthy":
        status = "Resolved"
    elif new_health == "Healthy" and status in {"Open", "Retry", "Repairing"}:
        status = "Resolved"
    return {"incident_key": key, "incident_status": status}


def policy_for(name: str, policies: dict[str, dict[str, Any]], fallback_owner: str) -> dict[str, Any]:
    policy = dict(policies.get(name, {}))
    policy.setdefault("name", name)
    policy.setdefault("owner_email", fallback_owner)
    policy.setdefault("repair_policy", "detect-only")
    policy.setdefault("source_repo", "")
    policy.setdefault("source_path", f"scripts/{name}")
    policy.setdefault("test_command", "")
    policy.setdefault("deployment_method", "")
    policy.setdefault("rollback_method", "")
    policy.setdefault("alert_target", policy["owner_email"])
    return policy


def is_safe_repo_restore(name: str, policy: dict[str, Any]) -> bool:
    if policy.get("source_repo") != "https://github.com/Pro-Coffee-Gear/pcg-agents":
        return False
    path = str(policy.get("source_path", ""))
    if not path or path.startswith("/") or ".." in path.split("/"):
        return False
    return path == name or path == f"scripts/{name}"


def _prop_text(prop: dict[str, Any]) -> str:
    typ = prop.get("type")
    if typ in {"title", "rich_text"}:
        return "".join(x.get("plain_text", "") for x in prop.get(typ, []))
    if typ == "email":
        return prop.get("email") or ""
    if typ == "url":
        return prop.get("url") or ""
    if typ == "select":
        return (prop.get("select") or {}).get("name") or ""
    return ""


def _prop_multi(prop: dict[str, Any]) -> list[str]:
    return [x.get("name", "") for x in prop.get("multi_select", []) if x.get("name")]


def approved_deliverables_from_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    items = []
    for row in rows:
        p = row.get("properties", {})
        if _prop_text(p.get("Curation Status", {})) != "Approved":
            continue
        items.append({
            "name": _prop_text(p.get("Name", {})),
            "type": _prop_text(p.get("Type", {})),
            "status": _prop_text(p.get("Status", {})),
            "owner_email": _prop_text(p.get("Owner Email", {})),
            "purpose": _prop_text(p.get("Business Purpose", {})),
            "functions": _prop_multi(p.get("Functions", {})),
            "audience": _prop_text(p.get("Audience", {})),
            "visibility": _prop_text(p.get("Visibility", {})),
            "schedule": _prop_text(p.get("Schedule", {})),
            "url": _prop_text(p.get("Artifact URL", {})),
            "source_repo": _prop_text(p.get("Source Repository", {})),
            "scripts": _prop_text(p.get("Scripts", {})),
            "jobs": _prop_text(p.get("Jobs", {})),
            "repair_policy": _prop_text(p.get("Repair Policy", {})),
            "test_command": _prop_text(p.get("Test Command", {})),
            "deployment_method": _prop_text(p.get("Deployment Method", {})),
            "rollback_method": _prop_text(p.get("Rollback Method", {})),
            "alert_target": _prop_text(p.get("Alert Target", {})),
        })
    return sorted(items, key=lambda x: (x.get("name", "").lower(), x.get("owner_email", "").lower()))


def render_deliverables_toml(items: list[dict[str, Any]]) -> str:
    keys = ("name", "type", "status", "owner_email", "purpose", "functions", "audience",
            "visibility", "schedule", "url", "source_repo", "scripts", "jobs", "repair_policy",
            "test_command", "deployment_method", "rollback_method", "alert_target")
    lines = ["# GENERATED from Approved Notion deliverables. Review history in GitHub.", ""]
    for item in sorted(items, key=lambda x: (x.get("name", "").lower(), x.get("owner_email", "").lower())):
        lines.append("[[deliverable]]")
        for key in keys:
            value = item.get(key, [] if key == "functions" else "")
            lines.append(f"{key} = {json.dumps(value, ensure_ascii=False)}")
        lines.append("")
    return "\n".join(lines)


def script_row_to_incident(row: dict[str, Any]) -> dict[str, Any]:
    p = row.get("properties", {})
    return {
        "row_id": row.get("id", ""),
        "display_name": _prop_text(p.get("Name", {})),
        "name": _prop_text(p.get("Script File", {})) or _prop_text(p.get("Name", {})),
        "instance": _prop_text(p.get("Instance", {})),
        "health": _prop_text(p.get("Health", {})),
        "repair_policy": _prop_text(p.get("Repair Policy", {})),
        "incident_status": _prop_text(p.get("Incident Status", {})),
        "incident_key": _prop_text(p.get("Incident Key", {})),
        "failure_detail": _prop_text(p.get("Failure Detail", {})),
        "owner_email": _prop_text(p.get("Owner Email", {})),
        "source_repo": _prop_text(p.get("Source Repository", {})),
        "test_command": _prop_text(p.get("Test Command", {})),
        "deployment_method": _prop_text(p.get("Deployment Method", {})),
        "rollback_method": _prop_text(p.get("Rollback Method", {})),
        "fix_pr": _prop_text(p.get("Fix PR", {})),
    }


def select_owner_notifications(rows: list[dict[str, Any]], owner_email: str,
                               seen_incidents: set[str]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        if row.get("owner_email", "").lower() != owner_email.lower():
            continue
        if row.get("incident_status") != "Awaiting Approval" or not row.get("fix_pr"):
            continue
        marker = "pr:" + row["fix_pr"]
        if marker in seen_incidents:
            continue
        groups.setdefault(marker, []).append(row)
    selected = []
    for marker, members in sorted(groups.items()):
        members = sorted(members, key=lambda x: (x.get("instance", ""), x.get("row_id", "")))
        item = dict(members[0])
        item["notification_marker"] = marker
        item["instances"] = [m.get("instance", "") for m in members]
        item["incident_keys"] = [m.get("incident_key", "") for m in members]
        selected.append(item)
    return selected


def group_repair_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    groups: dict[str, list[dict[str, Any]]] = {}
    for row in select_repair_candidates(rows):
        normalized = normalize_failure(row.get("failure_detail", ""))
        material = "|".join((row.get("source_repo", "").lower(), row.get("name", "").lower(), normalized))
        group_key = hashlib.sha256(material.encode()).hexdigest()[:16]
        groups.setdefault(group_key, []).append(row)
    output = []
    for group_key, members in sorted(groups.items()):
        members = sorted(members, key=lambda x: (x.get("instance", ""), x.get("row_id", "")))
        item = dict(members[0])
        item["repair_group_key"] = group_key
        item["row_ids"] = [m.get("row_id", "") for m in members]
        item["instances"] = [m.get("instance", "") for m in members]
        item["incident_keys"] = [m.get("incident_key", "") for m in members]
        output.append(item)
    return output


def select_repair_candidates(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    selected = []
    for row in rows:
        if row.get("health") != "Failing":
            continue
        if row.get("repair_policy") != "repair-pr":
            continue
        if row.get("incident_status") not in {"Open", "Retry"}:
            continue
        if row.get("fix_pr"):
            continue
        selected.append(row)
    return selected
