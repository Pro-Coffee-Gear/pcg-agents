---
name: pcg-deliverable-publishing
description: "Use when creating or updating any durable PCG work product. Catalog it before completion."
version: 2.0.0
metadata:
  hermes:
    tags: [pcg, deliverables, publishing, governance]
---

# PCG Deliverable Publishing

Every durable Pro Coffee Gear work product must appear in **Business Automations &
Deliverables**. Registration is part of the definition of done; it is not optional based
on whether the builder thinks the work is important enough.

This includes apps, dashboards, automations, integrations, reports, documents, models,
spreadsheets, shared skills, and scheduled jobs. A temporary scratch file or throwaway
test fixture is the only exemption.

## Publish as Proposed before reporting completion

Run the shared publisher on the builder's box:

```bash
python3 /opt/data/scripts/pcg-register-deliverable.py \
  --name "Stable business name" \
  --type "App / Dashboard" \
  --purpose "What business outcome this creates" \
  --owner-email "owner@procoffeegear.com" \
  --function "CS" \
  --audience "Function" \
  --visibility "Function" \
  --schedule "On demand" \
  --source-repo "https://github.com/WWWPCG/repo" \
  --scripts "path/to/artifact" \
  --repair-policy "detect-only" \
  --test-command "exact verification command" \
  --deployment-method "How people access it" \
  --rollback-method "How to undo it safely"
```

Use multiple `--function` flags for cross-functional work. Supply every field that is
actually known and leave unknown fields blank; never invent metadata. Reuse the same
stable name and owner for later updates so the existing row is updated rather than
duplicated.

The script creates or updates a **Proposed** Notion row. Verify the returned Notion URL
before reporting completion. The publisher cannot approve its own submission.

For the narrow scratch/test-fixture exemption, do not create a row and include exactly
`PCG catalog: not applicable — scratch/test fixture` in the final response.

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

Never merge or deploy a repair merely because an agent prepared it. The owner reviews the
GitHub PR or explicitly tells their agent to review it; the agent must re-check CI and
request explicit confirmation before merging.
