#!/usr/bin/env python3
"""
pcg_onboard.py — One-command onboarding for a Pro Coffee Gear team member's box.

Run this ON THE NEW MEMBER'S BOX. It:
  1. Stores the Honcho, Notion, and read-only GitHub sync keys into this box's .env.
  2. Looks the person up in the Notion roster by --email.
  3. Reads their Primary (single-select) + Adjacent (multi-select) -> the profiles they should have.
  4. Creates those Hermes profiles (idempotent - skips any that already exist).
  5. Writes honcho.json so each profile points at the shared 'procoffeegear' workspace
     with the correct aiPeer and the person's own peerName.
  6. Runs the sync-check -> writes Active Profiles + Sync Status + Onboarded back to the roster.

Model (post-2026-08-16 redesign):
  - PRIMARY (single-select) = their home function. The default profile points at it.
  - LT (checkbox) = leadership team member. Only LT members get the 'lt' profile (aiPeer=exec).
  - ADJACENT (multi-select) = extra function agents they can switch to.
  - The legacy 'Function' column is a fallback: first non-LT entry -> Primary; 'LT' in it -> LT member.

Usage (keys come from Wes, in the personalized command):
  GITHUB_SYNC_TOKEN=... HONCHO_API_KEY=... NOTION_API_KEY=... python3 pcg_onboard.py --email you@procoffeegear.com [--name "Your Name"]
  ...  --dry-run     # show what WOULD happen, create nothing
"""
import json, os, sys, subprocess, argparse, urllib.request

WORKSPACE   = "procoffeegear"
HONCHO_ENV  = "production"
DS_ID       = "69ad2d97-bbad-47bf-822f-e2c289f5164b"   # Agent Team Roster data_source
HERMES_BIN  = os.environ.get("HERMES_BIN", "/opt/hermes/bin/hermes")
HERMES_HOME = os.environ.get("HERMES_HOME", "/opt/data")

# Notion function label  ->  (hermes profile name, honcho aiPeer)
# LT maps to the exec aiPeer; every other function maps to itself.
FUNC_MAP = {
    "LT":              ("lt",          "exec"),
    "Finance":         ("finance",     "finance"),
    "Operations":      ("operations",  "operations"),
    "Sales":           ("sales",       "sales"),
    "CS":              ("cs",          "cs"),
    "Product/Merch":   ("product",     "product"),
    "Marketing/Growth":("marketing",   "marketing"),
    "Company":         ("company",     "company"),
}
# For the sync-check readback: honcho.json host-block key -> Notion function
BLOCK_TO_FUNC = {
    "hermes":"__default__",   # default profile; function comes from Primary
    "hermes_lt":"LT",
    "hermes_finance":"Finance","hermes_operations":"Operations","hermes_sales":"Sales",
    "hermes_cs":"CS","hermes_product":"Product/Merch","hermes_marketing":"Marketing/Growth",
    "hermes_company":"Company",
}

def log(msg): print(msg, flush=True)

# ---------- Notion ----------
def notion(path, method="GET", body=None, key=None):
    url=f"https://api.notion.com/v1{path}"
    h={"Authorization":f"Bearer {key}","Notion-Version":"2025-09-03","Content-Type":"application/json"}
    r=urllib.request.Request(url,method=method,headers=h,data=json.dumps(body).encode() if body else None)
    try:
        with urllib.request.urlopen(r) as resp: return json.loads(resp.read())
    except urllib.error.HTTPError as e: return {"error":e.code,"body":e.read().decode()}

def roster_row(email, key):
    q=notion(f"/data_sources/{DS_ID}/query","POST",
             {"filter":{"property":"Email","email":{"equals":email}}}, key=key)
    if "error" in q or not q.get("results"): return None, None, None, None
    row=q["results"][0]
    props = row["properties"]
    primary = (props.get("Primary",{}).get("select") or {}).get("name")
    adjacent = [o["name"] for o in props.get("Adjacent",{}).get("multi_select",[])]
    is_lt = bool(props.get("LT",{}).get("checkbox"))
    # Fallback: if Primary is empty but legacy Function has entries, use the first non-LT
    # entry as Primary so old rows still work. Legacy "LT" in Function also implies LT member.
    if not primary:
        legacy = [o["name"] for o in props.get("Function",{}).get("multi_select",[])]
        non_lt = [f for f in legacy if f != "LT"]
        if non_lt:
            primary = non_lt[0]
        if "LT" in legacy:
            is_lt = True
    return row["id"], primary, adjacent, is_lt

