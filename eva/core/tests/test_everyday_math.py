import unittest
from eva.core.task_router import TaskRouter
from eva.tools.math_engine import solve_math


class EverydayMathCoverageTests(unittest.TestCase):
    def test_algebra_commands(self):
        self.assertIn("x**2 + 2*x + 1", solve_math("expand (x+1)^2"))
        self.assertIn("(x - 2)*(x + 2)", solve_math("factor x^2 - 4"))
        self.assertIn("x + 1", solve_math("simplify (x^2-1)/(x-1)"))
        self.assertIn("2", solve_math("solve x^2 - 4 = 0"))

    def test_trigonometry_and_logs(self):
        self.assertEqual(solve_math("sin(0)"), "Result = 0")
        self.assertEqual(solve_math("sqrt(81)"), "Result = 9")
        self.assertIn("2", solve_math("log(100, 10)"))
        self.assertEqual(solve_math("sin 30 degrees"), "Result = 1/2")
        self.assertEqual(solve_math("sin(30°)"), "Result = 1/2")
        self.assertEqual(solve_math("log base 2 of 8"), "Result = 3")
        self.assertEqual(solve_math("5!"), "Result = 120")
        self.assertEqual(solve_math("absolute value of -5"), "Result = 5")

    def test_arithmetic_regressions(self):
        self.assertEqual(solve_math("2^10 + 3^4 - 5^3"), "Result = 980")
        self.assertEqual(solve_math("1,000,000 divided by 64"), "Result = 15625")
        self.assertEqual(solve_math("0.1 + 0.2"), "Result = 3/10")
        self.assertEqual(solve_math("sin(pi/2)"), "Result = 1")
        self.assertEqual(solve_math("cot(pi/4)"), "Result = 1")
        self.assertEqual(solve_math("sec(0)"), "Result = 1")

    def test_calculus(self):
        self.assertIn("6*x", solve_math("differentiate 3x^2"))
        self.assertEqual(solve_math("differentiate y^3 with respect to y"), "Derivative = 3*y**2")
        self.assertIn("x**2", solve_math("integrate 2x"))
        self.assertIn("2", solve_math("limit of (x^2-1)/(x-1) as x approaches 1"))

    def test_matrices(self):
        self.assertEqual(solve_math("determinant of [[1,2],[3,4]]"), "Determinant = -2")
        self.assertIn("Matrix rank = 2", solve_math("rank of matrix [[1,2],[3,4]]"))
        self.assertIn("Inverse matrix", solve_math("inverse of matrix [[1,0],[0,2]]"))
        with self.assertRaises(ValueError):
            solve_math("determinant of [[1,2,3],[4,5,6]]")

    def test_statistics(self):
        self.assertEqual(solve_math("mean of 2, 4, 6, 8"), "Mean = 5")
        self.assertEqual(solve_math("median of 1, 3, 9"), "Median = 3")
        self.assertEqual(solve_math("mode of 1, 1, 2, 3"), "Mode = 1")

    def test_combinatorics_and_geometry(self):
        self.assertEqual(solve_math("5 choose 2"), "Combination = 10")
        self.assertEqual(solve_math("5 permutations of 2"), "Permutation = 20")
        self.assertEqual(solve_math("distance between A(0,0) and B(3,4)"), "Distance = 5")
        self.assertEqual(solve_math("midpoint of A(0,0) and B(3,4)"), "Midpoint = (3/2, 2)")

    def test_common_requests_route_to_math_engine(self):
        router = TaskRouter()
        for prompt in (
            "expand (x+1)^2", "factor x^2 - 4", "mean of 2, 4, 6",
            "determinant of [[1,2],[3,4]]", "5 choose 2",
            "limit of (x^2-1)/(x-1) as x approaches 1",
            "ratio 12:18", "percentage increase from 80 to 100",
            "5 permutations of 2", "5 combinations of 2", "5!",
        ):
            self.assertEqual(router.route(prompt).handler, "math_engine", prompt)

    def test_geometry_and_probability(self):
        self.assertIn("Area = 25", solve_math("area of rectangle length 5 width 5"))
        self.assertIn("Area = 50", solve_math("area of triangle base 10 height 10"))
        self.assertIn("approximately 28.274", solve_math("area of circle radius 3"))
        self.assertIn("1/6", solve_math("probability of rolling a 4 on a fair six-sided die"))
        self.assertIn("1/8", solve_math("probability of rolling a 3 on a 8-sided die"))
        self.assertIn("3/10", solve_math("probability of 3 favourable outcomes out of 10"))
        self.assertEqual(solve_math("sin 30 degrees"), "Result = 1/2")
        self.assertEqual(solve_math("sin(30°)"), "Result = 1/2")
        self.assertEqual(solve_math("log base 2 of 8"), "Result = 3")
        self.assertEqual(solve_math("5!"), "Result = 120")
        self.assertEqual(solve_math("absolute value of -5"), "Result = 5")

    def test_simultaneous_equations_and_progressions(self):
        answer = solve_math("solve x+y=3 and x-y=1")
        self.assertIn("x = 2", answer)
        self.assertIn("y = 1", answer)
        self.assertEqual(solve_math("AP 2, 5, 8, ... find 10th term"), "AP term 10 = 29")
        self.assertEqual(solve_math("AP 2, 5, 8, ... sum first 10 terms"), "AP sum of first 10 terms = 155")
        self.assertEqual(solve_math("GP 3, 6, 12, ... find 5th term"), "GP term 5 = 48")

    def test_more_geometry(self):
        self.assertEqual(solve_math("area of square side 4"), "Area = 16")
        self.assertEqual(solve_math("volume of cuboid length 2 width 3 depth 4"), "Volume = 24")
        self.assertIn("Volume = 9*pi", solve_math("volume of cylinder radius 3 height 1"))

    def test_ratio_percentage_change_and_line(self):
        self.assertEqual(solve_math("simplify ratio 12:18"), "Simplified ratio = 2:3")
        self.assertEqual(solve_math("percentage increase from 80 to 100"), "Percentage change = 25% (increase)")
        self.assertIn("y = 2*x + (3)", solve_math("equation of a line with slope 2 and y-intercept 3"))

    def test_contextual_followup_variants(self):
        from eva.core.orchestrator import EVAOrchestrator
        eva = EVAOrchestrator()
        eva.memory.add_message("user", "what is (x+1)²")
        eva.memory.add_message("assistant", "Simplified expression = (x + 1)**2")
        self.assertIn("x**2 + 2*x + 1", eva._resolve_math_followup("Then type expand it]"))

    def test_unsafe_or_excessive_input_rejected(self):
        with self.assertRaises(ValueError): solve_math("x.__class__")
        with self.assertRaises(ValueError): solve_math("2^1001")
        with self.assertRaises(ValueError): solve_math("factorial(501)")


if __name__ == "__main__":
    unittest.main()
