import unittest
import eva.config as config


class TestConfig(unittest.TestCase):
    def test_online_enabled_matches_documentation(self):
        """Document and verify that online intelligence is enabled as intended."""
        self.assertIsInstance(config.ONLINE_ENABLED, bool)
        self.assertTrue(config.ONLINE_ENABLED, "ONLINE_ENABLED should be True when external web research is enabled")

    def test_parse_bool_env_supports_online_enabled(self):
        from eva.config import _parse_bool_env
        import os
        orig = os.environ.get("EVA_ONLINE_ENABLED")
        try:
            os.environ["EVA_ONLINE_ENABLED"] = "false"
            self.assertFalse(_parse_bool_env("EVA_ONLINE_ENABLED", True))
            os.environ["EVA_ONLINE_ENABLED"] = "0"
            self.assertFalse(_parse_bool_env("EVA_ONLINE_ENABLED", True))
            os.environ["EVA_ONLINE_ENABLED"] = "true"
            self.assertTrue(_parse_bool_env("EVA_ONLINE_ENABLED", False))
            os.environ["EVA_ONLINE_ENABLED"] = "1"
            self.assertTrue(_parse_bool_env("EVA_ONLINE_ENABLED", False))
        finally:
            if orig is not None:
                os.environ["EVA_ONLINE_ENABLED"] = orig
            else:
                os.environ.pop("EVA_ONLINE_ENABLED", None)

    def test_config_basic_types(self):
        self.assertIsInstance(config.OLLAMA_URL, str)
        self.assertIsInstance(config.OLLAMA_MODEL, str)
        self.assertIsInstance(config.OLLAMA_TIMEOUT, int)
        self.assertIsInstance(config.NUM_PREDICT, int)
        self.assertIsInstance(config.NUM_CTX, int)
        self.assertIsInstance(config.MAX_CONTEXT_TURNS, int)
        self.assertIsInstance(config.SYSTEM_PROMPT, str)

    def test_load_dotenv_if_present_parser(self):
        import os
        import tempfile
        from pathlib import Path
        with tempfile.TemporaryDirectory() as tmpdir:
            env_file = Path(tmpdir) / ".env"
            env_file.write_text(
                "# Sample env\n"
                "TEST_DOTENV_ALPHA=hello\n"
                "export TEST_DOTENV_BETA=\"world 123\"\n"
                "TEST_DOTENV_GAMMA='single quote'\n"
                "TEST_DOTENV_EXISTING=new_val\n",
                encoding="utf-8"
            )
            os.environ["TEST_DOTENV_EXISTING"] = "original_val"
            try:
                config.load_dotenv_if_present(env_file)
                self.assertEqual(os.environ.get("TEST_DOTENV_ALPHA"), "hello")
                self.assertEqual(os.environ.get("TEST_DOTENV_BETA"), "world 123")
                self.assertEqual(os.environ.get("TEST_DOTENV_GAMMA"), "single quote")
                self.assertEqual(os.environ.get("TEST_DOTENV_EXISTING"), "original_val")
            finally:
                for k in ["TEST_DOTENV_ALPHA", "TEST_DOTENV_BETA", "TEST_DOTENV_GAMMA", "TEST_DOTENV_EXISTING"]:
                    os.environ.pop(k, None)


if __name__ == "__main__":
    unittest.main()
