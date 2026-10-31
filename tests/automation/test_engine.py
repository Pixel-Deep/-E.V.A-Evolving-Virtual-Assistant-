from __future__ import annotations

import json
import tempfile
import unittest
import zipfile
import csv
from pathlib import Path
from threading import Event, Thread
from time import sleep
from unittest.mock import patch

from eva.automation.engine import (
    AutomationError, ExecutionContext, PermissionPolicy, RiskLevel, ToolExecutor,
    ToolRegistry, ToolResult, ToolSpec, WorkflowEngine, load_workflow,
)
from eva.automation.service import AutomationService
from eva.automation.tools.browser import validate_browser_url
from eva.automation.tools.windows_ui import register_windows_ui_tools
from eva.core.task_router import TaskRouter


class RegistryAndExecutorTests(unittest.TestCase):
    def setUp(self):
        self.registry = ToolRegistry()
        self.calls = []
        self.registry.register(
            ToolSpec(
                "test.echo", "Echo a value",
                {"type": "object", "properties": {"value": {"type": "string", "maxLength": 20}},
                 "required": ["value"], "additionalProperties": False},
                ("filesystem.read",), "test", timeout_seconds=1,
            ),
            lambda ctx, args: (self.calls.append(args["value"]) or {"ok": True, "value": args["value"]}),
        )

    def test_registry_rejects_duplicate_tool(self):
        with self.assertRaises(ValueError):
            self.registry.register(ToolSpec("test.echo", "duplicate"), lambda ctx, args: {})

    def test_unknown_tool_returns_structured_error(self):
        from eva.automation.engine import AuditLogger
        result = ToolExecutor(self.registry, audit=AuditLogger(None)).execute("missing.tool", {})
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "TOOL_NOT_FOUND")

    def test_schema_rejects_unknown_or_missing_arguments_before_handler(self):
        executor = ToolExecutor(self.registry, PermissionPolicy(), audit=__import__("eva.automation.engine", fromlist=["AuditLogger"]).AuditLogger(None))
        missing = executor.execute("test.echo", {})
        extra = executor.execute("test.echo", {"value": "ok", "shell": "danger"})
        self.assertEqual(missing.error_code, "INVALID_ARGUMENT")
        self.assertEqual(extra.error_code, "INVALID_ARGUMENT")
        self.assertEqual(self.calls, [])

    def test_nested_array_schema_rejects_wrong_item_types_before_handler(self):
        registry = ToolRegistry()
        seen = []
        registry.register(
            ToolSpec("test.paths", "Accept a list of path strings", {
                "type": "object", "properties": {
                    "paths": {"type": "array", "items": {"type": "string", "minLength": 1}, "maxItems": 5}
                }, "required": ["paths"], "additionalProperties": False
            }, ("filesystem.read",), "test"),
            lambda ctx, args: (seen.append(args["paths"]) or {"ok": True, "paths": args["paths"]}),
        )
        executor = ToolExecutor(registry, audit=__import__("eva.automation.engine", fromlist=["AuditLogger"]).AuditLogger(None))
        invalid = executor.execute("test.paths", {"paths": ["safe", 7]})
        self.assertFalse(invalid.ok)
        self.assertEqual(invalid.error_code, "INVALID_ARGUMENT")
        self.assertIn("paths[1]", invalid.message)
        self.assertEqual(seen, [])
        valid = executor.execute("test.paths", {"paths": ["safe", "also-safe"]})
        self.assertTrue(valid.ok)
        self.assertEqual(seen, [["safe", "also-safe"]])

    def test_type_error_inside_validated_tool_is_classified_as_internal_tool_error(self):
        registry = ToolRegistry()
        registry.register(
            ToolSpec("test.broken", "Simulate an internal implementation bug", {
                "type": "object", "properties": {"value": {"type": "string"}},
                "required": ["value"], "additionalProperties": False
            }, ("filesystem.read",), "test"),
            lambda ctx, args: (_ for _ in ()).throw(TypeError("'NoneType' object is not subscriptable")),
        )
        executor = ToolExecutor(registry, audit=__import__("eva.automation.engine", fromlist=["AuditLogger"]).AuditLogger(None))
        result = executor.execute("test.broken", {"value": "valid input"})
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "TOOL_ERROR")
        self.assertIn("Internal tool error (TypeError)", result.message)
        self.assertIn("NoneType", result.message)

    def test_permission_is_checked_before_handler(self):
        restricted = PermissionPolicy(allowed_permissions=set(), denied_permissions=set())
        result = ToolExecutor(self.registry, restricted, audit=__import__("eva.automation.engine", fromlist=["AuditLogger"]).AuditLogger(None)).execute(
            "test.echo", {"value": "ok"}
        )
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "PERMISSION_DENIED")
        self.assertEqual(self.calls, [])

    def test_dry_run_does_not_execute_handler_and_can_preview_approval_gated_action(self):
        self.registry.register(
            ToolSpec("test.high_risk", "high risk", {"type": "object", "properties": {}, "additionalProperties": False},
                     (), "test", risk=RiskLevel.HIGH, requires_approval=True),
            lambda ctx, args: self.calls.append("high-risk"),
        )
        executor = ToolExecutor(self.registry, audit=__import__("eva.automation.engine", fromlist=["AuditLogger"]).AuditLogger(None))
        result = executor.execute("test.high_risk", {}, context=ExecutionContext(dry_run=True))
        self.assertTrue(result.ok)
        self.assertTrue(result.data["dry_run"])
        self.assertEqual(self.calls, [])

    def test_high_risk_action_requires_approval_and_runs_when_granted(self):
        self.registry.register(
            ToolSpec("test.high_risk", "high risk", {"type": "object", "properties": {}, "additionalProperties": False},
                     (), "test", risk=RiskLevel.HIGH, requires_approval=True),
            lambda ctx, args: (self.calls.append("approved") or {"ok": True}),
        )
        executor = ToolExecutor(self.registry, audit=__import__("eva.automation.engine", fromlist=["AuditLogger"]).AuditLogger(None))
        denied = executor.execute("test.high_risk", {})
        granted = executor.execute("test.high_risk", {}, context=ExecutionContext(approved_actions=frozenset({"test.high_risk"})))
        self.assertEqual(denied.error_code, "APPROVAL_REQUIRED")
        self.assertTrue(granted.ok)
        self.assertEqual(self.calls, ["approved"])


    def test_cooperative_cancellation_is_available_to_tool_handlers(self):
        with tempfile.TemporaryDirectory() as td:
            service = AutomationService(allowed_roots=[td], audit_path=None)
            started = Event()
            def blocking_handler(context, arguments):
                started.set()
                while True:
                    context.check_cancelled()
                    sleep(0.005)
            service.registry.register(ToolSpec("test.blocking", "cooperative blocking test", {
                "type": "object", "properties": {}, "additionalProperties": False}, (), "test", timeout_seconds=5), blocking_handler)
            outcomes = {}
            worker = Thread(target=lambda: outcomes.setdefault("result", service.execute_tool("test.blocking", {}, run_id="cancel-this-run")))
            worker.start()
            self.assertTrue(started.wait(1))
            self.assertTrue(service.cancel("cancel-this-run"))
            worker.join(1)
            self.assertFalse(worker.is_alive())
            self.assertEqual(outcomes["result"].error_code, "CANCELLED")
            self.assertFalse(service.cancel("cancel-this-run"))
            service.close()

    def test_audit_redacts_tokens_and_content(self):
        with tempfile.TemporaryDirectory() as td:
            log = Path(td) / "events.jsonl"
            from eva.automation.engine import AuditLogger
            executor = ToolExecutor(self.registry, audit=AuditLogger(log))
            executor.execute("test.echo", {"value": "private text"})
            # Generic value is not automatically secret, but known content/token keys are.
            AuditLogger(log).write({"event": "secret-test", "token": "abc123", "content": "private body"})
            rows = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
            self.assertEqual(rows[-1]["token"], "[REDACTED]")
            self.assertEqual(rows[-1]["content"], "[REDACTED]")

    def test_audit_redacts_credentials_and_query_values_in_urls(self):
        from eva.automation.engine.audit import redact
        cleaned = redact({"url": "https://alice:secret@example.com/path?token=abc&search=private#fragment"})["url"]
        self.assertNotIn("alice", cleaned)
        self.assertNotIn("secret", cleaned)
        self.assertNotIn("abc", cleaned)
        self.assertNotIn("private", cleaned)
        self.assertIn("[REDACTED]", cleaned)


