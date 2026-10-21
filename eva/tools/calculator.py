"""Safe, deterministic arithmetic for EVA."""

import ast
import math as _math
import re
import operator
from decimal import (
    Decimal,
    DivisionByZero,
    InvalidOperation,
    localcontext,
)


_BINARY_OPERATORS = {
    ast.Add: operator.add,
    ast.Sub: operator.sub,
    ast.Mult: operator.mul,
    ast.Div: operator.truediv,
    ast.FloorDiv: operator.floordiv,
    ast.Mod: operator.mod,
}

_UNARY_OPERATORS = {
    ast.UAdd: operator.pos,
    ast.USub: operator.neg,
}

_MAX_EXPRESSION_LENGTH = 500
_MAX_AST_NODES = 100
_MAX_EXPONENT = 100

_SCIENTIFIC_FUNCTIONS = {
    "sqrt": lambda x: Decimal(str(_math.sqrt(float(x)))),
    "abs": lambda x: abs(x),
    "log": lambda x: Decimal(str(_math.log(float(x)))),
    "log10": lambda x: Decimal(str(_math.log10(float(x)))),
    "log2": lambda x: Decimal(str(_math.log2(float(x)))),
    "sin": lambda x: Decimal(str(_math.sin(float(x)))),
    "cos": lambda x: Decimal(str(_math.cos(float(x)))),
    "tan": lambda x: Decimal(str(_math.tan(float(x)))),
    "asin": lambda x: Decimal(str(_math.asin(float(x)))),
    "acos": lambda x: Decimal(str(_math.acos(float(x)))),
    "atan": lambda x: Decimal(str(_math.atan(float(x)))),
    "ceil": lambda x: Decimal(_math.ceil(float(x))),
    "floor": lambda x: Decimal(_math.floor(float(x))),
    "round": lambda x: Decimal(round(float(x))),
    "factorial": lambda x: Decimal(_math.factorial(int(x))),
    "exp": lambda x: Decimal(str(_math.exp(float(x)))),
}

_SCIENTIFIC_CONSTANTS = {
    "pi": Decimal(str(_math.pi)),
    "e": Decimal(str(_math.e)),
}



def normalize_expression(expression: str) -> str:
    """Normalize common mathematical symbols and operator words."""

    expression = expression.strip()

    # Convert natural-language percentages into the format
    # already supported by calculate_from_text(), e.g. 15% of 200.
    expression = re.sub(
        r"(-?\d+(?:\.\d+)?)\s+percent\s+of\s+(-?\d+(?:\.\d+)?)",
        r"\1% of \2",
        expression,
        flags=re.IGNORECASE,
    )

    # Remove commas used as thousands separators, not list separators.
    expression = re.sub(
        r"(?<=\d),(?=\d{3}(?:\D|$))",
        "",
        expression,
    )

    replacements = {
        "×": "*",
        "÷": "/",
        "−": "-",
        "–": "-",
        "^": "**",
    }

    for old, new in replacements.items():
        expression = expression.replace(old, new)

    word_replacements = [
        (r"\bmultiplied\s+by\b", "*"),
        (r"\bdivided\s+by\b", "/"),
        (r"\bplus\b", "+"),
        (r"\bminus\b", "-"),
        (r"\btimes\b", "*"),
    ]

    for pattern, replacement in word_replacements:
        expression = re.sub(
            pattern,
            replacement,
            expression,
            flags=re.IGNORECASE,
        )

    return expression.strip()



