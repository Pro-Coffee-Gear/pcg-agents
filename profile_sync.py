#!/usr/bin/env python3
"""
profile_sync.py — keep this box's profiles in sync with the Notion roster.

Installed by pcg_onboard.py and run by a no_agent cron job every 15 minutes.

Each run:
  1. Reads the roster row for this member (by email, from .pcg_member_email).
  2. OFFBOARD: if the roster's Offboard checkbox is set, strips shared access
     (Honcho host blocks, shared keys in .env, both sync cron jobs, the email
     marker), reports back to the roster, and exits. The box goes inert.
  3. Creates any missing profiles for the member's current Primary+Adjacent+LT.
  4. REVOCATION: if a previously-held function is no longer in the roster set,
     removes its Honcho host block (cuts shared-memory access) and drops a
     REVOKED marker file in the profile dir. The profile + its local history
     are never deleted — full removal stays a manual decision.
  5. HEARTBEAT: writes Last Profile Sync + Active Profiles back to the roster
     so the roster shows live fleet state, not just onboarding-time state.

Exit 0 always. Silent unless something changed.
"""
import json
import os
import subprocess
import sys
import urllib.request
from datetime import datetime, timezone

DS_ID = "69ad2d97-bbad-47bf-822f-e2c289f5164b"
WORKSPACE = "procoffeegear"
HERMES_HOME = os.environ.get("HERMES_HOME", "/opt/data")
HERMES_BIN = os.environ.get("HERMES_BIN", "/opt/hermes/bin/hermes")
EMAIL_FILE = os.path.join(HERMES_HOME, ".pcg_member_email")

FUNC_MAP = {
    "LT": ("lt", "exec"), "Finance": ("finance", "finance"),
    "Operations": ("operations", "operations"), "Sales": ("sales", "sales"),
    "CS": ("cs", "cs"), "Product/Merch": ("product", "product"),
    "Marketing/Growth": ("marketing", "marketing"), "Company": ("company", "company"),
}
SHARED_KEYS = ("HONCHO_API_KEY", "NOTION_API_KEY", "GITHUB_SYNC_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")


def env_key(name):
    p = os.path.join(HERMES_HOME, ".env")
    if os.path.exists(p):
        for line in open(p, "rb").read().decode("utf-8", "ignore").splitlines():
            if line.startswith(name + "="):
                return line.split("=", 1)[1].strip()
    return os.environ.get(name)


