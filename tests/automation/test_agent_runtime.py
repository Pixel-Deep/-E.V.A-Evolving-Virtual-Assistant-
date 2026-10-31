from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from eva.automation.service import AutomationService
from eva.automation.engine import RiskLevel, ToolSpec
from eva.automation.task_planner import TaskPlanner


class LocalAgentProvider:
    supports_agent_loop = True
    model = "local-agent-loop-test"

    def __init__(self):
        self.actions = [
            {"action": "plan", "steps": [
                {"id": "step_1", "goal": "Read the local clock", "tools": ["system.local_time"], "depends_on": []},
                {"id": "step_2", "goal": "Draft a status note from the actual time", "tools": ["content.generate_text"], "depends_on": ["step_1"]},
            ]},
            # Invalid schema is rejected before execution; the next turn must repair it.
            {"action": "call_tool", "step_id": "step_1", "tool": "system.local_time", "args": {"unexpected": "value"},
             "purpose": "Read the local clock"},
            {"action": "call_tool", "step_id": "step_1", "tool": "system.local_time", "args": {},
             "purpose": "Read the local clock using its actual schema"},
            {"action": "call_tool", "step_id": "step_2", "tool": "content.generate_text",
             "args": {"prompt": "Write a one-line status note using this actual time: ${steps.step_1.data.local_time}",
                      "style": "concise", "output_kind": "note"},
             "purpose": "Draft the requested status note from the real clock output"},
            {"action": "final", "message": "I read the local clock and generated the requested status note."},
        ]
        self.prompts = []
        self.text_prompt = None

    def generate_agent_action(self, system_prompt, user_prompt, *, timeout=45):
        payload = json.loads(user_prompt)
        self.prompts.append(payload)
        if not self.actions:
            raise AssertionError("Agent requested more turns than the test scenario defines")
        return json.dumps(self.actions.pop(0))

    def generate_text(self, system_prompt, user_prompt, *, timeout=45, num_predict=1200):
        self.text_prompt = user_prompt
        return "Status note: EVA checked the local time and is ready."


class AgentRuntimeTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eva-agent-loop-")
        self.root = Path(self.temp.name)
        self.service = AutomationService(
            allowed_roots=[self.root], audit_path=None,
            workflows_dir=self.root / "workflows",
            capability_gaps_path=self.root / "state" / "gaps.json",
            browser_headless=True,
        )

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()


    def test_agent_preflights_entire_plan_before_any_side_effect(self):
        calls = []
        self.service.registry.register(ToolSpec("test.preflight_safe", "Safe side effect for preflight test", {
            "type": "object", "properties": {}, "additionalProperties": False
        }, (), "test"), lambda ctx, args: (calls.append("safe") or {"ok": True, "verified": True}))
        self.service.registry.register(ToolSpec("test.preflight_blocked", "Forbidden side effect for preflight test", {
            "type": "object", "properties": {}, "additionalProperties": False
        }, ("system.admin",), "test", risk=RiskLevel.CRITICAL),
            lambda ctx, args: calls.append("blocked"))

        class PreflightProvider(LocalAgentProvider):
            def __init__(self):
                self.actions = [{"action": "plan", "steps": [
                    {"id": "step_1", "goal": "Run the safe tool", "tools": ["test.preflight_safe"], "depends_on": []},
                    {"id": "step_2", "goal": "Run the forbidden tool", "tools": ["test.preflight_blocked"], "depends_on": ["step_1"]},
                ]}]
                self.prompts = []
                self.text_prompt = None

        provider = PreflightProvider()
        result = TaskPlanner(self.service, provider, self.service.capability_gaps).run(
            "Run the safe tool and then run the forbidden tool"
        )
        self.assertFalse(result.get("ok"), result)
        self.assertEqual(result.get("action"), "action_blocked")
        self.assertEqual(result.get("error_code"), "PERMISSION_DENIED")
        self.assertFalse(result.get("partial_completion"))
        self.assertEqual(calls, [], "The safe prefix must not execute before the whole plan passes policy preflight")

    def test_agent_observes_results_repairs_invalid_call_and_hands_off_real_output(self):
        provider = LocalAgentProvider()
        planner = TaskPlanner(self.service, provider, self.service.capability_gaps)
        request = "Read the local time and then draft a one-line status note about it."

        result = planner.run(request)

        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result.get("action"), "agent_loop")
        self.assertTrue(result.get("verified"), result)
        steps = result["execution"]["steps"]
        self.assertEqual([step["status"] for step in steps], [
            "rejected_before_execution", "succeeded", "succeeded"
        ])
        self.assertEqual([step["tool"] for step in steps], [
            "system.local_time", "system.local_time", "content.generate_text"
        ])
        self.assertIn("actual time:", provider.text_prompt)
        self.assertIn(datetime.now().strftime("%d %B %Y"), provider.text_prompt)
        self.assertTrue(any(item.get("id") == "step_1" for item in provider.prompts[-1]["observations"]))
        ids = [item.get("id") for item in result["execution"]["steps"]]
        self.assertEqual(len(ids), len(set(ids)), "Rejected proposals must not reuse real execution step IDs")
        self.assertEqual(provider.actions, [])

    def test_agent_cannot_claim_success_after_an_unrepaired_rejected_tool_call(self):
        class UnrepairedProvider(LocalAgentProvider):
            def __init__(self):
                self.actions = [
                    {"action": "plan", "steps": [
                        {"id": "step_1", "goal": "Read the local clock", "tools": ["system.local_time"], "depends_on": []},
                        {"id": "step_2", "goal": "Draft a short status note", "tools": ["content.generate_text"], "depends_on": ["step_1"]},
                    ]},
                    {"action": "call_tool", "step_id": "step_1", "tool": "system.local_time", "args": {"unexpected": True},
                     "purpose": "Read the local time with an invalid argument"},
                    {"action": "call_tool", "step_id": "step_2", "tool": "content.generate_text",
                     "args": {"prompt": "Write a short status note", "style": "concise", "output_kind": "note"},
                     "purpose": "Draft the status note"},
                    {"action": "final", "message": "Everything is complete."},
                ]
                self.prompts = []
                self.text_prompt = None

        provider = UnrepairedProvider()
        result = TaskPlanner(self.service, provider, self.service.capability_gaps).run(
            "Read the local time and draft a status note"
        )
        self.assertFalse(result.get("ok"), result)
        self.assertFalse(result.get("verified"), result)
        self.assertTrue(any(step.get("status") == "rejected_before_execution" for step in result["execution"]["steps"]))
        self.assertTrue(any("incomplete" in str(item).lower() for item in result.get("execution", {}).get("steps", [])) or
                        "bounded" in result.get("message", "").lower() or "AssertionError" in result.get("message", ""))

    def test_agent_orchestrates_office_creation_script_generation_save_and_static_validation(self):
        workbook_path = self.root / "expense-tracker.xlsx"
        script_path = self.root / "read_expenses.py"
        output_root = self.root

        class CrossDomainProvider(LocalAgentProvider):
            def __init__(self):
                self.actions = [
                    {"action": "plan", "steps": [
                        {"id": "step_1", "goal": "Create the expense tracker workbook", "tools": ["files.create_xlsx"], "depends_on": []},
                        {"id": "step_2", "goal": "Generate a Python reader script", "tools": ["content.generate_text"], "depends_on": ["step_1"]},
                        {"id": "step_3", "goal": "Save the generated Python script", "tools": ["files.create_unique_text_file"], "depends_on": ["step_2"]},
                        {"id": "step_4", "goal": "Validate Python syntax", "tools": ["coding.validate_file"], "depends_on": ["step_3"]},
                    ]},
                    {"action": "call_tool", "step_id": "step_1", "tool": "files.create_xlsx",
                     "args": {"path": str(workbook_path), "content": "Date,Category,Description,Amount"},
                     "purpose": "Create the requested expense tracker workbook"},
                    {"action": "call_tool", "step_id": "step_2", "tool": "content.generate_text",
                     "args": {"prompt": "Write a small Python script that reads the workbook at ${steps.step_1.data.path} with openpyxl and prints its sheet title. Return code only.",
                               "style": "correct concise Python source", "output_kind": "other"},
                     "purpose": "Generate source code using the actual workbook path"},
                    {"action": "call_tool", "step_id": "step_3", "tool": "files.create_unique_text_file",
                     "args": {"directory": str(output_root), "filename": "read_expenses.py", "content": "${steps.step_2.data.text}"},
                     "purpose": "Save the generated script without overwriting existing files"},
                    {"action": "call_tool", "step_id": "step_4", "tool": "coding.validate_file",
                     "args": {"path": "${steps.step_3.data.path}"},
                     "purpose": "Statically validate the saved script without executing it"},
                    {"action": "final", "message": "Created the workbook, saved the reader script, and passed static syntax validation."},
                ]
                self.prompts = []
                self.text_prompt = None

            def generate_text(self, system_prompt, user_prompt, *, timeout=45, num_predict=1200):
                self.text_prompt = user_prompt
                import re
                match = re.search(r"workbook at (.+?) with openpyxl", user_prompt)
                path = match.group(1).strip().strip("`\'\"") if match else str(workbook_path)
                return (
                    "from openpyxl import load_workbook\n\n"
                    f"WORKBOOK_PATH = {path!r}\n\n"
                    "def main():\n"
                    "    workbook = load_workbook(WORKBOOK_PATH, read_only=True, data_only=True)\n"
                    "    try:\n"
                    "        print(workbook.active.title)\n"
                    "    finally:\n"
                    "        workbook.close()\n\n"
                    "if __name__ == '__main__':\n"
                    "    main()\n"
                )

        provider = CrossDomainProvider()
        planner = TaskPlanner(self.service, provider, self.service.capability_gaps)
        result = planner.run("Create an Excel expense tracker and a Python script that reads it")
        self.assertTrue(result.get("ok"), result)
        self.assertTrue(result.get("verified"), result)
        self.assertTrue(workbook_path.is_file())
        self.assertTrue(script_path.is_file())
        self.assertEqual(result["execution"]["steps"][-1]["tool"], "coding.validate_file")
        self.assertTrue(result["execution"]["steps"][-1]["result"]["verified"])
        self.assertFalse(result["execution"]["steps"][-1]["result"]["data"]["source_executed"])
        self.assertIn(str(workbook_path), provider.text_prompt)

    def test_agent_can_expand_more_than_two_registered_tool_schemas_in_one_workflow(self):
        target = self.root / "starter-project"
        source = target / "demo_project" / "__main__.py"
        readme = target / "README.md"

        class ExpandingProvider(LocalAgentProvider):
            def __init__(self):
                self.actions = [
                    {"action": "plan", "steps": [
                        {"id": "step_1", "goal": "Create the starter project", "tools": ["coding.scaffold_project"], "depends_on": []},
                        {"id": "step_2", "goal": "Validate generated Python source", "tools": ["coding.validate_file"], "depends_on": ["step_1"]},
                        {"id": "step_3", "goal": "Read the actual README", "tools": ["files.read_document"], "depends_on": ["step_1"]},
                    ]},
                    {"action": "call_tool", "step_id": "step_1", "tool": "coding.scaffold_project",
                     "args": {"template": "python-cli", "target": str(target), "name": "Demo Project"},
                     "purpose": "Create deterministic starter project"},
                    {"action": "call_tool", "step_id": "step_2", "tool": "coding.validate_file", "args": {"path": str(source)},
                     "purpose": "Statically validate generated Python source"},
                    {"action": "call_tool", "step_id": "step_3", "tool": "files.read_document", "args": {"path": str(readme)},
                     "purpose": "Read the actual scaffold README"},
                    {"action": "final", "message": "Created the starter, validated Python syntax, and read its README."},
                ]
                self.prompts = []
                self.text_prompt = None

        provider = ExpandingProvider()
        result = TaskPlanner(self.service, provider, self.service.capability_gaps).run("Do a multi-part local task")
        self.assertTrue(result.get("ok"), result)
        self.assertTrue(source.is_file())
        self.assertTrue(readme.is_file())
        self.assertTrue(any(step.get("tool") == "coding.validate_file" and step.get("status") == "succeeded"
                            for step in result["execution"]["steps"]))
        self.assertTrue(any(step.get("tool") == "files.read_document" and step.get("status") == "succeeded"
                            for step in result["execution"]["steps"]))

    def test_agent_continues_independent_work_after_recording_a_capability_gap(self):
        class PartialProvider(LocalAgentProvider):
            def __init__(self):
                self.actions = [
                    {"action": "plan", "steps": [
                        {"id": "step_1", "goal": "Read the current local time", "tools": ["system.local_time"], "depends_on": []},
                    ], "missing_capabilities": [
                        {"name": "calendar.create_event", "goal": "Create a calendar event", "category": "calendar",
                         "description": "Create a calendar event", "reason": "No calendar connector is registered", "risk": "moderate"}
                    ]},
                    {"action": "call_tool", "step_id": "step_1", "tool": "system.local_time", "args": {}, "purpose": "Read current local time"},
                    {"action": "final", "message": "I could not create the calendar event, but I did read the local time."},
                ]
                self.prompts = []
                self.text_prompt = None

        provider = PartialProvider()
        planner = TaskPlanner(self.service, provider, self.service.capability_gaps)
        result = planner.run("Create a calendar event and tell me the current local time")
        self.assertFalse(result.get("ok"), result)
        self.assertEqual(result.get("missing_capabilities"), ["calendar.create_event"])
        self.assertTrue(result.get("partial_completion"), result)
        self.assertTrue(any(step.get("tool") == "system.local_time" and step.get("status") == "succeeded"
                            for step in result["execution"]["steps"]))
        self.assertIn("continued with feasible independent steps", result.get("message", ""))

    def test_clarification_result_preserves_prior_capability_gaps(self):
        class GapThenQuestionProvider(LocalAgentProvider):
            def __init__(self):
                self.actions = [
                    {"action": "plan", "steps": [
                        {"id": "step_1", "goal": "Prepare a reminder note", "tools": ["content.generate_text"], "depends_on": []},
                    ], "missing_capabilities": [
                        {"name": "calendar.create_event", "goal": "Create a calendar event", "category": "calendar",
                         "description": "Create a calendar event", "reason": "No calendar connector is registered", "risk": "moderate"}
                    ]},
                    {"action": "need_input", "question": "Which date should I use for the remaining supported task?"},
                ]
                self.prompts = []
                self.text_prompt = None

        provider = GapThenQuestionProvider()
        result = TaskPlanner(self.service, provider, self.service.capability_gaps).run(
            "Create a calendar event and prepare a reminder note"
        )
        self.assertEqual(result.get("action"), "need_input")
        self.assertEqual(result.get("missing_capabilities"), ["calendar.create_event"])
        self.assertIn("calendar.create_event", result.get("message", ""))
        self.assertFalse(result.get("partial_completion"))


    def test_recovered_tool_failure_does_not_mark_the_whole_task_incomplete(self):
        from unittest.mock import patch
        from eva.automation.engine.models import ToolResult

        class RecoveringProvider(LocalAgentProvider):
            def __init__(self):
                self.actions = [
                    {"action": "plan", "steps": [
                        {"id": "step_1", "goal": "Draft a concise status note", "tools": ["content.generate_text"], "depends_on": []},
                    ]},
                    {"action": "call_tool", "step_id": "step_1", "tool": "content.generate_text",
                     "args": {"prompt": "Write a generic note", "style": "concise", "output_kind": "note"},
                     "purpose": "First draft attempt"},
                    {"action": "call_tool", "step_id": "step_1", "tool": "content.generate_text",
                     "args": {"prompt": "Write a concise status note about system readiness", "style": "concise", "output_kind": "note"},
                     "purpose": "Correct the failed draft attempt"},
                    {"action": "final", "message": "The status note was drafted after correcting the first attempt."},
                ]
                self.prompts = []
                self.text_prompt = None

        calls = {"count": 0}
        def execute(name, arguments, *, context=None):
            calls["count"] += 1
            if calls["count"] == 1:
                return ToolResult(False, name, message="Content contract did not pass.", error_code="CONTENT_CONTRACT")
            return ToolResult(True, name, {"text": "EVA is ready."}, "Draft created.", verified=True)

        provider = RecoveringProvider()
        planner = TaskPlanner(self.service, provider, self.service.capability_gaps)
        with patch.object(self.service.executor, "execute", side_effect=execute):
            result = planner.run("Draft a concise status note")
        self.assertTrue(result.get("ok"), result)
        self.assertTrue(result.get("verified"), result)
        self.assertEqual(calls["count"], 2)
        self.assertEqual([item.get("status") for item in result["execution"]["steps"] if item.get("tool")],
                         ["failed", "succeeded"])

    def test_agent_rejects_premature_final_until_every_planned_step_succeeds(self):
        class PrematureFinalProvider(LocalAgentProvider):
            def __init__(self):
                self.actions = [
                    {"action": "plan", "steps": [
                        {"id": "step_1", "goal": "Read the local clock", "tools": ["system.local_time"], "depends_on": []},
                        {"id": "step_2", "goal": "Draft a note based on the time", "tools": ["content.generate_text"], "depends_on": ["step_1"]},
                    ]},
                    {"action": "call_tool", "step_id": "step_1", "tool": "system.local_time", "args": {}, "purpose": "Read the clock"},
                    {"action": "final", "message": "Both requested operations are complete."},
                    {"action": "call_tool", "step_id": "step_2", "tool": "content.generate_text",
                     "args": {"prompt": "Draft a one-line status note", "style": "concise", "output_kind": "note"},
                     "purpose": "Complete the required note draft"},
                    {"action": "final", "message": "The clock was read and the status note was drafted."},
                ]
                self.prompts = []
                self.text_prompt = None

        provider = PrematureFinalProvider()
        result = TaskPlanner(self.service, provider, self.service.capability_gaps).run(
            "Read the local time and draft a one-line status note"
        )
        self.assertTrue(result.get("ok"), result)
        self.assertEqual([step["tool"] for step in result["execution"]["steps"] if step.get("tool")],
                         ["system.local_time", "content.generate_text"])
        self.assertTrue(any("Finalization rejected" in " ".join(item.get("feedback", []))
                            for item in provider.prompts if isinstance(item, dict)))

    def test_plan_coverage_rejects_omitted_operation_before_any_tool_runs(self):
        class CoverageRepairProvider(LocalAgentProvider):
            def __init__(self):
                self.actions = [
                    {"action": "plan", "steps": [
                        {"id": "step_1", "goal": "Read the local time", "tools": ["system.local_time"], "depends_on": []},
                    ]},
                    {"action": "plan", "steps": [
                        {"id": "step_1", "goal": "Read the local time", "tools": ["system.local_time"], "depends_on": []},
                        {"id": "step_2", "goal": "Draft a status note", "tools": ["content.generate_text"], "depends_on": ["step_1"]},
                    ]},
                    {"action": "call_tool", "step_id": "step_1", "tool": "system.local_time", "args": {}, "purpose": "Read time"},
                    {"action": "call_tool", "step_id": "step_2", "tool": "content.generate_text",
                     "args": {"prompt": "Draft a status note about the current local time", "style": "concise", "output_kind": "note"},
                     "purpose": "Draft the requested note"},
                    {"action": "final", "message": "Read the time and drafted the note."},
                ]
                self.prompts = []
                self.text_prompt = None

        provider = CoverageRepairProvider()
        result = TaskPlanner(self.service, provider, self.service.capability_gaps).run(
            "Read the local time and then draft a status note"
        )
        self.assertTrue(result.get("ok"), result)
        self.assertTrue(any(any("No planned step" in str(item) for item in prompt.get("feedback", []))
                            for prompt in provider.prompts))
        self.assertEqual([step["tool"] for step in result["execution"]["steps"] if step.get("tool")],
                         ["system.local_time", "content.generate_text"])

    def test_deterministic_math_is_registered_for_compound_workflows(self):
        result = self.service.execute_tool("math.solve", {"problem": "What is 18 percent of 450?"})
        self.assertTrue(result.ok, result.as_dict())
        self.assertTrue(result.verified, result.as_dict())
        self.assertIn("81", result.data.get("answer", ""))
        self.assertTrue(result.data.get("deterministic"))

    def test_compound_math_and_desktop_task_runs_deterministic_math_then_registered_app_tool(self):
        from unittest.mock import patch
        from eva.automation.engine.models import ToolResult

        class MathThenAppProvider(LocalAgentProvider):
            def __init__(self):
                self.actions = [
                    {"action": "plan", "steps": [
                        {"id": "step_1", "goal": "Calculate the requested arithmetic result", "tools": ["math.solve"], "depends_on": []},
                        {"id": "step_2", "goal": "Open YouTube", "tools": ["desktop.open_target"], "depends_on": ["step_1"]},
                    ]},
                    {"action": "call_tool", "step_id": "step_1", "tool": "math.solve",
                     "args": {"problem": "5 + 7"}, "purpose": "Calculate using EVA's deterministic math engine"},
                    {"action": "call_tool", "step_id": "step_2", "tool": "desktop.open_target",
                     "args": {"target": "YouTube"}, "purpose": "Open the requested website after calculation"},
                    {"action": "final", "message": "The calculation returned 12 and the YouTube launch request succeeded."},
                ]
                self.prompts = []
                self.text_prompt = None

        original_execute = self.service.executor.execute
        def safe_execute(name, arguments=None, *, context=None):
            if name == "desktop.open_target":
                return ToolResult(True, name, {"target": arguments["target"]}, "Launch request accepted.", verified=True)
            return original_execute(name, arguments, context=context)

        provider = MathThenAppProvider()
        with patch.object(self.service.executor, "execute", side_effect=safe_execute):
            result = self.service.handle_text("Calculate 5 + 7 and open YouTube", provider=provider)
        self.assertTrue(result.get("ok"), result)
        self.assertTrue(result.get("verified"), result)
        steps = [step for step in result["execution"]["steps"] if step.get("tool")]
        self.assertEqual([step["tool"] for step in steps], ["math.solve", "desktop.open_target"])
        self.assertIn("12", steps[0]["result"]["data"]["answer"])

    def test_compound_calculation_and_app_request_routes_before_single_calculator_shortcut(self):
        calls = []
        def fake_plan_and_execute(request, provider):
            calls.append(request)
            return {"ok": False, "action": "planner_test", "message": "fixture"}
        self.service.plan_and_execute = fake_plan_and_execute
        result = self.service.handle_text("Calculate 5 + 7 and open YouTube")
        self.assertEqual(result.get("action"), "planner_test")
        self.assertEqual(calls, ["Calculate 5 + 7 and open YouTube"])

    def test_agent_does_not_invent_or_execute_unregistered_tools(self):
        class InventingProvider(LocalAgentProvider):
            def __init__(self):
                self.actions = [
                    {"action": "plan", "steps": [
                        {"id": "step_1", "goal": "Read local time as an independent supported action", "tools": ["system.local_time"], "depends_on": []},
                    ], "missing_capabilities": [
                        {"name": "system.launch_everything", "goal": "Launch every application", "category": "system",
                         "description": "Invented capability", "reason": "No registered tool can perform this request", "risk": "unknown"}
                    ]},
                    {"action": "call_tool", "step_id": "step_1", "tool": "system.local_time", "args": {}, "purpose": "Read local time"},
                    {"action": "final", "message": "No registered tool can perform the unsupported part, but the independent supported action completed."},
                ]
                self.prompts = []
                self.text_prompt = None

        provider = InventingProvider()
        planner = TaskPlanner(self.service, provider, self.service.capability_gaps)
        result = planner.run("Do an unfamiliar local automation task safely.")

        self.assertFalse(result.get("ok"), result)
        self.assertIn(result.get("action"), {"agent_loop", "capability_gap"})
        self.assertEqual(result.get("missing_capabilities"), ["system.launch_everything"])
        self.assertTrue(any(step.get("tool") == "system.local_time" and step.get("status") == "succeeded"
                            for step in result.get("execution", {}).get("steps", [])))
        self.assertFalse(any(step.get("tool") == "system.launch_everything"
                             for step in result.get("execution", {}).get("steps", [])), "An unregistered tool is never executed")


if __name__ == "__main__":
    unittest.main()
