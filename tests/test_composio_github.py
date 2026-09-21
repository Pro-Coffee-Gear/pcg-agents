"""Mock-only tests for the GitHub translator and low-level snapshot writer."""
from __future__ import annotations

import base64
import hashlib
import sys
import unittest
from pathlib import Path
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"
sys.path.insert(0, str(SCRIPTS))

from pcg_composio import ComposioError  # noqa: E402
from pcg_github import (  # noqa: E402
    ComposioGitHub,
    GitHubError,
    REPO_API,
    SNAPSHOT_API,
    main as gateway_main,
)

H = "a" * 40
BASE_TREE = "b" * 40
OLD_BLOB = hashlib.sha1(b"blob 3\0old").hexdigest()
NEW_TREE = "d" * 40
NEW_COMMIT = "e" * 40
OTHER_BLOB = "f" * 40
TARGET = "deliverables.toml"


def blob_sha(raw: bytes) -> str:
    return hashlib.sha1(b"blob %d\0%s" % (len(raw), raw)).hexdigest()


def file_response(raw: bytes, sha=None):
    return {
        "type": "file",
        "encoding": "base64",
        "content": base64.b64encode(raw).decode(),
        "size": len(raw),
        "truncated": False,
        "sha": sha if sha is not None else blob_sha(raw),
    }


class ScriptedClient:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def execute(self, tool, arguments):
        self.calls.append((tool, arguments))
        if not self.responses:
            raise AssertionError(f"unexpected call: {tool} {arguments}")
        expected, response = self.responses.pop(0)
        if expected != tool:
            raise AssertionError(f"expected {expected}, got {tool}")
        if isinstance(response, BaseException):
            raise response
        return response


def adapter(responses, **kwargs):
    client = ScriptedClient(responses)
    return ComposioGitHub(client, **kwargs), client


def write_sequence(raw=b"new", *, update=None, new_tree_entries=None, creation=False):
    desired = blob_sha(raw)
    old_entries = [{"path": "keep.txt", "mode": "100644", "type": "blob", "sha": OTHER_BLOB}]
    if not creation:
        old_entries.insert(0, {"path": TARGET, "mode": "100644", "type": "blob", "sha": OLD_BLOB})
    expected_new = [
        {"path": TARGET, "mode": "100644", "type": "blob", "sha": desired},
        {"path": "keep.txt", "mode": "100644", "type": "blob", "sha": OTHER_BLOB},
    ]
    if new_tree_entries is not None:
        expected_new = new_tree_entries
    current = (
        ComposioError("missing", kind="provider_error", status=404)
        if creation
        else {"content": file_response(b"old", sha=OLD_BLOB)}
    )
    responses = [
        ("GITHUB_GET_A_REFERENCE", {"ref": "refs/heads/main", "object": {"sha": H}}),
        ("GITHUB_GET_REPOSITORY_CONTENT", current),
        ("GITHUB_GET_COMMIT_OBJECT", {"sha": H, "tree": {"sha": BASE_TREE}, "parents": []}),
        ("GITHUB_GET_A_TREE", {"sha": H, "truncated": False, "tree": old_entries}),
        ("GITHUB_CREATE_A_BLOB", {"sha": desired}),
        ("GITHUB_GET_A_BLOB", {
            "sha": desired,
            "encoding": "base64",
            "content": base64.b64encode(raw).decode(),
            "size": len(raw),
        }),
        ("GITHUB_CREATE_A_TREE", {"sha": NEW_TREE}),
        ("GITHUB_GET_A_TREE", {"sha": NEW_TREE, "truncated": False, "tree": expected_new}),
        ("GITHUB_CREATE_A_COMMIT", {"sha": NEW_COMMIT}),
        ("GITHUB_GET_COMMIT_OBJECT", {
            "sha": NEW_COMMIT,
            "tree": {"sha": NEW_TREE},
            "parents": [{"sha": H}],
        }),
        ("GITHUB_UPDATE_A_REFERENCE", update if update is not None else {
            "ref": "refs/heads/main", "object": {"sha": NEW_COMMIT}
        }),
    ]
    if not isinstance(responses[-1][1], BaseException):
        responses.extend([
            ("GITHUB_GET_A_REFERENCE", {"ref": "refs/heads/main", "object": {"sha": NEW_COMMIT}}),
            ("GITHUB_GET_REPOSITORY_CONTENT", {"content": file_response(raw, sha=desired)}),
        ])
    return responses, desired


