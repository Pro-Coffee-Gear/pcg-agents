"""Regression tests for the constrained Composio GitHub gateway.

All proxy and network behavior is mocked. No credential files outside temporary
homes are read.
"""
from __future__ import annotations

import base64
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))
VALID_SHA = "a" * 40
NEW_SHA = "b" * 40
COMMIT_SHA = "c" * 40

from pcg_github import (  # noqa: E402
    ComposioGitHub,
    GitHubError,
    REPO_API,
    SNAPSHOT_API,
    main as gateway_main,
    read_config,
)


class RecordingRunner:
    def __init__(self, stdout="{}", returncode=0, stderr=""):
        self.result = SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
        self.calls = []

    def __call__(self, command, **kwargs):
        if kwargs.get("text") and isinstance(kwargs.get("input"), bytes):
            raise AssertionError("text=True cannot receive bytes stdin")
        self.calls.append((command, kwargs))
        return self.result


def adapter(runner=None, allow=False):
    return ComposioGitHub(
        account="account_alias/id",
        cli_config="/reviewed/bin/composio",
        allow_snapshot_write=allow,
        runner=runner or RecordingRunner(),
    )


class ResponseContractTests(unittest.TestCase):
    def test_real_raw_proxy_dictionary_is_success(self):
        gateway = adapter()
        raw = json.dumps({"id": 123, "full_name": "WWWPCG/pcg-agents", "private": True})
        self.assertEqual("WWWPCG/pcg-agents", gateway.parse_response(raw)["full_name"])

    def test_real_raw_proxy_list_is_success(self):
        gateway = adapter()
        raw = json.dumps([{"name": "health.toml", "type": "file"}])
        self.assertEqual("health.toml", gateway.parse_response(raw)[0]["name"])

    def test_raw_github_error_has_typed_status(self):
        with self.assertRaises(GitHubError) as raised:
            adapter().parse_response('{"message":"Not Found","status":"404"}')
        self.assertEqual(404, raised.exception.status)
        self.assertNotIn("Not Found", str(raised.exception))

    def test_explicit_broker_failure_is_rejected_without_echoing_detail(self):
        with self.assertRaises(GitHubError) as raised:
            adapter().parse_response('{"successful":false,"error":"secret detail"}')
        self.assertNotIn("secret detail", str(raised.exception))

    def test_empty_scalar_and_malformed_output_fail(self):
        for raw in ("", "null", '"text"', "not json"):
            with self.subTest(raw=raw), self.assertRaises(GitHubError):
                adapter().parse_response(raw)


class RequestRestrictionTests(unittest.TestCase):
    def test_get_uses_configured_absolute_executable_account_and_text_stdin(self):
        runner = RecordingRunner(json.dumps({"full_name": "WWWPCG/pcg-agents"}))
        gateway = adapter(runner)
        gateway.get(REPO_API)
        command, kwargs = runner.calls[0]
        self.assertEqual("/reviewed/bin/composio", command[0])
        self.assertEqual("account_alias/id", command[command.index("--account") + 1])
        self.assertEqual("", kwargs["input"])
        self.assertTrue(kwargs["text"])
        self.assertGreater(kwargs["timeout"], 0)

    def test_actual_get_rejects_external_cross_repo_and_bypass_paths(self):
        runner = RecordingRunner()
        gateway = adapter(runner)
        bad = [
            "https://evil.example/repos/WWWPCG/pcg-agents",
            "/repos/other/repo",
            "/repos/WWWPCG/pcg-agents/../other",
            "/repos/WWWPCG/pcg-agents/contents/%2e%2e/secret",
            "/repos/WWWPCG/pcg-agents/contents/x#fragment",
            "/repos/WWWPCG/pcg-agents/contents/x?ref=main&extra=1",
            "/repos/WWWPCG/pcg-agents/contents/./x",
            "//repos/WWWPCG/pcg-agents",
        ]
        for path in bad:
            with self.subTest(path=path), self.assertRaises(ValueError):
                gateway.get(path)
        self.assertEqual([], runner.calls)

    def test_known_read_routes_are_allowed(self):
        routes = [
            REPO_API,
            REPO_API + "/contents",
            REPO_API + "/contents/scripts/a.py?ref=" + "a" * 40,
            REPO_API + "/git/ref/heads/main",
            REPO_API + "/git/trees/" + "a" * 40 + "?recursive=1",
        ]
        for route in routes:
            with self.subTest(route=route):
                runner = RecordingRunner("{}")
                adapter(runner).get(route)
                self.assertEqual(1, len(runner.calls))

    def test_contents_list_requires_raw_list(self):
        gateway = adapter(RecordingRunner("{}"))
        with self.assertRaises(GitHubError):
            gateway.get_contents_list(REPO_API + "/contents/scripts")

    def test_actual_put_only_allows_exact_snapshot_path_and_main(self):
        for path in (
            REPO_API + "/contents/other.toml",
            SNAPSHOT_API + "?ref=main",
            SNAPSHOT_API + "/../other",
        ):
            with self.subTest(path=path), self.assertRaises(ValueError):
                adapter(RecordingRunner(), allow=True).put_contents(
                    path, b"x", branch="main", message="test", sha=VALID_SHA
                )
        with self.assertRaises(ValueError):
            adapter(RecordingRunner(), allow=True).put_contents(
                SNAPSHOT_API, b"x", branch="feature", message="test", sha=VALID_SHA
            )

    def test_put_requires_gate_and_sends_json_as_text_with_content_type(self):
        with self.assertRaises(GitHubError):
            adapter(RecordingRunner()).put_contents(
                SNAPSHOT_API, b"x", branch="main", message="test", sha=VALID_SHA
            )
        runner = RecordingRunner(json.dumps({"content": {"sha": NEW_SHA}, "commit": {"sha": COMMIT_SHA}}))
        result = adapter(runner, allow=True).put_contents(
            SNAPSHOT_API, b"payload", branch="main", message="update", sha=VALID_SHA
        )
        command, kwargs = runner.calls[0]
        self.assertIn("Content-Type: application/json", command)
        self.assertIsInstance(kwargs["input"], str)
        body = json.loads(kwargs["input"])
        self.assertEqual("main", body["branch"])
        self.assertEqual(VALID_SHA, body["sha"])
        self.assertEqual(b"payload", base64.b64decode(body["content"]))
        self.assertEqual(COMMIT_SHA, result["commit"]["sha"])

    def test_put_rejects_malformed_current_sha_before_runner_call(self):
        runner = RecordingRunner()
        for sha in ("", "main", "g" * 40, "A" * 40, True):
            with self.subTest(sha=sha), self.assertRaises(ValueError):
                adapter(runner, allow=True).put_contents(
                    SNAPSHOT_API, b"payload", branch="main", message="update", sha=sha
                )
        self.assertEqual([], runner.calls)

    def test_nonzero_and_timeout_errors_do_not_leak_process_output(self):
        runner = RecordingRunner(returncode=1, stderr="token=top-secret", stdout="secret stdout")
        with self.assertRaises(GitHubError) as raised:
            adapter(runner).get(REPO_API)
        self.assertNotIn("secret", str(raised.exception))

        def timeout(*args, **kwargs):
            raise subprocess.TimeoutExpired(args[0], 30, output="secret", stderr="secret")

        with self.assertRaises(GitHubError) as raised:
            adapter(timeout).get(REPO_API)
        self.assertNotIn("secret", str(raised.exception))


