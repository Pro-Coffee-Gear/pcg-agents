#!/usr/bin/env python3
"""
pcg_sync.py — fleet asset sync. Pulls shared skills, scripts, and plugins from the
WWWPCG/pcg-agents private repo, scoped to THIS box's function set, and
reconciles team cron jobs from jobs.yaml.

Runs as a no_agent cron job (every 30m).

File rules:
  - skills/_common/*  -> skills/                (every box)
  - skills/<fn>/*     -> profiles/<fn>/skills/  (only held functions)
  - scripts/*         -> scripts/               (every box)
  - plugins/_common/* -> plugins/ in the default and held profile homes
                         (enabled automatically)
  - Files under a folder whose name starts with "pcg-" (or files themselves
    starting with "pcg-") are REPO-MANAGED: the repo wins on conflict. All other
    files: created if missing, never overwritten — a local edit to a
    non-prefixed file is preserved and reported as a conflict.
  - Files this script previously installed that DISAPPEAR from the repo are
    quarantined (renamed to .revoked) — never hard-deleted.

Job rules (jobs.yaml in repo root):
  - Each entry: {name, schedule, script, deliver, scope}. scope: "all" or a
    list of function labels (e.g. ["CS"]). Only jobs whose name starts with
    "pcg-" are managed. Missing managed jobs are created (add-only; existing
    jobs are never edited or removed).

Heartbeat: writes Last Fleet Sync back to the roster each run.

Exit 0 always. Silent unless something changed.
"""
import base64
import hashlib
import json
import os
import re
import subprocess
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

OWNER, REPO = "WWWPCG", "pcg-agents"
HERMES_HOME = os.environ.get("HERMES_HOME", "/opt/data")
HERMES_BIN = os.environ.get("HERMES_BIN", "/opt/hermes/bin/hermes")
EMAIL_FILE = os.path.join(HERMES_HOME, ".pcg_member_email")
MANIFEST_FILE = os.path.join(HERMES_HOME, ".pcg_fleet_manifest.json")
DS_ID = "69ad2d97-bbad-47bf-822f-e2c289f5164b"

FUNC_TO_PROFILE = {
    "LT": "lt", "Finance": "finance", "Operations": "operations", "Sales": "sales",
    "CS": "cs", "Product/Merch": "product", "Marketing/Growth": "marketing",
    "Company": "company",
}
REPO_MANAGED_PREFIX = "pcg-"
PLUGIN_NAME = "pcg-deliverable-autoregistration"
POLICY_START = "<!-- PCG DELIVERABLE CATALOG POLICY START -->"
POLICY_END = "<!-- PCG DELIVERABLE CATALOG POLICY END -->"
CODING_POLICY = """Whenever you create or materially update a durable PCG work product,
register or update its Proposed row in Business Automations & Deliverables before
reporting completion. Run /opt/data/scripts/pcg-register-deliverable.py with the known
owner, functions, purpose, source, test, deployment, and rollback metadata; leave unknown
fields blank rather than inventing them. Only temporary scratch and throwaway test
fixtures are exempt."""


def is_repo_managed(rel):
    """Repo-managed = top-level folder or file carries the pcg prefix.
    Both separators count (pcg- for skill dirs, pcg_ for script filenames)."""
    top = rel.split(os.sep)[0]
    return top.startswith("pcg-") or top.startswith("pcg_")


def plugin_destinations(root, functions, existing_only=True):
    """Return the default home plus each held function profile home."""
    root = Path(root)
    homes = [root]
    for function in sorted(functions):
        profile = root / "profiles" / function
        if not existing_only or profile.is_dir():
            homes.append(profile)
    return homes


def merge_coding_instructions(existing):
    """Add or replace PCG's managed catalog policy without touching local guidance."""
    block = f"{POLICY_START}\n{CODING_POLICY}\n{POLICY_END}"
    pattern = re.compile(re.escape(POLICY_START) + r".*?" + re.escape(POLICY_END), re.S)
    existing = (existing or "").strip()
    if pattern.search(existing):
        return pattern.sub(block, existing)
    return (existing + "\n\n" + block).strip()