class RouteTranslationTests(unittest.TestCase):
    def test_supported_compatibility_routes_translate_to_named_tools(self):
        cases = [
            (REPO_API, "GITHUB_GET_A_REPOSITORY", {"owner": "WWWPCG", "repo": "pcg-agents"}, {"full_name": "WWWPCG/pcg-agents"}),
            (REPO_API + "/contents/scripts/a.py?ref=" + H, "GITHUB_GET_REPOSITORY_CONTENT", {"owner": "WWWPCG", "repo": "pcg-agents", "path": "scripts/a.py", "ref": H}, {"content": {"type": "file"}}),
            (REPO_API + "/git/ref/heads/main", "GITHUB_GET_A_REFERENCE", {"owner": "WWWPCG", "repo": "pcg-agents", "ref": "heads/main"}, {"object": {"sha": H}}),
            (REPO_API + "/git/trees/" + H + "?recursive=1", "GITHUB_GET_A_TREE", {"owner": "WWWPCG", "repo": "pcg-agents", "tree_sha": H, "recursive": True}, {"tree": []}),
        ]
        for path, tool, arguments, response in cases:
            with self.subTest(path=path):
                gateway, client = adapter([(tool, response)])
                expected = response["content"] if tool == "GITHUB_GET_REPOSITORY_CONTENT" else response
                self.assertEqual(expected, gateway.get(path))
                self.assertEqual([(tool, arguments)], client.calls)

    def test_repo_target_is_not_a_local_authorization_allowlist(self):
        gateway, client = adapter([("GITHUB_GET_A_REPOSITORY", {"full_name": "Other/repo"})])
        self.assertEqual(
            {"full_name": "Other/repo"},
            gateway.get("/repos/Other/repo"),
        )
        self.assertEqual("Other", client.calls[0][1]["owner"])
        self.assertTrue(gateway.safe_repo_path("Other/repo"))

    def test_unknown_and_malicious_routes_are_unsupported_before_server_call(self):
        gateway, client = adapter([])
        paths = (
            "https://evil.example/repos/WWWPCG/pcg-agents",
            REPO_API + "/issues",
            REPO_API + "/contents/../secret",
            REPO_API + "/contents/%2e%2e/secret",
            REPO_API + "/git/trees/main",
            REPO_API + "/contents/x?extra=1",
        )
        for path in paths:
            with self.subTest(path=path), self.assertRaises(ValueError) as raised:
                gateway.get(path)
            self.assertIn("unsupported", str(raised.exception))
        self.assertEqual([], client.calls)

    def test_session_and_provider_errors_are_sanitized_with_typed_status(self):
        for error, expected_kind, status in (
            (ComposioError("secret", kind="session_denied", status=403), "session_denied", 403),
            (ComposioError("secret", kind="provider_error", status=404), "provider_error", 404),
        ):
            gateway, _ = adapter([("GITHUB_GET_A_REPOSITORY", error)])
            with self.subTest(expected_kind=expected_kind), self.assertRaises(GitHubError) as raised:
                gateway.get(REPO_API)
            self.assertEqual(expected_kind, raised.exception.kind)
            self.assertEqual(status, raised.exception.status)
            self.assertNotIn("secret", str(raised.exception))


class FileReadTests(unittest.TestCase):
    def read(self, response):
        gateway, _ = adapter([("GITHUB_GET_REPOSITORY_CONTENT", {"content": response})])
        return gateway.read_file("health.toml")

    def test_read_file_decodes_complete_sha_verified_base64(self):
        self.assertEqual(b"hello", self.read(file_response(b"hello")))

    def test_read_file_rejects_malformed_encoding_truncation_size_and_sha(self):
        base = file_response(b"hello")
        cases = (
            {**base, "type": "dir"},
            {**base, "encoding": "utf-8"},
            {**base, "truncated": True},
            {**base, "content": "%%%"},
            {**base, "size": True},
            {**base, "size": 99},
            {**base, "sha": "a" * 40},
        )
        for response in cases:
            with self.subTest(response=response), self.assertRaises(GitHubError):
                self.read(response)