# ---------- .env ----------
def store_keys(honcho_key, notion_key, github_sync_token):
    env_path=os.path.join(HERMES_HOME, ".env")
    existing=b""
    if os.path.exists(env_path):
        with open(env_path,"rb") as f: existing=f.read()
    text=existing.decode("utf-8","ignore")
    lines=[l for l in text.splitlines() if not l.startswith(("HONCHO_API_KEY","NOTION_API_KEY","GITHUB_SYNC_TOKEN"))]
    if honcho_key: lines.append(f"HONCHO_API_KEY={honcho_key}")
    if notion_key: lines.append(f"NOTION_API_KEY={notion_key}")
    if github_sync_token: lines.append(f"GITHUB_SYNC_TOKEN={github_sync_token}")
    with open(env_path,"wb") as f: f.write(("\n".join(lines)+"\n").encode())
    log(f"  stored keys in {env_path}")

# ---------- Hermes profiles ----------
def existing_profiles():
    pdir=os.path.join(HERMES_HOME,"profiles")
    return set(os.listdir(pdir)) if os.path.isdir(pdir) else set()

def create_profile(name, dry):
    if dry:
        log(f"  [dry] would create profile '{name}'"); return True
    r=subprocess.run([HERMES_BIN,"profile","create",name,"--no-skills"],
                     capture_output=True, text=True, env={**os.environ,"HERMES_HOME":HERMES_HOME})
    ok = r.returncode==0 or "already exists" in (r.stdout+r.stderr).lower()
    log(f"  create '{name}': {'ok' if ok else 'FAILED'}")
    if not ok: log("    "+(r.stderr or r.stdout)[:200])
    return ok

# ---------- honcho.json ----------
def write_honcho(primary, adjacent, is_lt, peer_name, honcho_key, notion_key, dry):
    """Default profile = Primary function's aiPeer. 'lt' profile (exec aiPeer) only if LT member. Adjacent = their own."""
    hpath=os.path.join(HERMES_HOME,"honcho.json")
    conf={}
    if os.path.exists(hpath):
        try: conf=json.load(open(hpath))
        except: conf={}
    # This is a dedicated PCG member box. The supplied shared-workspace key must
    # replace any Portal/default key already present, otherwise the host blocks
    # point at procoffeegear using credentials for a different workspace.
    conf["apiKey"]=honcho_key
    conf["workspace"]=WORKSPACE
    conf["environment"]=HONCHO_ENV
    conf["peerName"]=peer_name
    conf.setdefault("recallMode","hybrid")
    conf.setdefault("observationMode","directional")
    hosts=conf.setdefault("hosts",{})
    active_labels=[]

    # 1. Default profile -> Primary function
    if primary and primary in FUNC_MAP:
        _, aipeer = FUNC_MAP[primary]
        hosts["hermes"]={"workspace":WORKSPACE,"aiPeer":aipeer,"peerName":peer_name,"environment":HONCHO_ENV}
        active_labels.append(primary)

    # 2. LT profile (only for LT members — gated on the roster checkbox)
    if primary and is_lt:
        hosts["hermes_lt"]={"workspace":WORKSPACE,"aiPeer":"exec","peerName":peer_name,"environment":HONCHO_ENV}
        active_labels.append("LT")

    # 3. Adjacent profiles
    for f in adjacent:
        if f not in FUNC_MAP or f == "LT":
            continue  # LT is already covered above
        profile, aipeer = FUNC_MAP[f]
        block = f"hermes_{profile}"
        hosts[block]={"workspace":WORKSPACE,"aiPeer":aipeer,"peerName":peer_name,"environment":HONCHO_ENV}
        active_labels.append(f)

    if dry:
        log(f"  [dry] would write honcho.json hosts: {list(hosts.keys())}")
        return active_labels
    with open(hpath,"w") as f: json.dump(conf,f,indent=2)
    log(f"  wrote {hpath} ({len(active_labels)} host blocks)")
    return active_labels

# ---------- sync check / readback ----------
def actual_active():
    hpath=os.path.join(HERMES_HOME,"honcho.json")
    if not os.path.exists(hpath): return []
    conf=json.load(open(hpath)); root=conf.get("workspace")
    out=set()
    for k,b in conf.get("hosts",{}).items():
        if (b.get("workspace") or root)==WORKSPACE and b.get("aiPeer") and k in BLOCK_TO_FUNC:
            label = BLOCK_TO_FUNC[k]
            if label == "__default__":
                # resolve default's function from its aiPeer
                aip = b["aiPeer"]
                label = next((f for f,(p,a) in FUNC_MAP.items() if a == aip), aip)
            out.add(label)
    return sorted(out)

def sync_check(req, act):
    req,act=set(req),set(act)
    if req==act: return "✅ Match",[]
    d=[f"MISSING: {m}" for m in sorted(req-act)]+[f"extra: {e}" for e in sorted(act-req)]
    return "⚠️ Mismatch", d

