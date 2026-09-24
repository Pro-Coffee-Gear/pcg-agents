#!/usr/bin/env python3
"""pcg-credential-health.py — GitHub credential watchdog for the PCG fleet.

Checks the GitHub credential path the fleet ACTUALLY uses — the Composio broker
(account github_purger-sick = WWWPCG user) — and prints an alert ONLY when it
fails (watchdog pattern: empty stdout = healthy = nothing delivered). Runs as a
no_agent cron job; stdout is delivered to Wes's Slack DM.

Why this exists: 2026-09-23 — both stored GitHub tokens had been 401-dead for
weeks. The fleet sync failed silently (false-green heartbeats) and the first
symptom was a stalled fleet, not an alert. A stored credential going stale is
now a same-day event.

The canonical GitHub path is the Composio broker, NOT .env tokens:
  /opt/data/.composio/composio  (account github_purger-sick = WWWPCG user)
.env GITHUB_* tokens are dead and never minted. A missing token is therefore
NOT a failure. We still probe any PRESENT .env token so a stale leftover gets
caught, but their absence is the expected, healthy state.
"""
import json
import os
import subprocess
import urllib.error
import urllib.request

ENV_FILE = os.environ.get("PCG_CRED_ENV_FILE", "/opt/data/.env")
COMPOSIO = "/opt/data/.composio/composio"
REPO = "https://api.github.com/repos/Pro-Coffee-Gear/pcg-agents"
TOKEN_KEYS = ("GITHUB_SYNC_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")


def composio_github_connection_active():
    """Return (ok: bool, detail: str). Probes the real Composio broker path."""
    try:
        out = subprocess.run(
            [COMPOSIO, "connections", "list"],
            capture_output=True, text=True, timeout=60,
        ).stdout
        data = json.loads(out)
        gh = (data.get("github") or [])
        active = [c for c in gh if c.get("status") == "ACTIVE"]
        if active:
            return True, ", ".join(c.get("word_id", "?") for c in active)
        return False, (f"no ACTIVE github connection (found {len(gh)}: "
                       f"{[c.get('status') for c in gh]})")
    except Exception as e:
        return False, f"{type(e).__name__}: {e}"


def composio_github_probe():
    """Light live probe: can the broker actually list repositories?"""
    try:
        out = subprocess.run(
            [COMPOSIO, "execute", "GITHUB_LIST_REPOSITORIES", "-d", "{}"],
            capture_output=True, text=True, timeout=120,
        ).stdout
        data = json.loads(out)
        return data.get("successful") is True
    except Exception:
        return False


def read_tokens(path):
    tokens = {}
    try:
        lines = open(path, "rb").read().decode("utf-8", "ignore").splitlines()
    except OSError:
        return tokens
    for line in lines:
        line = line.strip()
        for key in TOKEN_KEYS:
            if line.startswith(key + "="):
                value = line.split("=", 1)[1].strip()
                if value:
                    tokens[key] = value
    return tokens


def probe(token, url):
    req = urllib.request.Request(
        url,
        headers={"Authorization": f"Bearer {token}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "User-Agent": "pcg-credential-health"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, {}
    except Exception as e:
        return None, {"error": f"{type(e).__name__}: {e}"}


def check_token(name, token):
    """Returns None if healthy, else a one-line problem description."""
    klass = ("fine-grained" if token.startswith("github_pat_")
             else "CLASSIC" if token.startswith("ghp_") else "other")
    status, body = probe(token, "https://api.github.com/user")
    if status != 200:
        return (f"{name} ({klass}) is DEAD: GET /user -> "
                f"HTTP {status or body.get('error', 'unreachable')}")
    if klass == "CLASSIC":
        return (f"{name} is a classic ghp_ token (admin-class, forbidden in "
                f"this slot) — replace with a fine-grained PAT")
    status, _ = probe(token, REPO)
    if status != 200:
        return (f"{name} ({klass}) authenticates as {body.get('login')} but "
                f"CANNOT read Pro-Coffee-Gear/pcg-agents -> HTTP {status}")
    return None


def main():
    problems = []

    # Primary path: the Composio broker.
    ok, detail = composio_github_connection_active()
    if not ok:
        problems.append(f"Composio GitHub connection BROKEN: {detail}")
    elif not composio_github_probe():
        problems.append("Composio GitHub connection is ACTIVE but "
                        "GITHUB_LIST_REPOSITORIES probe failed")

    # Secondary path: any PRESENT .env token must not be dead. Absence is fine.
    for name, token in sorted(read_tokens(ENV_FILE).items()):
        issue = check_token(name, token)
        if issue:
            problems.append(issue)

    if problems:
        print("🚨 PCG GitHub credential alert — action needed:")
        for p in problems:
            print(f"• {p}")
        print("Primary GitHub path is the Composio broker "
              "(/opt/data/.composio/composio, account github_purger-sick). "
              "If that connection is dead, re-link it; do NOT mint .env PATs.")
    # empty stdout when healthy -> no_agent job delivers nothing
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
