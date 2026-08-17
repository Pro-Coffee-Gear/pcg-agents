#!/usr/bin/env python3
"""
pcg_sync.py — fleet asset sync. Pulls shared skills + scripts from the
WWWPCG/pcg-agents private repo, scoped to THIS box's function set.

Runs as a no_agent cron job (every 30m). Reads the member's roster row to learn
which functions they hold (Primary + Adjacent + LT), then syncs:
  - skills/_common/*      -> /opt/data/skills/            (every box)
  - skills/<function>/*   -> /opt/data/profiles/<fn>/skills/  (only held functions)
  - scripts/*             -> /opt/data/scripts/           (every box)

Add-and-update only: never deletes local files (protects locally-authored skills
and conversation history). A file in the repo overwrites the same-named local
file; local-only files are left untouched.

Auth: GITHUB_SYNC_TOKEN in .env (fine-grained, read-only, scoped to pcg-agents).
Falls back to GITHUB_TOKEN if the sync token isn't set.

Exit 0 always. Empty stdout = nothing changed (silent). Non-empty = what synced.
"""
import base64
import json
import os
import urllib.request

OWNER, REPO = "WWWPCG", "pcg-agents"
HERMES_HOME = os.environ.get("HERMES_HOME", "/opt/data")
EMAIL_FILE = os.path.join(HERMES_HOME, ".pcg_member_email")
DS_ID = "69ad2d97-bbad-47bf-822f-e2c289f5164b"

# Notion function label -> local profile dir name
FUNC_TO_PROFILE = {
    "LT": "lt", "Finance": "finance", "Operations": "operations", "Sales": "sales",
    "CS": "cs", "Product/Merch": "product", "Marketing/Growth": "marketing",
    "Company": "company",
}
# repo skills subfolder name per profile (matches scaffold)
PROFILE_TO_REPO = {
    "lt": "lt", "finance": "finance", "operations": "operations", "sales": "sales",
    "cs": "cs", "product": "product", "marketing": "marketing", "company": "company",
}


def env_key(*names):
    p = os.path.join(HERMES_HOME, ".env")
    vals = {}
    if os.path.exists(p):
        for line in open(p, "rb").read().decode("utf-8", "ignore").splitlines():
            for n in names:
                if line.startswith(n + "="):
                    vals[n] = line.split("=", 1)[1].strip()
    for n in names:
        if vals.get(n):
            return vals[n]
    for n in names:
        if os.environ.get(n):
            return os.environ[n]
    return None


def gh(token, path):
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={"Authorization": f"token {token}",
                 "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def gh_raw(token, path):
    """Fetch a file's decoded bytes via the Contents API (works on private repos)."""
    req = urllib.request.Request(
        f"https://api.github.com/repos/{OWNER}/{REPO}/contents/{path}",
        headers={"Authorization": f"token {token}",
                 "Accept": "application/vnd.github.raw"})
    with urllib.request.urlopen(req) as r:
        return r.read()


def notion(key, path, method="GET", body=None):
    req = urllib.request.Request(
        f"https://api.notion.com/v1{path}", method=method,
        headers={"Authorization": f"Bearer {key}", "Notion-Version": "2025-09-03",
                 "Content-Type": "application/json"},
        data=json.dumps(body).encode() if body else None)
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def held_functions(email, notion_key):
    """Return the set of profile dir names this box holds (Primary + Adjacent + LT)."""
    q = notion(notion_key, f"/data_sources/{DS_ID}/query", "POST",
               {"filter": {"property": "Email", "email": {"equals": email}}})
    if not q.get("results"):
        return set()
    p = q["results"][0]["properties"]
    primary = (p.get("Primary", {}).get("select") or {}).get("name")
    adjacent = [o["name"] for o in p.get("Adjacent", {}).get("multi_select", [])]
    is_lt = bool(p.get("LT", {}).get("checkbox"))
    fns = set()
    if primary and primary in FUNC_TO_PROFILE:
        fns.add(FUNC_TO_PROFILE[primary])
    for a in adjacent:
        if a in FUNC_TO_PROFILE and a != "LT":
            fns.add(FUNC_TO_PROFILE[a])
    if is_lt:
        fns.add("lt")
    return fns


def list_tree(token, repo_dir):
    """List files (recursively) under a repo directory. Returns [(repo_path, sha)]."""
    out = []
    try:
        items = gh(token, f"/repos/{OWNER}/{REPO}/contents/{repo_dir}")
    except urllib.error.HTTPError:
        return out
    for it in items:
        if it["type"] == "dir":
            out.extend(list_tree(token, it["path"]))
        elif it["type"] == "file" and it["name"] != "README.md":
            out.append((it["path"], it["sha"]))
    return out


def sync_dir(token, repo_dir, local_dir, synced):
    """Mirror repo_dir -> local_dir (add/update only). repo_dir is stripped as prefix."""
    files = list_tree(token, repo_dir)
    if not files:
        return
    os.makedirs(local_dir, exist_ok=True)
    for repo_path, sha in files:
        rel = repo_path[len(repo_dir):].lstrip("/")
        dest = os.path.join(local_dir, rel)
        # Skip if local file already matches the repo blob sha (git blob hash)
        if os.path.exists(dest):
            import hashlib
            data = open(dest, "rb").read()
            blob = hashlib.sha1(b"blob %d\0%s" % (len(data), data)).hexdigest()
            if blob == sha:
                continue
        content = gh_raw(token, repo_path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(content)
        synced.append(dest.replace(HERMES_HOME + "/", ""))


def main():
    if not os.path.exists(EMAIL_FILE):
        return 0
    email = open(EMAIL_FILE).read().strip()
    if not email:
        return 0

    token = env_key("GITHUB_SYNC_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")
    notion_key = env_key("NOTION_API_KEY")
    if not token or not notion_key:
        return 0

    try:
        fns = held_functions(email, notion_key)
    except Exception:
        return 0

    synced = []

    # 1. _common -> default skills
    try:
        sync_dir(token, "skills/_common", os.path.join(HERMES_HOME, "skills"), synced)
    except Exception:
        pass

    # 2. each held function -> that profile's skills
    for fn in sorted(fns):
        repo_fn = PROFILE_TO_REPO.get(fn)
        if not repo_fn:
            continue
        profile_skills = os.path.join(HERMES_HOME, "profiles", fn, "skills")
        # only sync if the profile actually exists locally
        if not os.path.isdir(os.path.join(HERMES_HOME, "profiles", fn)):
            continue
        try:
            sync_dir(token, f"skills/{repo_fn}", profile_skills, synced)
        except Exception:
            pass

    # 3. shared scripts -> scripts/
    try:
        sync_dir(token, "scripts", os.path.join(HERMES_HOME, "scripts"), synced)
    except Exception:
        pass

    if synced:
        print(f"Fleet sync: updated {len(synced)} file(s):")
        for s in synced[:30]:
            print(f"  {s}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
