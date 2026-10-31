from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from eva.automation.service import AutomationService
from eva.automation.task_planner import TaskPlanner


class RepairingProvider:
    model = "repair-test-provider"

    def __init__(self):
        self.plan_calls = 0
        self.plan_payloads = []
        self.text_calls = 0

    def generate_project_json(self, system_prompt, user_prompt, *, timeout=45):
        self.plan_calls += 1
        parsed = json.loads(user_prompt)
        self.plan_payloads.append(parsed)
        if self.plan_calls == 1:
            # Deliberately invalid arguments. The planner must reject and repair
            # this draft before any execution can happen.
            return json.dumps({
                "goal": "Draft a status note",
                "steps": [{"id": "clock", "tool": "system.local_time", "purpose": "Read the time",
                           "args": {"unexpected": "value"}, "depends_on": [], "verify": "Time returned"}],
                "needs_input": [], "missing_capabilities": []
            })
        return json.dumps({
            "goal": "Read the local time then draft a status note",
            "steps": [
                {"id": "clock", "tool": "system.local_time", "purpose": "Read the actual local clock",
                 "args": {}, "depends_on": [], "verify": "A local timestamp is returned"},
                {"id": "draft", "tool": "content.generate_text", "purpose": "Draft a one-line status note",
                 "args": {"prompt": "Write a concise status note using this actual local time: ${steps.clock.data.local_time}",
                          "style": "concise", "output_kind": "note"},
                 "depends_on": ["clock"], "verify": "Non-empty note returned"}
            ],
            "needs_input": [], "missing_capabilities": []
        })

    def generate_text(self, system_prompt, user_prompt, *, timeout=45, num_predict=1200):
        self.text_calls += 1
        return "Status note: EVA checked the local time and is ready."


class PlannerSelfRepairTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="eva-plan-repair-")
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

    def test_invalid_tool_arguments_are_repaired_before_any_execution(self):
        provider = RepairingProvider()
        planner = TaskPlanner(self.service, provider, self.service.capability_gaps)
        result = planner.run("Read the local time, then draft a one-line status note about it.")

        self.assertTrue(result.get("ok"), result)
        self.assertEqual(provider.plan_calls, 2)
        self.assertTrue(provider.plan_payloads[1].get("planner_feedback"), provider.plan_payloads[1])
        self.assertEqual(provider.plan_payloads[1]["user_goal"],
                         "Read the local time, then draft a one-line status note about it.")
        self.assertEqual([step["tool"] for step in result["plan"]["steps"]],
                         ["system.local_time", "content.generate_text"])
        self.assertEqual(provider.text_calls, 1)
        self.assertTrue(result.get("verified"), result)

    def test_malformed_json_is_repaired_within_the_bounded_planning_loop(self):
        class MalformedThenValidProvider(RepairingProvider):
            def generate_project_json(self, system_prompt, user_prompt, *, timeout=45):
                self.plan_calls += 1
                parsed = json.loads(user_prompt)
                self.plan_payloads.append(parsed)
                if self.plan_calls == 1:
                    return "Here is the plan: {not valid JSON"
                return json.dumps({
                    "goal": "Read the local time then draft a status note",
                    "steps": [
                        {"id": "clock", "tool": "system.local_time", "purpose": "Read the actual local clock",
                         "args": {}, "depends_on": [], "verify": "A local timestamp is returned"},
                        {"id": "draft", "tool": "content.generate_text", "purpose": "Draft a one-line status note",
                         "args": {"prompt": "Write a concise status note using this actual local time: ${steps.clock.data.local_time}",
                                  "style": "concise", "output_kind": "note"},
                         "depends_on": ["clock"], "verify": "Non-empty note returned"}
                    ],
                    "needs_input": [], "missing_capabilities": []
                })

        provider = MalformedThenValidProvider()
        planner = TaskPlanner(self.service, provider, self.service.capability_gaps)
        result = planner.run("Read the local time, then draft a one-line status note about it.")
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(provider.plan_calls, 2)
        self.assertTrue(provider.plan_payloads[1].get("planner_feedback"), provider.plan_payloads[1])
        self.assertEqual([step["tool"] for step in result["plan"]["steps"]],
                         ["system.local_time", "content.generate_text"])
        self.assertEqual(provider.text_calls, 1)

    def test_schema_gate_catches_wrong_argument_types_and_extra_fields(self):
        provider = RepairingProvider()
        planner = TaskPlanner(self.service, provider, self.service.capability_gaps)
        issues = planner._draft_plan_issues({
            "goal": "Read time",
            "steps": [{"id": "clock", "tool": "system.local_time", "purpose": "Read time",
                       "args": {"unexpected": "value"}, "depends_on": [], "verify": "time returned"}],
            "needs_input": [], "missing_capabilities": []
        }, "Read the local time")
        self.assertTrue(any("unknown argument" in item for item in issues), issues)

    def test_explicit_then_adds_dependency_edges_even_without_data_references(self):
        payload = {"steps": [
            {"id": "open", "tool": "desktop.open_target", "args": {}, "depends_on": []},
            {"id": "type", "tool": "desktop.type", "args": {}, "depends_on": []},
            {"id": "save", "tool": "desktop.keys", "args": {}, "depends_on": []},
        ]}
        fixed = TaskPlanner._enforce_explicit_step_order(
            payload, "Open Notepad, then type the note, then save it."
        )
        self.assertEqual(fixed["steps"][0]["depends_on"], [])
        self.assertEqual(fixed["steps"][1]["depends_on"], ["open"])
        self.assertEqual(fixed["steps"][2]["depends_on"], ["type"])

    def test_clear_ordered_sequence_cannot_be_collapsed_to_one_step(self):
        provider = RepairingProvider()
        planner = TaskPlanner(self.service, provider, self.service.capability_gaps)
        issues = planner._draft_plan_issues({
            "goal": "Read then draft",
            "steps": [{"id": "clock", "tool": "system.local_time", "purpose": "Read time",
                       "args": {}, "depends_on": [], "verify": "time returned"}],
            "needs_input": [], "missing_capabilities": []
        }, "Read the local time, then draft a one-line status note about it.")
        self.assertTrue(any("multiple actions" in item for item in issues), issues)


if __name__ == "__main__":
    unittest.main()
