import unittest
from eva.core.task_router import TaskRouter
from eva.tools.math_engine import solve_math

class DeterministicMathTests(unittest.TestCase):
    def setUp(self):
        self.router = TaskRouter()

    def test_spoken_power_routes_to_engine(self):
        self.assertEqual(self.router.route("what is 7 to the power 4").handler, "math_engine")
        self.assertIn("2401", solve_math("what is 7 to the power 4"))

    def test_spoken_division_and_power(self):
        out = solve_math("121 to the power 11 by 721 to the power 4")
        self.assertIn("Result =", out)
        self.assertNotIn("could not", out.lower())

    def test_superscript_and_precedence(self):
        self.assertIn("67", solve_math("-8 + 3 * (12 - 7)²"))

    def test_thousands_and_division(self):
        self.assertIn("15625", solve_math("1,000,000 divided by 64"))

    def test_division_by_zero_is_not_reported_as_a_result(self):
        with self.assertRaises(ValueError):
            solve_math("0 divided by 0")

    def test_equation(self):
        out = solve_math("solve x^2 - 5x + 6 = 0")
        self.assertIn("2", out)
        self.assertIn("3", out)

    def test_derivative(self):
        self.assertIn("6*x", solve_math("differentiate 3x^2"))


    def test_unicode_superscript_quadratic_equation(self):
        out = solve_math("Solve x² − 5x + 6 = 0")
        self.assertIn("2", out)
        self.assertIn("3", out)

    def test_unicode_superscript_derivative(self):
        self.assertIn("6*x", solve_math("Differentiate 3x²"))

    def test_unicode_negative_superscript(self):
        self.assertIn("1/9", solve_math("3⁻²"))

    def test_solve_it_after_expression_explains_missing_equation(self):
        out = solve_math("solve it after (x+1)²")
        self.assertIn("needs an equation", out)
        self.assertIn("(x+1)²", out)

    def test_solve_it_after_equation_solves(self):
        out = solve_math("solve it after (x+1)² = 9")
        self.assertIn("-4", out)
        self.assertIn("2", out)

    def test_unicode_negative_superscript_routes_to_engine(self):
        self.assertEqual(self.router.route("3⁻²").handler, "math_engine")

    def test_unicode_superscript_expression_routes_to_engine(self):
        self.assertEqual(self.router.route("x²").handler, "math_engine")

    def test_integral(self):
        self.assertIn("x**2", solve_math("integrate 2x"))

    def test_vector_route(self):
        self.assertEqual(self.router.route("find the dot product of vector 3i cap + 4j cap and 2i cap + 5j cap").handler, "math_engine")
        self.assertIn("26", solve_math("find the dot product of vector 3i cap + 4j cap and 2i cap + 5j cap"))

    def test_vector_angle(self):
        out = solve_math("angle between vectors 3i + 4j and 2i + 5j")
        self.assertIn("Angle =", out)

    def test_math_never_routes_to_llm(self):
        for q in ["7 to the power 4", "find the derivative of x^2", "find the angle between two vectors", "what is 17.5 percent of 640 plus 12"]:
            self.assertEqual(self.router.route(q).handler, "math_engine", q)

if __name__ == "__main__":
    unittest.main()
