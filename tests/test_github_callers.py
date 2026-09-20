"""Tests for health and deliverables callers migrated to the GitHub gateway."""
from __future__ import annotations

import base64
import hashlib
import importlib.util
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pcg_github import GitHubError  # noqa: E402


def load_script(filename, name):
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


COMMIT_SHA = hashlib.sha1(b"deterministic test commit").hexdigest()


def blob_sha(raw: bytes) -> str:
    return hashlib.sha1(b"blob %d\0%s" % (len(raw), raw)).hexdigest()


def file_response(text, sha=None):
    raw = text.encode()
    return {
        "type": "file",
        "encoding": "base64",
        "content": base64.b64encode(raw).decode(),
        "size": len(raw),
        "truncated": False,
        "sha": sha if sha is not None else blob_sha(raw),
    }


class HealthGatewayTests(unittest.TestCase):
    def test_env_key_respects_requested_name_priority(self):
        module = load_script("pcg-automation-health.py", "health_env_priority")
        with tempfile.TemporaryDirectory() as tmp:
            module.HERMES_HOME = Path(tmp)
            (Path(tmp) / ".env").write_text("SECOND=second\nFIRST=first\n")
            with patch.dict(os.environ, {}, clear=True):
                self.assertEqual("first", module.env_key("FIRST", "SECOND"))

    def test_policy_fallback_uses_gateway(self):
        module = load_script("pcg-automation-health.py", "health_policy_gateway")

        class Gateway:
            def __init__(self):
                self.paths = []

            def read_file(self, path, ref=None):
                self.paths.append(path)
                return b'[[script]]\nname = "a.py"\nrepair_policy = "detect-only"\n'

        with tempfile.TemporaryDirectory() as tmp:
            module.HERMES_HOME = Path(tmp)
            gateway = Gateway()
            policies = module.load_health_policies(gateway)
        self.assertEqual(["health.toml"], gateway.paths)
        self.assertEqual("detect-only", policies["a.py"]["repair_policy"])

    def test_safe_restore_refuses_unpermitted_path_without_gateway_call(self):
        module = load_script("pcg-automation-health.py", "health_restore_refusal")

        class Gateway:
            def read_file(self, path, ref=None):
                raise AssertionError("gateway must not be called")

        bad = {"source_repo": "https://github.com/WWWPCG/pcg-agents", "source_path": "../a.py"}
        self.assertFalse(module.safe_restore_script("a.py", bad, Gateway()))

    def test_safe_restore_fetches_through_same_gateway_and_validates_syntax(self):
        module = load_script("pcg-automation-health.py", "health_restore_gateway")

        class Gateway:
            def __init__(self):
                self.paths = []

            def read_file(self, path, ref=None):
                self.paths.append(path)
                return b"VALUE = 1\n"

        policy = {
            "source_repo": "https://github.com/WWWPCG/pcg-agents",
            "source_path": "scripts/a.py",
        }
        with tempfile.TemporaryDirectory() as tmp:
            module.SCRIPTS_DIR = Path(tmp)
            gateway = Gateway()
            self.assertTrue(module.safe_restore_script("a.py", policy, gateway))
            self.assertEqual(b"VALUE = 1\n", (Path(tmp) / "a.py").read_bytes())
            self.assertEqual(["scripts/a.py"], gateway.paths)

    def test_probe_uses_gateway_and_sanitizes_failure(self):
        module = load_script("pcg-automation-health.py", "health_probe_gateway")

        class Good:
            def get(self, path):
                return {"full_name": "WWWPCG/pcg-agents"}

        class Bad:
            def get(self, path):
                raise GitHubError("secret broker output", status=404)

        self.assertEqual((True, ""), module.gh_probe(Good()))
        ok, detail = module.gh_probe(Bad())
        self.assertFalse(ok)
        self.assertEqual("pcg-agents repository probe failed (status 404)", detail)
        self.assertNotIn("secret", detail)


