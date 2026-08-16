#!/usr/bin/env python3
"""
pcg_onboard.py — One-command onboarding for a Pro Coffee Gear team member's box.

Run this ON THE NEW MEMBER'S BOX. It:
  1. Stores the Honcho + Notion keys into this box's .env (so the box can reach the shared brain).
  2. Looks the person up in the Notion roster by --email.
  3. Reads their Function multi-select -> the profiles they should have.
  4. Creates those Hermes profiles (idempotent - skips any that already exist).
  5. Writes honcho.json so each profile points at the shared 'procoffeegear' workspace
     with the correct aiPeer and the person's own peerName.
  6. Runs the sync-check -> writes Active Profiles + Sync Status + Onboarded back to the roster.

Usage (keys come from Wes, in the personalized command):
  HONCHO_API_KEY=... NOTION_API_KEY=... python3 pcg_onboard.py --email you@procoffeegear.com [--name "Your Name"]
  ...  --dry-run     # show what WOULD happen, create nothing
"""
import json, os, sys, subprocess, argparse, urllib.request

WORKSPACE   = "procoffeegear"
HONCHO_ENV  = "production"
DS_ID       = "69ad2d97-bbad-47bf-822f-e2c289f5164b"   # Agent Team Roster data_source
HERMES_BIN  = os.environ.get("HERMES_BIN", "/opt/hermes/bin/hermes")
HERMES_HOME = os.environ.get("HERMES_HOME", "/opt/data")

# Notion Function label  ->  (hermes profile name, honcho aiPeer)
# NOTE: LT is the person's leadership VIEW; it lives on the DEFAULT profile of their box.
FUNC_MAP = {
    "LT":              ("__default__", "exec"),     # default profile == their LT view
    "Finance":         ("finance",     "finance"),
    "Operations":      ("operations",  "operations"),
    "Sales":           ("sales",       "sales"),
    "CS":              ("cs",          "cs"),
    "Product/Merch":   ("product",     "product"),
    "Marketing/Growth":("marketing",   "marketing"),
    "Company":         ("company",     "company"),
}
# For the sync-check readback: honcho.json host-block key -> Notion Function
BLOCK_TO_FUNC = {
    "hermes":"LT",
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
    if "error" in q or not q.get("results"): return None, None
    row=q["results"][0]
    fns=[o["name"] for o in row["properties"]["Function"]["multi_select"]]
    return row["id"], fns

# ---------- .env ----------
def store_keys(honcho_key, notion_key):
    """Persist the two keys into this box's .env so future runs/agents can reach the brain."""
    env_path=os.path.join(HERMES_HOME, ".env")
    existing=b""
    if os.path.exists(env_path):
        with open(env_path,"rb") as f: existing=f.read()
    text=existing.decode("utf-8","ignore")
    lines=[l for l in text.splitlines() if not l.startswith(("HONCHO_API_KEY","NOTION_API_KEY"))]
    if honcho_key: lines.append(f"HONCHO_API_KEY={honcho_key}")
    if notion_key: lines.append(f"NOTION_API_KEY={notion_key}")
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
def write_honcho(functions, peer_name, honcho_key, notion_key, dry):
    """Point each requested profile at the shared workspace with correct aiPeer + this person's peerName."""
    hpath=os.path.join(HERMES_HOME,"honcho.json")
    conf={}
    if os.path.exists(hpath):
        try: conf=json.load(open(hpath))
        except: conf={}
    conf.setdefault("apiKey", honcho_key)
    conf["workspace"]=WORKSPACE
    conf["environment"]=HONCHO_ENV
    conf["peerName"]=peer_name
    conf.setdefault("recallMode","hybrid")
    conf.setdefault("observationMode","directional")
    hosts=conf.setdefault("hosts",{})
    active_labels=[]
    for f in functions:
        if f not in FUNC_MAP:
            log(f"  ! unknown function '{f}' — skipping"); continue
        profile, aipeer = FUNC_MAP[f]
        block = "hermes" if profile=="__default__" else f"hermes_{profile}"
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
            out.add(BLOCK_TO_FUNC[k])
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

# ---------- main ----------
def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--name", default=None, help="peerName override; default derived from email")
    ap.add_argument("--dry-run", action="store_true")
    args=ap.parse_args()

    honcho_key=(os.environ.get("HONCHO_API_KEY") or "").strip()
    notion_key=(os.environ.get("NOTION_API_KEY") or "").strip()
    if not honcho_key or not notion_key:
        log("ERROR: HONCHO_API_KEY and NOTION_API_KEY must be set (they're in the command Wes gave you).")
        sys.exit(2)

    peer_name = args.name or args.email.split("@")[0].lower().replace(".","-")

    log(f"== PCG Onboarding for {args.email} (peer: {peer_name}) ==")

    # 1. keys
    log("[1/5] storing keys")
    if not args.dry_run: store_keys(honcho_key, notion_key)
    else: log("  [dry] would store keys in .env")

    # 2. roster lookup
    log("[2/5] reading roster")
    pid, functions = roster_row(args.email, notion_key)
    if pid is None:
        log(f"ERROR: no roster row for {args.email}. Ask Wes to add you first."); sys.exit(2)
    log(f"  requested functions: {functions}")

    # 3. create profiles (skip default/LT — it already exists as the box default)
    log("[3/5] creating profiles")
    have=existing_profiles()
    for f in functions:
        if f not in FUNC_MAP: 
            log(f"  ! unknown function '{f}'"); continue
        profile,_=FUNC_MAP[f]
        if profile=="__default__":
            log("  LT -> uses the box default profile (no create needed)")
        elif profile in have:
            log(f"  '{profile}' already exists — skipping create")
        else:
            create_profile(profile, args.dry_run)

    # 4. wire honcho
    log("[4/5] wiring shared memory (honcho.json)")
    write_honcho(functions, peer_name, honcho_key, notion_key, args.dry_run)

    # 5. verify + write back
    log("[5/5] verifying")
    active=actual_active() if not args.dry_run else functions  # dry: assume success
    status,detail=sync_check(functions, active)
    log(f"  REQUESTED: {sorted(set(functions))}")
    log(f"  ACTIVE   : {active}")
    log(f"  STATUS   : {status}")
    if detail: log("  DETAIL   : "+"; ".join(detail))
    write_roster(pid, active, status, notion_key, args.dry_run)

    log("")
    if status=="✅ Match":
        log("DONE ✅  Open your agent, use the profile switcher (bottom-right) to pick a space.")
    else:
        log("DONE ⚠️  Mismatch — send Wes a screenshot of your roster row. Don't retry blindly.")

if __name__=="__main__":
    main()
