"""Regression tests for the fleet sync Composio migration."""
from __future__ import annotations

import hashlib
import importlib.util
import json
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

COMMIT = "a" * 40


def blob_sha(data: bytes) -> str:
    return hashlib.sha1(b"blob %d\0%s" % (len(data), data)).hexdigest()


def load_sync(path=ROOT / "pcg_sync.py", name="pcg_sync_under_test"):
    spec = importlib.util.spec_from_file_location(name, path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class FakeAdapter:
    def __init__(self, files=None, error=None, truncated=False):
        self.files = files or {}
        self.error = error
        self.truncated = truncated
        self.reads = []
        self.gets = []

    def get(self, path):
        self.gets.append(path)
        if self.error:
            raise self.error
        if path.endswith("/pcg-agents"):
            return {"full_name": "WWWPCG/pcg-agents", "private": True}
        if path.endswith("/git/ref/heads/main"):
            return {"object": {"sha": COMMIT}}
        if "/git/trees/" in path:
            return {
                "truncated": self.truncated,
                "tree": [
                    {"path": name, "type": "blob", "sha": blob_sha(content)}
                    for name, content in self.files.items()
                ],
            }
        raise AssertionError(path)

    def read_file(self, path, ref=None):
        self.reads.append((path, ref))
        if self.error:
            raise self.error
        return self.files[path]


class FleetSyncSnapshotTests(unittest.TestCase):
    def test_distribution_copies_are_byte_identical(self):
        self.assertEqual((ROOT / "pcg_sync.py").read_bytes(), (SCRIPTS / "pcg_sync.py").read_bytes())

    def test_wrappers_take_adapter_not_pat(self):
        module = load_sync()
        fake = FakeAdapter({"health.toml": b"x"})
        self.assertEqual("WWWPCG/pcg-agents", module.gh(fake, module.REPO_API)["full_name"])
        self.assertEqual(b"x", module.gh_raw(fake, "health.toml", COMMIT))
        self.assertEqual([("health.toml", COMMIT)], fake.reads)

    def test_snapshot_uses_one_ref_and_rejects_truncated_tree(self):
        module = load_sync()
        fake = FakeAdapter({"scripts/pcg_sync.py": b"x"})
        ref, tree = module.load_snapshot(fake)
        self.assertEqual(COMMIT, ref)
        self.assertEqual("scripts/pcg_sync.py", tree[0]["path"])
        self.assertIn("?recursive=1", fake.gets[-1])
        with self.assertRaises(module.SyncError):
            module.load_snapshot(FakeAdapter({}, truncated=True))

    def test_auth_or_disguised_404_is_not_an_empty_tree(self):
        module = load_sync()
        for status in (401, 404):
            with self.subTest(status=status), self.assertRaises(GitHubError):
                module.load_snapshot(FakeAdapter(error=GitHubError("probe failed", status=status)))

    def test_malformed_listing_path_is_rejected_before_local_write(self):
        module = load_sync()

        class Unsafe(FakeAdapter):
            def get(self, path):
                if path.endswith("/pcg-agents"):
                    return {"full_name": "WWWPCG/pcg-agents"}
                if path.endswith("/git/ref/heads/main"):
                    return {"object": {"sha": COMMIT}}
                return {"truncated": False, "tree": [{"path": "scripts/../escape", "type": "blob", "sha": "b" * 40}]}

        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(module.SyncError):
                module.sync_dir(Unsafe(), "scripts", tmp, [], [], {})
            self.assertEqual([], list(Path(tmp).iterdir()))

    def test_local_destination_cannot_escape_through_symlink_parent(self):
        module = load_sync()
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            root = Path(tmp)
            (root / "nested").symlink_to(outside, target_is_directory=True)
            with self.assertRaises(module.SyncError):
                module._safe_destination(root, "nested/escape.py")

    def test_empty_optional_directory_after_valid_snapshot_is_empty(self):
        module = load_sync()
        fake = FakeAdapter({"health.toml": b"x"})
        ref, tree = module.load_snapshot(fake)
        self.assertEqual([], module.list_tree(fake, "skills/not-installed", ref=ref, tree=tree))


class FleetSyncManifestTests(unittest.TestCase):
    def test_manifest_accepts_only_exact_managed_sync_projections(self):
        module = load_sync(name="pcg_sync_manifest_projections")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            valid = {
                str(home / "scripts" / "pcg_tool.py"): "scripts/pcg_tool.py",
                str(home / "skills" / "pcg-common" / "SKILL.md"):
                    "skills/_common/pcg-common/SKILL.md",
                str(home / "profiles" / "sales" / "skills" / "pcg-sales" / "SKILL.md"):
                    "skills/sales/pcg-sales/SKILL.md",
                str(home / "plugins" / "pcg-plugin" / "plugin.yaml"):
                    "plugins/_common/pcg-plugin/plugin.yaml",
                str(home / "profiles" / "cs" / "plugins" / "pcg-plugin" / "plugin.yaml"):
                    "plugins/_common/pcg-plugin/plugin.yaml",
            }
            for destination in valid:
                path = Path(destination)
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text("managed")
            with patch.object(module, "HERMES_HOME", str(home)):
                planned = module._manifest_quarantines(valid, set())
            self.assertEqual(set(valid), {str(path) for path, _ in planned})

    def test_manifest_rejects_protected_mismatched_traversal_and_symlink_destinations(self):
        module = load_sync(name="pcg_sync_manifest_rejections")
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as outside:
            home = Path(tmp)
            (home / ".env").write_text("keep")
            (home / "config").mkdir()
            (home / "config" / "pcg_tool.py").write_text("keep")
            (home / "profiles" / "finance" / "skills" / "pcg-sales").mkdir(parents=True)
            (home / "profiles" / "finance" / "skills" / "pcg-sales" / "SKILL.md").write_text("keep")
            (home / "profiles" / "unknown" / "plugins" / "pcg-plugin").mkdir(parents=True)
            (home / "profiles" / "unknown" / "plugins" / "pcg-plugin" / "plugin.yaml").write_text("keep")
            (home / "scripts" / "linked").parent.mkdir(parents=True, exist_ok=True)
            (home / "scripts" / "linked").symlink_to(outside, target_is_directory=True)
            cases = [
                {str(home / ".env"): "scripts/pcg_tool.py"},
                {str(home / "config" / "pcg_tool.py"): "scripts/pcg_tool.py"},
                {
                    str(home / "profiles" / "finance" / "skills" / "pcg-sales" / "SKILL.md"):
                        "skills/sales/pcg-sales/SKILL.md"
                },
                {
                    str(home / "profiles" / "unknown" / "plugins" / "pcg-plugin" / "plugin.yaml"):
                        "plugins/_common/pcg-plugin/plugin.yaml"
                },
                {str(home / "scripts" / "pcg_tool.py"): "scripts/../pcg_tool.py"},
                {str(home / "scripts" / "linked" / "pcg_tool.py"): "scripts/linked/pcg_tool.py"},
            ]
            with patch.object(module, "HERMES_HOME", str(home)):
                for manifest in cases:
                    with self.subTest(manifest=manifest), self.assertRaises(module.SyncError):
                        module._manifest_quarantines(manifest, set())
            self.assertEqual("keep", (home / ".env").read_text())

    def test_missing_destination_drops_stale_claim_before_local_recreation(self):
        module = load_sync(name="pcg_sync_stale_manifest")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            target = home / "scripts" / "pcg_tool.py"
            manifest = {str(target): "scripts/pcg_tool.py"}
            with patch.object(module, "HERMES_HOME", str(home)):
                self.assertEqual([], module._manifest_quarantines(manifest, set()))
                self.assertEqual({}, manifest)
                target.parent.mkdir(parents=True)
                target.write_text("local")
                self.assertEqual([], module._manifest_quarantines(manifest, set()))
            self.assertEqual("local", target.read_text())

    def test_deleted_nonmanaged_file_is_preserved_without_invented_ownership_hash(self):
        module = load_sync(name="pcg_sync_nonmanaged_manifest")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            target = home / "scripts" / "local_tool.py"
            target.parent.mkdir()
            target.write_text("locally edited")
            manifest = {str(target): "scripts/local_tool.py"}
            with patch.object(module, "HERMES_HOME", str(home)):
                self.assertEqual([], module._manifest_quarantines(manifest, set()))
            self.assertEqual({}, manifest)
            self.assertEqual("locally edited", target.read_text())

    def test_existing_revoked_history_causes_quarantine_to_fail_closed(self):
        module = load_sync(name="pcg_sync_revoked_collision")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            target = home / "scripts" / "pcg_old.py"
            target.parent.mkdir()
            target.write_text("current")
            revoked = Path(str(target) + ".revoked")
            revoked.write_text("history")
            manifest = {str(target): "scripts/pcg_old.py"}
            with patch.object(module, "HERMES_HOME", str(home)):
                with self.assertRaises(module.SyncError):
                    module._quarantine_file(target)
                with self.assertRaises(module.SyncError):
                    module._manifest_quarantines(manifest, set())
            self.assertEqual("current", target.read_text())
            self.assertEqual("history", revoked.read_text())


class FleetSyncFileBehaviorTests(unittest.TestCase):
    def test_managed_overwrite_nonmanaged_conflict_and_snapshot_pin(self):
        module = load_sync()
        files = {
            "scripts/pcg_sync.py": b"new managed",
            "scripts/local_tool.py": b"repo local",
        }
        fake = FakeAdapter(files)
        with tempfile.TemporaryDirectory() as tmp:
            local = Path(tmp)
            (local / "pcg_sync.py").write_bytes(b"old managed")
            (local / "local_tool.py").write_bytes(b"local edit")
            changes, conflicts, manifest = [], [], {}
            module.sync_dir(fake, "scripts", local, changes, conflicts, manifest)
            self.assertEqual(b"new managed", (local / "pcg_sync.py").read_bytes())
            self.assertEqual(b"local edit", (local / "local_tool.py").read_bytes())
            self.assertEqual(1, len(changes))
            self.assertEqual([str(local / "local_tool.py")], conflicts)
            self.assertEqual([("scripts/pcg_sync.py", COMMIT)], fake.reads)

    def test_noop_does_not_fetch_or_rewrite(self):
        module = load_sync()
        content = b"same"
        fake = FakeAdapter({"scripts/pcg_sync.py": content})
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "pcg_sync.py"
            target.write_bytes(content)
            before = target.stat().st_mtime_ns
            changes, manifest = [], {}
            module.sync_dir(fake, "scripts", tmp, changes, [], manifest)
            self.assertEqual([], fake.reads)
            self.assertEqual([], changes)
            self.assertEqual(before, target.stat().st_mtime_ns)
            self.assertEqual("scripts/pcg_sync.py", manifest[str(target)])


class FleetSyncMainFailureTests(unittest.TestCase):
    def configure_module(self, module, home):
        module.HERMES_HOME = str(home)
        module.EMAIL_FILE = str(home / ".pcg_member_email")
        module.MANIFEST_FILE = str(home / ".pcg_fleet_manifest.json")
        (home / ".pcg_member_email").write_text("member@example.com\n")
        (home / ".env").write_text("NOTION_API_KEY=test-notion\n")

    def test_missing_composio_config_returns_nonzero_before_heartbeat(self):
        module = load_sync(name="pcg_sync_missing_config")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.configure_module(module, home)
            heartbeats = []
            with patch.object(module, "roster_row", return_value=("page", set())), \
                 patch.object(module.ComposioGitHub, "from_environment", side_effect=ValueError("missing")), \
                 patch.object(module, "heartbeat", side_effect=lambda *args: heartbeats.append(args)):
                self.assertEqual(1, module.main())
            self.assertEqual([], heartbeats)

    def test_auth_disguised_404_never_quarantines_or_heartbeats(self):
        module = load_sync(name="pcg_sync_auth_404")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.configure_module(module, home)
            installed = home / "scripts" / "pcg_sync.py"
            installed.parent.mkdir()
            installed.write_text("keep")
            (home / ".pcg_fleet_manifest.json").write_text(json.dumps({str(installed): "scripts/pcg_sync.py"}))
            fake = FakeAdapter(error=GitHubError("not accessible", status=404))
            heartbeats = []
            with patch.object(module, "roster_row", return_value=("page", set())), \
                 patch.object(module.ComposioGitHub, "from_environment", return_value=fake), \
                 patch.object(module, "heartbeat", side_effect=lambda *args: heartbeats.append(args)):
                self.assertEqual(1, module.main())
            self.assertTrue(installed.exists())
            self.assertFalse(Path(str(installed) + ".revoked").exists())
            self.assertEqual([], heartbeats)

    def test_fetch_failure_occurs_before_any_file_mutation(self):
        module = load_sync(name="pcg_sync_fetch_failure")

        class FailsRead(FakeAdapter):
            def read_file(self, path, ref=None):
                raise GitHubError("read failed")

        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.configure_module(module, home)
            target = home / "scripts" / "pcg_sync.py"
            target.parent.mkdir()
            target.write_bytes(b"old")
            fake = FailsRead({"scripts/pcg_sync.py": b"new"})
            heartbeats = []
            with patch.object(module, "roster_row", return_value=("page", set())), \
                 patch.object(module.ComposioGitHub, "from_environment", return_value=fake), \
                 patch.object(module, "heartbeat", side_effect=lambda *args: heartbeats.append(args)):
                self.assertEqual(1, module.main())
            self.assertEqual(b"old", target.read_bytes())
            self.assertEqual([], heartbeats)

    def test_proven_missing_manifest_file_is_quarantined_then_heartbeat(self):
        module = load_sync(name="pcg_sync_quarantine")
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            self.configure_module(module, home)
            target = home / "scripts" / "pcg_old.py"
            target.parent.mkdir()
            target.write_bytes(b"old")
            (home / ".pcg_fleet_manifest.json").write_text(json.dumps({str(target): "scripts/pcg_old.py"}))
            events = []
            with patch.object(module, "roster_row", return_value=("page", set())), \
                 patch.object(module.ComposioGitHub, "from_environment", return_value=FakeAdapter({})), \
                 patch.object(module, "reconcile_deliverable_policy", return_value=None), \
                 patch.object(module, "heartbeat", side_effect=lambda *args: events.append("heartbeat")):
                self.assertEqual(0, module.main())
            self.assertFalse(target.exists())
            self.assertTrue(Path(str(target) + ".revoked").exists())
            self.assertEqual(["heartbeat"], events)


if __name__ == "__main__":
    unittest.main()
