# GitHub access through Composio: migration and rollout contract

## Status and current scope

This repository contains the reviewed code migration. It does not mean the fleet rollout is complete.

The migrated GitHub callers are:

- `pcg_sync.py` and its byte-identical distribution copy `scripts/pcg_sync.py`
- `scripts/pcg-automation-health.py`
- `scripts/pcg-deliverable-reconcile.py`
- `pcg_onboard.py`

They use the constrained stdlib adapter in `scripts/pcg_github.py`. The adapter invokes one configured Composio executable without a shell and selects one configured GitHub connection on every request. These callers have no GitHub PAT fallback.

Native Git operations are separate. A developer's `git fetch`, `git push`, credential helper, SSH key, or GitHub CLI login is not routed through this adapter and is not changed by this migration.

No production account selector is recorded in this repository. No fleet configuration, cron job, Composio authorization, deployment, login, or live probe is performed by this change.

## Required per-instance configuration

Each instance needs its own explicit connection selector and an already-installed, already-authorized Composio CLI:

```text
PCG_GITHUB_ACCOUNT=<instance-specific-selector>
PCG_COMPOSIO_CLI=/absolute/path/to/.composio/composio
```

`PCG_GITHUB_ACCOUNT` may be the selector form supported by the provisioned Composio connection, including an account ID or alias/ID. It must not be replaced with a shared example value.

`PCG_COMPOSIO_CLI` is an absolute executable path, not a configuration directory. If omitted by a normal adapter caller, the default is:

```text
~/.composio/composio
```

Snapshot writes are off unless this exact setting is explicitly enabled on the reconcile instance:

```text
PCG_GITHUB_ALLOW_SNAPSHOT_WRITE=true
```

That gate only permits `PUT /repos/WWWPCG/pcg-agents/contents/deliverables.toml` on branch `main`. It does not permit arbitrary repository writes, POST, or DELETE. The deliverables caller supplies the existing file SHA, does not retry an uncertain write, and reads the exact returned commit back before reporting success.

Do not configure `GITHUB_SYNC_TOKEN`, `GITHUB_TOKEN`, or `GH_TOKEN` for these callers. Onboarding removes legacy PAT entries rather than propagating them. It persists only the nonsecret Composio executable and account-selection settings; it never copies a Composio auth store or another person's CLI session.

## Connection scope

Provision a dedicated, least-privilege connection for each automation role or instance. Do not select a broad personal GitHub connection merely because it already works.

The local adapter is defense in depth, not an authorization sandbox. Its request allowlist limits this code to `WWWPCG/pcg-agents`, and its only write route is `deliverables.toml`, but the broker-side connection must independently enforce repository and operation scope. Account revocation, audit, and authorization remain Composio/GitHub controls.

Suggested separation:

- Fleet and health instances: repository read access only.
- Deliverables reconcile instance: read access plus only the reviewed snapshot update capability.
- Repair agent: a separately scoped connection and workflow for branches and draft pull requests.

## Read-only validation

After the reviewed bundle and per-instance connection are provisioned, an operator may run:

```bash
python3 scripts/pcg_github.py --check
```

The check is intentionally read-only. It probes repository metadata and reads `health.toml`, a representative private file. Output contains only a safe pass/fail status; it does not print the account selector, subprocess output, broker errors, file content, or credentials.

This command is a real network check. It is not run by unit tests or by a dry-run onboarding command.

## Reviewed bundle onboarding

New onboarding requires a reviewed bundle containing both:

```text
scripts/pcg_github.py
scripts/pcg_sync.py
```

The Composio executable and selected connection must be provisioned and authorized separately before onboarding starts. `pcg_onboard.py` does not install Composio, initiate login, copy CLI authorization data, or fetch the adapter through an adapter that is not yet present.

Live onboarding performs the GitHub repository/private-file preflight before storing keys, creating profiles, changing `honcho.json`, registering cron jobs, or writing onboarding completion to Notion. Fleet cron registration occurs only after the adapter and updater are installed from the same reviewed local bundle and an initial updater run succeeds. Onboarding completion is written last.

