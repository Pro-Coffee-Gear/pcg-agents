import importlib.util
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts" if (ROOT / "scripts").exists() else Path("/opt/data/scripts")
sys.path.insert(0, str(SCRIPTS))

from pcg_automation_core import (  # noqa: E402
    classify_deliverable,
    approved_deliverables_from_rows,
    group_repair_candidates,
    incident_key,
    incident_transition,
    is_safe_repo_restore,
    parse_deliverables_toml,
    parse_health_toml,
    policy_for,
    render_deliverables_toml,
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

    def test_approved_notion_rows_render_as_round_trip_manifest(self):
        approved = {
            "id": "p1",
            "properties": {
                "Name": {"type": "title", "title": [{"plain_text": "Front filter"}]},
                "Curation Status": {"type": "select", "select": {"name": "Approved"}},
                "Type": {"type": "select", "select": {"name": "Scheduled Automation"}},
                "Owner Email": {"type": "email", "email": "wes@procoffeegear.com"},
                "Business Purpose": {"type": "rich_text", "rich_text": [{"plain_text": "Filter recurring inbox noise"}]},
                "Functions": {"type": "multi_select", "multi_select": [{"name": "CS"}]},
                "Repair Policy": {"type": "select", "select": {"name": "repair-pr"}},
            },
        }
        proposed = {"id": "p2", "properties": {"Name": {"type": "title", "title": [{"plain_text": "Draft"}]}, "Curation Status": {"type": "select", "select": {"name": "Proposed"}}}}
        items = approved_deliverables_from_rows([proposed, approved])
        self.assertEqual(["Front filter"], [x["name"] for x in items])
        rendered = render_deliverables_toml(items)
        parsed = parse_deliverables_toml(rendered)
        self.assertEqual("Front filter", parsed[0]["name"])
        self.assertEqual(["CS"], parsed[0]["functions"])

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

    def test_every_completed_work_product_is_registerable(self):
        module = self.load_module()
        proposal = {
            "name": "One-time team handoff", "type": "Document",
            "purpose": "Give the team a durable operating reference",
            "owner_email": "tasha@procoffeegear.com", "function": ["CS"],
            "audience": "Individual",
        }
        self.assertEqual(["created work product"], module.registration_reasons(proposal))

    def test_main_registers_work_even_when_legacy_classifier_returns_false(self):
        module = self.load_module()
        proposal = {
            "name": "One-time team handoff", "type": "Document",
            "purpose": "Give the team a durable operating reference",
            "owner_email": "tasha@procoffeegear.com", "function": ["CS"],
            "audience": "Individual", "submitted_by": "tasha@procoffeegear.com",
        }
        setattr(module, "parse_args", lambda: __import__("argparse").Namespace(**proposal))
        setattr(module, "find_existing", lambda *_: None)
        writes = []
        setattr(module, "notion", lambda path, method="GET", body=None: writes.append((path, method, body)) or {"url": "https://notion.test/row"})
        self.assertEqual(0, module.main())
        self.assertEqual("/pages", writes[0][0])
        self.assertEqual("Proposed", writes[0][2]["properties"]["Curation Status"]["select"]["name"])

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

    def test_blank_alert_target_defaults_to_owner_and_starts_unverified(self):
        module = self.load_module()
        proposal = {
            "name": "CS dashboard", "type": "App / Dashboard",
            "purpose": "Shared CS view", "owner_email": "tasha@procoffeegear.com",
            "submitted_by": "tasha@procoffeegear.com", "function": ["CS"],
            "alert_target": "",
        }
        props = module.build_properties(proposal)
        alert = "".join(x["text"]["content"] for x in props["Alert Target"]["rich_text"])
        self.assertEqual("tasha@procoffeegear.com", alert)
        self.assertEqual("Unknown", props["Health"]["select"]["name"])
        self.assertIsNone(props["Last Verified"]["date"])

    def test_cli_omits_optional_defaults_when_updating(self):
        module = self.load_module()
        old_argv = sys.argv
        sys.argv = ["pcg-register-deliverable.py", "--name", "CS dashboard",
                    "--purpose", "Shared CS view", "--owner-email", "tasha@procoffeegear.com",
                    "--function", "CS", "--type", "App / Dashboard"]
        try:
            args = module.parse_args()
        finally:
            sys.argv = old_argv
        self.assertIsNone(args.audience)
        self.assertIsNone(args.visibility)
        self.assertIsNone(args.schedule)
        self.assertIsNone(args.repair_policy)

    def test_update_preserves_existing_health_and_explicit_alert_target(self):
        module = self.load_module()
        proposal = {
            "name": "CS dashboard", "type": "App / Dashboard",
            "purpose": "Shared CS view", "owner_email": "tasha@procoffeegear.com",
            "submitted_by": "tasha@procoffeegear.com", "function": ["CS"],
            "alert_target": "",
        }
        existing = {"properties": {
            "Curation Status": {"select": {"name": "Approved"}},
            "Status": {"select": {"name": "Live"}},
            "Health": {"select": {"name": "Healthy"}},
            "Audience": {"select": {"name": "Company"}},
            "Visibility": {"select": {"name": "Team"}},
            "Repair Policy": {"select": {"name": "repair-pr"}},
            "Alert Target": {"rich_text": [{"plain_text": "slack:#cs-alerts"}]},
            "Schedule": {"rich_text": [{"plain_text": "Every 15 minutes"}]},
            "Test Command": {"rich_text": [{"plain_text": "python3 healthcheck.py"}]},
            "URL": {"url": "https://dashboard.example.com/health"},
            "Source Repository": {"url": "https://github.com/WWWPCG/cs-dashboard"},
            "Last Verified": {"date": {"start": "2026-09-03T12:00:00+00:00"}},
        }}
        props = module.build_properties(proposal, existing)
        alert = "".join(x["text"]["content"] for x in props["Alert Target"]["rich_text"])
        self.assertEqual("Live", props["Status"]["select"]["name"])
        self.assertEqual("Healthy", props["Health"]["select"]["name"])
        self.assertEqual("Company", props["Audience"]["select"]["name"])
        self.assertEqual("Team", props["Visibility"]["select"]["name"])
        self.assertEqual("repair-pr", props["Repair Policy"]["select"]["name"])
        self.assertEqual("Every 15 minutes", props["Schedule"]["rich_text"][0]["text"]["content"])
        self.assertEqual("python3 healthcheck.py", props["Test Command"]["rich_text"][0]["text"]["content"])
        self.assertEqual("https://dashboard.example.com/health", props["URL"]["url"])
        self.assertEqual("https://github.com/WWWPCG/cs-dashboard", props["Source Repository"]["url"])
        self.assertEqual("slack:#cs-alerts", alert)
        self.assertEqual("2026-09-03T12:00:00+00:00", props["Last Verified"]["date"]["start"])

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


class DeliverableHealthTests(unittest.TestCase):
    def load_module(self):
        path = SCRIPTS / "pcg-automation-health.py"
        spec = importlib.util.spec_from_file_location("pcg_automation_health", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    @staticmethod
    def deps():
        return {key: (True, "") for key in ("front", "notion", "github", "dashboard")}

    @staticmethod
    def row(name="New dashboard", url=None, scripts=None):
        return {"id": "d1", "properties": {
            "Name": {"type": "title", "title": [{"plain_text": name}]},
            "Type": {"type": "select", "select": {"name": "App / Dashboard"}},
            "Status": {"type": "select", "select": {"name": "Building"}},
            "URL": {"type": "url", "url": url},
            "Job ID": {"type": "rich_text", "rich_text": []},
            "Scripts": {"type": "relation", "relation": scripts or []},
            "Owner Email": {"type": "email", "email": "tasha@procoffeegear.com"},
            "Alert Target": {"type": "rich_text", "rich_text": []},
        }}

    def test_deliverable_without_a_health_signal_is_unknown(self):
        module = self.load_module()
        health, issues = module.deliverable_health(self.row(), {}, self.deps(), {})
        self.assertEqual("Unknown", health)
        self.assertEqual(["No health check configured"], issues)

    def test_public_artifact_url_is_live_probed(self):
        module = self.load_module()
        setattr(module, "url_probe", lambda url: (url == "https://dashboard.example.com/health", "down"))
        healthy, healthy_issues = module.deliverable_health(
            self.row(url="https://dashboard.example.com/health"), {}, self.deps(), {})
        failing, failing_issues = module.deliverable_health(
            self.row(url="https://dashboard.example.com/fail"), {}, self.deps(), {})
        self.assertEqual(("Healthy", []), (healthy, healthy_issues))
        self.assertEqual(("Failing", ["down"]), (failing, failing_issues))

    def test_private_dashboard_url_is_rejected_without_fetching(self):
        module = self.load_module()
        calls = []
        setattr(module, "url_probe", lambda url: calls.append(url) or (True, ""))
        health, issues = module.deliverable_health(
            self.row(url="http://127.0.0.1/admin"), {}, self.deps(), {})
        self.assertEqual("Failing", health)
        self.assertEqual(["URL health check must use a public HTTPS endpoint"], issues)
        self.assertEqual([], calls)

    def test_related_script_health_rolls_up(self):
        module = self.load_module()
        health, issues = module.deliverable_health(
            self.row(scripts=[{"id": "s1"}]), {}, self.deps(), {"s1": "Failing"})
        self.assertEqual("Failing", health)
        self.assertEqual(["related script is Failing"], issues)

    def test_update_backfills_alert_target_and_clears_fake_verification(self):
        module = self.load_module()
        row = self.row()
        writes = []
        setattr(module, "query_rows", lambda ds: [row] if ds == "deliverables" else [])
        setattr(module, "notion", lambda path, method="GET", body=None: writes.append((path, method, body)) or {})
        result = module.update_deliverables("deliverables", "scripts", [], self.deps())
        props = writes[0][2]["properties"]
        self.assertEqual("Unknown", result["New dashboard"]["health"])
        self.assertIsNone(props["Last Verified"]["date"])
        alert = "".join(x["text"]["content"] for x in props["Alert Target"]["rich_text"])
        self.assertEqual("tasha@procoffeegear.com", alert)


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

    def test_fetch_repo_files_uses_one_recursive_tree(self):
        module = self.load_module()
        calls = []

        def fake_gh(token, path):
            calls.append((token, path))
            return {
                "truncated": False,
                "tree": [
                    {"type": "blob", "path": "scripts/pcg_sync.py", "sha": "abc"},
                    {"type": "tree", "path": "scripts", "sha": "def"},
                    {"type": "blob", "path": "jobs.yaml", "sha": "ghi"},
                ],
            }

        module.gh = fake_gh
        self.assertEqual(
            {"scripts/pcg_sync.py": "abc", "jobs.yaml": "ghi"},
            module.fetch_repo_files(None),
        )
        self.assertEqual(
            [(None, "/repos/Pro-Coffee-Gear/pcg-agents/git/trees/main?recursive=1")],
            calls,
        )

    def test_public_repo_reads_never_send_a_stale_token(self):
        module = self.load_module()
        requests = []

        class Response:
            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def read(self):
                return b'{}'

        def fake_urlopen(request):
            requests.append(request)
            return Response()

        module.urllib.request.urlopen = fake_urlopen
        module.gh("revoked-token", "/repos/Pro-Coffee-Gear/pcg-agents")
        headers = {key.lower(): value for key, value in requests[0].header_items()}
        self.assertNotIn("authorization", headers)

    def test_truncated_repo_tree_fails_closed(self):
        module = self.load_module()
        module.gh = lambda token, path: {"truncated": True, "tree": []}
        with self.assertRaisesRegex(RuntimeError, "truncated"):
            module.fetch_repo_files(None)

    def test_main_fetches_tree_once_and_records_failure_without_green_heartbeat(self):
        module = self.load_module()
        tempfile = __import__("tempfile")
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            email_file = root / ".pcg_member_email"
            email_file.write_text("audit@example.com\n")
            module.HERMES_HOME = str(root)
            module.EMAIL_FILE = str(email_file)
            module.MANIFEST_FILE = str(root / ".pcg_fleet_manifest.json")
            module.env_key = lambda *names: "notion-key" if "NOTION_API_KEY" in names else None
            module.roster_row = lambda email, key: ("page-id", set())
            module.reconcile_jobs = lambda *args, **kwargs: None
            module.reconcile_deliverable_policy = lambda *args, **kwargs: None
            fetch_calls = []

            def fail_fetch(token):
                fetch_calls.append(token)
                raise RuntimeError("rate limited")

            module.fetch_repo_files = fail_fetch
            writes = []
            module.notion = lambda key, path, method="GET", body=None: writes.append(body) or {}
            contextlib = __import__("contextlib")
            io = __import__("io")
            with contextlib.redirect_stdout(io.StringIO()):
                module.main()

            self.assertEqual([None], fetch_calls)
            properties = writes[-1]["properties"]
            self.assertNotIn("Last Fleet Sync", properties)
            error_text = properties[module.ERROR_PROP]["rich_text"][0]["text"]["content"]
            self.assertIn("rate limited", error_text)

    def test_plugin_destinations_cover_default_and_held_profiles(self):
        module = self.load_module()
        root = Path("/srv/member")
        homes = module.plugin_destinations(root, {"cs", "operations"}, existing_only=False)
        self.assertEqual([
            root,
            root / "profiles" / "cs",
            root / "profiles" / "operations",
        ], homes)

    def test_managed_coding_instruction_is_added_once_and_replaceable(self):
        module = self.load_module()
        original = "Keep diffs small."
        first = module.merge_coding_instructions(original)
        second = module.merge_coding_instructions(first)
        self.assertIn("Keep diffs small.", second)
        self.assertEqual(1, second.count(module.POLICY_START))
        self.assertIn("Business Automations & Deliverables", second)
        self.assertIn("Alert Target", second)

    def test_reconcile_policy_enables_plugin_and_preserves_local_instructions(self):
        module = self.load_module()
        calls = []

        def runner(command, **kwargs):
            calls.append(command)
            if command[1:4] == ["config", "get", "agent.coding_instructions"]:
                return __import__("types").SimpleNamespace(returncode=0, stdout="Keep diffs small.\n", stderr="")
            if command[1:4] == ["plugins", "list", "--json"]:
                return __import__("types").SimpleNamespace(
                    returncode=0,
                    stdout='[{"name":"pcg-deliverable-autoregistration","status":"not enabled"}]',
                    stderr="",
                )
            return __import__("types").SimpleNamespace(returncode=0, stdout="ok", stderr="")

        changes = []
        module.reconcile_deliverable_policy(Path("/srv/member"), changes, runner=runner)
        set_calls = [c for c in calls if c[1:3] == ["config", "set"]]
        self.assertEqual(1, len(set_calls))
        self.assertIn("Keep diffs small.", set_calls[0][4])
        self.assertIn([module.HERMES_BIN, "plugins", "enable", module.PLUGIN_NAME], calls)
        self.assertEqual(2, len(changes))

    def test_syncs_and_enables_plugin_in_default_and_profile_homes(self):
        module = self.load_module()
        with __import__("tempfile").TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "profiles" / "cs").mkdir(parents=True)
            setattr(module, "HERMES_HOME", str(root))
            sync_calls = []
            policy_calls = []
            setattr(module, "sync_dir", lambda token, repo, local, changes, conflicts, manifest: sync_calls.append((repo, Path(local))))
            setattr(module, "reconcile_deliverable_policy", lambda home, changes: policy_calls.append(Path(home)))
            module.sync_plugins_and_policy("token", {"cs", "sales"}, [], [], {})
            expected_homes = [root, root / "profiles" / "cs"]
            self.assertEqual(
                [("plugins/_common", home / "plugins") for home in expected_homes],
                sync_calls,
            )
            self.assertEqual(expected_homes, policy_calls)


class DeliverableAutoregistrationPluginTests(unittest.TestCase):
    def load_module(self):
        path = ROOT / "plugins" / "_common" / "pcg-deliverable-autoregistration" / "__init__.py"
        spec = importlib.util.spec_from_file_location("pcg_deliverable_autoregistration", path)
        assert spec is not None and spec.loader is not None
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module

    def test_pre_verify_blocks_completion_until_registration_succeeds(self):
        module = self.load_module()
        hooks = {}
        sections = {}

        class Context:
            def register_hook(self, name, callback):
                hooks[name] = callback

            def register_system_prompt_section(self, name, content, **kwargs):
                sections[name] = content

        module.register(Context())
        self.assertIn("pcg.deliverable-autoregistration", sections)
        self.assertIn("Alert Target", sections["pcg.deliverable-autoregistration"])
        directive = hooks["pre_verify"](
            session_id="s1", coding=True, attempt=0,
            changed_paths=["dashboard/index.html"], final_response="Done.",
        )
        self.assertEqual("continue", directive["action"])
        self.assertIn("pcg-register-deliverable.py", directive["message"])

        hooks["post_tool_call"](
            session_id="s1", tool_name="terminal",
            args={"command": "python3 /opt/data/scripts/pcg-register-deliverable.py --name x"},
            result="Deliverable proposed: x\nhttps://app.notion.com/p/x",
            status="success",
        )
        self.assertIsNone(hooks["pre_verify"](
            session_id="s1", coding=True, attempt=1,
            changed_paths=["dashboard/index.html"], final_response="Done.",
        ))

    def test_scratch_work_can_be_explicitly_excluded(self):
        module = self.load_module()
        directive = module.pre_verify(
            session_id="s2", coding=True, attempt=1,
            changed_paths=["tmp/probe.py"],
            final_response="PCG catalog: not applicable — scratch test fixture.",
        )
        self.assertIsNone(directive)


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
        good = {"source_repo": "https://github.com/Pro-Coffee-Gear/pcg-agents", "source_path": "scripts/a.py"}
        bad = {"source_repo": "https://github.com/Pro-Coffee-Gear/pcg-agents", "source_path": "../secrets"}
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
                "Source Repository": {"type": "url", "url": "https://github.com/Pro-Coffee-Gear/pcg-agents"},
                "Fix PR": {"type": "url", "url": None},
            },
        }
        incident = script_row_to_incident(row)
        self.assertEqual("row1", incident["row_id"])
        self.assertEqual("a.py", incident["name"])
        self.assertEqual("boom", incident["failure_detail"])

    def test_owner_notification_deduplicates_same_pr_across_instances(self):
        rows = [
            {"row_id": "1", "owner_email": "wes@procoffeegear.com", "incident_status": "Awaiting Approval", "incident_key": "k1", "fix_pr": "https://github/pr/1", "name": "a.py", "instance": "box-a"},
            {"row_id": "2", "owner_email": "wes@procoffeegear.com", "incident_status": "Awaiting Approval", "incident_key": "k2", "fix_pr": "https://github/pr/1", "name": "a.py", "instance": "box-b"},
            {"row_id": "3", "owner_email": "sina@procoffeegear.com", "incident_status": "Awaiting Approval", "incident_key": "k3", "fix_pr": "https://github/pr/2", "name": "b.py", "instance": "box-c"},
        ]
        selected = select_owner_notifications(rows, "wes@procoffeegear.com", set())
        self.assertEqual(1, len(selected))
        self.assertEqual(["box-a", "box-b"], selected[0]["instances"])
        self.assertEqual("pr:https://github/pr/1", selected[0]["notification_marker"])
        self.assertEqual([], select_owner_notifications(rows, "wes@procoffeegear.com", {"pr:https://github/pr/1"}))

    def test_groups_same_repair_across_instances(self):
        base = {"name": "a.py", "failure_detail": "SyntaxError line 4", "source_repo": "https://github.com/Pro-Coffee-Gear/pcg-agents", "health": "Failing", "repair_policy": "repair-pr", "incident_status": "Open", "fix_pr": ""}
        grouped = group_repair_candidates([
            {**base, "row_id": "r1", "instance": "box-a", "incident_key": "k1"},
            {**base, "row_id": "r2", "instance": "box-b", "incident_key": "k2"},
        ])
        self.assertEqual(1, len(grouped))
        self.assertEqual(["r1", "r2"], grouped[0]["row_ids"])
        self.assertEqual(["box-a", "box-b"], grouped[0]["instances"])

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
