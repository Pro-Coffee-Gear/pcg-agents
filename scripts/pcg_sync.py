#!/usr/bin/env python3
"""Synchronize reviewed PCG fleet assets from WWWPCG/pcg-agents.

GitHub reads use the constrained Composio adapter. A repository probe and a
single pinned recursive tree are completed before local files, policy, cron, or
heartbeat state is mutated. Failures return nonzero and never write a success
heartbeat.
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any

_ADAPTER_DIR = Path(__file__).resolve().parent / "scripts"
if not _ADAPTER_DIR.is_dir():
    _ADAPTER_DIR = Path(__file__).resolve().parent
if str(_ADAPTER_DIR) not in sys.path:
    sys.path.insert(0, str(_ADAPTER_DIR))

from pcg_github import ComposioGitHub, GitHubError  # noqa: E402

OWNER, REPO = "WWWPCG", "pcg-agents"
REPO_API = f"/repos/{OWNER}/{REPO}"
HERMES_HOME = os.environ.get("HERMES_HOME", "/opt/data")
HERMES_BIN = os.environ.get("HERMES_BIN", "/opt/hermes/bin/hermes")
EMAIL_FILE = os.path.join(HERMES_HOME, ".pcg_member_email")
MANIFEST_FILE = os.path.join(HERMES_HOME, ".pcg_fleet_manifest.json")
DS_ID = "69ad2d97-bbad-47bf-822f-e2c289f5164b"
SHA_RE = re.compile(r"^[0-9a-f]{40}$")

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
fields blank rather than inventing them. Alert Target defaults to the owner email. Health
must remain Unknown until a real URL, script, job, or named dependency check runs. Only
temporary scratch and throwaway test fixtures are exempt."""


class SyncError(RuntimeError):
    """Sanitized fatal fleet-sync failure."""


def is_repo_managed(rel):
    top = rel.split(os.sep)[0]
    return top.startswith("pcg-") or top.startswith("pcg_")


def plugin_destinations(root, functions, existing_only=True):
    root = Path(root)
    homes = [root]
    for function in sorted(functions):
        profile = root / "profiles" / function
        if not existing_only or profile.is_dir():
            homes.append(profile)
    return homes


def merge_coding_instructions(existing):
    block = f"{POLICY_START}\n{CODING_POLICY}\n{POLICY_END}"
    pattern = re.compile(re.escape(POLICY_START) + r".*?" + re.escape(POLICY_END), re.S)
    existing = (existing or "").strip()
    if pattern.search(existing):
        return pattern.sub(block, existing)
    return (existing + "\n\n" + block).strip()


def _run_hermes(command, home):
    try:
        return subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=60,
            env={**os.environ, "HERMES_HOME": str(home)},
        )
    except (OSError, subprocess.SubprocessError):
        raise SyncError("Hermes command failed") from None


def reconcile_deliverable_policy(home, changes, runner=None):
    """Install the managed policy and enable its plugin; failures are fatal."""
    home = str(home)
    env = {**os.environ, "HERMES_HOME": home}

    def run(command):
        if runner is not None:
            return runner(command, capture_output=True, text=True, env=env)
        return _run_hermes(command, home)

    get_result = run([HERMES_BIN, "config", "get", "agent.coding_instructions"])
    if get_result.returncode != 0:
        raise SyncError("unable to read Hermes coding instructions")
    existing = get_result.stdout.strip()
    desired = merge_coding_instructions(existing)
    if desired != existing:
        result = run([HERMES_BIN, "config", "set", "agent.coding_instructions", desired])
        if result.returncode != 0:
            raise SyncError("unable to install catalog policy")
        changes.append(f"catalog policy installed: {home}")

    result = run([HERMES_BIN, "plugins", "list", "--json", "--user"])
    if result.returncode != 0:
        raise SyncError("unable to inspect Hermes plugins")
    try:
        rows = json.loads(result.stdout or "[]")
    except json.JSONDecodeError:
        raise SyncError("Hermes plugin listing was malformed") from None
    if not isinstance(rows, list):
        raise SyncError("Hermes plugin listing was malformed")
    enabled = any(
        isinstance(row, dict)
        and row.get("name") == PLUGIN_NAME
        and row.get("status") == "enabled"
        for row in rows
    )
    if not enabled:
        result = run([HERMES_BIN, "plugins", "enable", PLUGIN_NAME])
        if result.returncode != 0:
            raise SyncError("unable to enable catalog plugin")
        changes.append(f"plugin enabled: {PLUGIN_NAME} ({home})")


