---
name: pcg-deliverable-publishing
description: "Use after building or deploying business-impacting work."
version: 1.0.0
metadata:
  hermes:
    tags: [pcg, deliverables, publishing, governance]
---

# PCG Deliverable Publishing

Use this after building, deploying, or scheduling anything for Pro Coffee Gear.

## Decide whether it belongs on the business board

Publish when at least one is true:
- It runs on a schedule.
- It changes business data.
- More than one person uses or depends on it.
- It supports a recurring business process.
- It is customer-facing.
- It requires ongoing maintenance.
- It creates a material dependency.
- Leadership or the function owner would care if it stopped.

Do not publish scratch scripts, temporary migrations, local cleanup utilities, or
one-time experiments unless they become an ongoing dependency.

## Publish as Proposed

Run the shared publisher on the builder's box:

```bash
python3 /opt/data/scripts/pcg-register-deliverable.py \
  --name "Clear business name" \
  --type "Scheduled Automation" \
  --purpose "What recurring business outcome this creates" \
  --owner-email "owner@procoffeegear.com" \
  --function "CS" \
  --audience "Function" \
  --visibility "Function" \
  --schedule "every 30m" \
  --source-repo "https://github.com/WWWPCG/repo" \
  --scripts "script.py" \
  --jobs "job-id" \
  --repair-policy "repair-pr" \
  --test-command "python3 -m unittest" \
  --deployment-method "How it reaches production" \
  --rollback-method "How to undo it safely"
```

Use multiple `--function` flags for cross-functional work. The script evaluates
the meaningful-deliverable criteria and creates or updates a **Proposed** Notion
row. It cannot approve its own submission.

## Approval

The function owner reviews the proposal in **Business Automations & Deliverables**:
- Confirm business purpose, owner, users, source, tests, deployment, and rollback.
- Set **Curation Status = Approved** when it belongs on the official board.
- Set **Needs Changes** or **Rejected** otherwise.
- Set Status to Live only after production verification.

## Repair governance

- `detect-only`: record and alert.
- `safe-auto-heal`: deterministic restoration only.
- `repair-pr`: an agent may prepare a tested PR; owner must merge it.
- `critical-approval`: no repair without explicit owner/LT approval.
- `manual`: human-led recovery.

Never merge or deploy a repair merely because an agent prepared it. The owner
reviews the GitHub PR or explicitly tells their agent to review it; the agent must
re-check CI and request explicit confirmation before merging.
