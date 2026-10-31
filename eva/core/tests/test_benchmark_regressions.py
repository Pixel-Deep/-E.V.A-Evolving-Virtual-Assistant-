"""Regression tests from EVA's adversarial manual smoke-test session."""
import unittest
from eva.tools.math_engine import solve_math
from eva.core.task_router import TaskRouter
from eva.core.orchestrator import EVAOrchestrator


class BenchmarkRegressionTests(unittest.TestCase):
    def test_instruction_prose_is_separated_from_expression(self):
        self.assertIn("x**2 + 9*x - 16", solve_math(
            "Expand and simplify (2x - 3)(x + 4) - (x - 2)²."
        ))
        self.assertIn("x + 3", solve_math(
            "Simplify (x² - 9)/(x - 3), stating every excluded value."
        ))
        self.assertIn("x != 3", solve_math(
            "Simplify (x² - 9)/(x - 3), stating every excluded value."
        ))
        self.assertIn("-5", solve_math(
            "Solve (x - 1)/(x + 2) = 2, including domain restrictions."
        ))
        self.assertIn("x != -2", solve_math(
            "Solve (x - 1)/(x + 2) = 2, including domain restrictions."
        ))

    def test_unicode_subscripts_and_superscript_variables(self):
        self.assertEqual(solve_math("Calculate log₂(32) + log₃(1/27)."), "Result = 2")
        self.assertIn("a**2/b**3", solve_math(
            "Simplify (a³b⁻²)² ÷ (a⁴b⁻¹), assuming a and b are nonzero."
        ))
        self.assertEqual(solve_math("3⁻²"), "Result = 1/9")

    def test_real_and_complex_solution_domains(self):
        self.assertIn("-3/2", solve_math("Solve 3^(2x) = 1/27."))
        self.assertNotIn("I", solve_math("Solve 3^(2x) = 1/27."))
        self.assertIn("No real solutions", solve_math("Solve x² + 1 = 0 over the real numbers"))
        self.assertIn("-I, I", solve_math("Solve x² + 1 = 0 over the complex numbers"))

    def test_real_square_root(self):
        self.assertIn("Abs(x)", solve_math(
            "For real x, simplify √(x²). Do not assume x is positive."
        ))

    def test_trig_bare_school_degrees(self):
        self.assertEqual(solve_math("sin 30 + cos 30"), "Result = 1/2 + sqrt(3)/2")

    def test_word_problem_regressions(self):
        self.assertEqual(solve_math(
            "A train travels 60 km at 30 km/h and another 60 km at 60 km/h. Find its average speed for the whole journey."
        ), "Average speed = 40 km/h")
        self.assertEqual(solve_math(
            "A train travels 120 km at 40 km/h and returns at 60 km/h. Find its average speed."
        ), "Average speed = 48 km/h")
        self.assertEqual(solve_math(
            "Find the distance between (-2, 3) and (4, -5)."
        ), "Distance = 10")
        self.assertEqual(solve_math(
            "A class has 40 students. 60% pass maths, 70% pass English, and 20 pass both. How many pass at least one subject?"
        ), "Students passing at least one subject = 32")
        self.assertEqual(solve_math(
            "A tank is 1/3 full. After adding 20 litres, it is 1/2 full. What is the tank's capacity."
        ), "Tank capacity = 120 litres")

    def test_router_never_sends_math_to_llm(self):
        router = TaskRouter()
        for prompt in (
            "Expand and simplify (2x - 3)(x + 4) - (x - 2)².",
            "Calculate log₂(32) + log₃(1/27).",
            "Find the distance between (-2, 3) and (4, -5).",
            "A tank is 1/3 full. After adding 20 litres, it is 1/2 full. What is the tank's capacity.",
            "Differentiate that result.",
            "Evaluate the derivative at x = 2.",
        ):
            self.assertEqual(router.route(prompt).handler, "math_engine", prompt)

    def test_coordinate_geometry_variants_are_deterministically_routed(self):
        router = TaskRouter()
        prompts = (
            "Find the distance between (-2, 3) and (4, -5).",
            "What is the midpoint of (-2, 3) and (4, -5)?",
            "Calculate the slope between A(-2, 3) and B(4, -5).",
            "Find the gradient of points (1.5, -2) and (3, 4).",
        )
        for prompt in prompts:
            with self.subTest(prompt=prompt):
                self.assertEqual(router.route(prompt).handler, "math_engine", prompt)

    def test_contextual_math_result_and_original_expression(self):
        eva = EVAOrchestrator()
        conversation = [
            ("What is (x + 1)²?", "Simplified expression = (x + 1)**2"),
            ("Now expand it", "Expanded expression = x**2 + 2*x + 1"),
        ]
        for user, assistant in conversation:
            eva.memory.add_message("user", user)
            eva.memory.add_message("assistant", assistant)
        self.assertEqual(eva._resolve_math_followup("Differentiate that result."), "Derivative = 2*x + 2")
        eva.memory.add_message("user", "Differentiate that result.")
        eva.memory.add_message("assistant", "Derivative = 2*x + 2")
        self.assertEqual(eva._resolve_math_followup("Evaluate the derivative at x = 2."),
                         "Derivative evaluated at x = 2 is 6")
        self.assertEqual(eva._resolve_math_followup("Now factor the original expression."),
                         "Factored expression = (x + 1)**2")
        self.assertIn("-4, 2", eva._resolve_math_followup("Solve the original expression equal to 9."))


if __name__ == "__main__":
    unittest.main()