class FileReadTests(unittest.TestCase):
    def response(self, **changes):
        data = {
            "type": "file",
            "encoding": "base64",
            "content": base64.b64encode(b"hello").decode(),
            "size": 5,
            "truncated": False,
        }
        data.update(changes)
        return json.dumps(data)

    def test_read_file_decodes_complete_base64_file(self):
        self.assertEqual(b"hello", adapter(RecordingRunner(self.response())).read_file("health.toml"))

    def test_read_file_fails_malformed_directory_encoding_truncation_and_size(self):
        cases = [
            {"type": "dir"},
            {"encoding": "utf-8"},
            {"truncated": True},
            {"content": "%%%"},
            {"size": 99},
        ]
        for changes in cases:
            with self.subTest(changes=changes), self.assertRaises(GitHubError):
                adapter(RecordingRunner(self.response(**changes))).read_file("health.toml")
    def test_read_file_requires_exact_nonnegative_integer_size(self):
        base = json.loads(self.response())
        cases = []
        missing = dict(base)
        missing.pop("size")
        cases.append(missing)
        for value in ("5", True, False, -1, 4, 6):
            cases.append({**base, "size": value})
        for payload in cases:
            with self.subTest(size=payload.get("size", "missing")), self.assertRaises(GitHubError):
                adapter(RecordingRunner(json.dumps(payload))).read_file("health.toml")

    def test_read_file_allows_absent_truncated_but_rejects_malformed_values(self):
        base = json.loads(self.response())
        without_truncated = dict(base)
        without_truncated.pop("truncated")
        self.assertEqual(
            b"hello",
            adapter(RecordingRunner(json.dumps(without_truncated))).read_file("health.toml"),
        )
        for value in (True, None, 0, "false"):
            with self.subTest(truncated=value), self.assertRaises(GitHubError):
                adapter(RecordingRunner(self.response(truncated=value))).read_file("health.toml")


class ConfigurationAndCliTests(unittest.TestCase):
    def test_constructor_requires_account_and_absolute_executable(self):
        with self.assertRaises(ValueError):
            ComposioGitHub("", "/bin/composio")
        with self.assertRaises(ValueError):
            ComposioGitHub("account", "composio")

    def test_config_is_per_key_file_first_and_defaults_to_executable(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            (home / ".env").write_text(
                "PCG_GITHUB_ACCOUNT=file-account\n"
                "PCG_GITHUB_ALLOW_SNAPSHOT_WRITE=true\n"
            )
            process = {
                "PCG_GITHUB_ACCOUNT": "process-account",
                "PCG_COMPOSIO_CLI": "/process/bin/composio",
                "PCG_GITHUB_ALLOW_SNAPSHOT_WRITE": "false",
            }
            with patch.dict(os.environ, process, clear=True):
                executable, account, allow = read_config(home)
            self.assertEqual("file-account", account)
            self.assertEqual("/process/bin/composio", executable)
            self.assertTrue(allow)

    def test_config_requires_explicit_account_and_has_no_pat_fallback(self):
        with tempfile.TemporaryDirectory() as tmp, patch.dict(
            os.environ,
            {"HOME": tmp, "GITHUB_TOKEN": "must-not-be-used"},
            clear=True,
        ):
            with self.assertRaises(ValueError):
                read_config(tmp)

    def test_check_reads_repo_and_representative_private_file(self):
        calls = []

        class FakeGateway:
            def get(self, path):
                calls.append(("get", path))
                return {"full_name": "WWWPCG/pcg-agents"}

            def read_file(self, path, ref=None):
                calls.append(("read_file", path, ref))
                return b"private"

        with patch.object(ComposioGitHub, "from_environment", return_value=FakeGateway()):
            self.assertEqual(0, gateway_main(["--check"]))
        self.assertEqual([("get", REPO_API), ("read_file", "health.toml", None)], calls)


if __name__ == "__main__":
    unittest.main()
