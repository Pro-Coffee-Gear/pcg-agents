#!/usr/bin/env python3
"""
profile_sync.py — keep this box's profiles in sync with the Notion roster.

Installed by pcg_onboard.py and run by a no_agent cron job every 15 minutes.
Reads the roster row for this member (by email), compares Primary/Adjacent/LT
against the profiles that exist locally, and creates any missing ones.

Add-only: never deletes a profile. Removing a function in Notion does NOT delete
the local profile (that would destroy conversation history) — removals stay manual.

Exit code always 0. Empty stdout = in sync (silent). Non-empty stdout = something
was created; the cron framework delivers it as a notification.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import urllib.request

DS_ID = "69ad2d97-bbad-47bf-822f-e2c289f5164b"
HERMES_HOME = os.environ.get("HERMES_HOME", "/opt/data")
HERMES_BIN = os.environ.get("HERMES_BIN", "/opt/hermes/bin/hermes")
EMAIL_FILE = os.path.join(HERMES_HOME, ".pcg_member_email")

FUNC_MAP = {
    "LT":               ("lt",        "exec"),
    "Finance":          ("finance",   "finance"),
    "Operations":       ("operations","operations"),
    "Sales":            ("sales",     "sales"),
    "CS":               ("cs",        "cs"),
    "Product/Merch":    ("product",   "product"),
    "Marketing/Growth": ("marketing", "marketing"),
    "Company":          ("company",   "company"),
}


def env_key(name):
    p = os.path.join(HERMES_HOME, ".env")
    if os.path.exists(p):
        for line in open(p, "rb").read().decode("utf-8", "ignore").splitlines():
            if line.startswith(name):
                return line.split("=", 1)[1].strip()
    return os.environ.get(name)


def notion(key, path, method="GET", body=None):
    req = urllib.request.Request(
        f"https://api.notion.com/v1{path}", method=method,
        headers={"Authorization": f"Bearer {key}", "Notion-Version": "2025-09-03",
                 "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body else None,
    )
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def roster(email, key):
    q = notion(key, f"/data_sources/{DS_ID}/query", "POST",
               {"filter": {"property": "Email", "email": {"equals": email}}})
    if not q.get("results"):
        return None
    p = q["results"][0]["properties"]
    primary = (p.get("Primary", {}).get("select") or {}).get("name")
    adjacent = [o["name"] for o in p.get("Adjacent", {}).get("multi_select", [])]
    is_lt = bool(p.get("LT", {}).get("checkbox"))
    return primary, adjacent, is_lt


def existing_profiles():
    pdir = os.path.join(HERMES_HOME, "profiles")
    return set(os.listdir(pdir)) if os.path.isdir(pdir) else set()


def honcho_hosts():
    hpath = os.path.join(HERMES_HOME, "honcho.json")
    if not os.path.exists(hpath):
        return {}
    try:
        return json.load(open(hpath)).get("hosts", {})
    except Exception:
        return {}


def main() -> int:
    if not os.path.exists(EMAIL_FILE):
        return 0  # not onboarded via pcg_onboard — nothing to sync
    email = open(EMAIL_FILE).read().strip()
    if not email:
        return 0

    key = env_key("NOTION_API_KEY")
    if not key:
        return 0  # can't check without the key; stay silent rather than alert hourly

    row = roster(email, key)
    if not row:
        return 0
    primary, adjacent, is_lt = row

    # What profiles SHOULD exist
    want_profiles = set()
    if is_lt:
        want_profiles.add("lt")
    for f in adjacent:
        if f in FUNC_MAP and f != "LT":
            want_profiles.add(FUNC_MAP[f][0])

    have = existing_profiles()
    hosts = honcho_hosts()
    created = []

    for pname in sorted(want_profiles):
        if pname in have and f"hermes_{pname}" in hosts:
            continue  # fully wired already

        label = next(f for f, (p, _) in FUNC_MAP.items() if p == pname)
        aipeer = FUNC_MAP[label][1]

        if pname not in have:
            r = subprocess.run(
                [HERMES_BIN, "profile", "create", pname, "--no-skills"],
                capture_output=True, text=True,
                env={**os.environ, "HERMES_HOME": HERMES_HOME},
            )
            if r.returncode != 0 and "already exists" not in (r.stdout + r.stderr).lower():
                continue  # failed — try next tick, don't alert on partial

        # Wire the host block if missing
        if f"hermes_{pname}" not in hosts:
            try:
                hpath = os.path.join(HERMES_HOME, "honcho.json")
                conf = json.load(open(hpath)) if os.path.exists(hpath) else {}
                conf.setdefault("workspace", "procoffeegear")
                conf.setdefault("environment", "production")
                h = conf.setdefault("hosts", {})
                h[f"hermes_{pname}"] = {
                    "workspace": "procoffeegear", "aiPeer": aipeer,
                    "peerName": email.split("@")[0].lower().replace(".", "-"),
                    "environment": "production",
                }
                json.dump(conf, open(hpath, "w"), indent=2)
            except Exception:
                continue

        created.append(pname)

    if created:
        print(f"Profile sync: created {', '.join(created)} "
              f"(roster updated for {email})")
    return 0


if __name__ == "__main__":
    sys.exit(main())
