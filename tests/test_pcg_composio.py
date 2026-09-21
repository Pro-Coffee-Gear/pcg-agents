"""Mock-only tests for the generic Composio MCP session client.

These tests prove client behavior, not live Composio authorization. No network or
credential store is accessed; every session file lives in a temporary directory.
"""
from __future__ import annotations

import asyncio
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pcg_composio import (  # noqa: E402
    ComposioClient,
    ComposioError,
    load_session_config,
)


class FakeResult:
    def __init__(self, *, structured=None, content=None, is_error=False):
        self.structuredContent = structured
        self.content = content or []
        self.isError = is_error


class FakeSession:
    def __init__(self, result, *, delay=0):
        self.result = result
        self.delay = delay
        self.calls = []
        self.initialized = 0

    async def initialize(self):
        self.initialized += 1

    async def call_tool(self, name, arguments=None):
        self.calls.append((name, arguments))
        if self.delay:
            await asyncio.sleep(self.delay)
        if isinstance(self.result, BaseException):
            raise self.result
        return self.result


class FakeSessionContext:
    def __init__(self, session):
        self.session = session

    async def __aenter__(self):
        return self.session

    async def __aexit__(self, exc_type, exc, tb):
        return False


def session_file(directory: str, *, url=None, headers=None, mode=0o600) -> Path:
    path = Path(directory) / "session.json"
    path.write_text(json.dumps({
        "url": url or "https://mcp.composio.dev/session-secret/mcp",
        "headers": headers if headers is not None else {"Authorization": "Bearer session-secret"},
    }))
    os.chmod(path, mode)
    return path


def client_for(config, session, **kwargs):
    return ComposioClient(
        config,
        session_factory=lambda *_: FakeSessionContext(session),
        **kwargs,
    )


class CentralAuthorizationBehaviorTests(unittest.TestCase):
    def test_same_client_code_obeys_each_fake_servers_central_decision(self):
        allowed_session = FakeSession(FakeResult(structured={
            "successful": True, "data": {"value": 7}, "error": None, "logId": "allowed"
        }))
        denied_session = FakeSession(FakeResult(
            structured={"code": "FORBIDDEN", "message": "central policy denied secret-user"},
            is_error=True,
        ))
        with tempfile.TemporaryDirectory() as tmp:
            config = session_file(tmp)
            allowed = client_for(config, allowed_session)
            denied = client_for(config, denied_session)
            self.assertEqual({"value": 7}, allowed.execute("EXAMPLE_TOOL", {"input": "same"}))
            with self.assertRaises(ComposioError) as raised:
                denied.execute("EXAMPLE_TOOL", {"input": "same"})
        self.assertEqual("session_denied", raised.exception.kind)
        self.assertNotIn("secret-user", str(raised.exception))
        self.assertEqual([("EXAMPLE_TOOL", {"input": "same"})], allowed_session.calls)
        self.assertEqual([("EXAMPLE_TOOL", {"input": "same"})], denied_session.calls)

    def test_server_revocation_fails_immediately_without_retry(self):
        sessions = [
            FakeSession(FakeResult(structured={"successful": True, "data": {"ok": True}, "error": None})),
            FakeSession(FakeResult(structured={"code": "ACCESS_DENIED"}, is_error=True)),
        ]

        def factory(*_):
            return FakeSessionContext(sessions.pop(0))

        with tempfile.TemporaryDirectory() as tmp:
            client = ComposioClient(session_file(tmp), session_factory=factory)
            self.assertEqual({"ok": True}, client.execute("APP_MUTATION", {"value": 1}))
            with self.assertRaises(ComposioError) as raised:
                client.execute("APP_MUTATION", {"value": 2})
        self.assertEqual("session_denied", raised.exception.kind)
        self.assertEqual([], sessions)

    def test_arbitrary_named_app_tool_is_dispatched_without_local_tool_list(self):
        session = FakeSession(FakeResult(structured={
            "successful": True, "data": {"calendar": "ok"}, "error": None
        }))
        with tempfile.TemporaryDirectory() as tmp:
            result = client_for(session_file(tmp), session).execute(
                "GOOGLECALENDAR_FIND_EVENT", {"query": "planning"}
            )
        self.assertEqual({"calendar": "ok"}, result)
        self.assertEqual([("GOOGLECALENDAR_FIND_EVENT", {"query": "planning"})], session.calls)

    def test_business_identity_fields_pass_verbatim_and_transport_injects_nothing(self):
        # user_id/account_id are legitimate business argument names (for example a
        # CRM payload). Identity is bound by the provisioned session; the MCP
        # tools/call envelope carries only the tool name and caller arguments, and
        # this client injects no identity into arguments, URL, or headers.
        session = FakeSession(FakeResult(structured={"successful": True, "data": {"ok": 1}, "error": None}))
        opened = []
        arguments = {
            "user_id": "crm-contact-123",
            "nested": {"account_id": "crm-org-9"},
            "tags": ["a"],
        }
        with tempfile.TemporaryDirectory() as tmp:
            config = session_file(tmp, headers={"Authorization": "Bearer session-secret"})

            def factory(url, headers):
                opened.append((url, dict(headers)))
                return FakeSessionContext(session)

            client = ComposioClient(config, session_factory=factory)
            self.assertEqual({"ok": 1}, client.execute("CRM_UPDATE_CONTACT", arguments))
        self.assertEqual([("CRM_UPDATE_CONTACT", arguments)], session.calls)
        [(url, headers)] = opened
        self.assertEqual({"Authorization": "Bearer session-secret"}, headers)
        self.assertNotIn("user_id", url)
        for name in headers:
            self.assertNotIn("user", name.lower())
            self.assertNotIn("account", name.lower())