def _evaluate_node(node: ast.AST) -> Decimal:
    """Evaluate an approved arithmetic syntax tree."""

    if isinstance(node, ast.Expression):
        return _evaluate_node(node.body)

    if isinstance(node, ast.Constant):
        if isinstance(node.value, bool):
            raise ValueError("Boolean values are not valid numbers.")

        if not isinstance(node.value, (int, float)):
            raise ValueError("Only numeric constants are supported.")

        if len(str(node.value)) > 100:
            raise ValueError("Numeric literal is too long.")

        return Decimal(str(node.value))

    if isinstance(node, ast.BinOp):
        # Handle powers separately so the exponent can be restricted.
        if isinstance(node.op, ast.Pow):
            left = _evaluate_node(node.left)
            right = _evaluate_node(node.right)

            if right != right.to_integral_value():
                raise ValueError("Exponent must be an integer.")

            if abs(right) > _MAX_EXPONENT:
                raise ValueError("Exponent is too large.")

            if left == 0 and right < 0:
                raise ValueError("Zero cannot have a negative exponent.")

            return left ** int(right)

        operation_type = type(node.op)

        if operation_type not in _BINARY_OPERATORS:
            raise ValueError("Unsupported arithmetic operator.")

        left = _evaluate_node(node.left)
        right = _evaluate_node(node.right)

        return _BINARY_OPERATORS[operation_type](left, right)

    if isinstance(node, ast.UnaryOp):
        operation_type = type(node.op)

        if operation_type not in _UNARY_OPERATORS:
            raise ValueError("Unsupported unary operator.")

        return _UNARY_OPERATORS[operation_type](
            _evaluate_node(node.operand)
        )

    if isinstance(node, ast.Call):
        if not isinstance(node.func, ast.Name):
            raise ValueError("Only named function calls are supported.")
        func_name = node.func.id.lower()
        if func_name not in _SCIENTIFIC_FUNCTIONS:
            raise ValueError(f"Unsupported function: {func_name}")
        if len(node.args) != 1 or node.keywords:
            raise ValueError(f"{func_name}() takes exactly one argument.")
        arg = _evaluate_node(node.args[0])
        return _SCIENTIFIC_FUNCTIONS[func_name](arg)

    if isinstance(node, ast.Name):
        name = node.id.lower()
        if name in _SCIENTIFIC_CONSTANTS:
            return _SCIENTIFIC_CONSTANTS[name]
        raise ValueError(f"Unknown variable: {node.id}")

    raise ValueError("Expression contains an unsupported operation.")


def calculate_expression(expression: str) -> Decimal:
    """Calculate a mathematical expression without evaluating arbitrary code."""

    expression = normalize_expression(expression)

    if not expression:
        raise ValueError("No arithmetic expression was provided.")

    if len(expression) > _MAX_EXPRESSION_LENGTH:
        raise ValueError("Expression is too long.")

    if not re.fullmatch(r"[0-9a-zA-Z_\s()+\-*/%.,:]+", expression):
        raise ValueError("Expression contains unsupported characters.")

    try:
        syntax_tree = ast.parse(expression, mode="eval")
    except SyntaxError as error:
        raise ValueError("Invalid arithmetic expression.") from error

    if len(list(ast.walk(syntax_tree))) > _MAX_AST_NODES:
        raise ValueError("Expression is too complex.")

    try:
        with localcontext() as context:
            context.prec = 100
            result = _evaluate_node(syntax_tree)

        if not result.is_finite():
            raise ValueError("Result is not a finite number.")

        return result

    except (DivisionByZero, InvalidOperation, ZeroDivisionError) as error:
        raise ValueError(
            "Invalid arithmetic operation, such as division by zero."
        ) from error


def _format_result(value: Decimal) -> str:
    """Format a Decimal without unnecessary trailing zeros."""

    text = format(value, "f")

    if "." in text:
        text = text.rstrip("0").rstrip(".")

    if text in ("-0", ""):
        return "0"

    return text


def _extract_expression(text: str) -> str:
    """Extract a simple arithmetic expression from a natural-language request."""

    normalized = normalize_expression(text)

    # Capture number/operator sequences while ignoring surrounding prose.
    candidates = re.findall(
        r"\d[\d\s()+\-*/%.]*",
        normalized,
    )

    valid_candidates = [
        candidate.strip()
        for candidate in candidates
        if re.search(r"[+\-*/%]", candidate)
        and re.search(r"\d", candidate)
    ]

    if not valid_candidates:
        raise ValueError(
            "Could not identify a supported arithmetic expression."
        )

    # Prefer the longest expression containing mathematical operators.
    return max(valid_candidates, key=len)


def calculate_from_text(text: str) -> str:
    """Calculate an arithmetic request and return a readable result."""

    normalized = normalize_expression(text)

    # Support common percentage questions such as "20% of 500".
    percentage_match = re.search(
        r"(-?\d+(?:\.\d+)?)\s*%\s*of\s*(-?\d+(?:\.\d+)?)",
        normalized,
        flags=re.IGNORECASE,
    )

    if percentage_match:
        percentage = Decimal(percentage_match.group(1))
        number = Decimal(percentage_match.group(2))

        with localcontext() as context:
            context.prec = 100
            result = percentage * number / Decimal("100")

        return (
            f"{percentage}% of {number} = "
            f"{_format_result(result)}"
        )

    expression = _extract_expression(normalized)
    result = calculate_expression(expression)

    return f"{expression} = {_format_result(result)}"