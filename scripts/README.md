# Shared scripts

Automation synced to every box's `scripts/` directory (add-only).

GitHub-backed jobs use the reviewed three-file bundle:

- `pcg_composio.py` — generic official-MCP direct-tool client
- `pcg_github.py` — GitHub compatibility translator
- `pcg_sync.py` — fleet updater

`mcp==2.0.0` is declared in `requirements-composio.txt`. Scheduled fleet sync
must use the onboarding-generated `pcg-fleet-sync.sh` launcher with an explicitly
validated application interpreter; do not assume system `python3` can import MCP.
Per-person session URL and headers are provisioned separately and never belong in
this directory.