def reconcile_deliverable_policy(home, changes, runner=subprocess.run):
    """Install the immediate coding rule and enable the durable plugin guard."""
    home = str(home)
    env = {**os.environ, "HERMES_HOME": home}
    get_result = runner(
        [HERMES_BIN, "config", "get", "agent.coding_instructions"],
        capture_output=True, text=True, env=env,
    )
    existing = get_result.stdout.strip() if get_result.returncode == 0 else ""
    desired = merge_coding_instructions(existing)
    if desired != existing:
        set_result = runner(
            [HERMES_BIN, "config", "set", "agent.coding_instructions", desired],
            capture_output=True, text=True, env=env,
        )
        if set_result.returncode == 0:
            changes.append(f"catalog policy installed: {home}")

    list_result = runner(
        [HERMES_BIN, "plugins", "list", "--json", "--user"],
        capture_output=True, text=True, env=env,
    )
    rows = []
    if list_result.returncode == 0:
        try:
            rows = json.loads(list_result.stdout or "[]")
        except Exception:
            rows = []
    enabled = any(
        row.get("name") == PLUGIN_NAME and row.get("status") == "enabled"
        for row in rows if isinstance(row, dict)
    )
    if not enabled:
        enable_result = runner(
            [HERMES_BIN, "plugins", "enable", PLUGIN_NAME],
            capture_output=True, text=True, env=env,
        )
        if enable_result.returncode == 0:
            changes.append(f"plugin enabled: {PLUGIN_NAME} ({home})")


def env_key(*names):
    p = os.path.join(HERMES_HOME, ".env")
    if os.path.exists(p):
        for line in open(p, "rb").read().decode("utf-8", "ignore").splitlines():
            for n in names:
                if line.startswith(n + "="):
                    return line.split("=", 1)[1].strip()
    for n in names:
        if os.environ.get(n):
            return os.environ[n]
    return None


