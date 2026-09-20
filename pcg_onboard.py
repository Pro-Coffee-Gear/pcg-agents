#!/usr/bin/env python3
"""
pcg_onboard.py — onboarding from a reviewed PCG bundle.

New installations require an already-installed, already-authorized Composio CLI
and an explicit PCG_GITHUB_ACCOUNT selector. The reviewed bundle must contain
scripts/pcg_github.py and scripts/pcg_sync.py; onboarding never installs or logs
in to Composio and never accepts or stores a GitHub PAT.

Model (post-2026-08-16 redesign):
  - PRIMARY (single-select) = their home function. The default profile points at it.
  - LT (checkbox) = leadership team member. Only LT members get the 'lt' profile (aiPeer=exec).
  - ADJACENT (multi-select) = extra function agents they can switch to.
  - The legacy 'Function' column is a fallback: first non-LT entry -> Primary; 'LT' in it -> LT member.

Usage (non-GitHub keys are supplied out of band; account selector is per instance):
  PCG_GITHUB_ACCOUNT=... HONCHO_API_KEY=... NOTION_API_KEY=... python3 pcg_onboard.py --email you@procoffeegear.com
  ... --dry-run  # local prerequisite check only; no network and no mutation
"""
import argparse
import importlib.util
import json
import os
import py_compile
import subprocess
import sys
import tempfile
import urllib.request
from pathlib import Path

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


class OnboardingError(RuntimeError):
    pass


def reviewed_bundle_scripts():
    """Locate a bundle that already contains both gateway and updater."""
    here = Path(__file__).resolve().parent
    candidates = [here / "scripts", here, Path(HERMES_HOME) / "scripts"]
    for candidate in candidates:
        if (candidate / "pcg_github.py").is_file() and (candidate / "pcg_sync.py").is_file():
            return candidate
    raise OnboardingError(
        "reviewed bundle is incomplete: scripts/pcg_github.py and scripts/pcg_sync.py are required"
    )


def load_gateway_module(bundle):
    path = Path(bundle) / "pcg_github.py"
    spec = importlib.util.spec_from_file_location("pcg_onboard_github", path)
    if spec is None or spec.loader is None:
        raise OnboardingError("reviewed GitHub adapter could not be loaded")
    module = importlib.util.module_from_spec(spec)
    try:
        spec.loader.exec_module(module)
    except Exception:
        raise OnboardingError("reviewed GitHub adapter could not be loaded") from None
    return module


def github_preflight(dry_run=False):
    """Validate the local bundle/CLI, then prove private read access when live."""
    bundle = reviewed_bundle_scripts()
    account = (os.environ.get("PCG_GITHUB_ACCOUNT") or "").strip()
    executable = (os.environ.get("PCG_COMPOSIO_CLI") or str(Path.home() / ".composio" / "composio")).strip()
    if not account:
        raise OnboardingError("PCG_GITHUB_ACCOUNT must be set for this instance")
    cli_path = Path(executable)
    if not cli_path.is_absolute() or not cli_path.is_file() or not os.access(cli_path, os.X_OK):
        raise OnboardingError("PCG_COMPOSIO_CLI must be an existing absolute executable")
    module = load_gateway_module(bundle)
    if dry_run:
        return bundle, account, str(cli_path), None
    adapter = module.ComposioGitHub(account, str(cli_path))
    repo = adapter.get(module.REPO_API)
    if not isinstance(repo, dict) or repo.get("full_name") != "WWWPCG/pcg-agents":
        raise OnboardingError("Composio GitHub repository preflight returned malformed data")
    adapter.read_file("health.toml")
    return bundle, account, str(cli_path), adapter


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
def store_keys(honcho_key, notion_key, github_account, composio_cli):
    """Persist app keys plus nonsecret gateway selection; remove legacy PATs."""
    env_path = os.path.join(HERMES_HOME, ".env")
    existing = b""
    if os.path.exists(env_path):
        with open(env_path, "rb") as handle:
            existing = handle.read()
    managed = (
        "HONCHO_API_KEY", "NOTION_API_KEY", "GITHUB_SYNC_TOKEN", "GITHUB_TOKEN", "GH_TOKEN",
        "PCG_GITHUB_ACCOUNT", "PCG_COMPOSIO_CLI", "PCG_GITHUB_ALLOW_SNAPSHOT_WRITE",
    )
    lines = [
        line for line in existing.decode("utf-8", "ignore").splitlines()
        if not any(line.startswith(name + "=") for name in managed)
    ]
    lines.extend([
        f"HONCHO_API_KEY={honcho_key}",
        f"NOTION_API_KEY={notion_key}",
        f"PCG_GITHUB_ACCOUNT={github_account}",
        f"PCG_COMPOSIO_CLI={composio_cli}",
    ])
    Path(env_path).parent.mkdir(parents=True, exist_ok=True)
    Path(env_path).write_text("\n".join(lines) + "\n")
    log(f"  stored configuration in {env_path}")

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
    if "error" in r:
        raise OnboardingError("roster completion update failed")
    log("  roster updated: ok")

