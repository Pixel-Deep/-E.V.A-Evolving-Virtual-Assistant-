import unittest

from eva.core.orchestrator import EVAOrchestrator


class MathFollowupTests(unittest.TestCase):
    def setUp(self):
        self.eva = EVAOrchestrator()
        self.eva.memory.add_message("user", "what is (x+1)²")
        self.eva.memory.add_message("assistant", "Simplified expression = (x + 1)**2")

    def test_solve_it_does_not_go_to_llm_for_expression(self):
        answer = self.eva._resolve_math_followup("solve it")
        self.assertIsNotNone(answer)
        self.assertIn("not an equation", answer)
        self.assertIn("x**2 + 2*x + 1", answer)

    def test_expand_it_uses_previous_expression(self):
        answer = self.eva._resolve_math_followup("expand it")
        self.assertEqual(answer, "Expanded expression = x**2 + 2*x + 1")

    def test_unrelated_followup_is_not_assumed_to_be_math(self):
        self.eva.memory.reset()
        self.eva.memory.add_message("user", "How are you?")
        self.eva.memory.add_message("assistant", "I'm well.")
        self.assertIsNone(self.eva._resolve_math_followup("solve it"))


if __name__ == "__main__":
    unittest.main()