def env_key(*names):
    """Honor requested key priority; for each key, file precedes process env."""
    values = {}
    path = os.path.join(HERMES_HOME, ".env")
    try:
        if os.path.exists(path):
            for raw in Path(path).read_text(errors="ignore").splitlines():
                if "=" in raw and not raw.lstrip().startswith("#"):
                    key, value = raw.split("=", 1)
                    values[key.strip()] = value.strip()
    except OSError:
        values = {}
    for name in names:
        if values.get(name):
            return values[name]
        if os.environ.get(name):
            return os.environ[name]
    return None


def gh(adapter, path):
    return adapter.get(path)


def gh_raw(adapter, path, ref=None):
    return adapter.read_file(path, ref=ref)


def notion(key, path, method="GET", body=None):
    request = urllib.request.Request(
        f"https://api.notion.com/v1{path}",
        method=method,
        headers={
            "Authorization": f"Bearer {key}",
            "Notion-Version": "2025-09-03",
            "Content-Type": "application/json",
        },
        data=json.dumps(body).encode() if body is not None else None,
    )
    try:
        with urllib.request.urlopen(request, timeout=45) as response:
            return json.load(response)
    except urllib.error.HTTPError as exc:
        return {"error": exc.code}


def roster_row(email, key):
    query = notion(
        key,
        f"/data_sources/{DS_ID}/query",
        "POST",
        {"filter": {"property": "Email", "email": {"equals": email}}},
    )
    if query.get("error"):
        raise SyncError("roster lookup failed")
    results = query.get("results")
    if not isinstance(results, list):
        raise SyncError("roster response was malformed")
    if not results:
        return None
    row = results[0]
    try:
        props = row["properties"]
        primary = (props.get("Primary", {}).get("select") or {}).get("name")
        adjacent = [item["name"] for item in props.get("Adjacent", {}).get("multi_select", [])]
        is_lt = bool(props.get("LT", {}).get("checkbox"))
    except (KeyError, TypeError):
        raise SyncError("roster response was malformed") from None
    functions = set()
    if primary in FUNC_TO_PROFILE:
        functions.add(FUNC_TO_PROFILE[primary])
    for item in adjacent:
        if item in FUNC_TO_PROFILE and item != "LT":
            functions.add(FUNC_TO_PROFILE[item])
    if is_lt:
        functions.add("lt")
    return row.get("id"), functions


def git_blob_sha(data):
    return hashlib.sha1(b"blob %d\0%s" % (len(data), data)).hexdigest()


def _safe_repo_file(path):
    if not isinstance(path, str) or not path or path.startswith(("/", "\\")):
        raise SyncError("repository tree contained an unsafe path")
    if "\\" in path or "%" in path or any(ord(char) < 32 for char in path):
        raise SyncError("repository tree contained an unsafe path")
    if any(part in {"", ".", ".."} for part in path.split("/")):
        raise SyncError("repository tree contained an unsafe path")
    pure = PurePosixPath(path)
    if any(part in {"", ".", ".."} for part in pure.parts):
        raise SyncError("repository tree contained an unsafe path")
    return pure.as_posix()


def load_snapshot(adapter):
    """Probe access, resolve main once, then obtain one complete recursive tree."""
    repo = gh(adapter, REPO_API)
    if not isinstance(repo, dict) or repo.get("full_name") != f"{OWNER}/{REPO}":
        raise SyncError("repository probe was malformed")
    ref = gh(adapter, REPO_API + "/git/ref/heads/main")
    try:
        commit = ref["object"]["sha"]
    except (KeyError, TypeError):
        raise SyncError("repository ref response was malformed") from None
    if not isinstance(commit, str) or not SHA_RE.fullmatch(commit):
        raise SyncError("repository ref response was malformed")
    result = gh(adapter, f"{REPO_API}/git/trees/{commit}?recursive=1")
    if not isinstance(result, dict) or result.get("truncated") is not False:
        raise SyncError("repository tree was incomplete")
    raw_tree = result.get("tree")
    if not isinstance(raw_tree, list):
        raise SyncError("repository tree response was malformed")
    tree = []
    seen = set()
    for item in raw_tree:
        if not isinstance(item, dict) or item.get("type") not in {"blob", "tree"}:
            raise SyncError("repository tree response was malformed")
        path = _safe_repo_file(item.get("path"))
        sha = item.get("sha")
        if path in seen or not isinstance(sha, str) or not SHA_RE.fullmatch(sha):
            raise SyncError("repository tree response was malformed")
        seen.add(path)
        tree.append({"path": path, "sha": sha, "type": item["type"]})
    return commit, tree


