#!/usr/bin/env python3
"""Generic, centrally authorized Composio direct-tool client over MCP.

The client carries no business authorization policy. A separately provisioned MCP
session binds the person, connected accounts, and exposed direct tools; the server's
decision is authoritative for every call.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import stat
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncContextManager, Callable
from urllib.parse import urlsplit

DEFAULT_TIMEOUT = 30.0
DEFAULT_MAX_RESPONSE_BYTES = 2 * 1024 * 1024
MAX_CONFIG_BYTES = 64 * 1024
# Real create-session responses return https://backend.composio.dev/tool_router/<id>/mcp;
# mcp.composio.dev is the documented MCP host. Both exact hosts are accepted.
OFFICIAL_MCP_HOSTS = frozenset({"mcp.composio.dev", "backend.composio.dev"})
_TOOL_RE = re.compile(r"^[A-Za-z][A-Za-z0-9_.:-]{0,127}$")
_PROJECT_KEY_HEADERS = frozenset({
    "apikey",
    "xapikey",
    "composioapikey",
    "xcomposioapikey",
    "xuserapikey",
    "xorgapikey",
    "xorgid",
    "xprojectid",
})
_MCP_ERROR_CODE_RE = re.compile(r"^MCP error (-?\d+)\b")


class ComposioError(RuntimeError):
    """Sanitized MCP/session/provider failure."""

    def __init__(self, message: str, *, kind: str = "tool_error", status: int | None = None):
        super().__init__(message)
        self.kind = kind
        self.status = status


def _normalized_name(value: str) -> str:
    return "".join(character for character in value.lower() if character.isalnum())


def _validate_url(url: object) -> str:
    if not isinstance(url, str) or not url:
        raise ValueError("Composio session URL is missing")
    parsed = urlsplit(url)
    try:
        port = parsed.port
    except ValueError:
        raise ValueError("Composio session URL is invalid") from None
    if (
        parsed.scheme != "https"
        or parsed.hostname not in OFFICIAL_MCP_HOSTS
        or parsed.username is not None
        or parsed.password is not None
        or port not in (None, 443)
        or not parsed.path.startswith("/")
        or parsed.fragment
    ):
        raise ValueError("Composio session URL is not an approved HTTPS endpoint")
    return url


def _validate_headers(headers: object) -> dict[str, str]:
    if not isinstance(headers, dict):
        raise ValueError("Composio session headers must be an object")
    clean: dict[str, str] = {}
    for name, value in headers.items():
        if not isinstance(name, str) or not name or not isinstance(value, str) or not value:
            raise ValueError("Composio session headers are invalid")
        if any(ord(character) < 33 or ord(character) > 126 for character in name):
            raise ValueError("Composio session headers are invalid")
        if any(character in value for character in "\r\n\x00"):
            raise ValueError("Composio session headers are invalid")
        if _normalized_name(name) in _PROJECT_KEY_HEADERS:
            raise ValueError("project API keys are not accepted as teammate session credentials")
        clean[name] = value
    return clean


def load_session_config(path: str | os.PathLike[str]) -> tuple[str, dict[str, str]]:
    """Read one mode-0600 per-person session file without consulting auth stores."""
    config_path = Path(path)
    if not config_path.is_absolute():
        raise ValueError("PCG_COMPOSIO_SESSION_FILE must be an absolute path")
    try:
        metadata = config_path.lstat()
    except OSError:
        raise ValueError("Composio session configuration is unavailable") from None
    if not stat.S_ISREG(metadata.st_mode) or stat.S_IMODE(metadata.st_mode) != 0o600:
        raise ValueError("Composio session configuration must be a mode 0600 regular file")
    if hasattr(os, "geteuid") and metadata.st_uid != os.geteuid():
        raise ValueError("Composio session configuration must be owned by this user")
    if metadata.st_size <= 0 or metadata.st_size > MAX_CONFIG_BYTES:
        raise ValueError("Composio session configuration has an invalid size")
    try:
        raw = config_path.read_bytes()
        config = json.loads(raw)
    except (OSError, UnicodeError, json.JSONDecodeError):
        raise ValueError("Composio session configuration is malformed") from None
    if not isinstance(config, dict) or set(config) != {"url", "headers"}:
        raise ValueError("Composio session configuration must contain only url and headers")
    return _validate_url(config["url"]), _validate_headers(config["headers"])


def _validate_arguments(value: object, *, depth: int = 0) -> None:
    """Check JSON compatibility only; never strip or rewrite business arguments.

    Argument keys such as user_id or account_id are legitimate application
    business fields (for example a CRM payload), not Composio identity. Identity
    is bound by the provisioned session URL/credential: the MCP tools/call
    envelope carries only the tool name and these caller-supplied arguments, and
    this client injects no identity into arguments, URL, or headers.
    """
    if depth > 40:
        raise ValueError("Composio tool arguments are too deeply nested")
    if isinstance(value, dict):
        for key, child in value.items():
            if not isinstance(key, str):
                raise ValueError("Composio tool argument keys must be strings")
            _validate_arguments(child, depth=depth + 1)
    elif isinstance(value, list):
        for child in value:
            _validate_arguments(child, depth=depth + 1)
    elif value is None or isinstance(value, (str, int, float, bool)):
        return
    else:
        raise ValueError("Composio tool arguments must be JSON-compatible")


def _typed_status(payload: dict[str, Any]) -> int | None:
    for container in (payload, payload.get("data")):
        if not isinstance(container, dict):
            continue
        for key in ("status_code", "status"):
            value = container.get(key)
            if isinstance(value, int) and not isinstance(value, bool):
                return value
            if isinstance(value, str) and value.isdigit():
                return int(value)
    return None


def _failure_kind(status: int | None, code: object = None) -> str:
    normalized = _normalized_name(str(code)) if code is not None else ""
    if status in (401, 403) or normalized in {"forbidden", "unauthorized", "accessdenied", "permissiondenied"}:
        return "session_denied"
    if normalized in {"methodnotfound", "toolnotfound"} or code in (-32601, -32602):
        return "provisioning"
    if status is not None:
        return "provider_error"
    return "tool_error"


class ComposioClient:
    """Execute named tools through one short-lived initialized MCP session."""

    def __init__(
        self,
        session_file: str | os.PathLike[str],
        *,
        timeout: int | float = DEFAULT_TIMEOUT,
        max_response_bytes: int = DEFAULT_MAX_RESPONSE_BYTES,
        session_factory: Callable[[str, dict[str, str]], AsyncContextManager[Any]] | None = None,
    ) -> None:
        if not isinstance(timeout, (int, float)) or isinstance(timeout, bool) or timeout <= 0:
            raise ValueError("Composio timeout must be positive")
        if not isinstance(max_response_bytes, int) or isinstance(max_response_bytes, bool) or max_response_bytes <= 0:
            raise ValueError("Composio response limit must be positive")
        self.session_file = Path(session_file)
        self.timeout = float(timeout)
        self.max_response_bytes = max_response_bytes
        self._session_factory = session_factory or self._open_mcp_session
        # Fail before any network activity; execution re-reads the file so rotation and
        # revocation take effect on the next short-lived session.
        load_session_config(self.session_file)

    @classmethod
    def from_environment(cls, **kwargs: Any) -> "ComposioClient":
        path = (os.environ.get("PCG_COMPOSIO_SESSION_FILE") or "").strip()
        if not path:
            raise ValueError("PCG_COMPOSIO_SESSION_FILE is required")
        return cls(path, **kwargs)

    @asynccontextmanager
    async def _open_mcp_session(self, url: str, headers: dict[str, str]):
        """Deferred imports keep stdlib-only tests and dry runs importable."""
        try:
            import httpx2
            from mcp import ClientSession
            from mcp.client.streamable_http import streamable_http_client
        except ImportError:
            raise ComposioError(
                "MCP runtime dependency is unavailable; use the configured application interpreter",
                kind="dependency",
            ) from None

        # Redirects stay disabled so credentials cannot follow a Location header.
        http_timeout = httpx2.Timeout(self.timeout)
        async with httpx2.AsyncClient(
            headers=headers,
            timeout=http_timeout,
            follow_redirects=False,
            trust_env=False,
        ) as http_client:
            async with streamable_http_client(url, http_client=http_client) as streams:
                read_stream, write_stream = streams
                async with ClientSession(read_stream, write_stream) as session:
                    yield session

    def execute(self, tool_slug: str, arguments: dict[str, Any]) -> Any:
        """Synchronous named-tool dispatch. Mutating calls are never retried."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return asyncio.run(self.execute_async(tool_slug, arguments))
        raise RuntimeError("execute() cannot run inside an active event loop; use execute_async()")

    async def execute_async(self, tool_slug: str, arguments: dict[str, Any]) -> Any:
        if not isinstance(tool_slug, str) or not _TOOL_RE.fullmatch(tool_slug):
            raise ValueError("Composio tool slug is invalid")
        if not isinstance(arguments, dict):
            raise ValueError("Composio tool arguments must be an object")
        _validate_arguments(arguments)
        try:
            encoded_arguments = json.dumps(arguments, separators=(",", ":"), allow_nan=False).encode()
        except (TypeError, ValueError):
            raise ValueError("Composio tool arguments must be valid JSON") from None
        if len(encoded_arguments) > self.max_response_bytes:
            raise ValueError("Composio tool arguments exceed the configured limit")
        url, headers = load_session_config(self.session_file)

        try:
            async with asyncio.timeout(self.timeout):
                async with self._session_factory(url, headers) as session:
                    await session.initialize()
                    result = await session.call_tool(tool_slug, arguments=arguments)
        except ComposioError:
            raise
        except TimeoutError:
            raise ComposioError("Composio tool call timed out", kind="timeout") from None
        except Exception as exc:
            error = getattr(exc, "error", None)
            code = getattr(exc, "code", getattr(error, "code", None))
            if code in (-32601, -32602):
                raise ComposioError(
                    "named tool is not exposed by the provisioned MCP session",
                    kind="provisioning",
                ) from None
            raise ComposioError("Composio MCP transport failed", kind="transport") from None
        return self._parse_result(result)

    def _parse_result(self, result: object) -> Any:
        is_error = bool(getattr(result, "is_error", getattr(result, "isError", False)))
        structured = getattr(result, "structured_content", None)
        if structured is None:
            structured = getattr(result, "structuredContent", None)

        text: str | None = None
        if structured is None:
            content = getattr(result, "content", None)
            if not isinstance(content, list) or len(content) != 1:
                raise ComposioError("Composio tool returned malformed content", kind="malformed")
            item = content[0]
            text_value = item.get("text") if isinstance(item, dict) else getattr(item, "text", None)
            item_type = item.get("type") if isinstance(item, dict) else getattr(item, "type", None)
            if item_type not in (None, "text") or not isinstance(text_value, str):
                raise ComposioError("Composio tool returned malformed content", kind="malformed")
            if len(text_value.encode("utf-8")) > self.max_response_bytes:
                raise ComposioError("Composio tool response exceeded the configured limit", kind="oversize")
            text = text_value

        if is_error:
            # MCP plain-text errors (for example 'MCP error -32602: Tool X not
            # found') are failures, never malformed success payloads. Only the
            # structured JSON-RPC code prefix is read; provider prose is never
            # parsed to infer a status such as a missing file.
            payload: Any = structured
            if payload is None and text is not None:
                try:
                    payload = json.loads(text)
                except json.JSONDecodeError:
                    payload = None
            code = payload.get("code") if isinstance(payload, dict) else None
            status = _typed_status(payload) if isinstance(payload, dict) else None
            if code is None and text is not None:
                match = _MCP_ERROR_CODE_RE.match(text)
                if match:
                    code = int(match.group(1))
            kind = _failure_kind(status, code)
            message = "Composio session denied the named tool" if kind == "session_denied" else (
                "named tool is not exposed by the provisioned MCP session"
                if kind == "provisioning"
                else "Composio named tool failed"
            )
            raise ComposioError(message, kind=kind, status=status)

        if structured is None:
            try:
                payload = json.loads(text if text is not None else "")
            except json.JSONDecodeError:
                raise ComposioError("Composio tool returned malformed JSON", kind="malformed") from None
        else:
            payload = structured

        try:
            encoded = json.dumps(payload, separators=(",", ":"), allow_nan=False).encode()
        except (TypeError, ValueError):
            raise ComposioError("Composio tool returned malformed data", kind="malformed") from None
        if len(encoded) > self.max_response_bytes:
            raise ComposioError("Composio tool response exceeded the configured limit", kind="oversize")
        if not isinstance(payload, (dict, list)):
            raise ComposioError("Composio tool returned malformed data", kind="malformed")

        if isinstance(payload, dict):
            status = _typed_status(payload)
            successful = payload.get("successful")
            errors = payload.get("errors")
            explicit_error = payload.get("error")
            failed = successful is False or bool(errors) or (explicit_error not in (None, "") and successful is not True)
            if failed or (status is not None and status >= 400):
                kind = _failure_kind(status, payload.get("code"))
                raise ComposioError("Composio named tool failed", kind=kind, status=status)
            if successful is True:
                if "data" not in payload or not isinstance(payload["data"], (dict, list)):
                    raise ComposioError("Composio tool returned malformed data", kind="malformed")
                return payload["data"]
        return payload


def execute(tool_slug: str, arguments: dict[str, Any], **kwargs: Any) -> Any:
    """Environment-configured convenience API reusable by application wrappers."""
    return ComposioClient.from_environment(**kwargs).execute(tool_slug, arguments)
