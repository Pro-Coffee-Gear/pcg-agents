"""Onboarding migration regressions; all external behavior is mocked."""
from __future__ import annotations

import importlib.util
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
        "PCG_GITHUB_ACCOUNT": "instance-account",
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
             patch.object(module, "github_preflight", return_value=(bundle, "account", "/bin/composio", None)), \
             patch.object(module, "roster_row", side_effect=lambda *a: calls.append("network")), \
             patch.object(module, "store_keys", side_effect=lambda *a: calls.append("store")), \
             patch.object(module, "create_profile", side_effect=lambda *a: calls.append("profile")), \
             patch.object(module, "write_roster", side_effect=lambda *a: calls.append("roster-write")):
            result = module.main(["--email", "member@example.com", "--dry-run"])
        self.assertEqual(0, result)
        self.assertEqual([], calls)

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
    def test_store_keys_removes_legacy_pats_and_never_enables_write(self):
        module = load_onboard("onboard_store_config")
        with tempfile.TemporaryDirectory() as tmp:
            module.HERMES_HOME = tmp
            env_file = Path(tmp) / ".env"
            env_file.write_text(
                "KEEP=value\nGITHUB_SYNC_TOKEN=old\nGITHUB_TOKEN=old2\n"
                "GH_TOKEN=old3\nPCG_GITHUB_ALLOW_SNAPSHOT_WRITE=true\n"
            )
            module.store_keys("honcho", "notion", "account_alias", "/absolute/composio")
            text = env_file.read_text()
        self.assertIn("KEEP=value", text)
        self.assertIn("PCG_GITHUB_ACCOUNT=account_alias", text)
        self.assertIn("PCG_COMPOSIO_CLI=/absolute/composio", text)
        self.assertNotIn("GITHUB_SYNC_TOKEN", text)
        self.assertNotIn("GITHUB_TOKEN", text)
        self.assertNotIn("GH_TOKEN", text)
        self.assertNotIn("PCG_GITHUB_ALLOW_SNAPSHOT_WRITE", text)

    def test_embedded_profile_sync_shared_keys_exclude_github_and_broker_credentials(self):
        module = load_onboard("onboard_shared_keys")
        line = next(line for line in module.PROFILE_SYNC_SRC.splitlines() if line.startswith("SHARED_KEYS"))
        self.assertIn("HONCHO_API_KEY", line)
        self.assertIn("NOTION_API_KEY", line)
        self.assertNotIn("GITHUB", line)
        self.assertNotIn("COMPOSIO", line)

    def test_fleet_install_places_gateway_and_updater_before_cron_registration(self):
        module = load_onboard("onboard_bundle_install")
        with tempfile.TemporaryDirectory() as tmp, tempfile.TemporaryDirectory() as bundle_tmp:
            home = Path(tmp)
            bundle = Path(bundle_tmp)
            (bundle / "pcg_github.py").write_text("VALUE = 'gateway'\n")
            (bundle / "pcg_sync.py").write_text("VALUE = 'sync'\n")
            module.HERMES_HOME = str(home)
            events = []

            def runner(command, **kwargs):
                if command[0] == module.sys.executable:
                    self.assertTrue((home / "scripts" / "pcg_github.py").exists())
                    self.assertTrue((home / "scripts" / "pcg_sync.py").exists())
                    events.append("initial")
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if command[1:3] == ["cron", "list"]:
                    events.append("list")
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                if command[1:3] == ["cron", "create"]:
                    events.append("create")
                    return SimpleNamespace(returncode=0, stdout="", stderr="")
                raise AssertionError(command)

            with patch.object(module.subprocess, "run", side_effect=runner):
                module.install_fleet_sync(False, bundle)
            self.assertEqual(["initial", "list", "create"], events)
            self.assertEqual("VALUE = 'gateway'\n", (home / "scripts" / "pcg_github.py").read_text())
            self.assertEqual("VALUE = 'sync'\n", (home / "scripts" / "pcg_sync.py").read_text())


if __name__ == "__main__":
    unittest.main()