def gh(token, path):
    req = urllib.request.Request(
        f"https://api.github.com{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github+json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def gh_raw(token, path):
    req = urllib.request.Request(
        f"https://api.github.com/repos/{OWNER}/{REPO}/contents/{path}",
        headers={"Authorization": f"Bearer {token}", "Accept": "application/vnd.github.raw"})
    with urllib.request.urlopen(req) as r:
        return r.read()


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
        return {"error": e.code}


def roster_row(email, key):
    q = notion(key, f"/data_sources/{DS_ID}/query", "POST",
               {"filter": {"property": "Email", "email": {"equals": email}}})
    if not q.get("results"):
        return None
    row = q["results"][0]
    p = row["properties"]
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
    return row["id"], fns


def git_blob_sha(data):
    return hashlib.sha1(b"blob %d\0%s" % (len(data), data)).hexdigest()


def list_tree(token, repo_dir):
    out = []
    try:
        items = gh(token, f"/repos/{OWNER}/{REPO}/contents/{repo_dir}")
    except Exception:
        return out
    for it in items:
        if it["type"] == "dir":
            out.extend(list_tree(token, it["path"]))
        elif it["type"] == "file" and it["name"] != "README.md":
            out.append((it["path"], it["sha"]))
    return out


def sync_dir(token, repo_dir, local_dir, changes, conflicts, manifest):
    files = list_tree(token, repo_dir)
    if not files:
        return
    os.makedirs(local_dir, exist_ok=True)
    for repo_path, sha in files:
        rel = repo_path[len(repo_dir):].lstrip("/")
        dest = os.path.join(local_dir, rel)
        # Managed = the top-level folder (skill dir) or the file itself carries
        # the pcg prefix. SKILL.md files inherit it from their skill directory.
        managed = is_repo_managed(rel)
        if os.path.exists(dest):
            if git_blob_sha(open(dest, "rb").read()) == sha:
                manifest[dest] = repo_path
                continue
            if not managed:
                conflicts.append(dest.replace(HERMES_HOME + "/", ""))
                continue  # local non-managed file wins; never overwrite
        content = gh_raw(token, repo_path)
        os.makedirs(os.path.dirname(dest), exist_ok=True)
        with open(dest, "wb") as f:
            f.write(content)
        manifest[dest] = repo_path
        changes.append("installed " + dest.replace(HERMES_HOME + "/", ""))


def sync_plugins_and_policy(token, functions, changes, conflicts, manifest):
    """Distribute and enable the catalog guard in every active profile home."""
    for home in plugin_destinations(HERMES_HOME, functions):
        sync_dir(
            token,
            "plugins/_common",
            str(home / "plugins"),
            changes,
            conflicts,
            manifest,
        )
        reconcile_deliverable_policy(home, changes)


def quarantine_deleted(manifest, changes):
    """Anything we installed that's no longer in the repo -> rename to .revoked."""
    for dest, repo_path in list(manifest.items()):
        if not os.path.exists(dest):
            del manifest[dest]
            continue
        try:
            gh_exists = True
            req = urllib.request.Request(
                f"https://api.github.com/repos/{OWNER}/{REPO}/contents/{repo_path}",
                headers={"Authorization": f"Bearer {env_key('GITHUB_SYNC_TOKEN','GITHUB_TOKEN','GH_TOKEN')}",
                         "Accept": "application/vnd.github+json"})
            urllib.request.urlopen(req)
        except urllib.error.HTTPError as e:
            gh_exists = e.code != 404
        except Exception:
            gh_exists = True  # network error: keep, don't quarantine on a flake
        if not gh_exists:
            revoked = dest + ".revoked"
            os.replace(dest, revoked)
            del manifest[dest]
            changes.append("quarantined " + dest.replace(HERMES_HOME + "/", "") + " (removed from repo)")


def load_manifest():
    if os.path.exists(MANIFEST_FILE):
        try:
            return json.load(open(MANIFEST_FILE))
        except Exception:
            pass
    return {}


def save_manifest(m):
    json.dump(m, open(MANIFEST_FILE, "w"), indent=2)


# ---------- jobs.yaml ----------

def parse_jobs_yaml(text):
    """Minimal YAML parse for the flat jobs manifest (avoids a pyyaml dependency)."""
    jobs, cur = [], None
    for raw in text.splitlines():
        line = raw.rstrip()
        stripped = line.strip()
        if stripped.startswith("- name:"):
            if cur:
                jobs.append(cur)
            cur = {"name": stripped.split(":", 1)[1].strip().strip('"')}
        elif cur is not None and ":" in stripped and not stripped.startswith("#"):
            k, v = stripped.split(":", 1)
            k, v = k.strip(), v.strip().strip('"')
            if k == "scope":
                if v.startswith("["):
                    cur["scope"] = [s.strip().strip('"') for s in v.strip("[]").split(",") if s.strip()]
                else:
                    cur["scope"] = v
            elif k in ("schedule", "script", "deliver", "workdir"):
                cur[k] = v
            elif k == "no_agent":
                cur[k] = v.lower() in ("true", "yes", "1")
    if cur:
        jobs.append(cur)
    return jobs


def cron_job_names():
    r = subprocess.run([HERMES_BIN, "cron", "list"], capture_output=True, text=True,
                       env={**os.environ, "HERMES_HOME": HERMES_HOME})
    return set(re.findall(r"Name:\s*(.+)", r.stdout or "")) if r.returncode == 0 else set()


def reconcile_jobs(token, held_labels, changes):
    try:
        text = gh_raw(token, "jobs.yaml").decode()
    except Exception:
        return  # no manifest yet — fine
    existing = cron_job_names()
    for job in parse_jobs_yaml(text):
        name = job.get("name", "")
        if not name.startswith(REPO_MANAGED_PREFIX) or not job.get("schedule") or not job.get("script"):
            continue
        scope = job.get("scope", "all")
        if scope != "all":
            if isinstance(scope, str):
                scope = [scope]
            if not any(s in held_labels for s in scope):
                continue
        if name in existing:
            continue
        cmd = [HERMES_BIN, "cron", "create", job["schedule"],
               "--name", name, "--script", os.path.basename(job["script"]),
               "--deliver", job.get("deliver", "local")]
        if job.get("no_agent"):
            cmd.append("--no-agent")
        if job.get("workdir"):
            cmd.extend(["--workdir", job["workdir"]])
        r = subprocess.run(
            cmd,
            capture_output=True, text=True,
            env={**os.environ, "HERMES_HOME": HERMES_HOME})
        if r.returncode == 0:
            changes.append(f"cron job created: {name}")


def heartbeat(pid, key):
    notion(key, f"/pages/{pid}", "PATCH", {"properties": {
        "Last Fleet Sync": {"date": {"start": datetime.now(timezone.utc).isoformat()}}}})


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

    row = None
    try:
        row = roster_row(email, notion_key)
    except Exception:
        pass
    pid, fns = row if row else (None, set())

    changes, conflicts = [], []
    manifest = load_manifest()
    repo_paths_now = set()

    try:
        sync_dir(token, "skills/_common", os.path.join(HERMES_HOME, "skills"), changes, conflicts, manifest)
    except Exception:
        pass
    for fn in sorted(fns):
        pdir = os.path.join(HERMES_HOME, "profiles", fn)
        if not os.path.isdir(pdir):
            continue
        try:
            sync_dir(token, f"skills/{fn}", os.path.join(pdir, "skills"), changes, conflicts, manifest)
        except Exception:
            pass
    try:
        sync_dir(token, "scripts", os.path.join(HERMES_HOME, "scripts"), changes, conflicts, manifest)
    except Exception:
        pass
    try:
        sync_plugins_and_policy(token, fns, changes, conflicts, manifest)
    except Exception:
        pass

    quarantine_deleted(manifest, changes)
    save_manifest(manifest)

    # held function LABELS for job scoping
    prof_to_label = {v: k for k, v in FUNC_TO_PROFILE.items()}
    held_labels = {prof_to_label.get(f, f) for f in fns}
    try:
        reconcile_jobs(token, held_labels, changes)
    except Exception:
        pass

    if pid:
        try:
            heartbeat(pid, notion_key)
        except Exception:
            pass

    if changes or conflicts:
        for c in changes[:30]:
            print(c)
        for c in conflicts[:10]:
            print(f"CONFLICT (kept local, repo not applied): {c}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