`--dry-run` performs local prerequisite checks only. It makes no network calls and performs no mutation.

## Fleet-sync behavior

A sync run:

1. Probes the expected private repository.
2. Resolves `main` once.
3. Loads one recursive Git tree at that commit and rejects a truncated or malformed tree.
4. Reads required files at the same pinned commit and validates each blob SHA.
5. Only then mutates local files, plugin policy, cron state, the fleet manifest, and finally the success heartbeat.

An auth failure, timeout, malformed response, failed file fetch, failed local/Hermes mutation, or missing gateway configuration returns nonzero and does not write the success heartbeat. A GitHub 404 is not inferred from exception text and cannot trigger quarantine. Quarantine is based only on absence from a complete tree obtained after a successful repository probe.

Non-PCG local files retain the existing conflict rule. Repo-managed `pcg-`/`pcg_` files may be replaced. Function profiles remain add-only. Repository response paths and manifest destinations are validated so they cannot escape their designated local roots.

## Health and deliverables behavior

The automation health monitor uses the same gateway for:

- the GitHub dependency probe;
- the `health.toml` policy fallback;
- a policy-permitted script restoration.

Restoration still requires the existing repository/path policy check and Python syntax validation. A detect-only, malformed, external-repository, or traversal policy cannot restore a file.

The deliverables reconcile publishes only Notion rows whose curation status is Approved. It probes repository access before treating a missing snapshot as a legitimate 404, performs a content no-op when possible, uses SHA optimistic concurrency, writes only branch `main`, never blindly retries, and verifies the exact commit/file content before printing success.

## Repair-agent path

Repair automation must use a separately authorized Composio API workflow to create a branch and open a draft pull request. The production adapter in this repository deliberately does not expose POST or arbitrary PUT routes and therefore is not a general PR client.

A repair run must remain read-only until it has reproduced the failure and produced tests. Any branch creation or draft-PR API call needs explicit authorization and read-back verification. This migration does not authorize edits to live cron jobs, direct pushes to `main`, automatic merges, or deployment changes.

## Deterministic staged rollout and rollback

Rollout is pending explicit approval. The parent operator should record a reviewed commit or immutable bundle digest and use that same artifact for each stage.

Recommended sequence:

1. Replace or disable the legacy invitation generator described below.
2. Review and pin the complete bundle containing adapter, updater, health, reconcile, onboarding, tests, and this document.
3. Provision dedicated scoped Composio connections separately on a test instance.
4. Run the read-only `--check` command.
5. Run the mocked/local test gates from the pinned bundle.
6. Enable one read-only fleet instance and observe a real sync and heartbeat.
7. Roll out read-only instances in approved batches.
8. Separately approve the one reconcile write connection before enabling its explicit write gate.

Rollback must also use a reviewed, pinned bundle. Disable affected schedules first, restore the previously approved code/config bundle, and run its documented read-only verification before re-enabling schedules. Do not restore legacy PAT distribution as an automatic rollback. Preserve manifests and `.revoked` files for review; do not delete them blindly.

## Blocking legacy generator

`/opt/data/scripts/gen_onboard_file.py` is outside this repository and is reported to embed GitHub PAT material.

**This is a blocking rollout dependency. Do not enable Composio-based onboarding or claim fleet migration complete until that generator is replaced or disabled and its generated artifacts are reviewed.**

This repository does not modify that live file. The replacement must distribute reviewed bundle instructions and per-instance scoped-connection provisioning steps, not credentials or a shared human Composio session.

## Local verification gates

All tests use mocked subprocess/network behavior and temporary homes:

```bash
python3 .github/lint.py
python3 -m unittest discover -s tests -v
git diff --check
```

The real `python3 scripts/pcg_github.py --check` command is deliberately excluded from automated local tests and must be run only by an authorized rollout operator.
