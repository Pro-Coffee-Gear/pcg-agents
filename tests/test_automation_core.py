import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts" if (ROOT / "scripts").exists() else Path("/opt/data/scripts")
sys.path.insert(0, str(SCRIPTS))

from pcg_automation_core import (  # noqa: E402
    classify_deliverable,
    incident_key,
    incident_transition,
    is_safe_repo_restore,
    parse_deliverables_toml,
    parse_health_toml,
    policy_for,
    script_row_to_incident,
    select_owner_notifications,
    select_repair_candidates,
)


class DeliverableCoreTests(unittest.TestCase):
    def test_classifies_recurring_business_workflow_as_meaningful(self):
        proposal = {
            "name": "CS refund exception monitor",
            "purpose": "Warn CS when refunds exceed policy thresholds",
            "owner_email": "sina@procoffeegear.com",
            "function": ["CS"],
            "type": "Scheduled Automation",
            "schedule": "every 30m",
            "audience": "Function",
        }
        meaningful, reasons = classify_deliverable(proposal)
        self.assertTrue(meaningful)
        self.assertIn("recurring", reasons)

    def test_rejects_utility_without_business_impact(self):
        proposal = {
            "name": "Rename temporary files",
            "purpose": "One-time local cleanup",
            "owner_email": "sina@procoffeegear.com",
            "function": ["CS"],
            "type": "Utility",
            "audience": "Individual",
        }
        meaningful, reasons = classify_deliverable(proposal)
        self.assertFalse(meaningful)
        self.assertEqual([], reasons)

    def test_parses_deliverables_manifest(self):
        text = '''
[[deliverable]]
name = "Returns Dashboard"
owner_email = "wes@procoffeegear.com"
function = ["Operations", "Finance"]
type = "App / Dashboard"
purpose = "Manage open-box returns"
status = "Live"
repair_policy = "repair-pr"
'''
        rows = parse_deliverables_toml(text)
        self.assertEqual("Returns Dashboard", rows[0]["name"])
        self.assertEqual(["Operations", "Finance"], rows[0]["function"])


