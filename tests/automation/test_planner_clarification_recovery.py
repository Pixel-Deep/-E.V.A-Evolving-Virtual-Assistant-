from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eva.automation.service import AutomationService
from eva.automation.task_planner import _deterministic_recovery_payload, _normalize_needs_input


class BrokenClarificationProvider:
    """Simulate the exact noisy clarification JSON emitted by the user's local model."""
    def __init__(self, clarification=None, generated_text="Dear Principal,\nI request leave because I am sick and have a fever.\nThank you."):
        self.clarification = clarification or []
        self.generated_text = generated_text
        self.plan_calls = 0
        self.text_calls = 0
        self.last_prompt = ""

    def generate_project_json(self, system_prompt, user_prompt, *, timeout=45):
        self.plan_calls += 1
        self.last_prompt = user_prompt
        # Intentionally include unsupported extra keys and sometimes omit field.
        return json.dumps({
            "goal": "Local model draft plan",
            "steps": [],
            "needs_input": self.clarification,
            "missing_capabilities": [],
        })

    def generate_text(self, system_prompt, user_prompt, *, timeout=45, num_predict=1200):
        self.text_calls += 1
        return self.generated_text


class PlannerClarificationRecoveryTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.service = AutomationService(
            allowed_roots=[self.root], audit_path=None,
            workflows_dir=self.root / "workflows",
            capability_gaps_path=self.root / "state" / "capability_gaps.json",
            browser_headless=True,
        )

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_unexpected_keys_and_missing_field_are_sanitized(self):
        result = _normalize_needs_input([
            {"question": "Enter recipient's email address", "kind": "required", "debug": "junk"},
            {"prompt": "What should be written?", "field": "message", "priority": 3},
        ], "Write an email to soumyadeep@example.com about the pending bill.")
        self.assertEqual(result, [])

    def test_principal_name_is_optional_but_letter_purpose_is_not(self):
        result = _normalize_needs_input([
            {"question": "What is the name of the school's principal?", "confidence": 0.4}
        ], "Open Notepad and write a letter to the principal of XYZ School.")
        self.assertEqual(result, [])
        payload = _deterministic_recovery_payload(
            self.service, "Open Notepad and write a letter to the principal of XYZ School."
        )
        self.assertIsNotNone(payload)
        self.assertEqual(payload["needs_input"][0]["field"], "purpose")

    def test_complete_letter_recovery_types_into_requested_notepad_without_extra_file(self):
        provider = BrokenClarificationProvider([
            {"question": "What should be written in the letter?", "type": "clarification", "extra": True},
            {"question": "What is the name of the school's principal?", "junk": "x"},
        ], "Dear Principal,\nI request leave for two days because of my fever.\nThank you.")
        request = "Open Notepad and write a letter to the principal (Ms Shila) of XYZ School that I want a leave for 2 days because of my fever."
        calls = []
        with patch("eva.automation.service.platform.system", return_value="Windows"), \
             patch("eva.automation.service.subprocess.Popen") as popen, \
             patch("eva.automation.desktop.execute", side_effect=lambda action, value="", **kwargs:
                   (calls.append((action, value)) or {"ok": True, "verified": True, "message": "Mocked desktop action completed"})):
            popen.return_value.pid = 43210
            result = self.service.handle_text(request, provider=provider)
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result.get("action"), "task_plan")
        steps = result["execution"]["steps"]
        self.assertEqual([x["status"] for x in steps], ["succeeded"] * 4)
        self.assertEqual(calls, [("app_open", "notepad"), ("notepad_new_document", ""), ("type", provider.generated_text)])
        self.assertFalse((self.root / "Documents" / "EVA-created-files" / "letter-to-principal.txt").exists(),
                         "A Notepad-only request must not create an unrequested extra file.")
        popen.assert_not_called()

    def test_email_draft_ignores_redundant_email_and_recipient_name_questions(self):
        provider = BrokenClarificationProvider([
            {"question": "Enter recipient's name (e.g., John Doe)", "field": "recipient_name", "source": "model"},
            {"question": "Enter recipient's email address", "input_type": "email"},
            {"question": "Enter your message for the email", "required": True},
        ], "Subject: Pending bill\nPlease arrange payment for the pending bill at your earliest convenience.")
        result = self.service.handle_text(
            "Write an email to soumyadeesen2006@gmail.com about the pending bill; write that the bills are pending please pay fast.",
            provider=provider,
        )
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result.get("action"), "task_plan")
        text_step = result["execution"]["steps"][0]["result"]["data"]["text"]
        self.assertIn("pending bill", text_step.lower())
        self.assertEqual(provider.text_calls, 1)
        self.assertNotIn("gmail.send_message", self.service.registry.names())

    def test_main_docs_prompt_recovers_from_invalid_needs_input_schema(self):
        target = self.root / "main.docs"
        expected = "Real file content from the smoke test.\nSecond line."
        target.write_text(expected, encoding="utf-8")
        provider = BrokenClarificationProvider([
            {"question": "Each needs_input item must contain a non-empty question and only supported fields.", "debug": "bad"}
        ])
        result = self.service.handle_text(
            "Find the main.docs file in my system, read the file and show me the text in my terminal.",
            provider=provider,
        )
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result.get("action"), "task_plan")
        steps = result["execution"]["steps"]
        self.assertEqual([s["status"] for s in steps], ["succeeded", "succeeded"])
        self.assertEqual(steps[1]["result"]["data"]["text"], expected)

    def test_followup_turns_remain_bound_to_pending_task(self):
        provider = BrokenClarificationProvider([
            {"question": "What is the name of the school's principal?", "unexpected": "ignored"}
        ])
        first = self.service.handle_text(
            "Open Notepad and write a letter to the principal of XYZ School.", provider=provider
        )
        self.assertEqual(first["action"], "need_input", first)
        self.assertTrue(self.service.has_pending_task_clarification())
        # A one-line answer must not be treated as a new unrelated prompt.
        second = self.service.handle_text("Ms Shila", provider=provider)
        self.assertEqual(second["action"], "need_input", second)
        self.assertIn("purpose", " ".join(second.get("questions", [])).lower())
        with patch("eva.automation.service.platform.system", return_value="Windows"), \
             patch("eva.automation.service.subprocess.Popen") as popen, \
             patch("eva.automation.desktop.execute", return_value={"ok": True, "verified": True, "message": "Mocked desktop action completed"}):
            popen.return_value.pid = 12345
            third = self.service.handle_text("I want leave for 2 days because of my fever", provider=provider)
        self.assertEqual(third["action"], "task_plan", third)
        self.assertTrue(third["ok"], third)
        self.assertFalse(self.service.has_pending_task_clarification())

    def test_cancel_clears_clarification_without_execution(self):
        provider = BrokenClarificationProvider([
            {"question": "What should the letter be about?", "metadata": "ignored"}
        ])
        first = self.service.handle_text("Write a letter to the principal of XYZ School", provider=provider)
        self.assertEqual(first["action"], "need_input")
        cancelled = self.service.handle_text("cancel", provider=provider)
        self.assertEqual(cancelled["action"], "task_cancelled")
        self.assertFalse(self.service.has_pending_task_clarification())

    def test_slash_commands_are_not_consumed_as_clarification_answers(self):
        provider = BrokenClarificationProvider([
            {"question": "What should the letter be about?", "metadata": "ignored"}
        ])
        first = self.service.handle_text("Write a letter to the principal of XYZ School", provider=provider)
        self.assertEqual(first["action"], "need_input")
        catalog = self.service.handle_text("/tools", provider=provider)
        self.assertEqual(catalog["action"], "tool_catalog")
        self.assertFalse(self.service.has_pending_task_clarification())

    def test_short_answer_is_routed_back_to_automation_in_orchestrator(self):
        from unittest.mock import patch as mock_patch
        from eva.core.orchestrator import EVAOrchestrator
        import eva.automation.service as service_module

        provider = BrokenClarificationProvider([
            {"question": "What is the name of the school's principal?", "extra": "ignored"}
        ])
        first = self.service.handle_text("Open Notepad and write a letter to the principal of XYZ School.", provider=provider)
        self.assertEqual(first["action"], "need_input")
        self.assertTrue(self.service.has_pending_task_clarification())
        with mock_patch.object(service_module, "_default_service", self.service), \
             mock_patch("eva.core.orchestrator.run_desktop_automation", return_value={
                 "ok": False, "action": "need_input", "goal": "Draft letter",
                 "questions": ["What should the letter be about?"],
                 "message": "No steps executed.",
             }) as automation_call:
            orchestrator = EVAOrchestrator()
            try:
                response = "".join(orchestrator.respond_stream("Ms Shila"))
            finally:
                orchestrator.memory.close() if hasattr(orchestrator.memory, "close") else None
        automation_call.assert_called_once()
        self.assertIn("What should the letter be about?", response)