def write_roster(pid, active, status, key, dry):
    props={
        "Active Profiles":{"multi_select":[{"name":a} for a in active]},
        "Sync Status":{"select":{"name":status}},
        "Onboarded":{"checkbox": status=="✅ Match"},
    }
    if dry:
        log(f"  [dry] would set roster: Active={active} Status={status}"); return
    r=notion(f"/pages/{pid}","PATCH",{"properties":props},key=key)
    log(f"  roster updated: {'ok' if 'error' not in r else 'FAILED'}")

# ---------- profile sync (self-updating) ----------
# The sync script's full source is embedded so pcg_onboard.py is self-contained
# even when piped via `curl | python3` (no repo checkout on the member's box).
PROFILE_SYNC_SRC = r'''#!/usr/bin/env python3
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
'''


def install_profile_sync(email, dry):
    """Install profile_sync.py and register a 15-min no_agent cron job so later
    roster changes (new Adjacent function, LT granted) auto-create profiles on
    this box without re-running onboarding."""
    marker = os.path.join(HERMES_HOME, ".pcg_member_email")
    scripts_dir = os.path.join(HERMES_HOME, "scripts")
    sync_src = os.path.join(scripts_dir, "profile_sync.py")

    if dry:
        log("  [dry] would install profile_sync.py + cron job 'pcg-profile-sync'")
        return

    # 1. Record this member's email so profile_sync knows which roster row to read
    with open(marker, "w") as f:
        f.write(email + "\n")

    # 2. Write the embedded sync script
    os.makedirs(scripts_dir, exist_ok=True)
    with open(sync_src, "w") as f:
        f.write(PROFILE_SYNC_SRC)
    log("  installed profile_sync.py")

    # 3. Register the cron job (idempotent — skip if it already exists)
    r = subprocess.run([HERMES_BIN, "cron", "list"], capture_output=True, text=True,
                       env={**os.environ, "HERMES_HOME": HERMES_HOME})
    if "pcg-profile-sync" in (r.stdout + r.stderr):
        log("  cron 'pcg-profile-sync' already registered — skipping")
        return

    r = subprocess.run(
        [HERMES_BIN, "cron", "create", "every 15m",
         "--name", "pcg-profile-sync",
         "--script", "profile_sync.py",
         "--no-agent",
         "--deliver", "local"],
        capture_output=True, text=True,
        env={**os.environ, "HERMES_HOME": HERMES_HOME},
    )
    ok = r.returncode == 0
    log(f"  cron 'pcg-profile-sync': {'registered (every 15m)' if ok else 'FAILED'}")
    if not ok:
        log("    " + (r.stderr or r.stdout)[:200])


def install_fleet_sync(dry):
    """Fetch pcg_sync.py from the repo and register a 30-min no_agent cron that
    pulls shared skills (+scripts) scoped to this box's function set. The sync
    script lives in the repo so it self-updates; we just fetch it once + schedule."""
    scripts_dir = os.path.join(HERMES_HOME, "scripts")
    sync_dest = os.path.join(scripts_dir, "pcg_sync.py")

    if dry:
        log("  [dry] would fetch pcg_sync.py + register cron 'pcg-fleet-sync' (every 30m)")
        return

    # Prefer the read-only sync token; fall back to the onboarding token.
    gh_token = None
    envp = os.path.join(HERMES_HOME, ".env")
    if os.path.exists(envp):
        for line in open(envp, "rb").read().decode("utf-8", "ignore").splitlines():
            for n in ("GITHUB_SYNC_TOKEN", "GITHUB_TOKEN", "GH_TOKEN"):
                if line.startswith(n + "="):
                    gh_token = gh_token or line.split("=", 1)[1].strip()
    gh_token = gh_token or os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN")
    if not gh_token:
        log("  ! no GitHub token in .env — skipping fleet-sync install")
        return

    # Fetch pcg_sync.py from the private repo (Contents API, raw accept).
    # Canonical home is scripts/ in the repo — after install, the 30-min fleet
    # sync itself keeps this file (and profile_sync.py) current forever.
    os.makedirs(scripts_dir, exist_ok=True)
    req = urllib.request.Request(
        "https://api.github.com/repos/WWWPCG/pcg-agents/contents/scripts/pcg_sync.py",
        headers={"Authorization": f"token {gh_token}",
                 "Accept": "application/vnd.github.raw"})
    try:
        with urllib.request.urlopen(req) as r:
            with open(sync_dest, "wb") as f:
                f.write(r.read())
        log("  fetched pcg_sync.py from repo")
    except Exception as e:
        log(f"  ! could not fetch pcg_sync.py ({e}) — skipping fleet-sync install")
        return

    # Register the cron (idempotent)
    r = subprocess.run([HERMES_BIN, "cron", "list"], capture_output=True, text=True,
                       env={**os.environ, "HERMES_HOME": HERMES_HOME})
    if "pcg-fleet-sync" in (r.stdout + r.stderr):
        log("  cron 'pcg-fleet-sync' already registered — skipping")
    else:
        r = subprocess.run(
            [HERMES_BIN, "cron", "create", "every 30m",
             "--name", "pcg-fleet-sync",
             "--script", "pcg_sync.py",
             "--no-agent",
             "--deliver", "local"],
            capture_output=True, text=True,
            env={**os.environ, "HERMES_HOME": HERMES_HOME},
        )
        ok = r.returncode == 0
        log(f"  cron 'pcg-fleet-sync': {'registered (every 30m)' if ok else 'FAILED'}")
        if not ok:
            log("    " + (r.stderr or r.stdout)[:200])

    # Initial pull so the member gets shared skills immediately
    r = subprocess.run([sys.executable, sync_dest], capture_output=True, text=True,
                       env={**os.environ, "HERMES_HOME": HERMES_HOME})
    out = (r.stdout or "").strip()
    log("  initial pull: " + (out.splitlines()[0] if out else "nothing to sync yet"))