class SessionConfigurationTests(unittest.TestCase):
    def test_only_exact_official_https_endpoint_is_accepted(self):
        bad_urls = (
            "http://mcp.composio.dev/a/mcp",
            "http://backend.composio.dev/tool_router/trs_1/mcp",
            "https://evil.example/a/mcp",
            "https://mcp.composio.dev.evil.example/a/mcp",
            "https://backend.composio.dev.evil.example/tool_router/trs_1/mcp",
            "https://user:secret@mcp.composio.dev/a/mcp",
            "https://user:secret@backend.composio.dev/tool_router/trs_1/mcp",
            "https://127.0.0.1/a/mcp",
            "https://mcp.composio.dev:444/a/mcp",
            "https://backend.composio.dev:8443/tool_router/trs_1/mcp",
            "https://mcp.composio.dev/a/mcp#fragment",
            "https://backend.composio.dev/tool_router/trs_1/mcp#fragment",
        )
        for index, url in enumerate(bad_urls):
            with self.subTest(url=url), tempfile.TemporaryDirectory() as tmp:
                path = session_file(tmp, url=url)
                with self.assertRaises(ValueError):
                    load_session_config(path)
        good_urls = (
            # Documented MCP host.
            "https://mcp.composio.dev:443/a/mcp?token=secret",
            # Exact host returned by the real create-session API.
            "https://backend.composio.dev/tool_router/trs_abc123/mcp",
        )
        for good in good_urls:
            with self.subTest(url=good), tempfile.TemporaryDirectory() as tmp:
                url, _ = load_session_config(session_file(tmp, url=good))
            self.assertIn(
                __import__("urllib.parse").parse.urlsplit(url).hostname,
                {"mcp.composio.dev", "backend.composio.dev"},
            )

    def test_mode_symlink_shape_and_extra_fields_fail_closed(self):
        with tempfile.TemporaryDirectory() as tmp:
            weak = session_file(tmp, mode=0o640)
            with self.assertRaises(ValueError):
                load_session_config(weak)
            os.chmod(weak, 0o600)
            link = Path(tmp) / "link.json"
            link.symlink_to(weak)
            with self.assertRaises(ValueError):
                load_session_config(link)
            weak.write_text(json.dumps({"url": "https://mcp.composio.dev/a", "headers": {}, "role": "admin"}))
            with self.assertRaises(ValueError):
                load_session_config(weak)

    def test_project_admin_key_headers_are_rejected_without_leaking_value(self):
        for name in (
            "x-api-key",
            "X-Composio-Api-Key",
            "api_key",
            "x-user-api-key",
            "X-Org-Api-Key",
            "x-org-id",
            "x-project-id",
        ):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                path = session_file(tmp, headers={name: "project-master-secret"})
                with self.assertRaises(ValueError) as raised:
                    load_session_config(path)
                self.assertNotIn("project-master-secret", str(raised.exception))

    def test_environment_factory_uses_only_explicit_session_file_pointer(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = session_file(tmp)
            with patch.dict(os.environ, {"PCG_COMPOSIO_SESSION_FILE": str(config)}, clear=True):
                client = ComposioClient.from_environment(session_factory=lambda *_: None)
            self.assertEqual(config, client.session_file)
        with patch.dict(os.environ, {"COMPOSIO_API_KEY": "must-not-be-used"}, clear=True):
            with self.assertRaises(ValueError):
                ComposioClient.from_environment()


class ResponseContractTests(unittest.TestCase):
    def execute_result(self, result, **kwargs):
        with tempfile.TemporaryDirectory() as tmp:
            return client_for(session_file(tmp), FakeSession(result), **kwargs).execute("EXAMPLE_TOOL", {})

    def test_structured_and_text_json_envelopes_are_supported(self):
        structured = FakeResult(structured={"successful": True, "data": {"typed": 1}, "error": None})
        text = FakeResult(content=[{
            "type": "text",
            "text": json.dumps({"successful": True, "data": [1, 2], "error": None}),
        }])
        self.assertEqual({"typed": 1}, self.execute_result(structured))
        self.assertEqual([1, 2], self.execute_result(text))

    def test_provider_404_is_typed_and_not_reported_as_session_denial(self):
        result = FakeResult(structured={
            "successful": False,
            "data": {"status_code": 404, "message": "provider-secret"},
            "error": "provider-secret",
            "logId": "log-secret",
        })
        with self.assertRaises(ComposioError) as raised:
            self.execute_result(result)
        self.assertEqual(404, raised.exception.status)
        self.assertEqual("provider_error", raised.exception.kind)
        self.assertNotIn("provider-secret", str(raised.exception))

    def test_auth_status_is_denial_and_missing_direct_tool_is_provisioning_failure(self):
        cases = [
            ({"successful": False, "status": 403, "error": "secret"}, "session_denied", 403),
            ({"code": "TOOL_NOT_FOUND", "message": "secret"}, "provisioning", None),
        ]
        for payload, kind, status in cases:
            with self.subTest(kind=kind), self.assertRaises(ComposioError) as raised:
                self.execute_result(FakeResult(structured=payload, is_error=True))
            self.assertEqual(kind, raised.exception.kind)
            self.assertEqual(status, raised.exception.status)

    def test_success_false_errors_bad_status_and_malformed_shapes_fail(self):
        results = (
            FakeResult(structured={"successful": False, "data": {}, "error": None}),
            FakeResult(structured={"errors": [{"private": "detail"}]}),
            FakeResult(structured={"status": "422", "data": {}}),
            FakeResult(structured={"successful": True, "error": None}),
            FakeResult(structured="scalar"),
            FakeResult(content=[]),
            FakeResult(content=[{"type": "image", "text": "{}"}]),
            FakeResult(content=[{"type": "text", "text": "not-json"}]),
        )
        for index, result in enumerate(results):
            with self.subTest(index=index), self.assertRaises(ComposioError):
                self.execute_result(result)

    def test_plain_text_mcp_error_is_typed_provisioning_not_malformed_or_404(self):
        # Real denied direct-tool response shape:
        # CallToolResult(is_error=True, content=[TextContent(text=
        #   'MCP error -32602: Tool HACKERNEWS_GET_USER not found')])
        result = FakeResult(
            content=[{
                "type": "text",
                "text": "MCP error -32602: Tool HACKERNEWS_GET_USER not found",
            }],
            is_error=True,
        )
        with self.assertRaises(ComposioError) as raised:
            self.execute_result(result)
        self.assertEqual("provisioning", raised.exception.kind)
        self.assertIsNone(raised.exception.status)
        self.assertNotIn("HACKERNEWS_GET_USER", str(raised.exception))

    def test_plain_text_mcp_errors_never_become_success_or_provider_404(self):
        cases = [
            ("MCP error -32601: Unknown tool", "provisioning"),
            ("MCP error -32000: upstream exploded", "tool_error"),
            ("totally unstructured failure prose", "tool_error"),
        ]
        for text, kind in cases:
            with self.subTest(text=text), self.assertRaises(ComposioError) as raised:
                self.execute_result(FakeResult(
                    content=[{"type": "text", "text": text}],
                    is_error=True,
                ))
            self.assertEqual(kind, raised.exception.kind)
            self.assertNotEqual(404, raised.exception.status)

    def test_json_text_error_retains_explicit_typed_codes(self):
        forbidden = FakeResult(
            content=[{"type": "text", "text": json.dumps({"code": "FORBIDDEN", "message": "secret"})}],
            is_error=True,
        )
        with self.assertRaises(ComposioError) as raised:
            self.execute_result(forbidden)
        self.assertEqual("session_denied", raised.exception.kind)
        self.assertNotIn("secret", str(raised.exception))

    def test_real_mcp_result_classes_use_snake_case_and_parse_correctly(self):
        # Proves behavior against the actual installed mcp==2.0.0 classes, whose
        # Python attributes are snake_case while wire keys stay camelCase.
        try:
            from mcp import types
        except ImportError:
            self.skipTest("mcp runtime is not installed for this interpreter")
        denied = types.CallToolResult(
            content=[types.TextContent(
                type="text",
                text="MCP error -32602: Tool HACKERNEWS_GET_USER not found",
            )],
            isError=True,
        )
        self.assertTrue(denied.is_error)
        self.assertIn("isError", denied.model_dump(by_alias=True))
        with self.assertRaises(ComposioError) as raised:
            self.execute_result(denied)
        self.assertEqual("provisioning", raised.exception.kind)
        self.assertIsNone(raised.exception.status)

        succeeded = types.CallToolResult(
            content=[],
            structuredContent={"successful": True, "data": {"ok": 1}, "error": None},
        )
        self.assertEqual({"ok": 1}, self.execute_result(succeeded))

    def test_response_size_is_bounded(self):
        result = FakeResult(structured={"successful": True, "data": {"value": "x" * 200}, "error": None})
        with self.assertRaises(ComposioError) as raised:
            self.execute_result(result, max_response_bytes=100)
        self.assertEqual("oversize", raised.exception.kind)


class RuntimeAndFailureTests(unittest.TestCase):
    def test_whole_call_timeout_and_transport_failures_are_sanitized(self):
        with tempfile.TemporaryDirectory() as tmp:
            config = session_file(tmp)
            slow = FakeSession(FakeResult(structured={"successful": True, "data": {}, "error": None}), delay=0.05)
            with self.assertRaises(ComposioError) as raised:
                client_for(config, slow, timeout=0.001).execute("MUTATION", {})
            self.assertEqual("timeout", raised.exception.kind)

            failed = FakeSession(RuntimeError("transport token=secret"))
            with self.assertRaises(ComposioError) as raised:
                client_for(config, failed).execute("MUTATION", {})
            self.assertEqual("transport", raised.exception.kind)
            self.assertNotIn("secret", str(raised.exception))
            self.assertEqual(1, len(failed.calls))

    def test_mcp_is_deferred_and_requirement_is_pinned(self):
        requirement = (ROOT / "requirements-composio.txt").read_text().splitlines()
        self.assertEqual(["mcp==2.0.0"], [line for line in requirement if line and not line.startswith("#")])
        command = [
            sys.executable,
            "-I",
            "-c",
            f"import sys; sys.path.insert(0, {str(SCRIPTS)!r}); import pcg_composio",
        ]
        result = subprocess.run(command, capture_output=True, text=True, timeout=10, check=False)
        self.assertEqual(0, result.returncode, result.stderr)

    def test_provided_hermes_runtime_has_expected_mcp_version(self):
        interpreter = Path("/opt/hermes/.venv/bin/python")
        self.assertTrue(interpreter.is_file())
        result = subprocess.run(
            [str(interpreter), "-c", "import importlib.metadata; print(importlib.metadata.version('mcp'))"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("2.0.0", result.stdout.strip())

    def test_production_transport_disables_redirects(self):
        source = (SCRIPTS / "pcg_composio.py").read_text()
        self.assertIn("follow_redirects=False", source)
        self.assertIn("backend.composio.dev", source)
        self.assertIn("mcp.composio.dev", source)
        self.assertNotIn("tools/list", source)
        self.assertNotIn("connected_account_id", source)
        self.assertNotIn("GITHUB_CREATE_OR_UPDATE_FILE_CONTENTS", source)


if __name__ == "__main__":
    unittest.main()