class WorkflowEngineTests(unittest.TestCase):
    def setUp(self):
        from eva.automation.engine import AuditLogger
        self.registry = ToolRegistry()
        self.calls = []
        self.registry.register(ToolSpec("test.echo", "Echo value", {
            "type": "object", "properties": {"value": {"type": "string"}},
            "required": ["value"], "additionalProperties": False}, (), "test"),
            lambda ctx, args: (self.calls.append(args["value"]) or {"ok": True, "echo": args["value"]}))
        self.registry.register(ToolSpec("test.fail", "Fail deliberately", {
            "type": "object", "properties": {}, "additionalProperties": False}, (), "test"),
            lambda ctx, args: {"ok": False, "error": "expected test failure"})
        self.executor = ToolExecutor(self.registry, audit=AuditLogger(None))
        self.engine = WorkflowEngine(self.registry, self.executor)

    def test_dag_is_sorted_by_dependencies_and_templates_are_resolved(self):
        workflow = {
            "name": "dependency-test",
            "inputs": {"value": {"type": "string", "required": True}},
            "steps": [
                {"id": "second", "tool": "test.echo", "depends_on": ["first"], "args": {"value": "${steps.first.data.echo}"}},
                {"id": "first", "tool": "test.echo", "args": {"value": "${inputs.value}"}},
            ],
        }
        result = self.engine.execute(workflow, {"value": "hello"})
        self.assertTrue(result.ok, result.as_dict())
        self.assertEqual(self.calls, ["hello", "hello"])
        self.assertEqual([step["id"] for step in result.steps], ["first", "second"])


    def test_safe_list_index_path_reference_reads_first_explicit_list_item(self):
        self.registry.register(ToolSpec("test.list", "Return a deterministic list", {
            "type": "object", "properties": {}, "additionalProperties": False}, (), "test"),
            lambda ctx, args: {"ok": True, "items": [{"value": "selected"}, {"value": "other"}]})
        workflow = {"name": "list-reference", "steps": [
            {"id": "find", "tool": "test.list", "args": {}},
            {"id": "read", "tool": "test.echo", "depends_on": ["find"],
             "args": {"value": "${steps.find.data.items[0].value}"}},
        ]}
        result = self.engine.execute(workflow)
        self.assertTrue(result.ok, result.as_dict())
        self.assertEqual(self.calls, ["selected"])

    def test_safe_list_index_references_fail_closed_on_out_of_range_index(self):
        self.registry.register(ToolSpec("test.list", "Return a deterministic list", {
            "type": "object", "properties": {}, "additionalProperties": False}, (), "test"),
            lambda ctx, args: {"ok": True, "items": [{"value": "selected"}]})
        workflow = {"name": "bad-list-reference", "steps": [
            {"id": "find", "tool": "test.list", "args": {}},
            {"id": "read", "tool": "test.echo", "depends_on": ["find"],
             "args": {"value": "${steps.find.data.items[99].value}"}},
        ]}
        result = self.engine.execute(workflow)
        self.assertFalse(result.ok)
        self.assertEqual(result.steps[-1]["result"]["error_code"], "INVALID_WORKFLOW_REFERENCE")
        self.assertEqual(self.calls, [])

    def test_undeclared_reference_dependency_is_rejected_before_any_tool_runs(self):
        workflow = {"name": "missing-ref-dependency", "steps": [
            {"id": "first", "tool": "test.echo", "args": {"value": "would have side effects"}},
            {"id": "second", "tool": "test.echo", "args": {"value": "${steps.first.data.echo}"}},
        ]}
        result = self.engine.execute(workflow)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "INVALID_WORKFLOW_REFERENCE")
        self.assertEqual(self.calls, [])

    def test_extra_argument_in_reference_step_is_rejected_during_preflight(self):
        workflow = {"name": "invalid-reference-args", "steps": [
            {"id": "first", "tool": "test.echo", "args": {"value": "would not run"}},
            {"id": "second", "tool": "test.echo", "depends_on": ["first"],
             "args": {"value": "${steps.first.data.echo}", "shell": "not allowed"}},
        ]}
        result = self.engine.execute(workflow)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "INVALID_ARGUMENT")
        self.assertEqual(self.calls, [])

    def test_malformed_template_is_rejected_during_preflight(self):
        workflow = {"name": "malformed-reference", "steps": [
            {"id": "first", "tool": "test.echo", "args": {"value": "${steps.first.data.echo"}},
        ]}
        result = self.engine.execute(workflow)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "INVALID_WORKFLOW_REFERENCE")
        self.assertEqual(self.calls, [])

    def test_workflow_validation_rejects_cycles_and_unknown_tools(self):
        cyclic = {"name": "cycle", "steps": [
            {"id": "a", "tool": "test.echo", "depends_on": ["b"], "args": {"value": "a"}},
            {"id": "b", "tool": "test.echo", "depends_on": ["a"], "args": {"value": "b"}},
        ]}
        unknown = {"name": "unknown", "steps": [{"id": "a", "tool": "missing.tool", "args": {}}]}
        with self.assertRaises(AutomationError):
            self.engine.validate(cyclic)
        with self.assertRaises(AutomationError):
            self.engine.validate(unknown)


    def test_unknown_workflow_and_step_fields_fail_closed(self):
        unknown_workflow = {"name": "unknown-field", "shell": "echo should-not-run",
                            "steps": [{"id": "step", "tool": "test.echo", "args": {"value": "x"}}]}
        unknown_step = {"name": "unknown-step-field", "steps": [
            {"id": "step", "tool": "test.echo", "args": {"value": "x"}, "shell": "echo should-not-run"}
        ]}
        for workflow in (unknown_workflow, unknown_step):
            with self.subTest(workflow=workflow["name"]):
                result = self.engine.execute(workflow)
                self.assertFalse(result.ok)
                self.assertEqual(result.error_code, "INVALID_WORKFLOW")
        self.assertEqual(self.calls, [])

    def test_whole_workflow_permission_preflight_blocks_before_safe_prefix(self):
        from eva.automation.engine import RiskLevel
        self.registry.register(ToolSpec("test.forbidden", "Categorically forbidden action", {
            "type": "object", "properties": {}, "additionalProperties": False
        }, ("system.admin",), "test", risk=RiskLevel.CRITICAL),
            lambda ctx, args: self.calls.append("forbidden"))
        workflow = {"name": "permission-preflight", "steps": [
            {"id": "safe", "tool": "test.echo", "args": {"value": "must-not-run"}},
            {"id": "forbidden", "tool": "test.forbidden", "depends_on": ["safe"], "args": {}},
        ]}
        result = self.engine.execute(workflow)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "PERMISSION_DENIED")
        self.assertEqual(self.calls, [])
        self.assertIn("before execution", result.message.lower())

    def test_workflow_continues_independent_steps_but_skips_failed_dependencies(self):
        workflow = {"name": "continue-independent-work", "steps": [
            {"id": "bad", "tool": "test.fail", "args": {}},
            {"id": "dependent", "tool": "test.echo", "depends_on": ["bad"],
             "args": {"value": "must not run"}},
            {"id": "independent", "tool": "test.echo", "args": {"value": "should run"}},
        ]}
        result = self.engine.execute(workflow)
        self.assertFalse(result.ok)
        by_id = {step["id"]: step["status"] for step in result.steps}
        self.assertEqual(by_id, {"bad": "failed", "dependent": "skipped", "independent": "succeeded"})
        self.assertEqual(self.calls, ["should run"])

    def test_workflow_marks_unverified_step_and_fails_closed(self):
        self.registry.register(ToolSpec("test.unverified", "Tool that returns unverified success", {
            "type": "object", "properties": {}, "additionalProperties": False}, (), "test"),
            lambda ctx, args: ToolResult(True, "test.unverified", {}, "Ran but unverified", verified=False))
        workflow = {"name": "verify-postcondition", "steps": [
            {"id": "step_1", "tool": "test.unverified", "args": {}, "require_verification": True, "verify": "file exists on disk"},
            {"id": "step_2", "tool": "test.echo", "depends_on": ["step_1"], "args": {"value": "must not run"}},
        ]}
        result = self.engine.execute(workflow)
        self.assertFalse(result.ok)
        self.assertFalse(result.verified)
        self.assertEqual(result.error_code, "VERIFICATION_FAILED")
        by_id = {step["id"]: step["status"] for step in result.steps}
        self.assertEqual(by_id["step_1"], "failed")
        self.assertEqual(by_id["step_2"], "skipped")
        self.assertEqual(self.calls, [])


    def test_workflow_timeout_is_validated_and_stops_before_next_step(self):
        from eva.automation.engine.workflows import _MAX_WORKFLOW_SECONDS
        invalid = {"name": "too-long", "timeout_seconds": _MAX_WORKFLOW_SECONDS + 1,
                   "steps": [{"id": "step", "tool": "test.echo", "args": {"value": "x"}}]}
        with self.assertRaises(AutomationError):
            self.engine.validate(invalid)
        short = {"name": "short-budget", "timeout_seconds": 0.000001, "steps": [
            {"id": "first", "tool": "test.echo", "args": {"value": "runs"}},
            {"id": "second", "tool": "test.echo", "args": {"value": "does-not-run"}},
        ]}
        with patch("eva.automation.engine.workflows.monotonic", side_effect=[0.0, 0.0, 1.0]):
            result = self.engine.execute(short)
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "WORKFLOW_TIMEOUT")
        self.assertEqual(self.calls, ["runs"])

    def test_workflow_deadline_enforced_during_active_tool_call(self):
        # A slow tool registered with 20s timeout must be stopped within the workflow deadline
        def slow_handler(ctx, args):
            import time
            for _ in range(50):
                if ctx.cancelled:
                    return None
                time.sleep(0.05)
            return ToolResult(True, "test.slow", {}, "Finished late", verified=True)

        self.registry.register(
            ToolSpec("test.slow", "Slow tool with long default timeout", {
                "type": "object", "properties": {}, "additionalProperties": False
            }, (), "test", timeout_seconds=20.0),
            slow_handler
        )
        workflow = {
            "name": "strict-deadline",
            "timeout_seconds": 0.05,
            "steps": [{"id": "slow_step", "tool": "test.slow", "args": {}}]
        }
        import time
        t0 = time.monotonic()
        result = self.engine.execute(workflow)
        elapsed = time.monotonic() - t0
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "WORKFLOW_TIMEOUT")
        self.assertLess(elapsed, 1.0)

    def test_preview_validates_without_running_tools(self):
        workflow = {"name": "preview", "steps": [{"id": "step", "tool": "test.echo", "args": {"value": "ok"}}]}
        preview = self.engine.preview(workflow)
        self.assertTrue(preview["ok"])
        self.assertTrue(preview["dry_run"])
        self.assertEqual(self.calls, [])

    def test_bundled_yaml_workflow_loads_and_previews(self):
        workflow = load_workflow(Path(__file__).parents[2] / "config" / "workflows" / "find_pdfs.yaml")
        # The workflow is designed for filesystem tools; validate it using a registry with that family.
        with tempfile.TemporaryDirectory() as td:
            service = AutomationService(allowed_roots=[td], audit_path=Path(td) / "audit.jsonl")
            preview = service.workflows.preview(workflow, {"path": td})
            self.assertTrue(preview["ok"])
            self.assertEqual(preview["steps"][0]["tool"], "files.search")
            service.close()

    def test_all_shipped_workflows_reference_registered_tools_and_preview(self):
        workflows_dir = Path(__file__).parents[2] / "config" / "workflows"
        with tempfile.TemporaryDirectory() as td:
            service = AutomationService(allowed_roots=[td], audit_path=None)
            paths = sorted([*workflows_dir.glob("*.yaml"), *workflows_dir.glob("*.yml"), *workflows_dir.glob("*.json")])
            self.assertGreaterEqual(len(paths), 7)
            for path in paths:
                workflow = load_workflow(path)
                values = {}
                for name, rule in workflow.get("inputs", {}).items():
                    if name == "url":
                        values[name] = "https://example.com"
                    elif name == "pattern":
                        values[name] = "*.pdf"
                    else:
                        values[name] = str(Path(td) / (name + "-sample"))
                with self.subTest(workflow=path.name):
                    preview = service.workflows.preview(workflow, values)
                    self.assertTrue(preview["ok"])
            service.close()


class FilesystemToolTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = AutomationService(allowed_roots=[self.root], audit_path=self.root / "audit.jsonl")

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_safe_directory_create_and_listing(self):
        result = self.service.execute_tool("files.create_directory", {"path": str(self.root / "reports")})
        listing = self.service.execute_tool("files.list", {"path": str(self.root), "limit": 20})
        self.assertTrue(result.ok, result.as_dict())
        self.assertTrue(result.verified)
        self.assertTrue(listing.ok)
        self.assertIn("reports", [entry["name"] for entry in listing.data["entries"]])

    def test_text_file_creation_is_verified_and_never_overwrites(self):
        target = self.root / "hello.txt"
        first = self.service.execute_tool("files.create_text_file", {"path": str(target), "content": "hello EVA"})
        second = self.service.execute_tool("files.create_text_file", {"path": str(target), "content": "different"})
        self.assertTrue(first.ok, first.as_dict())
        self.assertTrue(first.verified)
        self.assertEqual(target.read_text(encoding="utf-8"), "hello EVA")
        self.assertEqual(second.error_code, "ALREADY_EXISTS")

    def test_search_and_copy_file(self):
        (self.root / "paper.pdf").write_bytes(b"PDF-test")
        search = self.service.execute_tool("files.search", {"path": str(self.root), "pattern": "*.pdf"})
        copy = self.service.execute_tool("files.copy", {"source": str(self.root / "paper.pdf"), "destination": str(self.root / "copy.pdf")})
        self.assertEqual(search.data["count"], 1)
        self.assertTrue(copy.ok, copy.as_dict())
        self.assertEqual((self.root / "copy.pdf").read_bytes(), b"PDF-test")

    def test_path_traversal_and_sensitive_paths_are_blocked(self):
        outside = self.root.parent / "outside.txt"
        traversal = self.service.execute_tool("files.create_text_file", {"path": str(outside), "content": "no"})
        private = self.service.execute_tool("files.list", {"path": str(self.root / ".ssh")})
        self.assertEqual(traversal.error_code, "PATH_OUTSIDE_ALLOWED_ROOTS")
        self.assertIn(private.error_code, {"SENSITIVE_PATH_BLOCKED", "RESOURCE_NOT_FOUND"})
        self.assertFalse(outside.exists())

    def test_copy_refuses_overwrite(self):
        source, destination = self.root / "src.txt", self.root / "dst.txt"
        source.write_text("source", encoding="utf-8")
        destination.write_text("preserve", encoding="utf-8")
        result = self.service.execute_tool("files.copy", {"source": str(source), "destination": str(destination)})
        self.assertEqual(result.error_code, "ALREADY_EXISTS")
        self.assertEqual(destination.read_text(encoding="utf-8"), "preserve")

    def test_move_requires_approval(self):
        source, destination = self.root / "src.txt", self.root / "dst.txt"
        source.write_text("moving", encoding="utf-8")
        blocked = self.service.execute_tool("files.move", {"source": str(source), "destination": str(destination)})
        moved = self.service.execute_tool("files.move", {"source": str(source), "destination": str(destination)}, approved_actions={"files.move"})
        self.assertEqual(blocked.error_code, "APPROVAL_REQUIRED")
        self.assertTrue(moved.ok, moved.as_dict())
        self.assertTrue(destination.exists())
        self.assertFalse(source.exists())

    def test_text_commands_use_deterministic_file_tools(self):
        result = self.service.handle_text(f'create folder named "shortcuts"')
        expected = self.root / "Documents" / "EVA-created-files" / "shortcuts"
        listing = self.service.handle_text(f'list files in "{expected.parent}"')
        self.assertTrue(result["ok"], result)
        self.assertTrue(expected.is_dir())
        self.assertTrue(listing["ok"], listing)

    def test_file_commands_route_to_automation_not_chat(self):
        router = TaskRouter()
        for prompt in ("list files in Downloads", "find files named *.pdf in Documents", "create folder named temp", 'copy file "a.txt" to "b.txt"', 'move file "a.txt" to "b.txt"', "/tools", "approve move", "inspect webpage https://example.com", "create text file notes.txt with content hello"):
            with self.subTest(prompt=prompt):
                self.assertEqual(router.route(prompt).handler, "desktop_automation")


    def test_move_requires_current_run_confirmation_and_confirmation_is_single_use(self):
        source, destination = self.root / "old name.txt", self.root / "new name.txt"
        source.write_text("data", encoding="utf-8")
        request = f'move file "{source}" to "{destination}"'
        confirmation = self.service.handle_text(request)
        self.assertFalse(confirmation["ok"])
        self.assertEqual(confirmation["action"], "approval_required")
        self.assertTrue(source.exists())
        approved = self.service.handle_text("approve move")
        self.assertTrue(approved["ok"], approved)
        self.assertTrue(destination.exists())
        second_approval = self.service.handle_text("approve move")
        self.assertEqual(second_approval["action"], "approval_expired")

    def test_pending_move_approval_is_cleared_by_a_different_automation_request(self):
        source, destination = self.root / "old.txt", self.root / "new.txt"
        source.write_text("data", encoding="utf-8")
        confirmation = self.service.handle_text(f'move file "{source}" to "{destination}"')
        self.assertEqual(confirmation["action"], "approval_required")
        self.service.handle_text(f'list files in "{self.root}"')
        result = self.service.handle_text("approve move")
        self.assertEqual(result["action"], "approval_expired")
        self.assertTrue(source.exists())
        self.assertFalse(destination.exists())

    def test_freeform_typing_requires_single_use_confirmation(self):
        from eva.automation import desktop
        with patch.object(desktop, "route_and_execute", return_value={"ok": True, "action": "type"}) as dispatch:
            confirmation = self.service.handle_text("type hello from EVA")
            self.assertEqual(confirmation["action"], "approval_required")
            dispatch.assert_not_called()
            approved = self.service.handle_text("approve action")
            self.assertTrue(approved["ok"])
            dispatch.assert_called_once_with("type hello from EVA")
            self.assertEqual(self.service.handle_text("approve action")["action"], "approval_expired")
        audit_text = (self.root / "audit.jsonl").read_text(encoding="utf-8")
        self.assertNotIn("hello from EVA", audit_text)
        self.assertIn("legacy_desktop_input_approved", audit_text)

    def test_generic_key_sequence_requires_confirmation_and_mismatched_approval_fails(self):
        from eva.automation import desktop
        with patch.object(desktop, "route_and_execute", return_value={"ok": True, "action": "keys"}) as dispatch:
            confirmation = self.service.handle_text("press keys ctrl+enter")
            self.assertEqual(confirmation["action"], "approval_required")
            wrong = self.service.handle_text("approve move")
            self.assertEqual(wrong["action"], "approval_expired")
            dispatch.assert_not_called()

    def test_pending_typing_approval_is_cleared_by_unrelated_request(self):
        from eva.automation import desktop
        with patch.object(desktop, "route_and_execute", return_value={"ok": True, "action": "type"}) as dispatch:
            self.service.handle_text("type do not run")
            self.service.handle_text("list files in .")
            expired = self.service.handle_text("approve action")
            self.assertEqual(expired["action"], "approval_expired")
            dispatch.assert_not_called()

    def test_approval_action_is_recognized_by_router(self):
        self.assertEqual(TaskRouter().route("approve action").handler, "desktop_automation")

    def test_router_blocks_consequential_actions_even_with_polite_prefix(self):
        router = TaskRouter()
        for prompt in ("delete my files", "please delete my files", "could you restart the PC", "please send an email", "can you install this program"):
            with self.subTest(prompt=prompt):
                self.assertEqual(router.route(prompt).handler, "unavailable")

    def test_send_keys_is_not_misclassified_as_sending_a_message(self):
        self.assertEqual(TaskRouter().route("send keys ctrl+t").handler, "desktop_automation")

    def test_tool_catalog_is_deterministic_and_exposes_schemas(self):
        result = self.service.handle_text("list automation tools")
        self.assertTrue(result["ok"])
        self.assertGreaterEqual(result["count"], 20)
        names = {item["name"] for item in result["tools"]}
        self.assertIn("files.copy", names)
        self.assertIn("browser.navigate", names)
        self.assertIn("desktop.system_info", names)

    def test_web_search_tool_returns_structured_result(self):
        with patch("eva.tools.web_search.search_web", return_value="Search results for: EVA\n1. Example — https://example.com"):
            result = self.service.execute_tool("web.search", {"query": "EVA", "max_results": 1})
        self.assertTrue(result.ok, result.as_dict())
        self.assertIn("https://example.com", result.data["text"])

    def test_web_search_failures_are_not_reported_as_success(self):
        with patch("eva.tools.web_search.search_web", return_value="No usable search results could be extracted."):
            result = self.service.execute_tool("web.search", {"query": "EVA"})
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "EXTERNAL_SERVICE_ERROR")

    def test_local_time_tool_is_registered_and_structured(self):
        result = self.service.execute_tool("system.local_time", {})
        self.assertTrue(result.ok, result.as_dict())
        self.assertTrue(result.data.get("local_time"))

    def test_browser_submission_capabilities_require_explicit_approval(self):
        click = self.service.execute_tool("browser.click", {"selector": "button.delete"})
        download = self.service.execute_tool("browser.download_link", {
            "url": "https://example.com/files", "selector": "a.download", "path": str(self.root / "download.bin")})
        upload = self.service.execute_tool("browser.upload_file", {
            "url": "https://example.com/upload", "selector": "input[type=file]", "path": str(self.root / "input.txt")})
        self.assertEqual(click.error_code, "APPROVAL_REQUIRED")
        self.assertEqual(download.error_code, "APPROVAL_REQUIRED")
        self.assertEqual(upload.error_code, "APPROVAL_REQUIRED")

    def test_csv_json_round_trip_preserves_records(self):
        source = self.root / "source.csv"
        json_path = self.root / "rows.json"
        csv_path = self.root / "roundtrip.csv"
        source.write_text("name,score\nAda,9\nLinus,10\n", encoding="utf-8")
        converted = self.service.execute_tool("data.csv_to_json", {"source": str(source), "destination": str(json_path)})
        returned = self.service.execute_tool("data.json_to_csv", {"source": str(json_path), "destination": str(csv_path)})
        self.assertTrue(converted.ok, converted.as_dict())
        self.assertTrue(returned.ok, returned.as_dict())
        self.assertEqual(json.loads(json_path.read_text(encoding="utf-8")), [{"name": "Ada", "score": "9"}, {"name": "Linus", "score": "10"}])
        with csv_path.open(encoding="utf-8", newline="") as stream:
            self.assertEqual(list(csv.DictReader(stream)), [{"name": "Ada", "score": "9"}, {"name": "Linus", "score": "10"}])

    def test_json_to_csv_neutralizes_dangerous_formulas_and_verifies_cells(self):
        json_path = self.root / "dangerous.json"
        csv_path = self.root / "safe.csv"
        roundtrip_json = self.root / "roundtrip.json"
        records = [
            {"cmd": "=cmd|' /C calc'!A0", "at_formula": "@SUM(1+1)", "plus_formula": "+2*5",
             "neg_num": -42, "pos_float": 3.14, "normal": "Alice"}
        ]
        json_path.write_text(json.dumps(records), encoding="utf-8")
        result = self.service.execute_tool("data.json_to_csv", {"source": str(json_path), "destination": str(csv_path)})
        self.assertTrue(result.ok, result.as_dict())
        self.assertTrue(result.verified)
        raw_csv = csv_path.read_text(encoding="utf-8")
        self.assertIn("'=cmd|", raw_csv)
        self.assertIn("'@SUM(1+1)", raw_csv)
        self.assertIn("'+2*5", raw_csv)
        self.assertIn("-42", raw_csv)
        self.assertIn("3.14", raw_csv)
        # Roundtrip via csv_to_json unescapes safely
        converted = self.service.execute_tool("data.csv_to_json", {"source": str(csv_path), "destination": str(roundtrip_json)})
        self.assertTrue(converted.ok, converted.as_dict())
        self.assertTrue(converted.verified)
        restored = json.loads(roundtrip_json.read_text(encoding="utf-8"))
        self.assertEqual(restored[0]["cmd"], "=cmd|' /C calc'!A0")
        self.assertEqual(restored[0]["at_formula"], "@SUM(1+1)")
        self.assertEqual(restored[0]["plus_formula"], "+2*5")
        self.assertEqual(restored[0]["neg_num"], "-42")
        self.assertEqual(restored[0]["pos_float"], "3.14")
        self.assertEqual(restored[0]["normal"], "Alice")

    def test_csv_roundtrip_preserves_legitimate_apostrophes_and_neutralizes_formulas(self):
        json_path = self.root / "apostrophe_test.json"
        csv_path = self.root / "apostrophe_test.csv"
        roundtrip_json = self.root / "apostrophe_roundtrip.json"
        records = [
            {
                "formula": "=1+1",
                "apostrophe_formula": "'=1+1",
                "at_formula": "'@SUM(A1:A2)",
                "double_apostrophe": "''=1+1",
                "regular_apostrophe": "'hello",
                "normal": "plain text",
            }
        ]
        json_path.write_text(json.dumps(records), encoding="utf-8")
        csv_res = self.service.execute_tool("data.json_to_csv", {"source": str(json_path), "destination": str(csv_path)})
        self.assertTrue(csv_res.ok, csv_res.as_dict())
        self.assertTrue(csv_res.verified)

        # In CSV, leading formula prefixes must be neutralized by apostrophes
        csv_content = csv_path.read_text(encoding="utf-8")
        self.assertIn("'=1+1", csv_content)
        self.assertIn("''=1+1", csv_content)
        self.assertIn("''@SUM(A1:A2)", csv_content)

        # On roundtrip back to JSON, all original values must be preserved exactly
        json_res = self.service.execute_tool("data.csv_to_json", {"source": str(csv_path), "destination": str(roundtrip_json)})
        self.assertTrue(json_res.ok, json_res.as_dict())
        self.assertTrue(json_res.verified)
        restored = json.loads(roundtrip_json.read_text(encoding="utf-8"))
        self.assertEqual(restored[0]["formula"], "=1+1")
        self.assertEqual(restored[0]["apostrophe_formula"], "'=1+1")
        self.assertEqual(restored[0]["at_formula"], "'@SUM(A1:A2)")
        self.assertEqual(restored[0]["double_apostrophe"], "''=1+1")
        self.assertEqual(restored[0]["regular_apostrophe"], "'hello")
        self.assertEqual(restored[0]["normal"], "plain text")

    def test_screenshot_and_lock_screen_require_explicit_approval_in_central_policy(self):
        # desktop.screenshot and desktop.lock_screen are high risk and require approval
        res_shot = self.service.execute_tool("desktop.screenshot", {})
        self.assertFalse(res_shot.ok)
        self.assertEqual(res_shot.error_code, "APPROVAL_REQUIRED")

        res_lock = self.service.execute_tool("desktop.lock_screen", {})
        self.assertFalse(res_lock.ok)
        self.assertEqual(res_lock.error_code, "APPROVAL_REQUIRED")

        # When explicitly approved, approval check passes
        shot_spec = self.service.registry.require("desktop.screenshot").spec
        self.assertTrue(shot_spec.requires_approval)
        self.assertEqual(shot_spec.risk.value, "high")

        lock_spec = self.service.registry.require("desktop.lock_screen").spec
        self.assertTrue(lock_spec.requires_approval)
        self.assertEqual(lock_spec.risk.value, "high")


    def test_zip_create_and_extract_are_verified(self):
        source = self.root / "bundle-source"
        (source / "nested").mkdir(parents=True)
        (source / "nested" / "hello.txt").write_text("hello EVA", encoding="utf-8")
        archive = self.root / "bundle.zip"
        output = self.root / "bundle-output"
        created = self.service.execute_tool("archive.zip_create", {"source": str(source), "destination": str(archive)})
        extracted = self.service.execute_tool("archive.zip_extract", {"source": str(archive), "destination": str(output)})
        self.assertTrue(created.ok, created.as_dict())
        self.assertTrue(extracted.ok, extracted.as_dict())
        self.assertTrue(created.verified)
        self.assertTrue(extracted.verified)
        self.assertEqual((output / "nested" / "hello.txt").read_text(encoding="utf-8"), "hello EVA")

    def test_zip_extraction_rejects_traversal_before_writing(self):
        archive_path = self.root / "malicious.zip"
        outside = self.root.parent / "EVA-archive-traversal-test.txt"
        with zipfile.ZipFile(archive_path, "w") as archive:
            archive.writestr("../EVA-archive-traversal-test.txt", "no")
        result = self.service.execute_tool("archive.zip_extract", {"source": str(archive_path), "destination": str(self.root / "extracted")})
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "UNSAFE_ARCHIVE_PATH")
        self.assertFalse(outside.exists())

    def test_archive_and_data_commands_route_to_automation(self):
        router = TaskRouter()
        for prompt in (
            'convert CSV file "a.csv" to JSON file "a.json"',
            'convert JSON file "a.json" to CSV file "a.csv"',
            'create zip archive from folder "photos" to "photos.zip"',
            'extract zip file "photos.zip" to "photos-output"',
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(router.route(prompt).handler, "desktop_automation")

    def test_data_and_archive_text_commands_invoke_registered_tools(self):
        source = self.root / "rows.csv"
        source.write_text("name,age\nAda,9\n", encoding="utf-8")
        converted = self.service.handle_text(f'convert CSV file "{source}" to JSON file "{self.root / "rows.json"}"')
        self.assertTrue(converted["ok"], converted)
        self.assertTrue((self.root / "rows.json").is_file())
        folder = self.root / "folder"
        folder.mkdir()
        (folder / "notes.txt").write_text("EVA", encoding="utf-8")
        created = self.service.handle_text(f'create zip archive from folder "{folder}" to "{self.root / "folder.zip"}"')
        self.assertTrue(created["ok"], created)
        extracted = self.service.handle_text(f'extract zip file "{self.root / "folder.zip"}" to "{self.root / "folder-output"}"')
        self.assertTrue(extracted["ok"], extracted)
        self.assertEqual((self.root / "folder-output" / "notes.txt").read_text(encoding="utf-8"), "EVA")


class WindowsUIToolTests(unittest.TestCase):
    def test_windows_ui_tools_register_with_approval_for_clicks(self):
        registry = ToolRegistry()
        register_windows_ui_tools(registry)
        self.assertIn("windows.list", registry.names())
        spec = registry.require("windows.click_control").spec
        self.assertTrue(spec.requires_approval)
        self.assertEqual(spec.risk, RiskLevel.HIGH)

    def test_windows_ui_tools_fail_cleanly_off_windows(self):
        if __import__("platform").system().lower() == "windows":
            self.skipTest("This test covers the non-Windows adapter path.")
        registry = ToolRegistry()
        register_windows_ui_tools(registry)
        executor = ToolExecutor(registry, audit=__import__("eva.automation.engine", fromlist=["AuditLogger"]).AuditLogger(None))
        result = executor.execute("windows.list", {})
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "PLATFORM_UNSUPPORTED")


