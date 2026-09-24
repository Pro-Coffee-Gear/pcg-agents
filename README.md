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
Keys are supplied at runtime (never stored here):

```bash
curl -sL https://raw.githubusercontent.com/Pro-Coffee-Gear/pcg-agents/main/pcg_onboard.py -o pcg_onboard.py && \
HONCHO_API_KEY=<key> NOTION_API_KEY=<key> \
python3 pcg_onboard.py --email you@procoffeegear.com
```

These scripts contain **no credentials**. Access to the shared brain is
controlled entirely by the keys passed at runtime.
