"""Evaluation and regression suite for the real Ollama model and hard/twisted prompts."""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

from eva.automation.agent_runtime import LocalAgentRuntime, estimate_tokens, max_prompt_chars
from eva.automation.service import AutomationService
from eva.automation.task_planner import TaskPlanner, max_plan_chars
from eva.core.task_router import TaskRouter
from eva.providers.ollama_provider import OllamaProvider


class LiveModelAndHardPromptsTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.root_dir = tempfile.mkdtemp(prefix="eva-live-eval-")
        cls.root = Path(cls.root_dir)
        cls.service = AutomationService(
            allowed_roots=[cls.root],
            audit_path=cls.root / "audit.jsonl",
            workflows_dir=cls.root / "workflows",
            browser_headless=True,
        )
        cls.router = TaskRouter()
        cls.provider = OllamaProvider()

        # Check if Ollama is accessible
        cls.ollama_online = False
        try:
            import urllib.request
            req = urllib.request.Request(f"{cls.provider.url.replace('/api/chat', '/api/tags')}", method="GET")
            with urllib.request.urlopen(req, timeout=2.0) as resp:
                if resp.status == 200:
                    data = json.loads(resp.read().decode("utf-8"))
                    models = [m.get("name", "") for m in data.get("models", [])]
                    cls.ollama_online = any("qwen2.5-coder" in m or cls.provider.model in m for m in models)
        except Exception:
            cls.ollama_online = False

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.root_dir, ignore_errors=True)

    def test_token_coordination_and_character_limits(self):
        """Coordinate prompt character limits with context window tokens."""
        self.assertEqual(max_prompt_chars(4096), max_plan_chars(4096))
        self.assertLess(max_prompt_chars(4096) / 3.3, 4096, "Prompt limit must not exceed context window")
        # Scaled contexts:
        self.assertLess(max_prompt_chars(2048), max_prompt_chars(4096))
        self.assertGreater(max_prompt_chars(8192), max_prompt_chars(4096))
        self.assertEqual(max_prompt_chars(4096), 6_824)
        self.assertEqual(max_plan_chars(4096), 6_824)
        # Token estimation
        self.assertGreaterEqual(estimate_tokens("A" * 360), 100)

    def test_hard_routing_prompts_distinguish_harmless_from_dangerous(self):
        """Verify that harmless formatting/cleaning operations are permitted while consequential operations remain blocked."""
        # Harmless text & data operations:
        harmless_cases = [
            ("Format this paragraph into bullet points.", "CHAT", "ollama"),
            ("Remove duplicate rows from the spreadsheet.", "DESKTOP_AUTOMATION", "desktop_automation"),
            ("Please format this table into markdown", "CHAT", "ollama"),
            ("Remove empty lines from the text file.", "DESKTOP_AUTOMATION", "desktop_automation"),
            ("Explain how to remove empty lines from a text file.", "EXPLANATION", "ollama"),
            ("Can you delete duplicate records from the dataset?", "DESKTOP_AUTOMATION", "desktop_automation"),
            ("Format code according to PEP 8 standards.", "CHAT", "ollama"),
        ]
        for prompt, expected_cat, expected_handler in harmless_cases:
            with self.subTest(prompt=prompt):
                decision = self.router.route(prompt)
                self.assertEqual(decision.category, expected_cat, f"Prompt {prompt!r} got {decision}")
                self.assertEqual(decision.handler, expected_handler, f"Prompt {prompt!r} got {decision}")
                self.assertTrue(decision.implemented, f"Prompt {prompt!r} should be implemented")

        # Consequential / dangerous operations must be blocked:
        dangerous_cases = [
            "Format drive C:",
            "format disk",
            "delete all files in my documents",
            "remove folder C:\\Windows",
            "could you restart the PC",
            "shutdown the system now",
            "buy items in my shopping cart",
            "can you install this program",
        ]
        for prompt in dangerous_cases:
            with self.subTest(prompt=prompt):
                decision = self.router.route(prompt)
                self.assertEqual(decision.category, "ACTION")
                self.assertEqual(decision.handler, "unavailable")
                self.assertFalse(decision.implemented)

    def test_twisted_and_compound_navigation_prompts(self):
        """Hard compound phrases with conjunctions and mixed targets."""
        twisted = [
            "go to instagram and facebook",
            "open apps in this order Spotify, GitHub, YouTube",
            "open Notepad and Calculator and then open Paint",
        ]
        for prompt in twisted:
            with self.subTest(prompt=prompt):
                decision = self.router.route(prompt)
                self.assertEqual(decision.handler, "desktop_automation")

    def test_live_ollama_model_planning_quality_and_latency(self):
        """When local Ollama is available, evaluate real inference quality, plan structure, and latency."""
        if not self.ollama_online:
            self.skipTest("Local Ollama service with qwen2.5-coder:3b is not reachable on this machine.")

        system_prompt = (
            "You are EVA task planner. Return exactly one valid JSON object with 'goal' and 'steps' list. "
            "No markdown, no backticks. Available tools: ['system.local_time', 'desktop.open_target', 'files.list']."
        )
        user_prompt = "Goal: check local time and list files in current directory."
        t0 = time.monotonic()
        raw = self.provider.generate_project_json(system_prompt, user_prompt, timeout=30)
        latency = time.monotonic() - t0

        self.assertLess(latency, 25.0, f"Model generation took {latency:.2f}s, expected < 25s")
        # Strip potential markdown if model included it
        cleaned = raw.strip()
        if cleaned.startswith("```"):
            cleaned = cleaned.split("\n", 1)[1].rsplit("```", 1)[0].strip()
        data = json.loads(cleaned)
        self.assertIsInstance(data, dict)
        self.assertIn("goal", data)
        self.assertIn("steps", data)
        self.assertIsInstance(data["steps"], list)
        self.assertGreaterEqual(len(data["steps"]), 1)


if __name__ == "__main__":
    unittest.main()