def list_tree(adapter, repo_dir, ref=None, tree=None):
    """Return files under repo_dir from a validated, nontruncated Git tree."""
    repo_dir = _safe_repo_file(repo_dir).rstrip("/")
    if tree is None:
        snapshot_ref, tree = load_snapshot(adapter)
        ref = ref or snapshot_ref
    prefix = repo_dir + "/"
    output = []
    for item in tree:
        path = item["path"]
        if item["type"] == "blob" and path.startswith(prefix) and Path(path).name != "README.md":
            output.append((path, item["sha"]))
    return sorted(output)


def _safe_destination(local_dir, rel):
    rel_path = PurePosixPath(_safe_repo_file(rel))
    root = Path(local_dir).absolute()
    destination = root.joinpath(*rel_path.parts).absolute()
    try:
        destination.relative_to(root)
        destination.parent.resolve(strict=False).relative_to(root.resolve(strict=False))
    except ValueError:
        raise SyncError("repository path escaped its local destination") from None
    return destination


def _prepare_directory(adapter, repo_dir, local_dir, manifest, ref, tree, cache):
    writes = []
    conflicts = []
    updates = {}
    for repo_path, sha in list_tree(adapter, repo_dir, ref=ref, tree=tree):
        rel = repo_path[len(repo_dir):].lstrip("/")
        destination = _safe_destination(local_dir, rel)
        if destination.exists():
            try:
                local = destination.read_bytes()
            except OSError:
                raise SyncError("unable to read a managed local file") from None
            if git_blob_sha(local) == sha:
                updates[str(destination)] = repo_path
                continue
            if not is_repo_managed(rel):
                conflicts.append(str(destination))
                continue
        if repo_path not in cache:
            content = gh_raw(adapter, repo_path, ref=ref)
            if git_blob_sha(content) != sha:
                raise SyncError("pinned GitHub file did not match the repository tree")
            cache[repo_path] = content
        writes.append((destination, cache[repo_path], repo_path))
        updates[str(destination)] = repo_path
    return writes, conflicts, updates