class BrowserValidationTests(unittest.TestCase):
    def test_only_http_and_https_urls_are_allowed(self):
        self.assertEqual(validate_browser_url("https://example.com/path"), "https://example.com/path")
        for value in ("file:///C:/secret.txt", "javascript:alert(1)", "https:///missing-host", "https://user:pass@example.com", "https://example.com\nmalicious"):
            with self.subTest(value=value), self.assertRaises(AutomationError):
                validate_browser_url(value)


if __name__ == "__main__":
    unittest.main()


class CLIApprovalTests(unittest.TestCase):
    def test_run_tool_requires_explicit_high_risk_confirmation(self):
        from contextlib import redirect_stdout
        from io import StringIO
        import main as eva_main

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            service = AutomationService(allowed_roots=[root], audit_path=None)
            source, destination = root / "old.txt", root / "new.txt"
            source.write_text("keep", encoding="utf-8")
            command = f'/run-tool files.move {json.dumps({"source": str(source), "destination": str(destination)})}'
            with patch("main.get_automation_service", return_value=service), patch("builtins.input", return_value="no"), redirect_stdout(StringIO()):
                eva_main._run_tool(command)
            self.assertTrue(source.exists())
            self.assertFalse(destination.exists())
            with patch("main.get_automation_service", return_value=service), patch("builtins.input", return_value="APPROVE files.move"), redirect_stdout(StringIO()):
                eva_main._run_tool(command)
            self.assertFalse(source.exists())
            self.assertTrue(destination.exists())
            service.close()

    def test_run_workflow_collects_approval_for_high_risk_steps(self):
        from contextlib import redirect_stdout
        from io import StringIO
        import main as eva_main

        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            workflows = root / "workflows"
            workflows.mkdir()
            workflow = {
                "name": "move_test_file",
                "inputs": {
                    "source": {"type": "string", "required": True},
                    "destination": {"type": "string", "required": True},
                },
                "steps": [{
                    "id": "move",
                    "tool": "files.move",
                    "args": {"source": "${inputs.source}", "destination": "${inputs.destination}"},
                }],
            }
            (workflows / "move_test_file.json").write_text(json.dumps(workflow), encoding="utf-8")
            service = AutomationService(allowed_roots=[root], audit_path=None, workflows_dir=workflows)
            source, destination = root / "src.txt", root / "dst.txt"
            source.write_text("data", encoding="utf-8")
            inputs = json.dumps({"source": str(source), "destination": str(destination)})
            with patch("main.get_automation_service", return_value=service), patch("builtins.input", return_value="APPROVE WORKFLOW move_test_file"), redirect_stdout(StringIO()):
                eva_main._run_workflow(f"/run-workflow move_test_file {inputs}")
            self.assertFalse(source.exists())
            self.assertTrue(destination.exists())
            service.close()

    def test_blocked_synchronous_handler_interrupted_by_timeout(self):
        import time
        from eva.automation.engine import ToolRegistry, ToolSpec, ToolExecutor
        reg = ToolRegistry()
        def blocked_handler(ctx, args):
            time.sleep(5)
            return {"ok": True}
        reg.register(ToolSpec("test.blocked", "blocked", {"type": "object"}, (), timeout_seconds=0.2), blocked_handler)
        executor = ToolExecutor(reg)
        started = time.monotonic()
        res = executor.execute("test.blocked", {})
        elapsed = time.monotonic() - started
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, "TIMEOUT")
        self.assertLess(elapsed, 2.0, "Executor must interrupt blocked handler rather than waiting for it to finish")
        self.assertTrue(res.data.get("timeout"))

    def test_tool_timeout_signals_cooperative_cancellation_without_cancelling_parent(self):
        import time
        from eva.automation.engine import ToolRegistry, ToolSpec, ToolExecutor, ExecutionContext
        reg = ToolRegistry()
        received_ctx = []
        def cancellable_handler(ctx, args):
            received_ctx.append(ctx)
            for _ in range(50):
                if ctx.cancelled:
                    return {"ok": False, "cancelled": True}
                time.sleep(0.05)
            return {"ok": True}
        reg.register(ToolSpec("test.cancellable", "cancellable", {"type": "object"}, (), timeout_seconds=0.2), cancellable_handler)
        executor = ToolExecutor(reg)
        parent_ctx = ExecutionContext()
        res = executor.execute("test.cancellable", {}, context=parent_ctx)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, "TIMEOUT")
        self.assertTrue(res.data.get("cancellation_requested"))
        self.assertFalse(parent_ctx.cancelled, "Parent context must remain uncancelled for independent steps")
        self.assertTrue(received_ctx and received_ctx[0].cancelled, "Handler context must receive cancellation signal")

    def test_legacy_dispatch_approval_enforcement_and_agent_prevention(self):
        from eva.automation.service import AutomationService
        service = AutomationService()
        # 1. Without approval, desktop.legacy_dispatch requires explicit approval
        agent_ctx = ExecutionContext(metadata={"agent_runtime": "local"})
        res = service.executor.execute("desktop.legacy_dispatch", {"command": "open notepad"}, context=agent_ctx)
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, "APPROVAL_REQUIRED")

        # 2. Even with approval, agent context is forbidden from using desktop.legacy_dispatch
        agent_approved_ctx = ExecutionContext(approved_actions=frozenset({"desktop.legacy_dispatch"}),
                                              metadata={"agent_runtime": "local"})
        res_agent = service.executor.execute("desktop.legacy_dispatch", {"command": "open notepad"}, context=agent_approved_ctx)
        self.assertFalse(res_agent.ok)
        self.assertEqual(res_agent.error_code, "PERMISSION_DENIED")

        # 3. Typing commands through legacy dispatch require desktop.type approval
        user_ctx = ExecutionContext(approved_actions=frozenset({"desktop.legacy_dispatch"}))
        res_type = service.executor.execute("desktop.legacy_dispatch", {"command": "type secret password"}, context=user_ctx)
        self.assertFalse(res_type.ok)
        self.assertEqual(res_type.error_code, "APPROVAL_REQUIRED")

        # 4. Sensitive operations (lock screen, screenshot, keys, quit app) require specific action approvals
        res_lock = service.executor.execute("desktop.legacy_dispatch", {"command": "lock computer"}, context=user_ctx)
        self.assertFalse(res_lock.ok)
        self.assertEqual(res_lock.error_code, "APPROVAL_REQUIRED")

        res_shot = service.executor.execute("desktop.legacy_dispatch", {"command": "take a screenshot"}, context=user_ctx)
        self.assertFalse(res_shot.ok)
        self.assertEqual(res_shot.error_code, "APPROVAL_REQUIRED")

        res_quit = service.executor.execute("desktop.legacy_dispatch", {"command": "quit chrome"}, context=user_ctx)
        self.assertFalse(res_quit.ok)
        self.assertEqual(res_quit.error_code, "APPROVAL_REQUIRED")

        # Direct chat handle_text also enforces approval for sensitive commands
        chat_lock = service.handle_text("lock screen")
        self.assertFalse(chat_lock.get("ok"))
        self.assertEqual(chat_lock.get("action"), "approval_required")

        chat_shot = service.handle_text("take a screenshot")
        self.assertFalse(chat_shot.get("ok"))
        self.assertEqual(chat_shot.get("action"), "approval_required")

    def test_tool_timeout_actively_terminates_registered_processes_and_callbacks(self):
        import time
        from unittest.mock import MagicMock
        from eva.automation.engine import ToolRegistry, ToolSpec, ToolExecutor, ExecutionContext
        reg = ToolRegistry()
        mock_proc = MagicMock()
        mock_proc.poll.return_value = None
        callback_called = []
        def handler_with_process(ctx, args):
            ctx.register_process(mock_proc)
            ctx.add_cancel_callback(lambda: callback_called.append(True))
            for _ in range(50):
                if ctx.cancelled:
                    return {"ok": False}
                time.sleep(0.05)
            return {"ok": True}
        reg.register(ToolSpec("test.proc", "proc", {"type": "object"}, (), timeout_seconds=0.2), handler_with_process)
        executor = ToolExecutor(reg)
        res = executor.execute("test.proc", {})
        self.assertFalse(res.ok)
        self.assertEqual(res.error_code, "TIMEOUT")
        self.assertTrue(res.data.get("cancellation_signalled"))
        self.assertTrue(callback_called, "Cancellation callback must be invoked on timeout")
        mock_proc.terminate.assert_called_once()