class ReconcileGatewayTests(unittest.TestCase):
    @staticmethod
    def rows():
        return [
            {
                "id": "approved",
                "properties": {
                    "Name": {"type": "title", "title": [{"plain_text": "Approved item"}]},
                    "Curation Status": {"type": "select", "select": {"name": "Approved"}},
                    "Type": {"type": "select", "select": {"name": "Document"}},
                    "Owner Email": {"type": "email", "email": "owner@example.com"},
                    "Business Purpose": {"type": "rich_text", "rich_text": [{"plain_text": "Sentinel purpose"}]},
                    "Functions": {"type": "multi_select", "multi_select": [{"name": "Company"}]},
                },
            },
            {
                "id": "proposed",
                "properties": {
                    "Name": {"type": "title", "title": [{"plain_text": "Must not publish"}]},
                    "Curation Status": {"type": "select", "select": {"name": "Proposed"}},
                },
            },
        ]

    def test_decode_file_requires_exact_nonnegative_integer_size(self):
        module = load_script("pcg-deliverable-reconcile.py", "reconcile_decode_size")
        base = file_response("hello")
        cases = []
        missing = dict(base)
        missing.pop("size")
        cases.append(missing)
        for value in ("5", True, False, -1, 4, 6):
            cases.append({**base, "size": value})
        for response in cases:
            with self.subTest(size=response.get("size", "missing")), self.assertRaises(module.ReconcileError):
                module._decode_file(response)

    def test_decode_file_allows_absent_truncated_but_rejects_malformed_values(self):
        module = load_script("pcg-deliverable-reconcile.py", "reconcile_decode_truncated")
        response = file_response("hello")
        response.pop("truncated")
        self.assertEqual((blob_sha(b"hello"), "hello"), module._decode_file(response))
        for value in (True, None, 0, "false"):
            with self.subTest(truncated=value), self.assertRaises(module.ReconcileError):
                module._decode_file(file_response("hello") | {"truncated": value})

    def test_decode_file_rejects_nonimmutable_or_content_mismatched_sha(self):
        module = load_script("pcg-deliverable-reconcile.py", "reconcile_decode_sha")
        for sha in ("", "main", "g" * 40, "A" * 40, "a" * 40):
            with self.subTest(sha=sha), self.assertRaises(module.ReconcileError):
                module._decode_file(file_response("hello", sha=sha))

    def test_reconcile_rejects_malformed_returned_ids_before_readback(self):
        module = load_script("pcg-deliverable-reconcile.py", "reconcile_returned_ids")
        malformed = ("", "main", "g" * 40, "A" * 40)
        for bad_commit, bad_file in [*((value, None) for value in malformed), *((None, value) for value in malformed)]:
            class Gateway:
                def __init__(self):
                    self.reads = 0
                    self.desired = None

                def get(self, path):
                    return {"full_name": "WWWPCG/pcg-agents"}

                def get_contents(self, path, ref=None):
                    self.reads += 1
                    if self.reads == 1:
                        return file_response("old")
                    assert self.desired is not None
                    sha = bad_file if bad_file is not None else blob_sha(self.desired.encode())
                    return file_response(self.desired, sha=sha)

                def put_contents(self, path, content, branch, message, sha):
                    self.desired = content
                    commit_sha = bad_commit if bad_commit is not None else COMMIT_SHA
                    written_sha = bad_file if bad_file is not None else blob_sha(content.encode())
                    return {
                        "commit": {"sha": commit_sha, "html_url": "https://attacker.invalid"},
                        "content": {"sha": written_sha},
                    }

            gateway = Gateway()
            with self.subTest(commit=bad_commit, file=bad_file), self.assertRaises(module.ReconcileError):
                module.reconcile(gateway, self.rows())
            self.assertEqual(1, gateway.reads)

    def test_reconcile_rejects_valid_but_wrong_written_blob_sha_before_readback(self):
        module = load_script("pcg-deliverable-reconcile.py", "reconcile_written_blob")

        class Gateway:
            def __init__(self):
                self.reads = 0

            def get(self, path):
                return {"full_name": "WWWPCG/pcg-agents"}

            def get_contents(self, path, ref=None):
                self.reads += 1
                return file_response("old")

            def put_contents(self, path, content, branch, message, sha):
                return {
                    "commit": {"sha": COMMIT_SHA},
                    "content": {"sha": "b" * 40},
                }

        gateway = Gateway()
        with self.assertRaises(module.ReconcileError):
            module.reconcile(gateway, self.rows())
        self.assertEqual(1, gateway.reads)

    def test_content_noop_performs_no_put(self):
        module = load_script("pcg-deliverable-reconcile.py", "reconcile_noop")
        desired = module.render_deliverables_toml(module.approved_deliverables_from_rows(self.rows()))

        class Gateway:
            def __init__(self):
                self.puts = []

            def get(self, path):
                return {"full_name": "WWWPCG/pcg-agents"}

            def get_contents(self, path, ref=None):
                return file_response(desired)

            def put_contents(self, *args, **kwargs):
                self.puts.append((args, kwargs))

        gateway = Gateway()
        self.assertEqual((False, None, 1), module.reconcile(gateway, self.rows()))
        self.assertEqual([], gateway.puts)

    def test_write_is_approved_only_sha_guarded_and_read_back_at_commit(self):
        module = load_script("pcg-deliverable-reconcile.py", "reconcile_write_verify")

        class Gateway:
            def __init__(self):
                self.puts = []
                self.read_refs = []
                self.desired = None

            def get(self, path):
                return {"full_name": "WWWPCG/pcg-agents"}

            def get_contents(self, path, ref=None):
                self.read_refs.append(ref)
                if ref == "main":
                    return file_response("old")
                return file_response(self.desired)

            def put_contents(self, path, content, branch, message, sha):
                self.desired = content
                self.puts.append({"path": path, "content": content, "branch": branch, "sha": sha})
                return {
                    "commit": {"sha": COMMIT_SHA, "html_url": "https://attacker.invalid/untrusted"},
                    "content": {"sha": blob_sha(content.encode())},
                }

        gateway = Gateway()
        changed, url, count = module.reconcile(gateway, self.rows())
        expected_url = f"https://github.com/WWWPCG/pcg-agents/commit/{COMMIT_SHA}"
        self.assertEqual((True, expected_url, 1), (changed, url, count))
        self.assertEqual(["main", COMMIT_SHA], gateway.read_refs)
        self.assertEqual("main", gateway.puts[0]["branch"])
        self.assertEqual(blob_sha(b"old"), gateway.puts[0]["sha"])
        self.assertIn("Approved item", gateway.puts[0]["content"])
        self.assertNotIn("Must not publish", gateway.puts[0]["content"])

    def test_conflict_is_not_retried(self):
        module = load_script("pcg-deliverable-reconcile.py", "reconcile_conflict")

        class Gateway:
            def __init__(self):
                self.put_count = 0

            def get(self, path):
                return {"full_name": "WWWPCG/pcg-agents"}

            def get_contents(self, path, ref=None):
                return file_response("old")

            def put_contents(self, *args, **kwargs):
                self.put_count += 1
                raise GitHubError("conflict", status=409)

        gateway = Gateway()
        with self.assertRaises(GitHubError):
            module.reconcile(gateway, self.rows())
        self.assertEqual(1, gateway.put_count)

    def test_missing_file_is_accepted_only_after_repo_probe_and_readback_must_match(self):
        module = load_script("pcg-deliverable-reconcile.py", "reconcile_missing_readback")

        class Gateway:
            def __init__(self):
                self.events = []
                self.desired = None

            def get(self, path):
                self.events.append("probe")
                return {"full_name": "WWWPCG/pcg-agents"}

            def get_contents(self, path, ref=None):
                self.events.append(("read", ref))
                if ref == "main":
                    raise GitHubError("missing", status=404)
                return file_response("wrong")

            def put_contents(self, path, content, branch, message, sha):
                self.desired = content
                self.events.append("put")
                return {
                    "commit": {"sha": COMMIT_SHA, "html_url": "https://attacker.invalid/untrusted"},
                    "content": {"sha": blob_sha(content.encode())},
                }

        gateway = Gateway()
        with self.assertRaises(module.ReconcileError):
            module.reconcile(gateway, self.rows())
        self.assertEqual("probe", gateway.events[0])
        self.assertEqual(1, gateway.events.count("put"))
        self.assertIn(("read", COMMIT_SHA), gateway.events)


if __name__ == "__main__":
    unittest.main()
