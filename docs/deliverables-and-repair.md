# PCG Deliverables and Repair Workflow

## What appears on the business board

A deliverable is **curated**, not blindly discovered. It belongs on the board when it
runs repeatedly, changes business data, supports several people, is customer-facing,
needs ongoing maintenance, creates an important dependency, or would matter if it stopped.

Every team agent receives the `pcg-deliverable-publishing` skill and
`pcg-register-deliverable.py`. After meaningful work is built, the agent submits a
**Proposed** row to Notion. The submitting agent cannot approve it.

The function owner reviews:
- business purpose and audience;
- owner and affected functions;
- source repository, scripts, and scheduled jobs;
- test command;
- deployment and rollback method;
- repair policy.

The owner then sets **Curation Status** to Approved, Needs Changes, or Rejected.
Approved rows are versioned automatically in `deliverables.toml`.

## What happens when a script fails

1. The 15-minute health monitor records the failure and opens a stable incident.
2. For a missing repo-managed file, it may restore the exact reviewed GitHub version,
   validate Python syntax, and atomically replace the missing copy.
3. If the script is eligible for `repair-pr`, the central repair agent reproduces the
   failure with a regression test, makes a minimal patch, runs tests, and opens a
   **draft GitHub pull request**.
4. The repair agent cannot merge or deploy. The Script Health row becomes
   **Awaiting Approval**, and the owner receives the PR, failure, test command, and rollback.
5. The owner reviews and merges in GitHub. Alternatively, the owner can tell their agent
   `review and approve <PR URL>`; the agent must re-check the diff and CI and ask for
   explicit confirmation before merging.
6. Fleet sync distributes the merged version. Health checks verify recovery and mark the
   incident Resolved.

## Repair policies

| Policy | Meaning |
|---|---|
| `detect-only` | Record and alert; do not repair. |
| `safe-auto-heal` | Only deterministic, predefined restoration. |
| `repair-pr` | Prepare a tested draft PR; owner approval required. |
| `critical-approval` | No repair without owner/LT authorization. |
| `manual` | Human-led diagnosis and recovery. |

Unknown scripts default to `detect-only`. OAuth, credentials, financial workflows,
customer-data automations, and local-only integrations should not receive automatic
code or configuration changes.

## Key files

- `health.toml` — script ownership and repair policies.
- `deliverables.toml` — generated audit snapshot of Approved Notion rows.
- `jobs.yaml` — fleet-managed deterministic jobs.
- `scripts/pcg-register-deliverable.py` — team proposal publisher.
- `scripts/pcg-automation-health.py` — detection and deterministic restoration.
- `scripts/pcg-repair-gate.py` — deduplicated repair-agent trigger.
- `scripts/pcg-repair-status.py` — incident state and PR handoff.
- `scripts/pcg-repair-notify.py` — owner-routed approval notification.
