import unittest
from decimal import Decimal

from eva.tools.calculator import (
    calculate_expression,
    calculate_from_text,
)
from eva.core.task_router import TaskRouter


class CalculatorTests(unittest.TestCase):

    def test_large_arithmetic(self):
        result = calculate_expression(
            "18743 * 926 + 17892 / 6"
        )
        self.assertEqual(result, Decimal("17359000"))

    def test_arithmetic_progression_sum_expression(self):
        result = calculate_expression("(9 + 97) * 12 / 2")
        self.assertEqual(result, Decimal("636"))

    def test_percentage(self):
        result = calculate_from_text("What is 20% of 500?")
        self.assertEqual(result, "20% of 500 = 100")

    def test_natural_language_calculation(self):
        result = calculate_from_text(
            "Calculate 18,743 × 926 + 17,892 ÷ 6 accurately."
        )
        self.assertTrue(result.endswith("= 17359000"))

    def test_division_by_zero_is_rejected(self):
        with self.assertRaises(ValueError):
            calculate_expression("10 / 0")

    def test_arbitrary_code_is_rejected(self):
        with self.assertRaises(ValueError):
            calculate_expression("__import__('os')")


class RouterTests(unittest.TestCase):

    def setUp(self):
        self.router = TaskRouter()

    def test_arithmetic_uses_calculator(self):
        decision = self.router.route(
            "Calculate 100 * 25 accurately"
        )
        self.assertEqual(decision.handler, "math_engine")

    def test_conversation_uses_language_model(self):
        decision = self.router.route(
            "Let's talk about my day"
        )
        self.assertEqual(decision.handler, "ollama")

    def test_current_information_uses_web_search(self):
        decision = self.router.route(
            "What is the latest Python version?"
        )
        self.assertEqual(decision.category, "WEB_RESEARCH")
        self.assertEqual(decision.handler, "web_search")
        self.assertTrue(decision.implemented)

    def test_typo_in_time_request(self):
        decision = self.router.route("what is the curent time")
        self.assertEqual(decision.handler, "system_time")

    def test_progression_uses_math_solver(self):
        decision = self.router.route(
            "Find the sum of 12 terms of the arithmetic progression "
            "9, 17, 25"
        )
        self.assertEqual(decision.handler, "math_engine")


if __name__ == "__main__":
    unittest.main()