class ThirtyPromptSmokeMatrix(unittest.TestCase):
    """Thirty noisy/ambiguous variants are covered by validated deterministic recovery."""
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory()
        cls.root = Path(cls.temp.name)
        cls.service = AutomationService(
            allowed_roots=[cls.root], audit_path=None,
            workflows_dir=cls.root / "workflows",
            capability_gaps_path=cls.root / "state" / "capability_gaps.json",
            browser_headless=True,
        )

    @classmethod
    def tearDownClass(cls):
        cls.service.close()
        cls.temp.cleanup()

    def test_30_prompt_variants(self):
        letter_prompts = [
            f"Open Notepad and write a letter to the principal of XYZ School requesting leave for {n} days because I am sick."
            for n in range(1, 11)
        ]
        email_prompts = [
            f"Write an email to user{n}@example.com about the pending bill; please pay soon."
            for n in range(1, 11)
        ]
        file_prompts = [
            f"Find the main.docs file, read it, and show the text in my terminal (search variant {n})."
            for n in range(1, 11)
        ]
        prompts = letter_prompts + email_prompts + file_prompts
        self.assertEqual(len(prompts), 30)
        (self.root / "main.docs").write_text("Verified main.docs contents\nSecond line.", encoding="utf-8")
        provider = BrokenClarificationProvider([
            {"question": "What is the name of the school's principal?", "confidence": 0.4},
            {"question": "Enter recipient's name", "source": "model"},
            {"question": "Enter recipient's email address", "unexpected": "schema-noise"},
            {"question": "What should be written?", "data_type": "text"},
            {"question": "Which folder should I search?", "required": True},
        ])
        with patch("eva.automation.service.platform.system", return_value="Windows"), \
             patch("eva.automation.service.subprocess.Popen") as popen, \
             patch("eva.automation.desktop.execute", return_value={"ok": True, "verified": True, "message": "Mocked desktop action completed"}):
            popen.return_value.pid = 8765
            for prompt in prompts:
                with self.subTest(prompt=prompt):
                    payload = _deterministic_recovery_payload(self.service, prompt)
                    self.assertIsNotNone(payload, f"No deterministic recovery for prompt: {prompt}")
                    self.assertLessEqual(len(payload.get("steps", [])), 12)
                    for step in payload.get("steps", []):
                        self.assertIn(step["tool"], self.service.registry.names())
                    result = self.service.handle_text(prompt, provider=provider)
                    self.assertEqual(result.get("action"), "task_plan", result)
                    self.assertTrue(result.get("ok"), result)
                    self.assertTrue(result.get("execution", {}).get("steps"), result)
        # These high-confidence workflows should not spend a planning-model call
        # merely to receive an empty or malformed plan; only requested prose is generated.
        self.assertEqual(provider.plan_calls, 0)
        self.assertGreaterEqual(provider.text_calls, 20)



if __name__ == "__main__":
    unittest.main()