class SnapshotWriterTests(unittest.TestCase):
    def test_happy_path_verifies_objects_then_fast_forwards_once(self):
        responses, desired = write_sequence()
        gateway, client = adapter(responses)
        result = gateway.put_contents(
            SNAPSHOT_API, b"new", branch="main", message="snapshot", sha=OLD_BLOB
        )
        self.assertEqual(NEW_COMMIT, result["commit"]["sha"])
        self.assertEqual(desired, result["content"]["sha"])
        self.assertEqual([], client.responses)

        calls = {tool: arguments for tool, arguments in client.calls}
        self.assertEqual("base64", calls["GITHUB_CREATE_A_BLOB"]["encoding"])
        self.assertEqual(BASE_TREE, calls["GITHUB_CREATE_A_TREE"]["base_tree"])
        self.assertEqual([{
            "path": TARGET, "mode": "100644", "type": "blob", "sha": desired
        }], calls["GITHUB_CREATE_A_TREE"]["tree"])
        self.assertEqual([H], calls["GITHUB_CREATE_A_COMMIT"]["parents"])
        self.assertEqual(NEW_TREE, calls["GITHUB_CREATE_A_COMMIT"]["tree"])
        self.assertIs(calls["GITHUB_UPDATE_A_REFERENCE"]["force"], False)
        self.assertNotIn("GITHUB_CREATE_OR_UPDATE_FILE_CONTENTS", [tool for tool, _ in client.calls])

    def test_creation_requires_content_404_and_absence_from_complete_tree(self):
        responses, desired = write_sequence(creation=True)
        gateway, client = adapter(responses)
        result = gateway.put_contents(SNAPSHOT_API, b"new", "main", "create", None)
        self.assertEqual(desired, result["content"]["sha"])
        self.assertEqual([], client.responses)

    def test_creation_fails_if_complete_tree_contains_target(self):
        responses, _ = write_sequence(creation=True)
        responses[3] = ("GITHUB_GET_A_TREE", {
            "sha": H,
            "truncated": False,
            "tree": [{"path": TARGET, "mode": "100644", "type": "blob", "sha": OLD_BLOB}],
        })
        gateway, client = adapter(responses)
        with self.assertRaises(GitHubError) as raised:
            gateway.put_contents(SNAPSHOT_API, b"new", "main", "create", None)
        self.assertEqual(409, raised.exception.status)
        self.assertNotIn("GITHUB_CREATE_A_BLOB", [tool for tool, _ in client.calls])

    def test_stale_head_409_and_422_do_not_retry_or_force(self):
        for status in (409, 422):
            failure = ComposioError("secret conflict", kind="provider_error", status=status)
            responses, _ = write_sequence(update=failure)
            gateway, client = adapter(responses)
            with self.subTest(status=status), self.assertRaises(GitHubError) as raised:
                gateway.put_contents(SNAPSHOT_API, b"new", "main", "snapshot", OLD_BLOB)
            self.assertEqual(status, raised.exception.status)
            updates = [arguments for tool, arguments in client.calls if tool == "GITHUB_UPDATE_A_REFERENCE"]
            self.assertEqual(1, len(updates))
            self.assertIs(updates[0]["force"], False)
            self.assertEqual([], [tool for tool, _ in client.calls[client.calls.index(("GITHUB_UPDATE_A_REFERENCE", updates[0])) + 1:]])

    def test_missing_branch_has_no_default_branch_fallback(self):
        gateway, client = adapter([(
            "GITHUB_GET_A_REFERENCE",
            ComposioError("missing", kind="provider_error", status=404),
        )])
        with self.assertRaises(GitHubError) as raised:
            gateway.put_contents(SNAPSHOT_API, b"new", "feature", "snapshot", OLD_BLOB)
        self.assertEqual(404, raised.exception.status)
        self.assertEqual([("GITHUB_GET_A_REFERENCE", {
            "owner": "WWWPCG", "repo": "pcg-agents", "ref": "heads/feature"
        })], client.calls)

    def test_unrelated_tree_change_fails_before_commit_or_ref_mutation(self):
        desired = blob_sha(b"new")
        changed = [
            {"path": TARGET, "mode": "100644", "type": "blob", "sha": desired},
            {"path": "keep.txt", "mode": "100644", "type": "blob", "sha": "9" * 40},
        ]
        responses, _ = write_sequence(new_tree_entries=changed)
        gateway, client = adapter(responses)
        with self.assertRaises(GitHubError):
            gateway.put_contents(SNAPSHOT_API, b"new", "main", "snapshot", OLD_BLOB)
        tools = [tool for tool, _ in client.calls]
        self.assertNotIn("GITHUB_CREATE_A_COMMIT", tools)
        self.assertNotIn("GITHUB_UPDATE_A_REFERENCE", tools)

    def test_blob_commit_parent_and_tree_mismatches_fail_before_ref_mutation(self):
        cases = []
        wrong_blob, _ = write_sequence()
        wrong_blob[4] = ("GITHUB_CREATE_A_BLOB", {"sha": "1" * 40})
        cases.append(wrong_blob)
        wrong_commit, _ = write_sequence()
        wrong_commit[9] = ("GITHUB_GET_COMMIT_OBJECT", {
            "sha": NEW_COMMIT,
            "tree": {"sha": "2" * 40},
            "parents": [{"sha": H}],
        })
        cases.append(wrong_commit)
        wrong_parent, _ = write_sequence()
        wrong_parent[9] = ("GITHUB_GET_COMMIT_OBJECT", {
            "sha": NEW_COMMIT,
            "tree": {"sha": NEW_TREE},
            "parents": [{"sha": "3" * 40}],
        })
        cases.append(wrong_parent)
        for index, responses in enumerate(cases):
            gateway, client = adapter(responses)
            with self.subTest(index=index), self.assertRaises(GitHubError):
                gateway.put_contents(SNAPSHOT_API, b"new", "main", "snapshot", OLD_BLOB)
            self.assertNotIn("GITHUB_UPDATE_A_REFERENCE", [tool for tool, _ in client.calls])

    def test_malformed_expected_sha_and_target_fail_before_any_call(self):
        for sha in ("", "main", "g" * 40, "A" * 40, True):
            gateway, client = adapter([])
            with self.subTest(sha=sha), self.assertRaises((ValueError, GitHubError)):
                gateway.put_contents(SNAPSHOT_API, b"new", "main", "snapshot", sha)
            self.assertEqual([], client.calls)
        gateway, client = adapter([])
        with self.assertRaises(ValueError):
            gateway.put_contents(REPO_API + "/contents/../other", b"new", "main", "snapshot", OLD_BLOB)
        self.assertEqual([], client.calls)


class CliTests(unittest.TestCase):
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