def _atomic_write(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=path.parent, prefix="." + path.name + ".", delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except OSError:
        if temporary is not None:
            try:
                temporary.unlink()
            except OSError:
                pass
        raise SyncError("unable to install a fleet file") from None


def sync_dir(adapter, repo_dir, local_dir, changes, conflicts, manifest):
    """Compatibility entry point; production main shares one snapshot across dirs."""
    ref, tree = load_snapshot(adapter)
    writes, found_conflicts, updates = _prepare_directory(
        adapter, repo_dir, local_dir, manifest, ref, tree, {}
    )
    for destination, content, repo_path in writes:
        _atomic_write(destination, content)
        changes.append("installed " + _display_path(destination))
    conflicts.extend(_display_path(Path(path)) for path in found_conflicts)
    manifest.update(updates)


def sync_plugins_and_policy(adapter, functions, changes, conflicts, manifest):
    for home in plugin_destinations(HERMES_HOME, functions):
        sync_dir(adapter, "plugins/_common", str(home / "plugins"), changes, conflicts, manifest)
        reconcile_deliverable_policy(home, changes)


def _manifest_projection(root, destination, repo_path):
    parts = PurePosixPath(repo_path).parts
    profiles = set(FUNC_TO_PROFILE.values())
    relative = None
    expected = []
    if len(parts) >= 2 and parts[0] == "scripts":
        relative = parts[1:]
        expected.append(root.joinpath("scripts", *relative))
    elif len(parts) >= 3 and parts[0] == "skills":
        relative = parts[2:]
        if parts[1] == "_common":
            expected.append(root.joinpath("skills", *relative))
        elif parts[1] in profiles:
            expected.append(root.joinpath("profiles", parts[1], "skills", *relative))
    elif len(parts) >= 3 and parts[:2] == ("plugins", "_common"):
        relative = parts[2:]
        expected.append(root.joinpath("plugins", *relative))
        expected.extend(
            root.joinpath("profiles", profile, "plugins", *relative)
            for profile in profiles
        )
    if relative is None or destination not in expected:
        raise SyncError("fleet manifest source and destination did not match")
    return "/".join(relative)


def _manifest_quarantines(manifest, tree_paths):
    quarantines = []
    root = Path(HERMES_HOME).absolute()
    resolved_root = root.resolve(strict=False)
    for destination_text, repo_path in list(manifest.items()):
        if not isinstance(destination_text, str) or not Path(destination_text).is_absolute():
            raise SyncError("fleet manifest was malformed")
        repo_path = _safe_repo_file(repo_path)
        destination = Path(destination_text).absolute()
        managed_relative = _manifest_projection(root, destination, repo_path)
        try:
            destination.parent.resolve(strict=False).relative_to(resolved_root)
        except ValueError:
            raise SyncError("fleet manifest referenced a path outside HERMES_HOME") from None
        if not destination.exists():
            manifest.pop(destination_text, None)
            continue
        if repo_path not in tree_paths:
            if not is_repo_managed(managed_relative):
                manifest.pop(destination_text, None)
                continue
            if os.path.lexists(str(destination) + ".revoked"):
                raise SyncError("fleet quarantine target already exists")
            quarantines.append((destination, repo_path))
    return quarantines


def _quarantine_file(destination):
    revoked = Path(str(destination) + ".revoked")
    try:
        os.link(destination, revoked, follow_symlinks=False)
    except FileExistsError:
        raise SyncError("fleet quarantine target already exists") from None
    except OSError:
        raise SyncError("unable to quarantine a removed fleet file") from None
    try:
        destination.unlink()
    except OSError:
        try:
            revoked.unlink()
        except OSError:
            pass
        raise SyncError("unable to quarantine a removed fleet file") from None


def quarantine_deleted(adapter, manifest, changes):
    """Compatibility entry point using a proven-access snapshot, never string-matched 404s."""
    _, tree = load_snapshot(adapter)
    tree_paths = {item["path"] for item in tree if item["type"] == "blob"}
    for destination, _ in _manifest_quarantines(manifest, tree_paths):
        _quarantine_file(destination)
        del manifest[str(destination)]
        changes.append("quarantined " + _display_path(destination) + " (removed from repo)")


def load_manifest():
    path = Path(MANIFEST_FILE)
    if not path.exists():
        return {}
    try:
        data = json.loads(path.read_text())
    except (OSError, json.JSONDecodeError):
        raise SyncError("fleet manifest was malformed") from None
    if not isinstance(data, dict):
        raise SyncError("fleet manifest was malformed")
    return data


def save_manifest(manifest):
    _atomic_write(Path(MANIFEST_FILE), (json.dumps(manifest, indent=2, sort_keys=True) + "\n").encode())


def parse_jobs_yaml(text):
    jobs, current = [], None
    for raw in text.splitlines():
        stripped = raw.strip()
        if stripped.startswith("- name:"):
            if current:
                jobs.append(current)
            current = {"name": stripped.split(":", 1)[1].strip().strip('"')}
        elif current is not None and ":" in stripped and not stripped.startswith("#"):
            key, value = stripped.split(":", 1)
            key, value = key.strip(), value.strip().strip('"')
            if key == "scope":
                if value.startswith("["):
                    current[key] = [part.strip().strip('"') for part in value.strip("[]").split(",") if part.strip()]
                else:
                    current[key] = value
            elif key in ("schedule", "script", "deliver", "workdir"):
                current[key] = value
            elif key == "no_agent":
                current[key] = value.lower() in ("true", "yes", "1")
    if current:
        jobs.append(current)
    return jobs


def cron_job_names():
    result = _run_hermes([HERMES_BIN, "cron", "list"], HERMES_HOME)
    if result.returncode != 0:
        raise SyncError("unable to inspect fleet cron jobs")
    return set(re.findall(r"Name:\s*(.+)", result.stdout or ""))


def reconcile_jobs(adapter, held_labels, changes, text=None, ref=None):
    if text is None:
        if ref is None:
            ref, tree = load_snapshot(adapter)
            if "jobs.yaml" not in {item["path"] for item in tree if item["type"] == "blob"}:
                return
        text = gh_raw(adapter, "jobs.yaml", ref=ref).decode("utf-8")
    existing = cron_job_names()
    for job in parse_jobs_yaml(text):
        name = job.get("name", "")
        if not name.startswith(REPO_MANAGED_PREFIX) or not job.get("schedule") or not job.get("script"):
            continue
        scope = job.get("scope", "all")
        if scope != "all":
            scopes = [scope] if isinstance(scope, str) else scope
            if not any(item in held_labels for item in scopes):
                continue
        if name in existing:
            continue
        command = [
            HERMES_BIN, "cron", "create", job["schedule"], "--name", name,
            "--script", os.path.basename(job["script"]), "--deliver", job.get("deliver", "local"),
        ]
        if job.get("no_agent"):
            command.append("--no-agent")
        if job.get("workdir"):
            command.extend(["--workdir", job["workdir"]])
        result = _run_hermes(command, HERMES_HOME)
        if result.returncode != 0:
            raise SyncError("unable to create a fleet cron job")
        changes.append(f"cron job created: {name}")
        existing.add(name)


def heartbeat(page_id, key):
    result = notion(key, f"/pages/{page_id}", "PATCH", {"properties": {
        "Last Fleet Sync": {"date": {"start": datetime.now(timezone.utc).isoformat()}}
    }})
    if result.get("error"):
        raise SyncError("fleet heartbeat failed")


def _display_path(path):
    try:
        return str(path.relative_to(Path(HERMES_HOME).absolute()))
    except ValueError:
        return str(path)


def main():
    if not os.path.exists(EMAIL_FILE):
        return 0
    try:
        email = Path(EMAIL_FILE).read_text().strip()
    except OSError:
        print("fleet sync failed: member marker is unreadable", file=sys.stderr)
        return 1
    if not email:
        return 0
    notion_key = env_key("NOTION_API_KEY")
    if not notion_key:
        print("fleet sync failed: NOTION_API_KEY is missing", file=sys.stderr)
        return 1
    try:
        adapter = ComposioGitHub.from_environment(home=HERMES_HOME)
        row = roster_row(email, notion_key)
        if row is None or not row[0]:
            raise SyncError("member roster row was not found")
        page_id, functions = row

        # No local/cron/policy/heartbeat mutation occurs before all GitHub data is
        # fetched and validated against this one main commit.
        ref, tree = load_snapshot(adapter)
        tree_paths = {item["path"] for item in tree if item["type"] == "blob"}
        manifest = load_manifest()
        cache = {}
        writes = []
        conflicts = []
        updates = {}
        targets = [("skills/_common", Path(HERMES_HOME) / "skills")]
        for function in sorted(functions):
            profile = Path(HERMES_HOME) / "profiles" / function
            if profile.is_dir():
                targets.append((f"skills/{function}", profile / "skills"))
        targets.append(("scripts", Path(HERMES_HOME) / "scripts"))
        for home in plugin_destinations(HERMES_HOME, functions):
            targets.append(("plugins/_common", home / "plugins"))
        for repo_dir, local_dir in targets:
            planned, found_conflicts, found_updates = _prepare_directory(
                adapter, repo_dir, local_dir, manifest, ref, tree, cache
            )
            writes.extend(planned)
            conflicts.extend(found_conflicts)
            updates.update(found_updates)

        jobs_text = None
        if "jobs.yaml" in tree_paths:
            jobs_text = gh_raw(adapter, "jobs.yaml", ref=ref).decode("utf-8")
        quarantines = _manifest_quarantines(manifest, tree_paths)

        changes = []
        for destination, content, _repo_path in writes:
            _atomic_write(destination, content)
            changes.append("installed " + _display_path(destination))
        manifest.update(updates)
        for destination, _repo_path in quarantines:
            _quarantine_file(destination)
            manifest.pop(str(destination), None)
            changes.append("quarantined " + _display_path(destination) + " (removed from repo)")

        for home in plugin_destinations(HERMES_HOME, functions):
            reconcile_deliverable_policy(home, changes)
        labels = {value: key for key, value in FUNC_TO_PROFILE.items()}
        held_labels = {labels.get(function, function) for function in functions}
        if jobs_text is not None:
            reconcile_jobs(adapter, held_labels, changes, text=jobs_text, ref=ref)
        save_manifest(manifest)
        heartbeat(page_id, notion_key)
    except (GitHubError, SyncError, ValueError, UnicodeError) as exc:
        print(f"fleet sync failed: {exc}", file=sys.stderr)
        return 1
    except Exception:
        print("fleet sync failed: unexpected local error", file=sys.stderr)
        return 1

    for change in changes[:30]:
        print(change)
    for conflict in conflicts[:10]:
        print(f"CONFLICT (kept local, repo not applied): {_display_path(Path(conflict))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
