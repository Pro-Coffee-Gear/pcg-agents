#!/usr/bin/env python3
"""
roster_sync_check.py — Onboarding verification for the PCG Agent Team Roster.

Run this ON A MEMBER'S BOX after onboarding. It:
  1. Inspects what profiles are REALLY live + wired to the shared honcho workspace.
  2. Reads the requested Function list from the Notion roster (by --email).
  3. Compares requested vs actual and writes back:
       - Active Profiles (multi_select) = what's actually live
       - Sync Status                    = ✅ Match / ⚠️ Mismatch / ❌ Error
       - Onboarded (checkbox)           = True only on ✅ Match (unless --no-onboard)

Usage:
  python3 roster_sync_check.py --email sina@procoffeegear.com
  python3 roster_sync_check.py --email sina@procoffeegear.com --dry-run
"""
import json, os, sys, argparse, urllib.request

DS_ID = "69ad2d97-bbad-47bf-822f-e2c289f5164b"   # Agent Team Roster data_source
WORKSPACE = "procoffeegear"

# Notion Function label  <->  Hermes profile name
FUNC_TO_PROFILE = {
    "LT":"lt", "Finance":"finance", "Operations":"operations", "Sales":"sales",
    "CS":"cs", "Product/Merch":"product", "Marketing/Growth":"marketing", "Company":"company",
}
# host-block key in honcho.json -> Notion Function.
# "hermes" is the default profile; on a member box the default IS their LT view.
BLOCK_TO_FUNC = {
    "hermes":"LT", "hermes_lt":"LT",
    "hermes_finance":"Finance", "hermes_operations":"Operations", "hermes_sales":"Sales",
    "hermes_cs":"CS", "hermes_product":"Product/Merch", "hermes_marketing":"Marketing/Growth",
    "hermes_company":"Company",
}

def notion_key():
    for line in open("/opt/data/.env","rb").read().decode("utf-8","ignore").splitlines():
        if line.startswith("NOTION_API_KEY"):
            return line.split("=",1)[1].strip()
    return os.environ.get("NOTION_API_KEY")

def notion(path, method="GET", body=None, key=None):
    url=f"https://api.notion.com/v1{path}"
    h={"Authorization":f"Bearer {key}","Notion-Version":"2025-09-03","Content-Type":"application/json"}
    r=urllib.request.Request(url,method=method,headers=h,data=json.dumps(body).encode() if body else None)
    try:
        with urllib.request.urlopen(r) as resp: return json.loads(resp.read())
    except urllib.error.HTTPError as e:
        return {"error":e.code,"body":e.read().decode()}

def actual_active(honcho_path="/opt/data/honcho.json"):
    """What functions are genuinely live + wired to the shared workspace on THIS box."""
    if not os.path.exists(honcho_path): return []
    h=json.load(open(honcho_path))
    root_ws=h.get("workspace")
    out=set()
    for k,b in h.get("hosts",{}).items():
        ws=b.get("workspace") or root_ws
        peer=b.get("aiPeer")
        func=BLOCK_TO_FUNC.get(k)
        if func and ws==WORKSPACE and peer:
            out.add(func)
    return sorted(out)

def requested_functions(email, key):
    q=notion(f"/data_sources/{DS_ID}/query","POST",
             {"filter":{"property":"Email","email":{"equals":email}}}, key=key)
    if "error" in q or not q.get("results"):
        return None, None
    row=q["results"][0]
    fns=[o["name"] for o in row["properties"]["Function"]["multi_select"]]
    return row["id"], fns

def sync_check(requested, active):
    req,act=set(requested),set(active)
    if req==act: return "✅ Match",[]
    missing=req-act; extra=act-req
    detail=[f"MISSING: {m}" for m in sorted(missing)]+[f"extra: {e}" for e in sorted(extra)]
    return "⚠️ Mismatch", detail

def main():
    ap=argparse.ArgumentParser()
    ap.add_argument("--email", required=True)
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--no-onboard", action="store_true",
                    help="never flip Onboarded even on a match")
    args=ap.parse_args()

    key=notion_key()
    if not key:
        print("ERROR: no NOTION_API_KEY"); sys.exit(2)

    pid, requested = requested_functions(args.email, key)
    if pid is None:
        print(f"ERROR: no roster row for {args.email}"); sys.exit(2)

    active = actual_active()
    status, detail = sync_check(requested, active)

    print(f"Email     : {args.email}")
    print(f"REQUESTED : {requested}")
    print(f"ACTIVE    : {active}")
    print(f"STATUS    : {status}")
    if detail: print("DETAIL    : " + "; ".join(detail))

    if args.dry_run:
        print("(dry-run — roster not written)")
        return

    props={
        "Active Profiles":{"multi_select":[{"name":a} for a in active]},
        "Sync Status":{"select":{"name":status}},
    }
    if not args.no_onboard:
        props["Onboarded"]={"checkbox": status=="✅ Match"}
    r=notion(f"/pages/{pid}","PATCH",{"properties":props},key=key)
    print("roster updated:", "error" not in r)
    if "error" in r: print(r["body"][:300])

if __name__=="__main__":
    main()