# ---------- profile sync ----------
# Profile sync remains embedded, but fleet GitHub access comes only from the
# separately reviewed bundle and its already-provisioned Composio connection.
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
SHARED_KEYS = ("HONCHO_API_KEY", "NOTION_API_KEY")
LEGACY_GITHUB_KEYS = ("GITHUB_SYNC_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")


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
    removable = SHARED_KEYS + LEGACY_GITHUB_KEYS
    lines = [l for l in data.splitlines() if not any(l.startswith(k + "=") for k in removable)]
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
        raise OnboardingError("could not register pcg-profile-sync")


def _install_bundle_file(source, destination):
    destination = Path(destination)
    source = Path(source)
    if source.resolve() == destination.resolve():
        return
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=destination.parent, prefix="." + destination.name + ".", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(source.read_bytes())
        py_compile.compile(str(temporary), doraise=True)
        os.replace(temporary, destination)
    except Exception:
        if temporary is not None and temporary.exists():
            temporary.unlink()
        raise OnboardingError(f"could not install reviewed {destination.name}") from None


def install_fleet_sync(dry, bundle_scripts=None):
    """Install gateway and updater from one reviewed local bundle before cron."""
    bundle = Path(bundle_scripts) if bundle_scripts is not None else reviewed_bundle_scripts()
    adapter_source = bundle / "pcg_github.py"
    sync_source = bundle / "pcg_sync.py"
    if not adapter_source.is_file() or not sync_source.is_file():
        raise OnboardingError("reviewed fleet bundle is incomplete")
    scripts_dir = Path(HERMES_HOME) / "scripts"
    adapter_dest = scripts_dir / "pcg_github.py"
    sync_dest = scripts_dir / "pcg_sync.py"
    if dry:
        log("  [dry] would install reviewed gateway + updater and register pcg-fleet-sync")
        return

    _install_bundle_file(adapter_source, adapter_dest)
    _install_bundle_file(sync_source, sync_dest)
    # Both files must still be importable Python before any schedule is created.
    try:
        py_compile.compile(str(adapter_dest), doraise=True)
        py_compile.compile(str(sync_dest), doraise=True)
    except Exception:
        raise OnboardingError("installed fleet bundle failed syntax validation") from None

    # Initial pull proves the installed updater works before cron registration.
    result = subprocess.run(
        [sys.executable, str(sync_dest)],
        capture_output=True,
        text=True,
        timeout=180,
        env={**os.environ, "HERMES_HOME": HERMES_HOME},
    )
    if result.returncode != 0:
        raise OnboardingError("initial fleet sync failed; cron was not registered")
    output = (result.stdout or "").strip()
    log("  initial pull: " + (output.splitlines()[0] if output else "no changes"))

    result = subprocess.run(
        [HERMES_BIN, "cron", "list"], capture_output=True, text=True,
        env={**os.environ, "HERMES_HOME": HERMES_HOME},
    )
    if result.returncode != 0:
        raise OnboardingError("could not inspect fleet cron jobs")
    if "pcg-fleet-sync" in (result.stdout + result.stderr):
        log("  cron 'pcg-fleet-sync' already registered — skipping")
        return
    result = subprocess.run(
        [HERMES_BIN, "cron", "create", "every 30m",
         "--name", "pcg-fleet-sync", "--script", "pcg_sync.py",
         "--no-agent", "--deliver", "local"],
        capture_output=True,
        text=True,
        env={**os.environ, "HERMES_HOME": HERMES_HOME},
    )
    if result.returncode != 0:
        raise OnboardingError("could not register pcg-fleet-sync")
    log("  cron 'pcg-fleet-sync': registered (every 30m)")