# ---------- main ----------
def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--name", default=None, help="peerName override; default derived from email")
    ap.add_argument("--dry-run", action="store_true")
    args=ap.parse_args()

    honcho_key=(os.environ.get("HONCHO_API_KEY") or "").strip()
    notion_key=(os.environ.get("NOTION_API_KEY") or "").strip()
    github_sync_token=(os.environ.get("GITHUB_SYNC_TOKEN") or "").strip()
    if not honcho_key or not notion_key or not github_sync_token:
        log("ERROR: GITHUB_SYNC_TOKEN, HONCHO_API_KEY, and NOTION_API_KEY must be set (they're in the command Wes gave you).")
        sys.exit(2)

    peer_name = args.name or args.email.split("@")[0].lower().replace(".","-")

    log(f"== PCG Onboarding for {args.email} (peer: {peer_name}) ==")

    # 1. keys
    log("[1/7] storing keys")
    if not args.dry_run: store_keys(honcho_key, notion_key, github_sync_token)
    else: log("  [dry] would store keys in .env")

    # 2. roster lookup
    log("[2/7] reading roster")
    pid, primary, adjacent, is_lt = roster_row(args.email, notion_key)
    if pid is None:
        log(f"ERROR: no roster row for {args.email}. Ask Wes to add you first."); sys.exit(2)
    log(f"  Primary: {primary}  Adjacent: {adjacent}  LT member: {is_lt}")
    if not primary:
        log("ERROR: no Primary function set in roster. Set the Primary column first."); sys.exit(2)

    # 3. create profiles (lt if LT member + adjacent; default is the box default, no create needed)
    log("[3/7] creating profiles")
    have=existing_profiles()
    to_create = ["lt"] if is_lt else []  # LT profile only for LT members
    for f in adjacent:
        if f in FUNC_MAP and f != "LT":
            to_create.append(FUNC_MAP[f][0])
    for pname in to_create:
        if pname in have:
            log(f"  '{pname}' already exists — skipping create")
        else:
            create_profile(pname, args.dry_run)

    # 4. wire honcho
    log("[4/7] wiring shared memory (honcho.json)")
    active = write_honcho(primary, adjacent, is_lt, peer_name, honcho_key, notion_key, args.dry_run)

    # 5. verify + write back
    log("[5/7] verifying")
    actual = actual_active() if not args.dry_run else active
    # expected = primary + LT (if member) + adjacent
    expected = [primary] + (["LT"] if is_lt else []) + [f for f in adjacent if f != "LT"]
    status,detail=sync_check(expected, actual)
    log(f"  EXPECTED: {sorted(set(expected))}")
    log(f"  ACTIVE   : {actual}")
    log(f"  STATUS   : {status}")
    if detail: log("  DETAIL   : "+"; ".join(detail))
    write_roster(pid, actual, status, notion_key, args.dry_run)

    # 6. install profile self-sync so later roster changes propagate automatically
    log("[6/7] installing profile self-sync (every 15m)")
    install_profile_sync(args.email, args.dry_run)

    # 7. install fleet asset sync (shared skills + scripts, scoped to function set)
    log("[7/7] installing fleet asset sync (every 30m)")
    install_fleet_sync(args.dry_run)

    log("")
    if status=="✅ Match":
        log("DONE ✅  Open your agent, use the profile switcher (bottom-right) to pick a space.")
    else:
        log("DONE ⚠️  Mismatch — send Wes a screenshot of your roster row. Don't retry blindly.")

if __name__=="__main__":
    main()
