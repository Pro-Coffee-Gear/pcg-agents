"""PCG fleet policy: completed work must be visible in Notion."""
from __future__ import annotations

import json
from typing import Any

PLUGIN_NAME = "pcg-deliverable-autoregistration"
PROMPT_SECTION_ID = "pcg.deliverable-autoregistration"
SKIP_MARKER = "pcg catalog: not applicable"
PUBLISHER = "pcg-register-deliverable.py"

_registered_sessions: set[str] = set()

POLICY = """## PCG work-product catalog policy (mandatory)

When you create or materially update a durable Pro Coffee Gear work product—an app,
dashboard, automation, integration, report, document, model, spreadsheet, shared skill,
or scheduled job—catalog it in **Business Automations & Deliverables** before reporting
completion. Run `/opt/data/scripts/pcg-register-deliverable.py` with a stable business
name, owner email, function(s), type, purpose, and every source/deployment/test field that
is actually known. Leave unknown fields blank; never invent metadata. The publisher
upserts a **Proposed** row and cannot self-approve it.

This registration is part of the definition of done, not an optional judgment about
importance. Reuse the same name and owner when updating existing work so its row is
updated instead of duplicated. The only exemption is a temporary scratch file or a
throwaway test fixture. For that narrow case, state exactly
`PCG catalog: not applicable — scratch/test fixture` in the final response.
"""

NUDGE = """PCG catalog registration was not detected for the files changed in this turn.
Before finishing, run `/opt/data/scripts/pcg-register-deliverable.py` to create or update
the **Proposed** Business Automations & Deliverables row, then verify the returned Notion
URL. Use the current member's email from `/opt/data/.pcg_member_email` as submitter/owner
unless the work has a different known owner. Include only metadata you actually know.
If every changed path is exclusively temporary scratch or a throwaway test fixture, do
not create a row; instead include exactly `PCG catalog: not applicable — scratch/test
fixture` in the final response."""


def _result_text(result: Any) -> str:
    if isinstance(result, str):
        return result
    try:
        return json.dumps(result, default=str)
    except Exception:
        return str(result)


def post_tool_call(
    tool_name: str = "",
    args: dict | None = None,
    result: Any = None,
    session_id: str = "",
    status: str = "",
    **_: Any,
) -> None:
    """Remember a successful publisher call for the current turn/session."""
    if tool_name not in {"terminal", "execute_code"} or not session_id:
        return
    raw_args = _result_text(args or {})
    if PUBLISHER not in raw_args or "--help" in raw_args:
        return
    text = _result_text(result)
    if status.lower() in {"error", "failed", "blocked"}:
        return
    if "Deliverable proposed:" in text or "Deliverable updated:" in text:
        _registered_sessions.add(session_id)


def pre_verify(
    session_id: str = "",
    coding: bool = False,
    attempt: int = 0,
    final_response: str = "",
    changed_paths: list[str] | None = None,
    **_: Any,
) -> dict[str, str] | None:
    """Keep a completed build turn open until cataloging is evidenced."""
    if not coding or not changed_paths:
        return None
    if session_id in _registered_sessions:
        _registered_sessions.discard(session_id)
        return None
    if SKIP_MARKER in (final_response or "").lower():
        return None
    if attempt >= 2:
        return None
    return {"action": "continue", "message": NUDGE}


def on_session_end(session_id: str = "", **_: Any) -> None:
    _registered_sessions.discard(session_id)


def register(ctx: Any) -> None:
    ctx.register_system_prompt_section(
        PROMPT_SECTION_ID,
        POLICY,
        position="after_memory",
        max_chars=3000,
    )
    ctx.register_hook("post_tool_call", post_tool_call)
    ctx.register_hook("pre_verify", pre_verify)
    ctx.register_hook("on_session_end", on_session_end)
