from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from eva.automation.service import AutomationService
from eva.core.task_router import TaskRouter


class FakeProvider:
    def __init__(self, plan: dict, generated_text: str = "Dear Principal,\\nPlease grant me leave for two days.\\nYours faithfully"):
        self.plan = plan
        self.generated_text = generated_text
        self.plan_calls = 0
        self.text_calls = 0
        self.last_prompt_length = 0
        self.last_prompt = ""
        self.last_system_prompt = ""
        self.last_text_prompt = ""
        self.last_text_system_prompt = ""

    def generate_project_json(self, system_prompt, user_prompt, *, timeout=45):
        self.plan_calls += 1
        self.last_prompt_length = len(user_prompt)
        self.last_prompt = user_prompt
        self.last_system_prompt = system_prompt
        return json.dumps(self.plan)

    def generate_text(self, system_prompt, user_prompt, *, timeout=45, num_predict=1200):
        self.text_calls += 1
        self.last_text_prompt = user_prompt
        self.last_text_system_prompt = system_prompt
        return self.generated_text


def letter_plan(directory: Path, *, open_file: bool = False) -> dict:
    steps = [
        {
            "id": "draft",
            "tool": "content.generate_text",
            "purpose": "Draft the requested letter without inventing missing facts",
            "args": {"prompt": "Write a respectful two-day leave application to the principal of XYZ School.",
                     "style": "formal school application"},
            "depends_on": [],
            "verify": "Non-empty draft returned",
        },
        {
            "id": "save",
            "tool": "files.create_unique_text_file",
            "purpose": "Save the letter into a new file without overwriting an existing draft",
            "args": {"directory": str(directory), "filename": "leave-application.txt",
                     "content": "${steps.draft.data.text}"},
            "depends_on": ["draft"],
            "verify": "File exists and contents match the generated draft",
        },
    ]
    if open_file:
        steps.append({
            "id": "open", "tool": "desktop.open_text_file",
            "purpose": "Open the saved application in the text editor",
            "args": {"path": "${steps.save.data.path}"},
            "depends_on": ["save"],
            "verify": "Editor launch requested",
        })
    return {"goal": "Write a leave application in a text editor", "steps": steps,
            "needs_input": [], "missing_capabilities": []}


class TaskPlannerIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.gap_path = self.root / "state" / "capability_gaps.json"
        self.service = AutomationService(allowed_roots=[self.root], audit_path=None,
                                         workflows_dir=self.root / "workflows",
                                         capability_gaps_path=self.gap_path, browser_headless=True)

    def tearDown(self):
        self.service.close()
        self.temp.cleanup()

    def test_plan_hands_real_draft_output_to_unique_file_tool(self):
        destination = self.root / "Documents" / "EVA-created-files"
        provider = FakeProvider(letter_plan(destination),
                                generated_text="Dear Principal,\nPlease grant me leave for two days because of personal work.\nYours faithfully")
        result = self.service.plan_and_execute(
            "Write and save a letter to the principal of XYZ School requesting two days of leave because of personal work.", provider
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["action"], "task_plan")
        self.assertEqual(provider.plan_calls, 0, "The validated deterministic letter plan should not depend on a model planning completion.")
        self.assertEqual(provider.text_calls, 1)
        steps = result["execution"]["steps"]
        self.assertEqual([item["status"] for item in steps], ["succeeded", "succeeded"])
        saved_path = Path(steps[1]["result"]["data"]["path"])
        self.assertTrue(saved_path.is_file())
        self.assertEqual(saved_path.read_text(encoding="utf-8"), provider.generated_text)
        self.assertTrue(steps[1]["result"]["verified"])
        self.assertLess(provider.last_prompt_length, 12000)

    def test_open_notepad_plan_generates_and_types_without_browser_or_store_fallback(self):
        provider = FakeProvider(letter_plan(self.root / "unused"),
                                generated_text="To the Principal, XYZ School\nDate: 22 October 2026\nPlease grant me leave for personal work.")
        calls = []
        with patch("eva.automation.desktop.execute", side_effect=lambda action, value="", **kwargs:
                   (calls.append((action, value)) or {"ok": True, "verified": True, "message": "Mocked desktop action completed"})):
            result = self.service.plan_and_execute(
                "Open Notepad and write a letter to the principal of XYZ School requesting two days of leave because of personal work.", provider
            )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["action"], "task_plan")
        self.assertEqual(provider.plan_calls, 0)
        self.assertEqual([step["status"] for step in result["execution"]["steps"]], ["succeeded", "succeeded", "succeeded", "succeeded"])
        self.assertEqual(calls, [("app_open", "notepad"), ("notepad_new_document", ""), ("type", provider.generated_text)])
        self.assertFalse((self.root / "unused").exists(), "A no-save request should not create a separate file.")


    def test_notepad_application_without_a_purpose_asks_one_focused_question(self):
        provider = FakeProvider({"goal": "Write a leave application in Notepad", "steps": [],
                                 "needs_input": [], "missing_capabilities": []})
        with patch("eva.automation.desktop.execute") as desktop_execute:
            result = self.service.plan_and_execute(
                "Open Notepad and write an application to the principal of XYZ School.", provider
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["action"], "need_input")
        self.assertEqual(result["questions"], ["What is the purpose of the letter—what should it say or request?"])
        desktop_execute.assert_not_called()
        self.assertEqual(provider.plan_calls, 0)
        self.assertEqual(provider.text_calls, 0)

    def test_resolve_and_read_docx_plan_hands_off_actual_path_and_text(self):
        import zipfile
        target = self.root / "project" / "main.docx"
        target.parent.mkdir(parents=True)
        with zipfile.ZipFile(target, "w") as archive:
            archive.writestr("word/document.xml", "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'><w:body><w:p><w:r><w:t>REAL DOCX CONTENT</w:t></w:r></w:p></w:body></w:document>")
        provider = FakeProvider({
            "goal": "Find main.docs, read the document, and display its text",
            "steps": [
                {"id": "find", "tool": "files.resolve_by_name", "purpose": "Find exact name or likely extension variant",
                 "args": {"path": str(self.root), "filename": "main.docs", "recursive": True}, "depends_on": [],
                 "verify": "Unique file path returned"},
                {"id": "read", "tool": "files.read_document", "purpose": "Extract document text",
                 "args": {"path": "${steps.find.data.path}", "max_bytes": 4096}, "depends_on": ["find"],
                 "verify": "Actual document text returned"},
            ],
            "needs_input": [], "missing_capabilities": [],
        })
        result = self.service.plan_and_execute("Find main.docs, read it and show the text in my terminal", provider)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["execution"]["steps"][1]["result"]["data"]["text"], "REAL DOCX CONTENT")
        from eva.core.orchestrator import _format_task_response
        self.assertIn("REAL DOCX CONTENT", _format_task_response(result))


    def test_generic_leave_application_uses_neutral_conventional_template_without_clarification(self):
        provider = FakeProvider({"goal": "Write a leave application", "steps": [],
                                 "needs_input": [], "missing_capabilities": []},
                                generated_text=("Subject: Leave Application\n\nRespected Principal,\n" 
                                               "I request leave for personal reasons.\n" 
                                               "Yours faithfully,\n[Student Name]"))
        result = self.service.plan_and_execute("Write an application for leave", provider)
        self.assertNotEqual(result.get("action"), "need_input", result)
        self.assertTrue(result.get("ok"), result)
        self.assertGreaterEqual(provider.text_calls, 1)
        self.assertNotIn("What is the purpose", str(result))

    def test_planner_replans_instead_of_asking_for_file_extension(self):
        target = self.root / "main.docx"
        import zipfile
        with zipfile.ZipFile(target, "w") as archive:
            archive.writestr("word/document.xml", "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'><w:body><w:p><w:r><w:t>Found content</w:t></w:r></w:p></w:body></w:document>")
        first = {"goal": "Find and read main.docs", "steps": [],
                 "needs_input": [{"field": "file_type", "question": "What type of document file is that?"}],
                 "missing_capabilities": []}
        second = {"goal": "Find and read main.docs", "steps": [
            {"id": "find", "tool": "files.resolve_by_name", "purpose": "Search exact name and extension fallback",
             "args": {"path": str(self.root), "filename": "main.docs", "recursive": True}, "depends_on": []},
            {"id": "read", "tool": "files.read_document", "purpose": "Read document text",
             "args": {"path": "${steps.find.data.path}"}, "depends_on": ["find"]},
        ], "needs_input": [], "missing_capabilities": []}
        class SequencedProvider(FakeProvider):
            def __init__(self):
                super().__init__(first)
                self.plans = [first, second]
            def generate_project_json(self, system_prompt, user_prompt, *, timeout=45):
                self.plan_calls += 1
                self.last_system_prompt = system_prompt
                self.last_prompt = user_prompt
                return json.dumps(self.plans.pop(0))
        provider = SequencedProvider()
        result = self.service.plan_and_execute("Find main.docs, read it and show the text", provider)
        self.assertTrue(result["ok"], result)
        self.assertEqual(provider.plan_calls, 0, "Exact filename resolution should not ask the planner what type the document is.")
        self.assertEqual(result["execution"]["steps"][1]["result"]["data"]["text"], "Found content")

    def test_leave_letter_without_reason_uses_neutral_template_not_a_clarification(self):
        destination = self.root / "Documents" / "EVA-created-files"
        provider = FakeProvider(letter_plan(destination), generated_text=(
            "Subject: Request for Two Days of Leave\n\nRespected Principal,\n"
            "I respectfully request two days of leave for personal reasons.\n"
            "Yours faithfully,\n[Student Name]"))
        result = self.service.handle_text(
            "Write a letter to the principal of XYZ School asking for two days of leave.", provider=provider
        )
        self.assertNotEqual(result.get("action"), "need_input", result)
        self.assertTrue(result.get("ok"), result)
        self.assertNotIn("What is the purpose", str(result))
        self.assertGreaterEqual(provider.text_calls, 1)

    def test_unspecified_letter_purpose_still_asks_before_opening_notepad(self):
        provider = FakeProvider(letter_plan(self.root / "unused"))
        with patch("eva.tools.system_tools.get_local_datetime", return_value="2026-10-22 10:00:00"), \
             patch("eva.automation.desktop.execute") as desktop_execute:
            result = self.service.handle_text(
                "Open Notepad and write a letter to the principal of XYZ School.", provider=provider
            )
        self.assertFalse(result["ok"])
        self.assertEqual(result["action"], "need_input")
        self.assertEqual(result["questions"], ["What is the purpose of the letter—what should it say or request?"])
        self.assertEqual(provider.plan_calls, 0)
        self.assertEqual(provider.text_calls, 0)
        desktop_execute.assert_not_called()

    def test_email_write_prompt_drafts_without_claiming_or_attempting_to_send(self):
        provider = FakeProvider({
            "goal": "Draft a pending-bill reminder email",
            "steps": [{"id": "draft", "tool": "content.generate_text",
                       "purpose": "Draft a polite reminder without inventing bill details",
                       "args": {"prompt": "Write a polite email to xyz@gmail.com saying a bill remains pending. Do not invent amounts or dates.",
                                "style": "professional and polite"},
                       "depends_on": [], "verify": "Non-empty draft text returned"}],
            "needs_input": [], "missing_capabilities": [],
        }, generated_text="Subject: Friendly reminder\nHello, please check the pending bill status.")
        result = self.service.handle_text("Write an email to xyz@gmail.com about the pending bill", provider=provider)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["action"], "task_plan")
        final_step = result["execution"]["steps"][-1]
        self.assertIn("pending bill", final_step["result"]["data"]["text"])
        self.assertIn("do not address the recipient by their email address", provider.last_text_system_prompt.lower())
        self.assertIn("subject:", provider.last_text_system_prompt.lower())
        self.assertNotIn("gmail.send_message", self.service.registry.names())
        self.assertFalse(self.gap_path.exists(), "Draft-only requests must not create a false send-tool gap")

    def test_main_docs_search_read_prompt_hands_real_text_to_terminal_result(self):
        target = self.root / "project" / "main.docs"
        target.parent.mkdir(parents=True)
        expected = "EVA file search regression\nThis is the real source text."
        target.write_text(expected, encoding="utf-8")
        provider = FakeProvider({
            "goal": "Find and show main.docs contents",
            "steps": [
                {"id": "find", "tool": "files.find_one", "purpose": "Find exactly one main.docs file",
                 "args": {"path": str(self.root), "pattern": "main.docs", "recursive": True},
                 "depends_on": [], "verify": "Exactly one path returned"},
                {"id": "read", "tool": "files.read_text", "purpose": "Read the actual file text",
                 "args": {"path": "${steps.find.data.path}", "max_bytes": 10000},
                 "depends_on": ["find"], "verify": "Actual file text returned"},
            ],
            "needs_input": [], "missing_capabilities": [],
        })
        result = self.service.handle_text(
            "Find the main.docs file, read it, and show the text in my terminal.", provider=provider
        )
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["action"], "task_plan")
        steps = result["execution"]["steps"]
        self.assertEqual([step["status"] for step in steps], ["succeeded", "succeeded"])
        self.assertEqual(steps[1]["result"]["data"]["text"], expected)
        self.assertEqual(steps[1]["result"]["data"]["path"], str(target))

    def test_unknown_tool_is_recorded_and_no_step_executes(self):
        provider = FakeProvider({
            "goal": "Send a Gmail message",
            "steps": [{"id": "send", "tool": "gmail.send_message", "purpose": "Send email",
                       "args": {"to": "xyz@example.com", "body": "unpaid bill"}, "depends_on": []}],
            "needs_input": [], "missing_capabilities": [],
        })
        result = self.service.plan_and_execute("Use a hypothetical Gmail dispatch connector to send a message about a pending bill", provider)
        self.assertEqual(result["action"], "capability_gap")
        self.assertIn("gmail.send_message", result["missing_capabilities"])
        self.assertTrue(self.gap_path.is_file())
        stored = json.loads(self.gap_path.read_text(encoding="utf-8"))
        self.assertEqual(stored["schema_version"], 1)
        self.assertEqual(stored["gaps"][0]["name"], "gmail.send_message")
        self.assertEqual(stored["gaps"][0]["occurrences"], 1)

    def test_malformed_missing_capability_payload_is_rejected_without_false_gap_claim(self):
        provider = FakeProvider({
            "goal": "Send email", "steps": [], "needs_input": [],
            "missing_capabilities": ["gmail.send_message"],
        })
        result = self.service.plan_and_execute("Write an email", provider)
        self.assertEqual(result["action"], "invalid_plan")
        self.assertFalse(self.gap_path.exists())

    def test_unsupported_step_metadata_is_rejected_before_any_step_runs(self):
        destination = self.root / "must-not-exist.txt"
        provider = FakeProvider({
            "goal": "Create file then use malformed step", "needs_input": [], "missing_capabilities": [],
            "steps": [
                {"id": "create", "tool": "files.create_text_file", "args": {"path": str(destination), "content": "not yet"}, "depends_on": []},
                {"id": "later", "tool": "files.create_text_file", "args": {"path": str(self.root / "later.txt"), "content": "x"},
                 "depends_on": [], "shell": "forbidden metadata"},
            ],
        })
        result = self.service.plan_and_execute("Create these files", provider)
        self.assertEqual(result["action"], "invalid_plan")
        self.assertFalse(destination.exists())
        self.assertFalse((self.root / "later.txt").exists())

    def test_same_gap_is_deduplicated_and_occurrence_count_increases(self):
        provider = FakeProvider({
            "goal": "Read Gmail spam",
            "steps": [], "needs_input": [],
            "missing_capabilities": [{"name": "gmail.list_spam_messages", "category": "gmail",
                                      "description": "List unread spam messages", "reason": "No Gmail query tool is registered.",
                                      "risk": "sensitive_data_access", "required_inputs": ["account"],
                                      "dependencies": ["Gmail authorization"]}],
        })
        first = self.service.plan_and_execute("Summarize my unread Gmail spam", provider)
        second = self.service.plan_and_execute("List unread messages in Gmail spam", provider)
        self.assertEqual(first["action"], "capability_gap")
        self.assertEqual(second["action"], "capability_gap")
        stored = json.loads(self.gap_path.read_text(encoding="utf-8"))
        self.assertEqual(len(stored["gaps"]), 1)
        self.assertEqual(stored["gaps"][0]["occurrences"], 2)
        self.assertEqual(len(stored["gaps"][0]["examples"]), 2)

    def test_missing_inputs_are_asked_without_being_logged_as_a_gap(self):
        provider = FakeProvider({
            "goal": "Download a report",
            "steps": [], "needs_input": [{"field": "destination", "question": "Which approved folder should receive the report?"}],
            "missing_capabilities": [],
        })
        result = self.service.plan_and_execute("Download this report", provider)
        self.assertEqual(result["action"], "need_input")
        self.assertFalse(self.gap_path.exists())

    def test_high_risk_plan_waits_for_explicit_one_time_approval(self):
        source = self.root / "source.txt"
        destination = self.root / "destination.txt"
        source.write_text("example", encoding="utf-8")
        provider = FakeProvider({
            "goal": "Move a file",
            "steps": [{"id": "move", "tool": "files.move", "purpose": "Move user-selected file",
                       "args": {"source": str(source), "destination": str(destination)}, "depends_on": []}],
            "needs_input": [], "missing_capabilities": [],
        })
        preview = self.service.plan_and_execute("Move the selected file", provider)
        self.assertEqual(preview["action"], "plan_approval_required")
        self.assertTrue(source.exists())
        self.assertFalse(destination.exists())
        approved = self.service.handle_text("approve plan", provider=provider)
        self.assertEqual(approved["action"], "task_plan")
        self.assertTrue(approved["ok"], approved)
        self.assertFalse(source.exists())
        self.assertTrue(destination.exists())

    def test_unrelated_request_expires_pending_plan(self):
        source = self.root / "source.txt"
        source.write_text("example", encoding="utf-8")
        provider = FakeProvider({
            "goal": "Move a file",
            "steps": [{"id": "move", "tool": "files.move", "purpose": "Move user-selected file",
                       "args": {"source": str(source), "destination": str(self.root / "destination.txt")}, "depends_on": []}],
            "needs_input": [], "missing_capabilities": [],
        })
        self.service.plan_and_execute("Move the selected file", provider)
        # An unrelated direct request clears the one-time plan grant.
        self.service.handle_text("show registered automation tools", provider=provider)
        result = self.service.handle_text("approve plan", provider=provider)
        self.assertEqual(result["action"], "approval_expired")
        self.assertTrue(source.exists())

    def test_normal_chat_turn_expires_pending_plan_via_orchestrator(self):
        from eva.core.orchestrator import EVAOrchestrator
        source = self.root / "source.txt"
        source.write_text("example", encoding="utf-8")
        provider = FakeProvider({
            "goal": "Move a file",
            "steps": [{"id": "move", "tool": "files.move", "purpose": "Move user-selected file",
                       "args": {"source": str(source), "destination": str(self.root / "destination.txt")}, "depends_on": []}],
            "needs_input": [], "missing_capabilities": [],
        })
        preview = self.service.plan_and_execute("Move the selected file", provider)
        self.assertEqual(preview["action"], "plan_approval_required")
        provider.chat_stream = lambda messages: iter(["Hello from the fake provider."])
        with patch("eva.automation.service._default_service", self.service), \
             patch("eva.core.orchestrator.run_desktop_automation",
                   side_effect=lambda text, provider=None: self.service.handle_text(text, provider=provider)):
            assistant = EVAOrchestrator()
            assistant.provider = provider
            normal_chat = assistant.respond("hello")
            self.assertIn("Hello from the fake provider", normal_chat)
            approved = assistant.respond("approve plan")
        self.assertIn("no recent validated plan", approved.lower())
        self.assertTrue(source.exists())


    def test_gmail_spam_opens_exact_mail_url_not_google_search(self):
        # Keep this unit test hermetic: host-publicness is policy-tested separately,
        # and this test checks exact URL construction/dispatch without live DNS.
        with patch("eva.automation.tools.browser._host_is_public", return_value=True), \
             patch("eva.automation.service.platform.system", return_value="Windows"), \
             patch("eva.automation.service.webbrowser.open", return_value=True) as open_url:
            result = self.service.handle_text("open spam folder in my gmail")
        self.assertEqual(result["action"], "gmail_folder_open")
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["data"]["url"], "https://mail.google.com/mail/u/0/#spam")
        open_url.assert_called_once_with("https://mail.google.com/mail/u/0/#spam", new=2, autoraise=True)

    def test_task_router_sends_compound_and_email_requests_to_automation(self):
        router = TaskRouter()
        prompts = [
            "Open Notepad and write a letter to the principal of XYZ School.",
            "Find the main.docs file, read it, and show the text in my terminal.",
            "Write an email to xyz@gmail.com about the pending bill.",
        ]
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                decision = router.route(prompt)
                self.assertEqual(decision.handler, "desktop_automation", decision)


    def test_planner_sends_complete_name_inventory_and_compact_relevant_schemas(self):
        provider = FakeProvider({"goal": "Write a letter", "steps": [], "needs_input": [
            {"field": "task", "question": "Where should the letter be saved?"}
        ], "missing_capabilities": []})
        request = "Prepare a report letter about the architecture of a fictional technical project."
        result = self.service.plan_and_execute(request, provider)
        self.assertEqual(result["action"], "need_input")
        payload = json.loads(provider.last_prompt)
        supplied = {item["name"] for item in payload["tool_specs"]}
        self.assertEqual(set(payload["registered_tool_names"]), set(self.service.registry.names()))
        self.assertLess(len(supplied), len(self.service.registry.names()))
        self.assertIn("content.generate_text", supplied)
        self.assertIn("desktop.open_text_file", supplied)
        self.assertIn("files.create_unique_text_file", supplied)
        self.assertLess(provider.last_prompt_length, 12000)
        self.assertIn("default_output_directory", payload)
        self.assertTrue(payload["default_output_directory"].startswith(str(self.root)))

    def test_current_catalogue_overflow_prompts_reach_model_with_compact_schemas(self):
        prompts = [
            "Prepare a report letter about the architecture of a fictional technical project.",
            "Compose a project newsletter about pending bills and upcoming work.",
            "Analyze a project directory and prepare a report of its document contents.",
        ]
        for prompt_text in prompts:
            with self.subTest(prompt=prompt_text):
                provider = FakeProvider({"goal": "Need a required input", "steps": [], "needs_input": [
                    {"field": "input", "question": "Please provide the missing detail. " + prompt_text[:80]}
                ], "missing_capabilities": []})
                result = self.service.plan_and_execute(prompt_text, provider=provider)
                self.assertEqual(provider.plan_calls, 1, result)
                self.assertNotEqual(result.get("action"), "planner_error", result)
                self.assertLess(provider.last_prompt_length, 12000)
                payload = json.loads(provider.last_prompt)
                self.assertEqual(set(payload["registered_tool_names"]), set(self.service.registry.names()))
                self.assertTrue(payload["tool_specs"])

    def test_filename_without_extension_is_searched_by_basename(self):
        import zipfile
        target = self.root / "project" / "main.docx"
        target.parent.mkdir(parents=True)
        with zipfile.ZipFile(target, "w") as archive:
            archive.writestr("word/document.xml", "<w:document xmlns:w='http://schemas.openxmlformats.org/wordprocessingml/2006/main'><w:body><w:p><w:r><w:t>FOUND BY BASENAME</w:t></w:r></w:p></w:body></w:document>")
        provider = FakeProvider({"goal": "unused", "steps": [], "needs_input": [], "missing_capabilities": []})
        result = self.service.plan_and_execute("Find the main file, read it and show its text in my terminal", provider)
        self.assertTrue(result["ok"], result)
        self.assertEqual(provider.plan_calls, 0)
        self.assertEqual(result["execution"]["steps"][1]["result"]["data"]["text"], "FOUND BY BASENAME")

    def test_legacy_current_date_reference_is_normalized_to_a_registered_time_step(self):
        provider = FakeProvider({
            "goal": "Write a dated note",
            "steps": [{
                "id": "draft", "tool": "content.generate_text", "purpose": "Draft a dated note",
                "args": {"prompt": "Write a short note and include this date context: ${system.local_time.data.local_date}",
                         "style": "clear and natural"},
                "depends_on": [], "verify": "Non-empty text returned",
            }],
            "needs_input": [], "missing_capabilities": [],
        }, generated_text="Date: 22 October 2026\nTeam note")
        result = self.service.plan_and_execute("Write a note for the team", provider)
        self.assertTrue(result["ok"], result)
        steps = result["plan"]["steps"]
        self.assertEqual(steps[0]["tool"], "system.local_time")
        draft = next(step for step in steps if step["tool"] == "content.generate_text")
        self.assertIn("${steps." + steps[0]["id"] + ".data.local_time}", draft["args"]["prompt"])
        self.assertIn(steps[0]["id"], draft["depends_on"])

    def test_planner_questions_with_extra_fields_are_normalized_and_defaultable_dates_removed(self):
        provider = FakeProvider({
            "goal": "Write a team note",
            "steps": [{"id": "draft", "tool": "content.generate_text", "purpose": "Write note",
                       "args": {"prompt": "Write a team note." , "style": "clear"}, "depends_on": []}],
            "needs_input": [{"field": "date", "question": "What date should I put on the note?", "legacy_debug": "extra"}],
            "missing_capabilities": [],
        }, generated_text="Date: 22 October 2026\nTeam note")
        result = self.service.plan_and_execute("Write a note for the team", provider)
        self.assertTrue(result["ok"], result)
        self.assertEqual(result["questions"], [])
        self.assertNotIn("legacy_debug", str(result["plan"]))

    def test_registered_tool_with_omitted_schema_triggers_bounded_replan_before_execution(self):
        class ReplanningProvider:
            def __init__(self):
                self.prompts = []
                self.plan = {"goal": "Read local time", "steps": [
                    {"id": "time", "tool": "system.local_time", "purpose": "Read the local clock",
                     "args": {}, "depends_on": [], "verify": "Actual local timestamp returned"}
                ], "needs_input": [], "missing_capabilities": []}

            def generate_project_json(self, system_prompt, user_prompt, *, timeout=45):
                self.prompts.append(user_prompt)
                return json.dumps(self.plan)

        provider = ReplanningProvider()
        result = self.service.plan_and_execute("Make a complex task", provider)
        self.assertEqual(result["action"], "task_plan", result)
        self.assertTrue(result["ok"], result)
        self.assertEqual(len(provider.prompts), 2)
        first = json.loads(provider.prompts[0])
        second = json.loads(provider.prompts[1])
        self.assertNotIn("system.local_time", {row["name"] for row in first["tool_specs"]})
        self.assertIn("system.local_time", {row["name"] for row in second["tool_specs"]})

    def test_registered_tool_cannot_be_falsely_recorded_as_missing(self):
        provider = FakeProvider({
            "goal": "Search files", "steps": [], "needs_input": [],
            "missing_capabilities": [{"name": "files.search", "category": "files",
                                      "description": "Search files", "reason": "Model mistake"}],
        })
        result = self.service.plan_and_execute("Find a file", provider)
        self.assertEqual(result["action"], "invalid_plan")
        self.assertIn("registered tool", result["error"])
        self.assertFalse(self.gap_path.exists())

    def test_missing_capability_examples_redact_email_urls_and_secrets(self):
        provider = FakeProvider({
            "goal": "Send email", "steps": [], "needs_input": [],
            "missing_capabilities": [{"name": "gmail.send_message", "category": "gmail",
                                      "description": "Send through authorized Gmail", "reason": "No Gmail send tool"}],
        })
        result = self.service.plan_and_execute(
            "Send email to person@example.com using https://example.com/?token=abc and api_key=secret-value", provider
        )
        self.assertEqual(result["action"], "capability_gap")
        stored = json.loads(self.gap_path.read_text(encoding="utf-8"))
        example = stored["gaps"][0]["examples"][0]
        self.assertNotIn("person@example.com", example)
        self.assertNotIn("https://example.com", example)
        self.assertNotIn("secret-value", example)
        self.assertIn("[email]", example)
        self.assertIn("[url]", example)

    def test_planner_executes_feasible_independent_steps_when_later_capability_is_missing(self):
        destination = self.root / "should-not-exist.txt"
        provider = FakeProvider({
            "goal": "Create a file then send email",
            "steps": [
                {"id": "create", "tool": "files.create_text_file", "purpose": "Prepare draft",
                 "args": {"path": str(destination), "content": "draft"}, "depends_on": []},
                {"id": "send", "tool": "gmail.send_message", "purpose": "Send message",
                 "args": {"to": "person@example.com", "body": "draft"}, "depends_on": ["create"]},
            ],
            "needs_input": [], "missing_capabilities": [],
        })
        result = self.service.plan_and_execute("Create a file and send an email", provider)
        self.assertEqual(result["action"], "task_plan")
        self.assertTrue(destination.exists(), "the independent feasible file-creation step should still run")
        self.assertIn("gmail.send_message", result["missing_capabilities"])
        self.assertTrue(result["gap_recorded"])
        self.assertTrue(result["partial_completion"])

    def test_gap_write_failure_is_not_reported_as_successfully_persisted(self):
        self.gap_path.parent.mkdir(parents=True, exist_ok=True)
        self.gap_path.write_text("not-json", encoding="utf-8")
        provider = FakeProvider({
            "goal": "Use missing tool", "steps": [], "needs_input": [],
            "missing_capabilities": [{"name": "gmail.list_messages", "category": "gmail",
                                      "description": "List messages", "reason": "No tool"}],
        })
        result = self.service.plan_and_execute("Read Gmail messages", provider)
        self.assertEqual(result["action"], "capability_gap")
        self.assertFalse(result["gap_recorded"])
        self.assertIn("gap_record_errors", result)
        self.assertNotIn("capability_gap_file", result)

    def test_high_risk_plan_does_not_execute_safe_prefix_before_approval(self):
        draft_path = self.root / "draft.txt"
        moved_path = self.root / "subdir" / "draft.txt"
        provider = FakeProvider({
            "goal": "Create then move file",
            "steps": [
                {"id": "create", "tool": "files.create_text_file", "purpose": "Create draft",
                 "args": {"path": str(draft_path), "content": "hello"}, "depends_on": []},
                {"id": "move", "tool": "files.move", "purpose": "Move it after review",
                 "args": {"source": str(draft_path), "destination": str(moved_path)}, "depends_on": ["create"]},
            ],
            "needs_input": [], "missing_capabilities": [],
        })
        result = self.service.plan_and_execute("Create and move the draft", provider)
        self.assertEqual(result["action"], "plan_approval_required")
        self.assertFalse(draft_path.exists())
        self.assertFalse(moved_path.exists())
        self.assertIn("files.move", result["approval_steps"][0]["tool"])



    def test_find_main_docs_read_it_and_handoff_real_path_and_text(self):
        (self.root / "other").mkdir()
        first = self.root / "other" / "main.docs"
        first.write_text("REAL DOCUMENT TEXT", encoding="utf-8")
        provider = FakeProvider({
            "goal": "Read main.docs and show its text",
            "steps": [
                {"id": "find", "tool": "files.find_one", "purpose": "Find exactly one main.docs file",
                 "args": {"path": str(self.root), "pattern": "main.docs", "recursive": True}, "depends_on": [],
                 "verify": "Exactly one file path returned"},
                {"id": "read", "tool": "files.read_text", "purpose": "Read the located document",
                 "args": {"path": "${steps.find.data.path}", "max_bytes": 4096}, "depends_on": ["find"],
                 "verify": "File contents returned"},
            ],
            "needs_input": [], "missing_capabilities": [],
        })
        result = self.service.plan_and_execute("Find the main.docs file, open it and show the text in the terminal", provider)
        self.assertTrue(result["ok"], result)
        steps = result["execution"]["steps"]
        self.assertEqual(steps[1]["result"]["data"]["text"], "REAL DOCUMENT TEXT")
        self.assertEqual(steps[1]["result"]["data"]["path"], str(first))
        from eva.core.orchestrator import _format_task_response
        formatted = _format_task_response(result)
        self.assertIn("REAL DOCUMENT TEXT", formatted)
        self.assertIn(str(first), formatted)

    def test_find_one_refuses_ambiguous_file_matches(self):
        (self.root / "a").mkdir()
        (self.root / "b").mkdir()
        (self.root / "a" / "main.docs").write_text("one", encoding="utf-8")
        (self.root / "b" / "main.docs").write_text("two", encoding="utf-8")
        result = self.service.execute_tool("files.find_one", {"path": str(self.root), "pattern": "main.docs", "recursive": True})
        self.assertFalse(result.ok)
        self.assertEqual(result.error_code, "AMBIGUOUS_MATCH")
        self.assertEqual(len(result.data["matches"]), 2)

    def test_capability_gap_command_lists_persisted_records(self):
        provider = FakeProvider({
            "goal": "List missing capability", "steps": [], "needs_input": [],
            "missing_capabilities": [{"name": "gmail.list_messages", "category": "gmail",
                                      "description": "List Gmail messages", "reason": "No authorized Gmail read tool"}],
        })
        gap_result = self.service.plan_and_execute("Read Gmail messages", provider)
        self.assertEqual(gap_result["action"], "capability_gap")
        result = self.service.handle_text("/gaps")
        self.assertEqual(result["action"], "capability_gaps")
        self.assertEqual(result["gaps"][0]["name"], "gmail.list_messages")
        self.assertIn("Found 1", result["message"])

    def test_capability_gap_command_is_deterministically_routed(self):
        decision = TaskRouter().route("show capability gaps")
        self.assertEqual(decision.handler, "desktop_automation", decision)

    def test_files_read_text_returns_actual_bounded_text(self):
        path = self.root / "main.docs"
        path.write_text("real file contents", encoding="utf-8")
        result = self.service.execute_tool("files.read_text", {"path": str(path)})
        self.assertTrue(result.ok, result.as_dict())
        self.assertEqual(result.data["text"], "real file contents")
        self.assertIn("sha256", result.data)
        self.assertFalse(result.data["truncated"])

    def test_compound_math_to_clipboard_workflow(self):
        import sys
        if sys.platform not in ("win32", "darwin"):
            self.skipTest("Desktop clipboard requires Windows or macOS desktop environment")
        result = self.service.handle_text("Calculate (1200 / 4) * 3 and copy the result to clipboard")
        self.assertTrue(result.get("ok"), result)
        self.assertEqual(result.get("action"), "task_plan")
        clip = self.service.execute_tool("desktop.clipboard.read", {})
        self.assertTrue(clip.ok)
        self.assertIn("900", clip.data.get("text", ""))

    def test_compound_math_to_clipboard_workflow_plan_structure(self):
        """Cross-platform plan structure check for calculation to clipboard hand-off."""
        from eva.automation.task_planner import TaskPlanner
        planner = TaskPlanner(self.service, None, self.service.capability_gaps)
        plan = planner._deterministic_plan("Calculate (1200 / 4) * 3 and copy the result to clipboard")
        self.assertIsNotNone(plan)
        step_ids = [s["id"] for s in plan["steps"]]
        self.assertEqual(step_ids, ["calculate", "copy_to_clipboard"])
        self.assertEqual(plan["steps"][0]["tool"], "math.solve")
        self.assertEqual(plan["steps"][1]["tool"], "desktop.clipboard.write")

    def test_integer_range_sum_to_file_and_clipboard_is_deterministic(self):
        from eva.automation.task_planner import TaskPlanner
        planner = TaskPlanner(self.service, None, self.service.capability_gaps)
        request = (
            "Calculate the sum of the integers from 1 through 100, save the result and calculation method "
            f"in sum_1_to_100.txt inside {self.root}, and copy the final numerical result to my clipboard."
        )
        plan = planner._deterministic_plan(request)
        self.assertIsNotNone(plan)
        steps = {step["id"]: step for step in plan["steps"]}
        self.assertEqual(steps["calculate"]["tool"], "math.solve")
        self.assertIn("sum of the integers from 1 through 100", steps["calculate"]["args"]["problem"].lower())
        self.assertEqual(steps["copy_to_clipboard"]["args"]["value"], "${steps.calculate.data.value}")
        self.assertEqual(steps["save_calculation_file"]["args"]["path"], str(self.root / "sum_1_to_100.txt"))
        self.assertIn("inclusive arithmetic range sum", steps["save_calculation_file"]["args"]["content"])

    def test_compound_math_to_file_workflow(self):
        result = self.service.handle_text(f"Calculate 15% of 850 and save the result into a text file named tip.txt inside {self.root}")
        self.assertTrue(result.get("ok"), result)
        created = self.root / "tip.txt"
        self.assertTrue(created.is_file())
        content = created.read_text(encoding="utf-8")
        self.assertTrue("255/2" in content or "127.5" in content, content)

    def test_compound_math_to_notepad_plan_structure(self):
        from eva.automation.task_planner import TaskPlanner
        planner = TaskPlanner(self.service, None, self.service.capability_gaps)
        plan = planner._deterministic_plan("Calculate (500 * 24) / 12, copy the result to clipboard, open notepad, and write the answer there")
        self.assertIsNotNone(plan)
        step_ids = [s["id"] for s in plan["steps"]]
        self.assertIn("calculate", step_ids)
        self.assertIn("copy_to_clipboard", step_ids)
        self.assertIn("open_notepad", step_ids)
        self.assertIn("type_calculation_answer", step_ids)

    def test_compound_browser_screenshot_zip_plan_structure(self):
        from eva.automation.task_planner import TaskPlanner
        planner = TaskPlanner(self.service, None, self.service.capability_gaps)
        plan = planner._deterministic_plan("Open browser, go to python.org, extract the headings, take a screenshot, and save everything into a zip file on desktop")
        self.assertIsNotNone(plan)
        step_ids = [s["id"] for s in plan["steps"]]
        self.assertIn("browse", step_ids)
        self.assertIn("capture_screenshot", step_ids)
        self.assertIn("create_zip_archive", step_ids)


if __name__ == "__main__":
    unittest.main()
