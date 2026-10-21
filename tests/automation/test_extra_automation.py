import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from unittest.mock import patch
from eva.core.task_router import TaskRouter
import eva.automation.desktop as desktop


class ExtraAutomationTests(unittest.TestCase):
    def setUp(self):
        self.router = TaskRouter()

    def test_new_commands_route_to_desktop(self):
        for prompt in (
            "open Canva", "open Notion", "open new tab", "close tab",
            "refresh page", "scroll down", "scroll to top",
            "find my public ip", "create a Python file named sample",
            "open browser history", "go back", "increase playback speed",
            "zoom in", "open private window", "pause video",
        ):
            with self.subTest(prompt=prompt):
                self.assertEqual(self.router.route(prompt).handler, "desktop_automation")


    def test_browser_and_scroll_commands_dispatch_shortcuts(self):
        original = desktop.execute
        seen = []
        try:
            desktop.execute = lambda action, value="": (seen.append((action, value)) or {"ok": True})
            self.assertTrue(desktop.route_and_execute("open new tab")["ok"])
            self.assertTrue(desktop.route_and_execute("scroll down")["ok"])
            self.assertTrue(desktop.route_and_execute("zoom in")["ok"])
            self.assertEqual(seen, [("keys", "ctrl+t"), ("keys", "down"), ("keys", "ctrl+plus")])
        finally:
            desktop.execute = original

    def test_create_file_uses_safe_directory_and_does_not_overwrite(self):
        with TemporaryDirectory() as temp:
            with patch.object(Path, "home", return_value=Path(temp)):
                first = desktop._create_file("create a Python file named demo")
                second = desktop._create_file("create a Python file named demo")
            self.assertTrue(first["ok"])
            self.assertTrue(Path(first["path"]).is_file())
            self.assertFalse(second["ok"])
            self.assertIn("already exists", second["error"].lower())

    def test_all_website_targets_are_dispatched(self):
        original = desktop.execute
        seen = []
        try:
            desktop.execute = lambda action, value="": (seen.append((action, value)) or {"ok": True, "opened": value})
            result = desktop.route_and_execute("open insta facebook and whatsap")
            self.assertTrue(result["ok"])
            self.assertEqual([target for action, target in seen], ["instagram", "facebook", "whatsapp"])
            self.assertEqual(len(result["results"]), 3)
        finally:
            desktop.execute = original

    def test_mixed_action_keeps_every_step(self):
        original = desktop.execute
        seen = []
        try:
            desktop.execute = lambda action, value="": (seen.append((action, value)) or {"ok": True, "opened": value})
            result = desktop.route_and_execute("play baby on youtube and open github")
            self.assertTrue(result["ok"])
            self.assertEqual(len(result["results"]), 2)
            self.assertEqual(result["results"][0]["action"], "play_youtube")
            self.assertEqual(result["results"][1]["target"], "github")
        finally:
            desktop.execute = original

    def test_multi_open_supports_multiword_sites(self):
        original = desktop.execute
        seen = []
        try:
            desktop.execute = lambda action, value="": (seen.append((action, value)) or {"ok": True})
            result = desktop.route_and_execute("open Google Drive, ChatGPT and Stack Overflow")
            self.assertTrue(result["ok"])
            self.assertEqual([value for action, value in seen], ["google drive", "chatgpt", "stack overflow"])
        finally:
            desktop.execute = original

    def test_partial_failure_is_reported_per_step(self):
        original = desktop.execute
        seen = []
        try:
            def fake(action, value=""):
                seen.append((action, value))
                return {"ok": action == "web", "error": "mock failure"} if action != "web" else {"ok": True}
            desktop.execute = fake
            result = desktop.route_and_execute("open GitHub then increase volume")
            self.assertEqual(result["action"], "multi_action")
            self.assertEqual(len(result["results"]), 2)
            self.assertTrue(result["results"][0]["ok"])
            self.assertFalse(result["results"][1]["ok"])
        finally:
            desktop.execute = original


if __name__ == "__main__":
    unittest.main()