# ---------- main ----------
def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument("--email", required=True)
    parser.add_argument("--name", default=None, help="peerName override; default derived from email")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    honcho_key = (os.environ.get("HONCHO_API_KEY") or "").strip()
    notion_key = (os.environ.get("NOTION_API_KEY") or "").strip()
    if not honcho_key or not notion_key:
        log("ERROR: HONCHO_API_KEY and NOTION_API_KEY must be set")
        return 2
    peer_name = args.name or args.email.split("@")[0].lower().replace(".", "-")
    log(f"== PCG Onboarding for {args.email} (peer: {peer_name}) ==")

    try:
        # This must remain first: a failed gateway/bundle preflight may not store
        # keys, create profiles, touch roster state, or register jobs.
        bundle, github_account, composio_cli, _adapter = github_preflight(args.dry_run)
        if args.dry_run:
            log("  [dry] local bundle, account selector, and Composio executable are present")
            log("  [dry] no network calls or mutations were performed")
            return 0

        log("[1/7] reading roster after GitHub preflight")
        pid, primary, adjacent, is_lt = roster_row(args.email, notion_key)
        adjacent = adjacent or []
        if pid is None:
            raise OnboardingError("no roster row found; add the member before onboarding")
        if not primary:
            raise OnboardingError("roster Primary function is missing")
        log(f"  Primary: {primary}  Adjacent: {adjacent}  LT member: {is_lt}")

        log("[2/7] storing instance configuration")
        store_keys(honcho_key, notion_key, github_account, composio_cli)

        log("[3/7] creating profiles")
        have = existing_profiles()
        to_create = ["lt"] if is_lt else []
        for function in adjacent:
            if function in FUNC_MAP and function != "LT":
                to_create.append(FUNC_MAP[function][0])
        for profile in to_create:
            if profile in have:
                log(f"  '{profile}' already exists — skipping create")
            elif not create_profile(profile, False):
                raise OnboardingError(f"profile creation failed for {profile}")

        log("[4/7] wiring shared memory (honcho.json)")
        active = write_honcho(primary, adjacent, is_lt, peer_name, honcho_key, notion_key, False)
        actual = actual_active()
        expected = [primary] + (["LT"] if is_lt else []) + [f for f in adjacent if f != "LT"]
        status, detail = sync_check(expected, actual)
        if status != "✅ Match":
            raise OnboardingError("profile verification mismatch: " + "; ".join(detail))

        log("[5/7] installing profile self-sync")
        install_profile_sync(args.email, False)
        log("[6/7] installing reviewed fleet gateway and updater")
        install_fleet_sync(False, bundle)

        # Completion is written only after every required setup step succeeded.
        log("[7/7] recording verified onboarding completion")
        write_roster(pid, actual, status, notion_key, False)
    except OnboardingError as exc:
        log(f"ERROR: {exc}")
        return 2
    except Exception:
        log("ERROR: onboarding preflight or setup failed")
        return 2

    log("DONE — onboarding verified")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
