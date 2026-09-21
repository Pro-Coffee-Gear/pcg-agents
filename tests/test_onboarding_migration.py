"""Onboarding migration regressions; all external behavior is mocked."""
from __future__ import annotations

import importlib.util
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]


def load_onboard(name="pcg_onboard_under_test"):
    spec = importlib.util.spec_from_file_location(name, ROOT / "pcg_onboard.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class OnboardingPreflightTests(unittest.TestCase):
    ENV = {
        "HONCHO_API_KEY": "honcho-sentinel",
        "NOTION_API_KEY": "notion-sentinel",
        "PCG_COMPOSIO_SESSION_FILE": "/secure/member-session.json",
        "PCG_COMPOSIO_PYTHON": "/opt/hermes/.venv/bin/python",
    }

    def test_failed_github_preflight_prevents_all_storage_profile_and_roster_work(self):
        module = load_onboard("onboard_preflight_failure")
        calls = []
        with patch.dict(os.environ, self.ENV, clear=True), \
             patch.object(module, "github_preflight", side_effect=module.OnboardingError("preflight failed")), \
             patch.object(module, "store_keys", side_effect=lambda *a: calls.append("store")), \
             patch.object(module, "create_profile", side_effect=lambda *a: calls.append("profile")), \
             patch.object(module, "roster_row", side_effect=lambda *a: calls.append("roster-read")), \
             patch.object(module, "write_roster", side_effect=lambda *a: calls.append("roster-write")):
            result = module.main(["--email", "member@example.com"])
        self.assertEqual(2, result)
        self.assertEqual([], calls)

    def test_dry_run_has_no_network_or_mutation(self):
        module = load_onboard("onboard_dry_run")
        calls = []
        bundle = ROOT / "scripts"
        with patch.dict(os.environ, self.ENV, clear=True), \
             patch.object(module, "github_preflight", return_value=(bundle, "/secure/session.json", "/opt/hermes/.venv/bin/python", None)), \
             patch.object(module, "roster_row", side_effect=lambda *a: calls.append("network")), \
             patch.object(module, "store_keys", side_effect=lambda *a: calls.append("store")), \
             patch.object(module, "create_profile", side_effect=lambda *a: calls.append("profile")), \
             patch.object(module, "write_roster", side_effect=lambda *a: calls.append("roster-write")):
            result = module.main(["--email", "member@example.com", "--dry-run"])
        self.assertEqual(0, result)
        self.assertEqual([], calls)

    def test_actual_dry_preflight_uses_only_fixture_session_and_no_network(self):
        module = load_onboard("onboard_actual_dry_preflight")
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "session.json"
            session.write_text(json.dumps({
                "url": "https://mcp.composio.dev/member-secret/mcp",
                "headers": {"Authorization": "Bearer member-secret"},
            }))
            os.chmod(session, 0o600)
            environment = {
                "PCG_COMPOSIO_SESSION_FILE": str(session),
                "PCG_COMPOSIO_PYTHON": "/reviewed/venv/bin/python",
            }
            with patch.dict(os.environ, environment, clear=True), \
                 patch.object(module, "_validate_composio_runtime", return_value=environment["PCG_COMPOSIO_PYTHON"]):
                bundle, configured, runtime, adapter = module.github_preflight(True)
        self.assertEqual(ROOT / "scripts", bundle)
        self.assertEqual(str(session), configured)
        self.assertEqual("/reviewed/venv/bin/python", runtime)
        self.assertIsNone(adapter)

    def test_insecure_session_fails_before_runtime_or_network(self):
        module = load_onboard("onboard_insecure_session")
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "session.json"
            session.write_text(json.dumps({
                "url": "https://mcp.composio.dev/member/mcp",
                "headers": {"Authorization": "Bearer fixture"},
            }))
            os.chmod(session, 0o644)
            with patch.dict(os.environ, {"PCG_COMPOSIO_SESSION_FILE": str(session)}, clear=True), \
                 patch.object(module, "_validate_composio_runtime") as runtime_check:
                with self.assertRaises(module.OnboardingError):
                    module.github_preflight(True)
        runtime_check.assert_not_called()

    def test_setup_failure_cannot_mark_roster_onboarded(self):
        module = load_onboard("onboard_no_early_success")
        calls = []
        with patch.dict(os.environ, self.ENV, clear=True), \
             patch.object(module, "github_preflight", return_value=(ROOT / "scripts", "account", "/bin/composio", object())), \
             patch.object(module, "roster_row", return_value=("page", "Company", [], False)), \
             patch.object(module, "store_keys", return_value=None), \
             patch.object(module, "existing_profiles", return_value=set()), \
             patch.object(module, "write_honcho", return_value=["Company"]), \
             patch.object(module, "actual_active", return_value=["Company"]), \
             patch.object(module, "install_profile_sync", return_value=None), \
             patch.object(module, "install_fleet_sync", side_effect=module.OnboardingError("install failed")), \
             patch.object(module, "write_roster", side_effect=lambda *a: calls.append("roster-write")):
            result = module.main(["--email", "member@example.com"])
        self.assertEqual(2, result)
        self.assertEqual([], calls)


class OnboardingConfigurationTests(unittest.TestCase):
    def test_store_keys_replaces_legacy_settings_with_local_session_pointers_only(self):
        module = load_onboard("onboard_store_config")
        with tempfile.TemporaryDirectory() as tmp:
            module.HERMES_HOME = tmp
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "KEEP=value\nGITHUB_SYNC_TOKEN=old\nGITHUB_TOKEN=old2\n"
                "GH_TOKEN=old3\nPCG_GITHUB_ACCOUNT=old-account\n"
                "PCG_COMPOSIO_CLI=/old/cli\nPCG_GITHUB_ALLOW_SNAPSHOT_WRITE=true\n"
            )
            module.store_keys(
                "honcho", "notion", "/secure/member-session.json", "/opt/app/bin/python"
            )
            text = env_file.read_text()
        self.assertIn("KEEP=value", text)
        self.assertIn("PCG_COMPOSIO_SESSION_FILE=/secure/member-session.json", text)
        self.assertIn("PCG_COMPOSIO_PYTHON=/opt/app/bin/python", text)
        for forbidden in (
            "GITHUB_SYNC_TOKEN", "GITHUB_TOKEN", "GH_TOKEN", "PCG_GITHUB_ACCOUNT",
            "PCG_COMPOSIO_CLI", "PCG_GITHUB_ALLOW_SNAPSHOT_WRITE", "mcp.composio.dev",
            "Authorization", "x-api-key",
        ):
            self.assertNotIn(forbidden, text)

    def test_embedded_profile_sync_never_shares_session_credentials_and_copies_match(self):
        module = load_onboard("onboard_shared_keys")
        line = next(line for line in module.PROFILE_SYNC_SRC.splitlines() if line.startswith("SHARED_KEYS"))
        self.assertIn("HONCHO_API_KEY", line)
        self.assertIn("NOTION_API_KEY", line)
        self.assertNotIn("GITHUB", line)
        self.assertNotIn("COMPOSIO", line)
        self.assertIn("LOCAL_SESSION_POINTERS", module.PROFILE_SYNC_SRC)
        self.assertEqual((ROOT / "profile_sync.py").read_text(), module.PROFILE_SYNC_SRC)
        self.assertEqual((ROOT / "profile_sync.py").read_bytes(), (ROOT / "scripts" / "profile_sync.py").read_bytes())

    def test_fleet_install_places_three_file_bundle_and_explicit_runtime_before_cron(self):
        module = load_onboard("onboard_bundle_install")
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as bundle_tmp:
            home = Path(tmp)
            bundle = Path(bundle_tmp)
            (bundle / "pcg_composio.py").write_text("VALUE = 'generic'\n")
            (bundle / "pcg_github.py").write_text("VALUE = 'gateway'\n")
            (bundle / "pcg_sync.py").write_text("VALUE = 'sync'\n")
            module.HERMES_HOME = str(home)
            runtime = "/reviewed/venv/bin/python"
            events = []

            def runner(command, **kwargs):
                if command[0] == runtime:
                    self.assertTrue((home / "scripts" / "pcg_composio.py").exists())
                    self.assertTrue((home / "scripts" / "pcg_github.py").exists())
                    self.assertTrue((home / "scripts" / "pcg_sync.py").exists())
                    launcher = home / "scripts" / "pcg-fleet-sync.sh"
                    self.assertTrue(launcher.exists())
                    self.assertIn(runtime, launcher.read_text())
                    events.append("initial")
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if command[1:3] == ["cron", "list"]:
                    events.append("list")
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if command[1:3] == ["cron", "create"]:
                    self.assertEqual("pcg-fleet-sync.sh", command[command.index("--script") + 1])
                    events.append("create")
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                raise AssertionError(command)

            with patch.object(module, "_validate_composio_runtime", return_value=runtime), \
                 patch.object(module.subprocess, "run", side_effect=runner):
                module.install_fleet_sync(False, bundle, runtime)
            self.assertEqual(["initial", "list", "create"], events)
            self.assertEqual("VALUE = 'generic'\n", (home / "scripts" / "pcg_composio.py").read_text())
            self.assertEqual("VALUE = 'gateway'\n", (home / "scripts" / "pcg_github.py").read_text())
            self.assertEqual("VALUE = 'sync'\n", (home / "scripts" / "pcg_sync.py").read_text())

    def test_missing_mcp_runtime_blocks_bundle_install_before_file_mutation(self):
        module = load_onboard("onboard_runtime_block")
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as bundle_tmp:
            home = Path(tmp)
            bundle = Path(bundle_tmp)
            for name in ("pcg_composio.py", "pcg_github.py", "pcg_sync.py"):
                (bundle / name).write_text("VALUE = 1\n")
            module.HERMES_HOME = str(home)
            with patch.object(
                module,
                "_validate_composio_runtime",
                side_effect=module.OnboardingError("mcp missing"),
            ):
                with self.assertRaises(module.OnboardingError):
                    module.install_fleet_sync(False, bundle, "/missing/python")
            self.assertFalse((home / "scripts").exists())


if __name__ == "__main__":
    unittest.main()
