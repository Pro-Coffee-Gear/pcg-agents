# GitHub access through centrally authorized Composio MCP sessions

## Status: code complete locally; authorization and rollout still blocked

This repository contains the client-side revision only. It does not prove that Composio currently enforces the intended per-person permissions, and it does not mean fleet rollout is complete.

Two external requirements remain unverified and blocking:

1. An administrator must centrally provision each person's Composio account, GitHub connection, MCP session, and exposed direct tools.
2. An authorized operator must demonstrate server-side allow, deny, and revocation behavior with real sessions. The unit tests use mocks and are not proof of live Composio enforcement.

## Evidence: parent live pilot vs mock build tests

The unit tests in this repository are mocks. They prove client behavior only and are not proof of live Composio enforcement.

Separately, the parent operator ran a live administrative pilot with a single central admin credential (parent-only probe, outside this repository). That pilot verified, on the real service:

- a session centrally allowed `HACKERNEWS_GET_USER` executed it, while a distinct session exposing only `HACKERNEWS_GET_ITEM` was denied the same slug (`MCP error -32602`, tool not found);
- a central policy update revoked a previously allowed tool with no client URL/config change, and the original policy was then restored and read back;
- raw session proxy execution was disabled server-side (HTTP 403, code 4327, slug `ToolRouterV2_ProxyExecutionDisabled`);
- the session MCP endpoint without authentication returned HTTP 401 "API key is required".

Both pilot sessions used the same central admin credential. This is server-policy evidence, not separate-user authentication proof: it does not demonstrate that per-person scoped credentials exist, that one person cannot use another person's session, or that a least-privilege execution-only credential is available. No project GitHub auth configs or connected accounts existed at pilot inspection time, so no GitHub tool has been exercised live. Rollout remains blocked until real scoped teammate credentials are verified server-side.

No production configuration, login, session creation, deployment, cron mutation, or live API probe was performed by this revision. The earlier CLI migration did not implement this central-session model and must not be cited as proof that it did.

## Authorization model

Every person gets their own centrally provisioned Composio session. GitHub connections remain company-owned; central administration selects which connection a session may use — the box never selects an account, and this document does not require each person to own a separate GitHub connection. The application code is identical on every box. Identity and connected-account selection are bound by the provisioned MCP session, not passed by the client.

A `user_id` string in a session-creation payload is a central provisioning label. It is not, by itself, a separate authenticated Composio human, and distinct labels do not prove per-person isolation. Safe identity binding (one session credential usable only by its intended person, with individually revocable person/job access) must be demonstrated server-side before rollout.

Composio is the authorization authority. Central administration must decide which named direct tools each person's session can execute. Repository and branch values sent by an application describe business targets; they are not local authorization rules. URL validation, response validation, and supported-route translation are security and compatibility controls, not business authorization.

The client deliberately has no:

- hardcoded user, role, connected-account, repository-permission, or allowed-tool list;
- local write-enable flag;
- client-supplied transport or session identity (identity is bound by the provisioned session; the MCP `tools/call` envelope carries only the tool name and caller-supplied business arguments, and argument keys such as `user_id` that belong to an app's own data model pass through as data);
- administrative session APIs (create/patch/delete session) or any proxy/workbench fallback reachable from generic tool dispatch;
- Composio CLI, proxy, direct unscoped execute API, or PAT fallback;
- automatic fallback to a default GitHub branch;
- fallback to broad proxy or meta/workbench tools when a direct tool is missing;
- mutation retry, rebase, or force-update behavior.

A server denial is terminal for that call. A missing direct tool is a provisioning failure. Neither condition triggers a broader fallback.

## Central MCP provisioning contract

The exact session-creation payload verified in the parent live pilot (read-only scratch source `policy_pilot.py`, never a credential store) is a `POST /api/v3.1/tool_router/session` body with:

- `user_id`: a central provisioning label for the person or automation role;
- `toolkits`: `enable` limited to the required toolkit(s);
- `tools`: per-toolkit `enable` allowlist naming only the approved direct tools;
- `manage_connections`: `enable: false`, `enable_connection_removal: false`;
- `workbench`: `enable: false`, `enable_tool_execution: false`, `enable_proxy_execution: false`;
- `preload`: `tools: "all"`;
- `search`: `enable: false`;
- `execute`: `enable_multi_execute: false`.

An earlier draft of this document described an `mcp: true` / direct-tools preset; that preset was not used in the verified pilot and is not the grounded contract. The response `mcp.url` is the session endpoint (`https://backend.composio.dev/tool_router/<session-id>/mcp` in the pilot) and is itself a bearer secret stored privately at mode 0600.

The generic client calls each tool slug through MCP `tools/call`; it does not call `tools/list` as a local permission gate. Server execution remains authoritative even if a tool name is known to the application.

Read workflows currently require these direct tools:

- `GITHUB_GET_A_REPOSITORY`
- `GITHUB_GET_A_REFERENCE`
- `GITHUB_GET_A_TREE`
- `GITHUB_GET_REPOSITORY_CONTENT`

The Approved-deliverables snapshot writer additionally requires:

- `GITHUB_GET_COMMIT_OBJECT`
- `GITHUB_CREATE_A_BLOB`
- `GITHUB_CREATE_A_TREE`
- `GITHUB_CREATE_A_COMMIT`
- `GITHUB_UPDATE_A_REFERENCE`
- `GITHUB_GET_A_BLOB`

Do not expose `GITHUB_CREATE_OR_UPDATE_FILE_CONTENTS` to this writer. Its inspected `20260916_00` behavior can retry a 409 with the latest SHA and can fall back from a nonexistent branch to the default branch. Both behaviors violate the writer's concurrency contract.

This document does not claim that a particular dashboard screen supports these controls. A centrally stored policy administered through a verified Composio administrative API is acceptable. The administrator must retain evidence of the actual mechanism and the resulting server behavior.

## Per-person session configuration

Each box needs a separately provisioned mode-0600 JSON file owned by the local user:

```json
{
  "url": "https://backend.composio.dev/tool_router/<session-id>/mcp",
  "headers": {
    "Authorization": "Bearer <per-person-session-credential>"
  }
}
```

The literal values above are placeholders, not a token format claim. Use only the URL and headers returned by the verified provisioning flow. Set a local pointer and an application interpreter:

```text
PCG_COMPOSIO_SESSION_FILE=/absolute/private/path/member-session.json
PCG_COMPOSIO_PYTHON=/opt/hermes/.venv/bin/python
```

The client accepts HTTPS endpoints only on the exact official hosts `backend.composio.dev` (returned by the real create-session API) and `mcp.composio.dev` (documented MCP host). It rejects lookalike hosts, userinfo, nonstandard ports, fragments, private/untrusted hosts, and disables HTTP redirects. It never prints the endpoint or headers because the URL itself may contain a bearer secret. Transport validation is a security control, not business authorization.

`x-api-key`, `x-composio-api-key`, `x-user-api-key`, `x-org-api-key`, `x-org-id`, `x-project-id`, and equivalent administration headers are rejected in teammate session files. A Composio administration key is not a teammate session credential and must not be distributed to boxes; the pilot client needed no headers beyond the session URL itself. These local header checks are a fail-closed guard against obvious admin-key misuse: they do not enforce permissions, do not prove an opaque bearer credential is safe, and do not prove that every project-scoped key is always full-access. Only server-side verification of the actual credential's least-privilege scope can prove that, and it remains a production dependency. If the actual hosted MCP deployment requires a privileged project key at the client, deployment is blocked until a safe server-mediated or otherwise scoped credential-delivery design is proven. Do not invent a scoped token.

The session file must be provisioned separately for that person. Onboarding records only its local path and the interpreter path; it does not copy the file, URL, headers, another person's auth store, or credentials into generated bundles or shared profiles. Offboarding removes local environment pointers but does not delete the separately managed session file; central revocation remains required.

## Runtime dependency

The dedicated dependency declaration is:

```text
requirements-composio.txt: mcp==2.0.0
```

Use a separate application virtual environment containing that exact dependency, or the provided Hermes runtime interpreter:

```text
/opt/hermes/.venv/bin/python
```

Do not install packages into or otherwise mutate the managed Hermes environment. The onboarding preflight verifies that the selected interpreter reports `mcp==2.0.0`. It installs an explicit `pcg-fleet-sync.sh` launcher that invokes the validated interpreter, and registers that launcher rather than silently scheduling `pcg_sync.py` under an unknown `python3`.

An existing `pcg-fleet-sync` job that does not identify the launcher is a migration blocker and must be replaced by an operator; onboarding fails rather than silently retaining it.

## Generic client behavior

`scripts/pcg_composio.py` is application-neutral. `ComposioClient.execute(tool_slug, arguments)` creates one short-lived MCP streamable-HTTP transport and one initialized official `mcp.ClientSession` per call. A whole-call asyncio timeout covers connection, initialization, tool execution, and teardown. Mutations are not retried.

The client supports Composio results in either MCP `structuredContent` or one `content[]` text JSON item. It:

- bounds arguments and response payloads;
- rejects malformed and non-JSON result shapes;
- validates MCP `isError` and Composio `successful`, `error`/`errors`, and explicit status fields;
- treats MCP plain-text errors (for example `MCP error -32602: Tool X not found`) as typed failures using only the structured JSON-RPC code prefix — never as malformed success payloads and never as a provider 404;
- preserves an explicitly typed provider status;
- distinguishes server/session denial from a typed provider 404;
- sanitizes timeout, transport, provider, and denial exceptions.

It does not parse provider error prose to infer a status. In particular, authorization denial is never converted to “missing file” based on a message string.

## GitHub compatibility adapter

`scripts/pcg_github.py` is a thin translator over the generic client. It preserves the existing `.get`, `.get_contents`, `.read_file`, and `.put_contents` interfaces used by fleet sync, health, and deliverables reconcile.

Known REST-shaped read paths translate to named direct tools. Unknown paths fail as unsupported compatibility API; that is not represented as a server authorization decision. Owner, repository, path, ref, and branch are validated as workflow inputs. The generic transport remains unaware of GitHub repositories.

File reads retain exact base64, nonnegative integer size, truncation, and immutable blob-SHA validation.

## Snapshot write concurrency contract

The writer performs one low-level Git Data transaction without retry:

1. Resolve the requested `heads/<branch>` once to head `H`. A missing branch fails; there is no default-branch fallback.
2. Read the target at immutable `H` and verify the caller's expected blob SHA, or obtain a typed provider 404 for creation.
3. Read commit `H`, obtain its tree, and fetch a complete recursive tree. Creation additionally requires the target path to be absent from this complete tree.
4. Create the desired base64 blob and read it back by immutable SHA.
5. Create a tree with the verified commit tree as `base_tree` and one exact target-path entry. Read the new tree back and prove every non-target entry is unchanged.
6. Create a commit with exactly parent `H` and the verified new tree. Read the commit back and verify its SHA, tree, and parent.
7. Update `heads/<branch>` once with `force: false`.
8. Read the reference back and require it to point to the new commit. Read the target at the immutable new commit and verify exact content and blob SHA.

A stale head typically causes GitHub to reject the non-fast-forward update with 409 or 422. The client does not retry, rebase, fetch a newer SHA, or force the ref. A failed ref update can leave unreachable immutable blob/tree/commit objects; those objects are harmless and require no destructive cleanup by this workflow.

The returned compatibility shape is:

```json
{
  "commit": {"sha": "<commit>", "html_url": "<canonical GitHub commit URL>"},
  "content": {"sha": "<blob>"}
}
```

The reconcile caller still reads the immutable commit back before reporting success.

## Onboarding and fleet safety

The reviewed distribution bundle now contains all three files:

```text
scripts/pcg_composio.py
scripts/pcg_github.py
scripts/pcg_sync.py
```

The per-person session file is pre-provisioned separately and is never part of this bundle.

Live onboarding validates the bundle, session-file security and endpoint, and MCP runtime before any storage, profile, roster, or cron mutation. It then proves repository and representative private-file read access before making local changes. Completion is written to the roster last.

`--dry-run` performs local prerequisite checks only. It makes no network calls and performs no mutation.

Fleet sync retains the independent-review guarantees from the previous migration: repository probe, one immutable main head, complete tree, blob verification, path/manifest safety, all remote fetches before local mutation, and heartbeat only after success. Root and distribution updater copies remain byte-identical. The generic client, adapter, and updater are installed before the explicit-runtime cron is registered.

## Safe read-only live check

After an administrator has provisioned one real per-person session and central policy, run:

```bash
PCG_COMPOSIO_SESSION_FILE=/absolute/private/path/member-session.json \
/opt/hermes/.venv/bin/python scripts/pcg_github.py --check
```

The command performs a real network call. It reads repository metadata and `health.toml` only. It does not write, create a session, log in, list tools as a permission gate, or print URL/header values.

Passing this read check proves only that this session can perform those reads. It does not prove read-only enforcement, write authorization, another person's isolation, or revocation. The pending live server-side proof must include at least:

- a session centrally allowed to execute an approved direct tool;
- a distinct session centrally denied the same tool;
- revocation taking effect without a client/code/config change;
- provider 404 remaining distinguishable from authorization denial;
- a missing direct tool failing without proxy/workbench fallback;
- a safe sandbox write proving the low-level output shapes and 409/422 concurrency behavior before any production write is enabled.

No real write was performed for this revision.

## Rollout blockers and rollback

Rollout remains blocked by both the pending central authorization proof above and the external legacy invitation generator:

`/opt/data/scripts/gen_onboard_file.py`

That file is outside this repository and is reported to embed GitHub PAT material. It was not changed here. Do not enable this rollout or claim migration complete until the generator is replaced or disabled and its generated artifacts are reviewed.

Rollout should use one reviewed immutable bundle. Start with one read-only test instance, run the live checks, verify central denial and revocation, then stage read-only instances. Approve the snapshot writer separately only after sandbox output-shape and concurrency checks pass.

For rollback, pause affected schedules, restore the previously reviewed bundle and job definition, and run its documented read-only check before re-enabling. Do not restore PAT distribution. Preserve manifests and `.revoked` files for review.

## Local verification

All local authorization and network behavior is mocked; fixtures use temporary directories:

```bash
python3 .github/lint.py
python3 -m unittest discover -s tests -v
/opt/hermes/.venv/bin/python -m unittest discover -s tests -v
git diff --check
```

These gates verify client logic only. They are not live Composio authorization evidence.
