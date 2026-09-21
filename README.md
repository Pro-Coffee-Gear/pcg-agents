# pcg-agents

Onboarding + verification scripts for the Pro Coffee Gear agent fleet.

## Files
- `pcg_onboard.py` — run on a new team member's box. Reads their row in the
  Notion Agent Team Roster (by email), creates the Hermes profiles for their
  functions, wires each to the shared `procoffeegear` Honcho workspace, and
  writes the result back to the roster.
- `roster_sync_check.py` — verification only: compares requested vs. active
  profiles and updates Sync Status / Onboarded in the roster.
- `plugins/_common/pcg-deliverable-autoregistration/` — fleet-wide definition-of-done
  guard. It injects the catalog policy into every profile and keeps completed build
  turns open until the agent has created or updated a Proposed Business Automations &
  Deliverables row (scratch/test fixtures are the only exemption).
- `scripts/pcg_sync.py` — distributes common plugins alongside skills/scripts, enables
  the registration guard in every held profile, and preserves any local coding guidance
  through a managed policy block.

## Usage
Keys and the pointer to a separately provisioned per-person Composio MCP session
are supplied at runtime (never stored in this repository):

```bash
PCG_COMPOSIO_SESSION_FILE=/absolute/private/path/member-session.json \
PCG_COMPOSIO_PYTHON=/opt/hermes/.venv/bin/python \
HONCHO_API_KEY=<key> NOTION_API_KEY=<key> \
python3 pcg_onboard.py --email you@procoffeegear.com
```

The session file must be mode 0600 and centrally authorized before onboarding.
Its URL and headers are never copied into this repository or shared profiles.
See `docs/github-composio-migration.md` for the pending live authorization and
rollout blockers.

These scripts contain **no credentials**. Access to the shared brain and GitHub
remains controlled by separately provisioned credentials and central services.