class DeliverablePublisherTests(unittest.TestCase):
    def load_module(self):
        path = SCRIPTS / "pcg-register-deliverable.py"
        spec = importlib.util.spec_from_file_location("pcg_register_deliverable", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_new_build_is_proposed_and_cannot_self_approve(self):
        module = self.load_module()
        proposal = {
            "name": "CS refund monitor", "type": "Scheduled Automation",
            "purpose": "Warn CS about refund exceptions", "owner_email": "sina@procoffeegear.com",
            "submitted_by": "sina@procoffeegear.com", "function": ["CS"],
        }
        props = module.build_properties(proposal)
        self.assertEqual("Proposed", props["Curation Status"]["select"]["name"])
        self.assertEqual("sina@procoffeegear.com", props["Owner Email"]["email"])

    def test_update_does_not_downgrade_approved_deliverable(self):
        module = self.load_module()
        proposal = {
            "name": "CS refund monitor", "type": "Scheduled Automation",
            "purpose": "Warn CS about refund exceptions", "owner_email": "sina@procoffeegear.com",
            "submitted_by": "sina@procoffeegear.com", "function": ["CS"],
        }
        existing = {"properties": {"Curation Status": {"select": {"name": "Approved"}}}}
        props = module.build_properties(proposal, existing)
        self.assertEqual("Approved", props["Curation Status"]["select"]["name"])


class FleetSyncTests(unittest.TestCase):
    def load_module(self):
        path = SCRIPTS / "pcg_sync.py"
        spec = importlib.util.spec_from_file_location("pcg_sync", path)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_pcg_hyphen_and_underscore_files_are_repo_managed(self):
        module = self.load_module()
        self.assertTrue(module.is_repo_managed("pcg-automation-health.py"))
        self.assertTrue(module.is_repo_managed("pcg_sync.py"))
        self.assertFalse(module.is_repo_managed("front_model_scan.py"))


class RepairCoreTests(unittest.TestCase):
    def test_incident_key_is_stable_but_changes_with_failure(self):
        a = incident_key("exec-default", "front_model_scan.py", "SyntaxError line 4")
        b = incident_key("exec-default", "front_model_scan.py", "SyntaxError line 4")
        c = incident_key("exec-default", "front_model_scan.py", "Timeout")
        self.assertEqual(a, b)
        self.assertNotEqual(a, c)

    def test_parses_health_manifest(self):
        text = '''
[[script]]
name = "front_model_scan.py"
owner_email = "wes@procoffeegear.com"
source_repo = "WWWPCG/pcg-agents"
repair_policy = "repair-pr"
test_command = "python3 -m unittest"
'''
        policies = parse_health_toml(text)
        self.assertEqual("repair-pr", policies["front_model_scan.py"]["repair_policy"])

    def test_incident_opens_on_new_failure_and_resolves_on_recovery(self):
        opened = incident_transition("Healthy", "Failing", "exec-default", "a.py", "boom", "", "None")
        self.assertEqual("Open", opened["incident_status"])
        self.assertTrue(opened["incident_key"])
        resolved = incident_transition("Failing", "Healthy", "exec-default", "a.py", "", opened["incident_key"], "Awaiting Approval")
        self.assertEqual("Resolved", resolved["incident_status"])
        self.assertEqual(opened["incident_key"], resolved["incident_key"])

    def test_existing_repair_status_is_not_reset_while_still_failing(self):
        state = incident_transition("Failing", "Failing", "exec-default", "a.py", "boom", "abc", "Awaiting Approval")
        self.assertEqual("Awaiting Approval", state["incident_status"])
        self.assertEqual("abc", state["incident_key"])

    def test_policy_defaults_to_detect_only(self):
        policy = policy_for("unknown.py", {}, "owner@example.com")
        self.assertEqual("detect-only", policy["repair_policy"])
        self.assertEqual("owner@example.com", policy["owner_email"])

    def test_safe_restore_accepts_only_expected_repo_path(self):
        good = {"source_repo": "https://github.com/WWWPCG/pcg-agents", "source_path": "scripts/a.py"}
        bad = {"source_repo": "https://github.com/WWWPCG/pcg-agents", "source_path": "../secrets"}
        self.assertTrue(is_safe_repo_restore("a.py", good))
        self.assertFalse(is_safe_repo_restore("a.py", bad))

    def test_converts_notion_script_row_to_repair_incident(self):
        row = {
            "id": "row1",
            "properties": {
                "Name": {"type": "title", "title": [{"plain_text": "a.py"}]},
                "Script File": {"type": "rich_text", "rich_text": [{"plain_text": "a.py"}]},
                "Instance": {"type": "rich_text", "rich_text": [{"plain_text": "exec-default"}]},
                "Health": {"type": "select", "select": {"name": "Failing"}},
                "Repair Policy": {"type": "select", "select": {"name": "repair-pr"}},
                "Incident Status": {"type": "select", "select": {"name": "Open"}},
                "Failure Detail": {"type": "rich_text", "rich_text": [{"plain_text": "boom"}]},
                "Owner Email": {"type": "email", "email": "wes@procoffeegear.com"},
                "Source Repository": {"type": "url", "url": "https://github.com/WWWPCG/pcg-agents"},
                "Fix PR": {"type": "url", "url": None},
            },
        }
        incident = script_row_to_incident(row)
        self.assertEqual("row1", incident["row_id"])
        self.assertEqual("a.py", incident["name"])
        self.assertEqual("boom", incident["failure_detail"])

    def test_owner_notification_only_returns_unseen_awaiting_approval(self):
        rows = [
            {"row_id": "1", "owner_email": "wes@procoffeegear.com", "incident_status": "Awaiting Approval", "incident_key": "k1", "fix_pr": "https://github/pr/1", "name": "a.py"},
            {"row_id": "2", "owner_email": "sina@procoffeegear.com", "incident_status": "Awaiting Approval", "incident_key": "k2", "fix_pr": "https://github/pr/2", "name": "b.py"},
        ]
        selected = select_owner_notifications(rows, "wes@procoffeegear.com", set())
        self.assertEqual(["1"], [r["row_id"] for r in selected])
        self.assertEqual([], select_owner_notifications(rows, "wes@procoffeegear.com", {"k1"}))

    def test_selects_only_unassigned_repair_pr_failures(self):
        rows = [
            {"row_id": "1", "name": "a.py", "health": "Failing", "repair_policy": "repair-pr", "incident_status": "Open", "fix_pr": ""},
            {"row_id": "2", "name": "b.py", "health": "Healthy", "repair_policy": "repair-pr", "incident_status": "Open", "fix_pr": ""},
            {"row_id": "3", "name": "c.py", "health": "Failing", "repair_policy": "manual", "incident_status": "Open", "fix_pr": ""},
            {"row_id": "4", "name": "d.py", "health": "Failing", "repair_policy": "repair-pr", "incident_status": "Awaiting Approval", "fix_pr": "https://github/pr/4"},
        ]
        selected = select_repair_candidates(rows)
        self.assertEqual(["1"], [r["row_id"] for r in selected])


if __name__ == "__main__":
    unittest.main()