def notion(key, path, method="GET", body=None):
    req = urllib.request.Request(
        f"https://api.notion.com/v1{path}", method=method,
        headers={"Authorization": f"Bearer {key}", "Notion-Version": "2025-09-03",
                 "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body else None)
    try:
        with urllib.request.urlopen(req) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        return {"error": e.code, "body": e.read().decode()[:200]}


def roster(email, key):
    q = notion(key, f"/data_sources/{DS_ID}/query", "POST",
               {"filter": {"property": "Email", "email": {"equals": email}}})
    if not q.get("results"):
        return None
    row = q["results"][0]
    p = row["properties"]
    primary = (p.get("Primary", {}).get("select") or {}).get("name")
    adjacent = [o["name"] for o in p.get("Adjacent", {}).get("multi_select", [])]
    is_lt = bool(p.get("LT", {}).get("checkbox"))
    offboard = bool(p.get("Offboard", {}).get("checkbox"))
    return row["id"], primary, adjacent, is_lt, offboard


def load_honcho():
    hpath = os.path.join(HERMES_HOME, "honcho.json")
    if not os.path.exists(hpath):
        return hpath, {}
    try:
        return hpath, json.load(open(hpath))
    except Exception:
        return hpath, {}


def expected_profiles(primary, adjacent, is_lt):
    want = set()
    if is_lt:
        want.add("lt")
    for f in adjacent:
        if f in FUNC_MAP and f != "LT":
            want.add(FUNC_MAP[f][0])
    return want


def create_profile(pname):
    r = subprocess.run([HERMES_BIN, "profile", "create", pname, "--no-skills"],
                       capture_output=True, text=True,
                       env={**os.environ, "HERMES_HOME": HERMES_HOME})
    return r.returncode == 0 or "already exists" in (r.stdout + r.stderr).lower()


def wire_host(conf, pname, aipeer, peer_name):
    conf.setdefault("workspace", WORKSPACE)
    conf.setdefault("environment", "production")
    h = conf.setdefault("hosts", {})
    h[f"hermes_{pname}"] = {
        "workspace": WORKSPACE, "aiPeer": aipeer,
        "peerName": peer_name, "environment": "production"}


def cron_ids_by_name(name_sub):
    r = subprocess.run([HERMES_BIN, "cron", "list", "--json"], capture_output=True, text=True,
                       env={**os.environ, "HERMES_HOME": HERMES_HOME})
    out = r.stdout or ""
    ids = []
    try:
        jobs = json.loads(out)
        for j in jobs:
            if name_sub in (j.get("name") or ""):
                ids.append(j.get("id") or j.get("job_id"))
    except Exception:
        pass
    if not ids:
        # fallback: parse plain-text list
        r2 = subprocess.run([HERMES_BIN, "cron", "list"], capture_output=True, text=True,
                            env={**os.environ, "HERMES_HOME": HERMES_HOME})
        cur = None
        for line in (r2.stdout or "").splitlines():
            ls = line.strip()
            if ls.startswith("ID:"):
                cur = ls.split("ID:", 1)[1].strip()
            elif "Name:" in ls and name_sub in ls and cur:
                ids.append(cur)
    return [i for i in ids if i]


def remove_cron(name_sub):
    removed = []
    for jid in cron_ids_by_name(name_sub):
        r = subprocess.run([HERMES_BIN, "cron", "remove", jid], capture_output=True, text=True,
                           env={**os.environ, "HERMES_HOME": HERMES_HOME})
        if r.returncode == 0:
            removed.append(jid)
    return removed


def strip_shared_keys():
    envp = os.path.join(HERMES_HOME, ".env")
    if not os.path.exists(envp):
        return
    data = open(envp, "rb").read().decode("utf-8", "ignore")
    lines = [l for l in data.splitlines() if not any(l.startswith(k + "=") for k in SHARED_KEYS)]
    open(envp, "wb").write(("\n".join(lines) + "\n").encode())


def self_offboard(pid, key, peer_name):
    """Cut this box off from the shared workspace and go inert. Never deletes profiles."""
    hpath, conf = load_honcho()
    hosts = conf.get("hosts", {})
    for blk in list(hosts.keys()):
        del hosts[blk]
    json.dump(conf, open(hpath, "w"), indent=2)

    strip_shared_keys()
    remove_cron("pcg-profile-sync")
    remove_cron("pcg-fleet-sync")
    if os.path.exists(EMAIL_FILE):
        os.remove(EMAIL_FILE)

    notion(key, f"/pages/{pid}", "PATCH", {"properties": {
        "Onboarded": {"checkbox": False},
        "Active Profiles": {"multi_select": []},
        "Sync Status": {"select": {"name": "Offboarded"}},
        "Last Profile Sync": {"date": {"start": datetime.now(timezone.utc).isoformat()}},
    }})
    print(f"Offboarded: shared access removed for {peer_name}. "
          "Profiles and local history kept; box is now standalone.")


def heartbeat(pid, key, active_labels):
    props = {"Last Profile Sync": {"date": {"start": datetime.now(timezone.utc).isoformat()}}}
    if active_labels is not None:
        props["Active Profiles"] = {"multi_select": [{"name": a} for a in sorted(active_labels)]}
    notion(key, f"/pages/{pid}", "PATCH", {"properties": props})


def main():
    if not os.path.exists(EMAIL_FILE):
        return 0
    email = open(EMAIL_FILE).read().strip()
    if not email:
        return 0
    key = env_key("NOTION_API_KEY")
    if not key:
        return 0

    row = roster(email, key)
    if not row:
        return 0
    pid, primary, adjacent, is_lt, offboard = row
    peer_name = email.split("@")[0].lower().replace(".", "-")

    if offboard:
        self_offboard(pid, key, peer_name)
        return 0

    want = expected_profiles(primary, adjacent, is_lt)

    pdir = os.path.join(HERMES_HOME, "profiles")
    have = set(os.listdir(pdir)) if os.path.isdir(pdir) else set()
    hpath, conf = load_honcho()
    hosts = conf.get("hosts", {})
    created, revoked = [], []

    # Create missing
    honcho_dirty = False
    for pname in sorted(want):
        if pname not in have:
            if not create_profile(pname):
                continue
            created.append(pname)
        label = next(f for f, (p, _) in FUNC_MAP.items() if p == pname)
        if f"hermes_{pname}" not in hosts:
            wire_host(conf, pname, FUNC_MAP[label][1], peer_name)
            honcho_dirty = True
        # clear any stale REVOKED marker on re-grant
        marker = os.path.join(pdir, pname, "REVOKED")
        if os.path.exists(marker):
            os.remove(marker)

    # Revoke: function profiles present but no longer in the expected set
    managed = {p for _, (p, _) in FUNC_MAP.items()}
    for pname in sorted(have & managed - want):
        blk = f"hermes_{pname}"
        if blk in hosts:
            del hosts[blk]
            honcho_dirty = True
        marker = os.path.join(pdir, pname, "REVOKED")
        if not os.path.exists(marker):
            open(marker, "w").write(
                f"Access revoked via roster {datetime.now(timezone.utc).isoformat()}. "
                "Local history kept; shared memory access cut. "
                "Full deletion is a manual decision.\n")
        revoked.append(pname)

    if honcho_dirty:
        json.dump(conf, open(hpath, "w"), indent=2)

    # Heartbeat: active = default primary + want
    active = sorted(want | ({primary} if primary else set()))
    labels = sorted({next((f for f, (p, _) in FUNC_MAP.items() if p == x), x) for x in active})
    heartbeat(pid, key, labels)

    if created or revoked:
        parts = []
        if created:
            parts.append(f"created {', '.join(created)}")
        if revoked:
            parts.append(f"revoked {', '.join(revoked)} (access cut, history kept)")
        print(f"Profile sync ({email}): " + "; ".join(parts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
