"""Tests for fleet-sync health reporting: a dead GitHub token must surface as
an error on the roster, never as a fresh Last Fleet Sync timestamp."""
import sys
import unittest
import urllib.error
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts" if (ROOT / "scripts").exists() else Path("/opt/data/scripts")
sys.path.insert(0, str(SCRIPTS))

import pcg_sync  # noqa: E402


class PreflightCheckTests(unittest.TestCase):
    def test_healthy_token_returns_none(self):
        with mock.patch.object(pcg_sync, "gh", return_value={"id": 1}):
            self.assertIsNone(pcg_sync.preflight_check("tok"))

    def test_dead_token_reports_http_code(self):
        err = urllib.error.HTTPError(url="u", code=401, msg="Unauthorized", hdrs=None, fp=None)
        with mock.patch.object(pcg_sync, "gh", side_effect=err):
            msg = pcg_sync.preflight_check("tok")
        self.assertIn("401", msg)
        self.assertIn("auth failed", msg)

    def test_network_failure_reports_unreachable(self):
        with mock.patch.object(pcg_sync, "gh", side_effect=OSError("no route")):
            self.assertIn("unreachable", pcg_sync.preflight_check("tok"))


class ReportFleetHealthTests(unittest.TestCase):
    def _spy(self):
        calls = []

        def fake_notion(key, path, method="GET", body=None):
            calls.append(body)
            return {}

        return calls, fake_notion

    def test_success_stamps_timestamp_and_clears_error(self):
        calls, fake = self._spy()
        with mock.patch.object(pcg_sync, "notion", fake):
            pcg_sync.report_fleet_health("pid", "key", [])
        props = calls[0]["properties"]
        self.assertIn("start", props["Last Fleet Sync"]["date"])
        self.assertEqual([], props[pcg_sync.ERROR_PROP]["rich_text"])

    def test_failure_leaves_timestamp_stale_and_writes_error(self):
        calls, fake = self._spy()
        with mock.patch.object(pcg_sync, "notion", fake):
            pcg_sync.report_fleet_health("pid", "key", ["github auth failed: HTTP 401"])
        props = calls[0]["properties"]
        self.assertNotIn("Last Fleet Sync", props)
        text = props[pcg_sync.ERROR_PROP]["rich_text"][0]["text"]["content"]
        self.assertIn("FAILED", text)
        self.assertIn("401", text)

    def test_failure_summary_is_capped_for_notion(self):
        calls, fake = self._spy()
        with mock.patch.object(pcg_sync, "notion", fake):
            pcg_sync.report_fleet_health("pid", "key", ["x" * 5000])
        text = calls[0]["properties"][pcg_sync.ERROR_PROP]["rich_text"][0]["text"]["content"]
        self.assertLessEqual(len(text), 1900)


class MainPreflightGateTests(unittest.TestCase):
    def _common_patches(self, health_spy):
        return [
            mock.patch.object(pcg_sync.os.path, "exists", lambda p: p.endswith(".pcg_member_email")),
            mock.patch("builtins.open", mock.mock_open(read_data="sina@procoffeegear.com")),
            mock.patch.object(pcg_sync, "env_key",
                              lambda *names: "tok" if "GITHUB_SYNC_TOKEN" in names else "notion-key"),
            mock.patch.object(pcg_sync, "roster_row", return_value=("pid", {"operations"})),
            mock.patch.object(pcg_sync, "load_manifest", return_value={}),
            mock.patch.object(pcg_sync, "save_manifest"),
            mock.patch.object(pcg_sync, "quarantine_deleted"),
            mock.patch.object(pcg_sync, "reconcile_jobs"),
            mock.patch.object(pcg_sync, "report_fleet_health", health_spy),
        ]

    def test_dead_token_skips_all_syncs_and_reports_error(self):
        reported = []

        def spy(pid, key, errors):
            reported.append(list(errors))

        patches = self._common_patches(spy)
        patches.append(mock.patch.object(pcg_sync, "preflight_check",
                                         return_value="github auth failed: HTTP 401"))
        patches.append(mock.patch.object(pcg_sync, "sync_dir",
                                         side_effect=AssertionError("sync_dir must not run")))
        for p in patches:
            p.start()
        try:
            self.assertEqual(0, pcg_sync.main())
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(1, len(reported))
        self.assertTrue(any("401" in e for e in reported[0]))

    def test_healthy_run_syncs_and_reports_success(self):
        reported = []

        def spy(pid, key, errors):
            reported.append(list(errors))

        ran = []
        patches = self._common_patches(spy)
        patches.append(mock.patch.object(pcg_sync, "preflight_check", return_value=None))
        patches.append(mock.patch.object(pcg_sync, "sync_dir",
                                         lambda *a, **k: ran.append(a[1])))
        patches.append(mock.patch.object(pcg_sync, "sync_plugins_and_policy",
                                         lambda *a, **k: None))
        for p in patches:
            p.start()
        try:
            self.assertEqual(0, pcg_sync.main())
        finally:
            for p in patches:
                p.stop()
        self.assertIn("skills/_common", ran)
        self.assertIn("scripts", ran)
        self.assertEqual([[]], reported)

    def test_partial_failure_still_reports_error_not_green(self):
        reported = []

        def spy(pid, key, errors):
            reported.append(list(errors))

        def flaky(token, repo_dir, *a, **k):
            if repo_dir == "scripts":
                raise OSError("disk full")

        patches = self._common_patches(spy)
        patches.append(mock.patch.object(pcg_sync, "preflight_check", return_value=None))
        patches.append(mock.patch.object(pcg_sync, "sync_dir", flaky))
        patches.append(mock.patch.object(pcg_sync, "sync_plugins_and_policy",
                                         lambda *a, **k: None))
        for p in patches:
            p.start()
        try:
            self.assertEqual(0, pcg_sync.main())
        finally:
            for p in patches:
                p.stop()
        self.assertEqual(1, len(reported))
        self.assertTrue(any("scripts" in e for e in reported[0]))


if __name__ == "__main__":
    unittest.main()
