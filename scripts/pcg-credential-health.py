#!/usr/bin/env python3
"""pcg-credential-health.py — GitHub credential watchdog for the PCG fleet.

Probes every GitHub token stored in this box's .env and prints an alert ONLY
when one fails (watchdog pattern: empty stdout = all healthy = nothing is
delivered). Runs as a no_agent cron job; stdout is delivered to Wes's Slack DM.

Why this exists: 2026-09-23 — both stored GitHub tokens had been 401-dead for
weeks. The fleet sync failed silently (false-green heartbeats) and the first
symptom was a stalled fleet, not an alert. A stored credential going stale is
now a same-day event.

Checks per token:
  1. GET /user                      -> identity (proves the token authenticates)
  2. GET /repos/WWWPCG/pcg-agents   -> scoped read (proves it can do its job)
A missing GITHUB_TOKEN is NOT a failure (empty push slot = agents use the
documented Composio write path). A present-but-dead one IS.
"""
import json
import os
import urllib.error
import urllib.request

ENV_FILE = os.environ.get("PCG_CRED_ENV_FILE", "/opt/data/.env")
REPO = "https://api.github.com/repos/WWWPCG/pcg-agents"
TOKEN_KEYS = ("GITHUB_SYNC_TOKEN", "GITHUB_TOKEN", "GH_TOKEN")


def read_tokens(path):
    tokens = {}
    for line in open(path, "rb").read().decode("utf-8", "ignore").splitlines():
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
                f"CANNOT read WWWPCG/pcg-agents -> HTTP {status}")
    return None


def main():
    problems = []
    tokens = read_tokens(ENV_FILE)
    if not tokens:
        problems.append(f"no GitHub tokens found in {ENV_FILE}")
    for name, token in sorted(tokens.items()):
        issue = check_token(name, token)
        if issue:
            problems.append(issue)
    if problems:
        print("🚨 PCG GitHub credential alert — action needed:")
        for p in problems:
            print(f"• {p}")
        print("Fix: mint a fine-grained PAT (github.com/settings/personal-access-tokens), "
              "update /opt/data/.env, then tell Alfred to verify.")
    # empty stdout when healthy -> no_agent job delivers nothing
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
