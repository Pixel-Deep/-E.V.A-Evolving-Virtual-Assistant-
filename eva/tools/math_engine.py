"""Deterministic mathematics engine for EVA.

Common school/college mathematics is handled with SymPy, with dedicated parsers
for matrices, vectors, statistics, coordinate geometry, and sequences. No maths
request is delegated to a language model. Inputs are length- and complexity-
limited and arbitrary Python names/attributes are rejected.
"""
from __future__ import annotations

import math
import re
from typing import Optional

import sympy as sp
from sympy.parsing.sympy_parser import (
    parse_expr, standard_transformations, implicit_multiplication_application,
    convert_xor, rationalize,
)

_TRANSFORMS = standard_transformations + (
    implicit_multiplication_application, convert_xor, rationalize,
)
MAX_INPUT = 2000
MAX_POWER = 1000
_ALLOWED_NAMES = {
    "pi": sp.pi, "e": sp.E, "sqrt": sp.sqrt, "sin": sp.sin,
    "cos": sp.cos, "tan": sp.tan, "cot": lambda x: 1/sp.tan(x),
    "sec": lambda x: 1/sp.cos(x), "csc": lambda x: 1/sp.sin(x),
    "cosec": lambda x: 1/sp.sin(x), "asin": sp.asin, "acos": sp.acos,
    "atan": sp.atan, "arcsin": sp.asin, "arccos": sp.acos, "arctan": sp.atan,
    "sinh": sp.sinh, "cosh": sp.cosh, "tanh": sp.tanh,
    "log": sp.log, "ln": sp.log, "log10": lambda x: sp.log(x, 10),
    "exp": sp.exp, "abs": sp.Abs,
    "factorial": sp.factorial, "floor": sp.floor, "ceil": sp.ceiling,
    "degrees": lambda x: x * sp.pi / 180, "sqrt": sp.sqrt,
}
_VARIABLES = {"x", "y", "z", "t", "a", "b", "c", "d", "n", "r", "i", "j", "k", "u", "v", "w", "theta", "alpha", "beta", "lambda", "p", "q", "m", "s", "f", "g", "h"}
_SUPERS = {"⁰":"0", "¹":"1", "²":"2", "³":"3", "⁴":"4", "⁵":"5", "⁶":"6", "⁷":"7", "⁸":"8", "⁹":"9"}
_SUP_DIGITS = "⁰¹²³⁴⁵⁶⁷⁸⁹"


def _normalise(text: str) -> str:
    s = text.strip().lower()
    for old, new in (("−", "-"), ("–", "-"), ("×", "*"), ("÷", "/"), ("·", "*"), ("π", " pi "), ("√", " sqrt "), ("²", "²")):
        s = s.replace(old, new)
    # Convert Unicode subscripts in logarithm bases: log₂(32) -> log(32,2).
    sub_digits = str.maketrans("₀₁₂₃₄₅₆₇₈₉", "0123456789")
    s = re.sub(
        r"\blog([₀₁₂₃₄₅₆₇₈₉]+)\s*\(([^()]*)\)",
        lambda m: f"log({m.group(2)},{m.group(1).translate(sub_digits)})",
        s,
    )
    # Handle grouped bases first, then a single symbol/number base. This avoids
    # greedily swallowing adjacent symbols in expressions such as a³b⁻².
    def superscript_repl(match):
        exponent = match.group(2)
        digits = exponent[1:] if exponent.startswith("⁻") else exponent
        sign = "-" if exponent.startswith("⁻") else ""
        return f"({match.group(1)})**({sign}{''.join(_SUPERS[c] for c in digits)})"
    s = re.sub(rf"(\([^()]+\))([⁻]?[{_SUP_DIGITS}]+)", superscript_repl, s)
    s = re.sub(rf"([A-Za-z0-9)])([⁻]?[{_SUP_DIGITS}]+)", superscript_repl, s)
    # Natural trig input such as "sin 30 degrees" and "sin(30°)" uses degrees explicitly.
    s = re.sub(r"\b(sin|cos|tan)\s*\(\s*(-?\d+(?:\.\d+)?)\s*°\s*\)", r"\1(degrees(\2))", s)
    s = re.sub(r"\b(sin|cos|tan)\s+(-?\d+(?:\.\d+)?)\s*(?:degrees?|°)", r"\1(degrees(\2))", s)
    # In conversational school-maths notation, bare "sin 30" means 30 degrees.
    s = re.sub(r"\b(sin|cos|tan)\s+(-?\d+(?:\.\d+)?)(?!\s*(?:degrees?|°|[a-z]))", r"\1(degrees(\2))", s)
    s = re.sub(r"(?<![A-Za-z0-9_])(\d+)!(?![=])", r"factorial(\1)", s)
    s = re.sub(r"\b(\d+)\s+factorial\b", r"factorial(\1)", s)
    s = re.sub(r"\blog base\s+(\d+(?:\.\d+)?)\s+of\s+(-?\d+(?:\.\d+)?)", r"log(\2,\1)", s)
    s = re.sub(r"\babsolute value of\s*(-?\d+(?:\.\d+)?|\([^()]+\))", r"abs(\1)", s)
    s = re.sub(r"(?<=\d),(?=\d{3}(?:\D|$))", "", s)
    s = re.sub(r"\b(?:what is|calculate|compute|evaluate|work out|please|for me|exactly|accurately)\b", " ", s)
    s = re.sub(r"\bto the power of\b|\bto the power\b|\braised to\b|\bpower of\b", "**", s)
    s = re.sub(r"\b(\d+(?:\.\d+)?)\s+power\s+(\d+(?:\.\d+)?)\b", r"\1**\2", s)
    s = re.sub(r"\b(\d+(?:\.\d+)?)\s+squared\b", r"(\1)**2", s)
    s = re.sub(r"\b(\d+(?:\.\d+)?)\s+cubed\b", r"(\1)**3", s)
    s = re.sub(r"\bsquare root of\s*(\d+(?:\.\d+)?)", r"sqrt(\1)", s)
    s = re.sub(r"\bsquare root\s+(\d+(?:\.\d+)?)", r"sqrt(\1)", s)
    s = re.sub(r"\bcube root of\s*(\d+(?:\.\d+)?)", r"(\1)**(1/3)", s)
    s = re.sub(r"\bdivided by\b|\bover\b", "/", s)
    s = re.sub(r"\bmultiplied by\b|\btimes\b", "*", s)
    s = re.sub(r"\bplus\b", "+", s)
    s = re.sub(r"\bminus\b", "-", s)
    s = re.sub(r"\bnegative\s+(\d)", r"-\1", s)
    s = re.sub(r"\bpositive\s+(\d)", r"+\1", s)
    s = re.sub(r"\bmodulo\b|\bmod\b", "%", s)
    s = re.sub(r"(?<=\d)\s+by\s+(?=\d)", " / ", s)
    s = re.sub(r"(\d+(?:\.\d+)?)\s*percent\s+of\s+", r"(\1/100)*", s)
    s = re.sub(r"(\d+(?:\.\d+)?)\s*%\s*of\s+", r"(\1/100)*", s)
    s = re.sub(r"\b(?:is equal to|equals|equal to)\b", "=", s)
    s = re.sub(r"\s+", " ", s)
    return s.strip(" ?.!\t\n")


def _strip_instruction_prose(text: str) -> str:
    """Remove common natural-language wrappers without deleting math tokens."""
    s = text.strip()
    s = re.sub(r"^\s*(?:for real x\s*,\s*|over the real numbers\s*,?\s*)", "", s, flags=re.I)
    s = re.sub(r"\s+over the real numbers\b|\s+over the complex numbers\b", "", s, flags=re.I)
    # These clauses describe presentation/checking, not part of the expression.
    s = re.sub(r"\s*,?\s*(?:(?:and then|and)\s+)?verify (?:both )?(?:the )?roots\b.*$", "", s, flags=re.I)
    s = re.sub(r"\s*,?\s*stating (?:every )?(?:excluded value|domain restrictions?)\b.*$", "", s, flags=re.I)
    s = re.sub(r"\s*,?\s*including domain restrictions?\b.*$", "", s, flags=re.I)
    s = re.sub(r"\s*,?\s*assuming (?:that )?.*?(?:are|is) nonzero\b.*$", "", s, flags=re.I)
    s = re.sub(r"\s*\.\s*do not assume .*?$", "", s, flags=re.I)
    s = re.sub(r"\s+as an exact value\b.*$", "", s, flags=re.I)
    s = re.sub(r"\s+give an exact answer and an approximation\b.*$", "", s, flags=re.I)
    return s.strip(" .?")

def _word_problem(text: str) -> Optional[str]:
    """Handle a few common word problems with explicit, deterministic models."""
    low = text.lower()
    # Average speed over equal distances: total distance / total time.
    m = re.search(r"train travels\s+([\d.]+)\s*km\s+at\s+([\d.]+)\s*km/h\s+and (?:another )?([\d.]+)\s*km\s+at\s+([\d.]+)\s*km/h", low)
    if m and "average speed" in low:
        d1,v1,d2,v2 = map(sp.Rational,m.groups())
        if v1 <= 0 or v2 <= 0: raise ValueError("Speeds must be positive.")
        return f"Average speed = {_fmt((d1+d2)/(d1/v1+d2/v2))} km/h"
    returning = re.search(r"train travels\s+([\d.]+)\s*km\s+at\s+([\d.]+)\s*km/h.*?returns?\s+at\s+([\d.]+)\s*km/h", low)
    if returning and "average speed" in low:
        distance, outward, returning_speed = map(sp.Rational, returning.groups())
        if outward <= 0 or returning_speed <= 0: raise ValueError("Speeds must be positive.")
        total_time = distance/outward + distance/returning_speed
        return f"Average speed = {_fmt(2*distance/total_time)} km/h"
    # Set inclusion-exclusion, with percentages converted to counts.
    m = re.search(r"(\d+) students.*?(\d+(?:\.\d+)?)% pass maths.*?(\d+(?:\.\d+)?)% pass english.*?(\d+) pass both.*?at least one", low)
    if m:
        total,p1,p2,both = m.groups()
        total,p1,p2,both = int(total),sp.Rational(p1),sp.Rational(p2),int(both)
        n1,n2 = total*p1/100,total*p2/100
        if n1.q != 1 or n2.q != 1: raise ValueError("The percentages do not correspond to whole-student counts.")
        union = int(n1)+int(n2)-both
        if not 0 <= union <= total: raise ValueError("The supplied student counts are inconsistent.")
        return f"Students passing at least one subject = {union}"
    # Tank capacity from fractional fill change.
    m = re.search(r"tank is\s+(\d+)\s*/\s*(\d+)\s*full.*?adding\s+([\d.]+)\s*lit(?:re|er)s?.*?(\d+)\s*/\s*(\d+)\s*full.*?capacity", low)
    if m:
        a,b,added,c,d = m.groups()
        a,b,c,d = map(int,(a,b,c,d)); added=sp.Rational(added)
        if b == 0 or d == 0 or b <= 0 or d <= 0: raise ValueError("Fill fractions must have positive denominators.")
        change=sp.Rational(c,d)-sp.Rational(a,b)
        if change <= 0: raise ValueError("The added amount must increase the tank's fill level.")
        capacity=added/change
        return f"Tank capacity = {_fmt(capacity)} litres"
    return None


def _safe_parse(expression: str):
    if not expression or len(expression) > MAX_INPUT:
        raise ValueError("Please provide a non-empty mathematical expression under 2,000 characters.")
    if not re.fullmatch(r"[0-9a-zA-Z_+\-*/%^().,= \[\]]+", expression):
        raise ValueError("I recognized a maths request, but its notation contains unsupported characters.")
    if "__" in expression or re.search(r"[A-Za-z_]\s*\.", expression) or "[" in expression or "]" in expression:
        raise ValueError("Unsupported or unsafe mathematical notation.")
    # Bound literal exponents before SymPy constructs huge integers.
    for match in re.finditer(r"(?:\*\*|\^)\s*\(?\s*(-?\d+)", expression):
        if abs(int(match.group(1))) > MAX_POWER:
            raise ValueError(f"Exponent magnitude is limited to {MAX_POWER} for reliability.")
    factorial_match = re.search(r"\bfactorial\s*\(\s*(\d+)\s*\)", expression)
    if factorial_match and int(factorial_match.group(1)) > 500:
        raise ValueError("Factorials above 500 are limited to keep the assistant responsive.")
    local = dict(_ALLOWED_NAMES)
    for name in re.findall(r"\b[a-zA-Z]\w*\b", expression):
        if name not in local:
            if name in _VARIABLES or len(name) == 1:
                local[name] = sp.Symbol(name)
            else:
                raise ValueError(f"Unsupported mathematical function or symbol: {name}")
    try:
        result = parse_expr(expression, local_dict=local, transformations=_TRANSFORMS, evaluate=True)
    except Exception as exc:
        raise ValueError("Invalid mathematical expression. Check the operators, brackets, and function syntax.") from exc
    if result.has(sp.zoo, sp.nan, sp.oo, -sp.oo):
        raise ValueError("The expression is undefined or does not have a finite result.")
    return result


def _fmt(expr) -> str:
    if expr.is_Integer:
        return str(expr)
    if expr.is_Rational:
        return str(expr)
    return str(expr)


def _parse_numbers(text: str):
    return [sp.Rational(x) for x in re.findall(r"(?<![A-Za-z])[-+]?\d+(?:\.\d+)?", text)]


def _parse_matrix_literal(text: str):
    """Parse a deliberately small matrix grammar: [[1,2],[3,4]]."""
    match = re.search(r"\[\s*\[.*\]\s*\]", text)
    if not match:
        return None
    literal = match.group(0)
    rows = re.findall(r"\[([^\[\]]+)\]", literal)
    if not rows:
        raise ValueError("Write a matrix as [[1, 2], [3, 4]].")
    parsed = []
    for row in rows:
        cells = [cell.strip() for cell in row.split(",")]
        if not cells or any(not cell for cell in cells):
            raise ValueError("Matrix rows must contain comma-separated values.")
        parsed.append([_safe_parse(_normalise(cell)) for cell in cells])
    if len({len(row) for row in parsed}) != 1:
        raise ValueError("Every matrix row must have the same number of entries.")
    return sp.Matrix(parsed)


def _vector_from_text(text: str) -> Optional[str]:
    low = text.lower().replace("î", "i").replace("ĵ", "j").replace("k̂", "k")
    low = re.sub(r"\b(?:i|j|k)\s*cap\b", lambda m: m.group(0).split()[0], low)
    def parse_vec(part):
        part = re.sub(r"\b(?:vector|unit vector|cap)\b", "", part)
        part = re.sub(r"\s+", "", part)
        # Tokenize linear combinations, requiring every term to be a scalar*i/j/k.
        terms = re.findall(r"[+-]?(?:\d+(?:\.\d+)?(?:/\d+(?:\.\d+)?)?)?\*?[ijk]", part)
        if not terms or "".join(terms) != part:
            raise ValueError("Write vectors like 3i + 4j + 2k.")
        out = sp.zeros(3, 1)
        axis = {"i":0, "j":1, "k":2}
        for term in terms:
            sign = -1 if term.startswith("-") else 1
            t = term.lstrip("+-")
            name = t[-1]
            coeff = t[:-1].replace("*", "")
            value = sp.Rational(coeff) if coeff else 1
            out[axis[name]] += sign * value
        return out
    # Only enter vector path when an operation is explicitly requested.
    pair_patterns = [
        (r"dot product of\s+(.+?)\s+and\s+(.+)", "dot"),
        (r"cross product of\s+(.+?)\s+and\s+(.+)", "cross"),
        (r"angle between (?:the )?vectors?\s+(.+?)\s+and\s+(.+)", "angle"),
    ]
    for pattern, op in pair_patterns:
        match = re.search(pattern, low)
        if match:
            a, b = parse_vec(match.group(1)), parse_vec(match.group(2))
            if op == "dot": return f"Dot product = {_fmt((a.T*b)[0])}"
            if op == "cross":
                c = a.cross(b)
                return "Cross product = " + ", ".join(_fmt(c[i]) for i in range(3)) + " (i, j, k components)"
            denom = sp.sqrt((a.T*a)[0] * (b.T*b)[0])
            if denom == 0: raise ValueError("The angle is undefined for a zero vector.")
            rad = sp.acos(sp.simplify((a.T*b)[0]/denom))
            return f"Angle = {_fmt(rad)} radians ({sp.N(rad*180/sp.pi, 8)} degrees)"
    norm = re.search(r"(?:magnitude|modulus|norm) of\s+(.+)", low)
    if norm:
        v = parse_vec(norm.group(1))
        return f"Magnitude = {_fmt(sp.sqrt((v.T*v)[0]))}"
    return None


def _stats(text: str) -> Optional[str]:
    low = text.lower()
    operation = None
    if re.search(r"\b(mean|average)\b", low): operation = "mean"
    elif re.search(r"\bmedian\b", low): operation = "median"
    elif re.search(r"\bmode\b", low): operation = "mode"
    elif re.search(r"standard deviation|\bstdev\b|\bstd\b", low): operation = "std"
    elif re.search(r"\bvariance\b", low): operation = "variance"
    if not operation: return None
    # Pull values from after common separators to avoid accidentally using the word's digits.
    data_match = re.search(r"(?:of|are|:|data(?: set)?\s*)\s*([-+\d.,\s]+)", low)
    values_text = data_match.group(1) if data_match else low
    nums = _parse_numbers(values_text)
    if not nums:
        raise ValueError("Provide a data set, for example: mean of 2, 4, 6, 8.")
    if len(nums) > 10000: raise ValueError("Please limit a data set to 10,000 values.")
    if operation == "mean": value = sp.Rational(sum(nums), len(nums)); label = "Mean"
    elif operation == "median":
        ordered = sorted(nums)
        middle = len(ordered) // 2
        value = ordered[middle] if len(ordered) % 2 else (ordered[middle-1] + ordered[middle]) / 2
        label = "Median"
    elif operation == "mode":
        counts = {}
        for n in nums: counts[n] = counts.get(n, 0) + 1
        highest = max(counts.values())
        modes = sorted(n for n, count in counts.items() if count == highest)
        if highest == 1: return "Mode = no unique mode (all values occur once)"
        return "Mode = " + ", ".join(_fmt(v) for v in modes)
    else:
        mean = sp.Rational(sum(nums), len(nums))
        variance = sum((n-mean)**2 for n in nums) / (len(nums) if "population" in low else max(1, len(nums)-1))
        if operation == "variance": value = variance; label = "Variance"
        else: value = sp.sqrt(variance); label = "Standard deviation"
    return f"{label} = {_fmt(value)}"


def _coordinate_geometry(text: str) -> Optional[str]:
    low = text.lower()
    # Coordinate pairs may be named or unnamed: (-2, 3) and (4, -5).
    pairs = re.findall(r"\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)", low)
    if len(pairs) >= 2 and any(k in low for k in ("distance", "midpoint", "slope", "gradient")):
        x1,y1 = map(sp.Rational,pairs[0]); x2,y2 = map(sp.Rational,pairs[1])
        if "distance" in low:
            return f"Distance = {_fmt(sp.sqrt((x2-x1)**2+(y2-y1)**2))}"
        if "midpoint" in low:
            return f"Midpoint = ({_fmt((x1+x2)/2)}, {_fmt((y1+y2)/2)})"
        if "slope" in low or "gradient" in low:
            if x2 == x1: return "Slope is undefined (the line is vertical)."
            return f"Slope = {_fmt((y2-y1)/(x2-x1))}"
    # Named points can be written A(1, 2) and B(4, 6).
    pts = re.findall(r"\b([a-z])\s*\(\s*(-?\d+(?:\.\d+)?)\s*,\s*(-?\d+(?:\.\d+)?)\s*\)", low)
    if len(pts) >= 2:
        _, x1, y1 = pts[0]; _, x2, y2 = pts[1]
        x1,y1,x2,y2 = map(sp.Rational, (x1,y1,x2,y2))
        if "distance" in low:
            return f"Distance = {_fmt(sp.sqrt((x2-x1)**2+(y2-y1)**2))}"
        if "midpoint" in low:
            return f"Midpoint = ({_fmt((x1+x2)/2)}, {_fmt((y1+y2)/2)})"
        if "slope" in low or "gradient" in low:
            if x2 == x1: return "Slope is undefined (the line is vertical)."
            return f"Slope = {_fmt((y2-y1)/(x2-x1))}"
    return None


def _matrix_operation(text: str) -> Optional[str]:
    low = text.lower()
    if not any(k in low for k in ("matrix", "determinant", "inverse of", "transpose of", "rank of")):
        return None
    matrix = _parse_matrix_literal(text)
    if matrix is None:
        return None
    if "determinant" in low or re.search(r"\bdet\b", low):
        if matrix.rows != matrix.cols: raise ValueError("A determinant is defined only for a square matrix.")
        return f"Determinant = {_fmt(matrix.det())}"
    if "inverse" in low:
        if matrix.rows != matrix.cols: raise ValueError("Only square matrices have an inverse.")
        if matrix.det() == 0: raise ValueError("This matrix is singular and has no inverse.")
        return f"Inverse matrix = {sp.pretty(matrix.inv())}"
    if "transpose" in low:
        return f"Transpose = {sp.pretty(matrix.T)}"
    if "rank" in low:
        return f"Matrix rank = {matrix.rank()}"
    return f"Matrix = {sp.pretty(matrix)}"


def _combinatorics(text: str) -> Optional[str]:
    low = text.lower()
    m = re.search(r"\b(\d+)\s*(?:choose|combination(?:s)? of)\s*(\d+)\b", low)
    if m:
        n, r = map(int, m.groups())
        if n > 100000 or r > n: raise ValueError("Combination requires 0 ≤ r ≤ n and a manageable n.")
        return f"Combination = {math.comb(n,r)}"
    m = re.search(r"\b(\d+)\s*(?:permutation(?:s)? of|permute)\s*(\d+)\b", low)
    if m:
        n, r = map(int, m.groups())
        if n > 100000 or r > n: raise ValueError("Permutation requires 0 ≤ r ≤ n and a manageable n.")
        return f"Permutation = {math.perm(n,r)}"
    return None


def _geometry(text: str) -> Optional[str]:
    low = text.lower()
    def get(pattern, label):
        m = re.search(pattern, low)
        if not m:
            return None
        return sp.Rational(m.group(1))
    radius = get(r"radius\s*(?:of\s*)?(?:is|=|of)?\s*(-?\d+(?:\.\d+)?)", "radius")
    length = get(r"length\s*(?:is|=)?\s*(-?\d+(?:\.\d+)?)", "length")
    width = get(r"width\s*(?:is|=)?\s*(-?\d+(?:\.\d+)?)", "width")
    base = get(r"base\s*(?:is|=)?\s*(-?\d+(?:\.\d+)?)", "base")
    height = get(r"height\s*(?:is|=)?\s*(-?\d+(?:\.\d+)?)", "height")
    if re.search(r"\barea of (?:a )?circle\b", low) or re.search(r"\barea of circle\b", low):
        if radius is None: raise ValueError("Provide the circle radius, for example: area of circle radius 3.")
        if radius < 0: raise ValueError("Radius cannot be negative.")
        return f"Area = {_fmt(sp.pi*radius**2)} (approximately {sp.N(sp.pi*radius**2, 8)})"
    if "circumference" in low and "circle" in low:
        if radius is None: raise ValueError("Provide the circle radius, for example: circumference of circle radius 3.")
        if radius < 0: raise ValueError("Radius cannot be negative.")
        return f"Circumference = {_fmt(2*sp.pi*radius)} (approximately {sp.N(2*sp.pi*radius, 8)})"
    if re.search(r"\b(area|perimeter) of (?:a )?rectangle\b", low):
        if length is None or width is None: raise ValueError("Provide both length and width, for example: area of rectangle length 4 width 5.")
        if length < 0 or width < 0: raise ValueError("Lengths cannot be negative.")
        if "area" in low: return f"Area = {_fmt(length*width)}"
        return f"Perimeter = {_fmt(2*(length+width))}"
    if re.search(r"\barea of (?:a )?triangle\b", low):
        if base is None or height is None: raise ValueError("Provide the triangle base and height, for example: area of triangle base 10 height 4.")
        if base < 0 or height < 0: raise ValueError("Lengths cannot be negative.")
        return f"Area = {_fmt(base*height/2)}"
    if re.search(r"\bvolume of (?:a )?sphere\b", low):
        if radius is None: raise ValueError("Provide the sphere radius, for example: volume of sphere radius 3.")
        if radius < 0: raise ValueError("Radius cannot be negative.")
        value = 4*sp.pi*radius**3/3
        return f"Volume = {_fmt(value)} (approximately {sp.N(value, 8)})"
    if re.search(r"\b(surface area|total surface area) of (?:a )?sphere\b", low):
        if radius is None: raise ValueError("Provide the sphere radius.")
        value = 4*sp.pi*radius**2
        return f"Surface area = {_fmt(value)} (approximately {sp.N(value, 8)})"
    if re.search(r"\b(area|perimeter) of (?:a )?square\b", low):
        side = get(r"side\s*(?:is|=)?\s*(-?\d+(?:\.\d+)?)", "side")
        if side is None: raise ValueError("Provide the square side length, for example: area of square side 4.")
        if side < 0: raise ValueError("Side length cannot be negative.")
        return f"{'Area' if 'area' in low else 'Perimeter'} = {_fmt(side**2 if 'area' in low else 4*side)}"
    if re.search(r"\bvolume of (?:a )?cylinder\b", low):
        if radius is None or height is None: raise ValueError("Provide the cylinder radius and height.")
        if radius < 0 or height < 0: raise ValueError("Dimensions cannot be negative.")
        value = sp.pi*radius**2*height
        return f"Volume = {_fmt(value)} (approximately {sp.N(value, 8)})"
    if re.search(r"\bvolume of (?:a )?cuboid\b", low):
        depth = get(r"depth\s*(?:is|=)?\s*(-?\d+(?:\.\d+)?)", "depth")
        if length is None or width is None or depth is None: raise ValueError("Provide length, width, and depth for the cuboid.")
        if min(length, width, depth) < 0: raise ValueError("Dimensions cannot be negative.")
        return f"Volume = {_fmt(length*width*depth)}"
    return None


def _probability(text: str) -> Optional[str]:
    low = text.lower()
    if "probability" not in low and "chance" not in low:
        return None
    m = re.search(r"(\d+)\s+(?:favourable|favorable|successful) outcomes?\s+(?:out of|from)\s+(\d+)", low)
    if m:
        good, total = map(int, m.groups())
        if total <= 0 or good > total: raise ValueError("Favourable outcomes must be between zero and the positive total number of outcomes.")
        return f"Probability = {_fmt(sp.Rational(good,total))} ({sp.N(sp.Rational(good,total)*100, 6)}%)"
    die = re.search(r"rolling (?:a )?(\d+) on (?:a )?(?:fair )?(\d+)[- ]sided die", low)
    if die:
        face, sides = map(int, die.groups())
        if sides < 2 or face < 1 or face > sides:
            raise ValueError("A die face must be between 1 and the number of sides, and the die must have at least two sides.")
        p = sp.Rational(1, sides)
        return f"Probability = {_fmt(p)} ({sp.N(p*100, 6)}%)"
    if re.search(r"rolling (?:a )?(?:4|four) on (?:a )?fair six[- ]sided die", low):
        return "Probability = 1/6 (approximately 16.6667%)"
    if re.search(r"tossing (?:a )?fair coin.*heads|getting heads.*fair coin", low):
        return "Probability = 1/2 (50%)"
    return None


def _number_theory(text: str) -> Optional[str]:
    low = text.lower()
    m = re.search(r"\b(?:gcd|hcf|greatest common (?:divisor|factor)|highest common factor)\s+(?:of\s+)?(\d+)\s*(?:,|and)\s*(\d+)(?:\s*(?:,|and)\s*(\d+))?", low)
    if m:
        nums = [int(x) for x in m.groups() if x]
        result = nums[0]
        for n in nums[1:]:
            result = math.gcd(result, n)
        return f"GCD = {result}"
    m = re.search(r"\b(?:lcm|least common multiple|lowest common multiple)\s+(?:of\s+)?(\d+)\s*(?:,|and)\s*(\d+)(?:\s*(?:,|and)\s*(\d+))?", low)
    if m:
        nums = [int(x) for x in m.groups() if x]
        result = nums[0]
        for n in nums[1:]:
            result = result * n // math.gcd(result, n)
        return f"LCM = {result}"
    m = re.search(r"\b(?:is\s+)?(\d+)\s+(?:a\s+)?prime(?:\s+number)?\b", low)
    if m:
        n = int(m.group(1))
        if n > 10**12:
            raise ValueError("Prime check is limited to numbers up to 10^12.")
        return f"{n} is {'prime' if sp.isprime(n) else 'not prime'}"
    m = re.search(r"\bprime factors?\s+(?:of\s+)?(\d+)\b", low)
    if not m:
        m = re.search(r"\bfactorise\s+(\d+)\b|\bfactorize\s+(\d+)\b", low)
        if m:
            val = m.group(1) or m.group(2)
            n = int(val)
            if n > 10**15:
                raise ValueError("Factorization is limited to numbers up to 10^15.")
            factors = sp.factorint(n)
            parts = [f"{p}^{e}" if e > 1 else str(p) for p, e in sorted(factors.items())]
            return f"Prime factorization of {n} = {' × '.join(parts)}"
    if m:
        n = int(m.group(1))
        if n > 10**15:
            raise ValueError("Factorization is limited to numbers up to 10^15.")
        factors = sp.factorint(n)
        parts = [f"{p}^{e}" if e > 1 else str(p) for p, e in sorted(factors.items())]
        return f"Prime factorization of {n} = {' × '.join(parts)}"
    m = re.search(r"\b(?:factors|divisors)\s+(?:of\s+)?(\d+)\b", low)
    if m:
        n = int(m.group(1))
        if n > 10**8:
            raise ValueError("Finding all divisors is limited to numbers up to 10^8.")
        divs = sorted(sp.divisors(n))
        return f"Divisors of {n} = {', '.join(str(d) for d in divs)} ({len(divs)} divisors)"
    return None


def _summation_product(text: str) -> Optional[str]:
    low = text.lower()
    m = re.search(r"\b(?:sum|summation|sigma|Σ)\s+(?:of\s+)?(.+?)\s+(?:from|for)\s+([a-z])\s*=\s*(\d+)\s+to\s+(\d+)", low)
    if m:
        expr_text, var, start, end = m.groups()
        start, end = int(start), int(end)
        if end - start > 100000:
            raise ValueError("Summation range is limited to 100,000 terms.")
        sym = sp.Symbol(var)
        expr = _safe_parse(_normalise(expr_text))
        result = sp.summation(expr, (sym, start, end))
        return f"Sum = {_fmt(result)}"
    m = re.search(r"\b(?:product|pi|Π)\s+(?:of\s+)?(.+?)\s+(?:from|for)\s+([a-z])\s*=\s*(\d+)\s+to\s+(\d+)", low)
    if m:
        expr_text, var, start, end = m.groups()
        start, end = int(start), int(end)
        if end - start > 10000:
            raise ValueError("Product range is limited to 10,000 terms.")
        sym = sp.Symbol(var)
        expr = _safe_parse(_normalise(expr_text))
        result = sp.product(expr, (sym, start, end))
        return f"Product = {_fmt(result)}"
    return None


def _series_expansion(text: str) -> Optional[str]:
    low = text.lower()
    m = re.search(r"\b(?:taylor|maclaurin|series)\s+(?:expansion\s+)?(?:of\s+)?(.+?)(?:\s+(?:about|around|at)\s+([a-z])\s*=\s*(-?\d+(?:\.\d+)?))?(?:\s+(?:up to|to|order)\s+(\d+))?\s*$", low)
    if m:
        expr_text = m.group(1).strip()
        var = m.group(2) or "x"
        point = sp.Rational(m.group(3)) if m.group(3) else 0
        order = int(m.group(4)) if m.group(4) else 6
        if order > 20:
            raise ValueError("Series expansion is limited to order 20.")
        expr = _safe_parse(_normalise(expr_text))
        sym = sp.Symbol(var)
        result = sp.series(expr, sym, point, n=order)
        return f"Series expansion = {result}"
    return None


def _binomial(text: str) -> Optional[str]:
    low = text.lower()
    m = re.search(r"\bbinomial\s+(?:expansion|theorem)\s+(?:of\s+)?\((.+?)\)\s*\^\s*(\d+)\b", low)
    if not m:
        m = re.search(r"\bbinomial\s+(?:expansion|theorem)\s+(?:of\s+)?(.+?)\s+(?:to the power|power)\s+(\d+)\b", low)
    if m:
        expr_text, power = m.groups()
        power = int(power)
        if power > 20:
            raise ValueError("Binomial expansion is limited to power 20.")
        expr = _safe_parse(_normalise(expr_text))
        result = sp.expand(expr**power)
        return f"Binomial expansion = {_fmt(result)}"
    return None


def _inequality(text: str) -> Optional[str]:
    low = text.lower()
    if not re.search(r"[<>]|<=|>=|\\le|\\ge|\bgreater than\b|\bless than\b", low):
        return None
    m = re.search(r"\bsolve\s+(.+)", low)
    if not m:
        return None
    body = m.group(1).strip()
    body = re.sub(r"\bgreater than or equal to\b", ">=", body)
    body = re.sub(r"\bless than or equal to\b", "<=", body)
    body = re.sub(r"\bgreater than\b", ">", body)
    body = re.sub(r"\bless than\b", "<", body)
    norm = _normalise(body)
    for op in (">=", "<=", ">", "<"):
        if op in norm:
            parts = norm.split(op, 1)
            if len(parts) == 2:
                left = _safe_parse(parts[0].strip())
                right = _safe_parse(parts[1].strip())
                if op == ">":
                    ineq = sp.Gt(left, right)
                elif op == "<":
                    ineq = sp.Lt(left, right)
                elif op == ">=":
                    ineq = sp.Ge(left, right)
                else:
                    ineq = sp.Le(left, right)
                variables = sorted(ineq.free_symbols, key=str)
                if variables:
                    result = sp.solve(ineq, variables[0])
                    return f"Solution: {variables[0]} ∈ {result}"
    return None


def _quadratic_formula(text: str) -> Optional[str]:
    low = text.lower()
    if not re.search(r"\bquadratic formula\b|\busing quadratic formula\b|\bby quadratic formula\b", low):
        return None
    m = re.search(r"(\d+)x\s*²\s*([+-]\s*\d+)x\s*([+-]\s*\d+)\s*=\s*0", _normalise(text))
    if not m:
        m = re.search(r"([+-]?\d*)x\^2\s*([+-]\s*\d+)x\s*([+-]\s*\d+)\s*=\s*0", _normalise(text))
    if m:
        a = sp.Rational(m.group(1).replace(" ", "") or "1")
        b = sp.Rational(m.group(2).replace(" ", ""))
        c = sp.Rational(m.group(3).replace(" ", ""))
    else:
        m2 = re.search(r"\ba\s*=\s*(-?\d+(?:\.\d+)?)\s*,?\s*b\s*=\s*(-?\d+(?:\.\d+)?)\s*,?\s*c\s*=\s*(-?\d+(?:\.\d+)?)", low)
        if not m2:
            return None
        a, b, c = map(sp.Rational, m2.groups())
    if a == 0:
        raise ValueError("Coefficient 'a' must be non-zero for a quadratic equation.")
    disc = b**2 - 4*a*c
    x1 = (-b + sp.sqrt(disc)) / (2*a)
    x2 = (-b - sp.sqrt(disc)) / (2*a)
    lines = [
        f"Quadratic: {_fmt(a)}x² + ({_fmt(b)})x + ({_fmt(c)}) = 0",
        f"Discriminant (b²-4ac) = {_fmt(disc)}",
    ]
    if disc > 0:
        lines.append(f"Two real roots: x = {_fmt(sp.simplify(x1))}, x = {_fmt(sp.simplify(x2))}")
    elif disc == 0:
        lines.append(f"One repeated root: x = {_fmt(sp.simplify(x1))}")
    else:
        lines.append(f"Two complex roots: x = {_fmt(sp.simplify(x1))}, x = {_fmt(sp.simplify(x2))}")
    return "\n".join(lines)


def _unit_conversion(text: str) -> Optional[str]:
    low = text.lower().strip()
    conversions = {
        ("km", "miles"): sp.Rational("0.621371"),
        ("miles", "km"): sp.Rational("1.60934"),
        ("kg", "pounds"): sp.Rational("2.20462"),
        ("kg", "lbs"): sp.Rational("2.20462"),
        ("pounds", "kg"): sp.Rational("0.453592"),
        ("lbs", "kg"): sp.Rational("0.453592"),
        ("celsius", "fahrenheit"): None,
        ("fahrenheit", "celsius"): None,
        ("cm", "inches"): sp.Rational("0.393701"),
        ("inches", "cm"): sp.Rational("2.54"),
        ("meters", "feet"): sp.Rational("3.28084"),
        ("metres", "feet"): sp.Rational("3.28084"),
        ("feet", "meters"): sp.Rational("0.3048"),
        ("feet", "metres"): sp.Rational("0.3048"),
        ("liters", "gallons"): sp.Rational("0.264172"),
        ("litres", "gallons"): sp.Rational("0.264172"),
        ("gallons", "liters"): sp.Rational("3.78541"),
        ("gallons", "litres"): sp.Rational("3.78541"),
        ("kg", "grams"): sp.Rational(1000),
        ("grams", "kg"): sp.Rational("0.001"),
        ("km", "meters"): sp.Rational(1000),
        ("km", "metres"): sp.Rational(1000),
        ("meters", "km"): sp.Rational("0.001"),
        ("metres", "km"): sp.Rational("0.001"),
        ("hours", "minutes"): sp.Rational(60),
        ("minutes", "hours"): sp.Rational(1, 60),
        ("hours", "seconds"): sp.Rational(3600),
        ("seconds", "hours"): sp.Rational(1, 3600),
        ("minutes", "seconds"): sp.Rational(60),
        ("seconds", "minutes"): sp.Rational(1, 60),
    }
    m = re.search(r"(-?\d+(?:\.\d+)?)\s*°?\s*(km|miles?|kg|pounds?|lbs?|celsius|fahrenheit|cm|inches?|meters?|metres?|feet|foot|liters?|litres?|gallons?|grams?|hours?|minutes?|seconds?)\s+(?:to|in|into)\s+(km|miles?|kg|pounds?|lbs?|celsius|fahrenheit|cm|inches?|meters?|metres?|feet|foot|liters?|litres?|gallons?|grams?|hours?|minutes?|seconds?)", low)
    if not m:
        m = re.search(r"(?:convert\s+)?(-?\d+(?:\.\d+)?)\s*°?\s*(km|miles?|kg|pounds?|lbs?|celsius|fahrenheit|cm|inches?|meters?|metres?|feet|foot|liters?|litres?|gallons?|grams?|hours?|minutes?|seconds?)\s+(?:to|in|into)\s+(km|miles?|kg|pounds?|lbs?|celsius|fahrenheit|cm|inches?|meters?|metres?|feet|foot|liters?|litres?|gallons?|grams?|hours?|minutes?|seconds?)", low)
    if not m:
        return None
    value, from_unit, to_unit = sp.Rational(m.group(1)), m.group(2), m.group(3)
    _strip_plural = {"mile": "miles", "pound": "pounds", "inch": "inches", "meter": "meters",
                     "metre": "metres", "liter": "liters", "litre": "litres", "gallon": "gallons",
                     "gram": "grams", "hour": "hours", "minute": "minutes", "second": "seconds", "lb": "lbs"}
    for singular, plural in _strip_plural.items():
        if from_unit == plural: from_unit = singular
        if to_unit == plural: to_unit = singular
    if from_unit == "foot": from_unit = "feet"
    if to_unit == "foot": to_unit = "feet"
    if from_unit == "celsius" and to_unit == "fahrenheit":
        result = value * sp.Rational(9, 5) + 32
        return f"{_fmt(value)}°C = {_fmt(result)}°F"
    if from_unit == "fahrenheit" and to_unit == "celsius":
        result = (value - 32) * sp.Rational(5, 9)
        return f"{_fmt(value)}°F = {_fmt(result)}°C"
    factor = conversions.get((from_unit, to_unit))
    if factor is None:
        return None
    result = value * factor
    approx = float(result)
    return f"{_fmt(value)} {from_unit} = {approx:.6g} {to_unit}"


def _polynomial_ops(text: str) -> Optional[str]:
    low = text.lower()
    m = re.search(r"\bdegree of\s+(.+)", low)
    if m:
        expr = _safe_parse(_normalise(m.group(1).strip()))
        variables = sorted(expr.free_symbols, key=str)
        if variables:
            deg = sp.degree(sp.Poly(expr, variables[0]))
            return f"Degree = {deg}"
    m = re.search(r"\b(?:roots|zeros|zeroes)\s+of\s+(.+)", low)
    if m:
        expr_text = m.group(1).strip()
        norm = _normalise(expr_text)
        expr = _safe_parse(norm)
        variables = sorted(expr.free_symbols, key=str)
        if variables:
            roots = sp.solve(expr, variables[0])
            if roots:
                return f"Roots: {', '.join(_fmt(r) for r in roots)}"
            return "No roots found."
    m = re.search(r"\bremainder\s+(?:when\s+)?(.+?)\s+(?:is\s+)?divided by\s+(.+)", low)
    if m:
        dividend = _safe_parse(_normalise(m.group(1).strip()))
        divisor = _safe_parse(_normalise(m.group(2).strip()))
        variables = sorted(dividend.free_symbols | divisor.free_symbols, key=str)
        if variables:
            q, r = sp.div(dividend, divisor, variables[0])
            return f"Quotient = {_fmt(q)}, Remainder = {_fmt(r)}"
    m = re.search(r"\bquotient\s+(?:when\s+)?(.+?)\s+(?:is\s+)?divided by\s+(.+)", low)
    if m:
        dividend = _safe_parse(_normalise(m.group(1).strip()))
        divisor = _safe_parse(_normalise(m.group(2).strip()))
        variables = sorted(dividend.free_symbols | divisor.free_symbols, key=str)
        if variables:
            q, r = sp.div(dividend, divisor, variables[0])
            return f"Quotient = {_fmt(q)}, Remainder = {_fmt(r)}"
    return None


def _complex_number(text: str) -> Optional[str]:
    low = text.lower()
    if not re.search(r"\bcomplex\b|\bimaginary\b|\breal part\b|\bimaginary part\b|\bmodulus of\b|\bargument of\b|\bconjugate of\b", low):
        return None
    m = re.search(r"\breal part of\s+(.+)", low)
    if m:
        expr = _safe_parse(_normalise(m.group(1).strip()))
        return f"Real part = {_fmt(sp.re(expr))}"
    m = re.search(r"\bimaginary part of\s+(.+)", low)
    if m:
        expr = _safe_parse(_normalise(m.group(1).strip()))
        return f"Imaginary part = {_fmt(sp.im(expr))}"
    m = re.search(r"\bmodulus of\s+(.+)", low)
    if m:
        expr = _safe_parse(_normalise(m.group(1).strip()))
        return f"Modulus = {_fmt(sp.Abs(expr))}"
    m = re.search(r"\bargument of\s+(.+)", low)
    if m:
        expr = _safe_parse(_normalise(m.group(1).strip()))
        return f"Argument = {_fmt(sp.arg(expr))}"
    m = re.search(r"\bconjugate of\s+(.+)", low)
    if m:
        expr = _safe_parse(_normalise(m.group(1).strip()))
        return f"Conjugate = {_fmt(sp.conjugate(expr))}"
    return None


def solve_math(text: str) -> str:
    """Solve supported everyday arithmetic, algebra, calculus, and school maths."""
    raw = text.strip()
    if not raw: raise ValueError("Please enter a mathematical problem.")
    if len(raw) > MAX_INPUT: raise ValueError("The maths request is too long.")
    low = raw.lower()
    original_wording = low

    word_answer = _word_problem(raw)
    if word_answer is not None:
        return word_answer

    # Strip presentation instructions before expression parsing, while preserving
    # domain phrases for explicit real/complex solution policy.
    cleaned_raw = _strip_instruction_prose(raw)
    if cleaned_raw != raw:
        raw = cleaned_raw
        low = raw.lower()

    # Contextual solve requests are handled before the generic command parser.
    solve_after = re.match(r"^\s*solve\s+it\s+(?:after|for)\s+(.+?)\s*[.!?]*\s*$", raw, re.I)
    if solve_after:
        target = solve_after.group(1).strip()
        target_norm = _normalise(target)
        if "=" in target_norm:
            return solve_math(target)
        _safe_parse(target_norm)  # Validate it before explaining the missing equation.
        display_target = re.sub(r"^\s*(?:what is|find|calculate|evaluate)\s+", "", target, flags=re.I).strip()
        expanded_hint = _fmt(sp.expand(_safe_parse(target_norm)))
        return ("The previous input is an expression, not an equation, so I can’t solve for x yet; it needs an equation. "
                f"You gave {display_target}. Its expanded form is {expanded_hint}. "
                f"For example, write {display_target} = 9 if that is the equation you mean.")

    # Compound instruction: perform expansion and simplification deterministically.
    compound = re.match(r"^\s*expand\s+and\s+simplify\s+(.+?)\s*$", raw, re.I)
    if compound:
        expr = _safe_parse(_normalise(compound.group(1)))
        return f"Expanded and simplified expression = {_fmt(sp.simplify(sp.expand(expr)))}"

    # Explicit operation commands, including contextual phrasing passed from EVA.
    command = re.match(r"^\s*(expand|factor|factorise|factorize|simplify|differentiate|derive|derivative of|integrate|integral of|limit of|solve|evaluate)\s+(.+?)\s*$", raw, re.I)
    if command:
        op, body = command.group(1).lower(), command.group(2).strip()
        variable_match = re.search(r"\bwith respect to\s+([a-z])\b", body, re.I)
        differentiation_variable = sp.Symbol(variable_match.group(1).lower()) if variable_match else sp.Symbol("x")
        body = re.sub(r"\bwith respect to\s+[a-z]\b|\bdx\b|\bdy\b|\bdt\b", "", body, flags=re.I).strip()
        if op == "solve":
            if "=" not in _normalise(body): raise ValueError("To solve for a variable, provide an equation containing '=', such as x^2 - 5x + 6 = 0.")
            raw = body; low = raw.lower()
        elif op in {"differentiate", "derive", "derivative of"}:
            expr = _safe_parse(_normalise(body)); return f"Derivative = {_fmt(sp.diff(expr, differentiation_variable))}"
        elif op in {"integrate", "integral of"}:
            expr = _safe_parse(_normalise(body)); return f"Integral = {_fmt(sp.integrate(expr, differentiation_variable))} + C"
        elif op == "expand": return f"Expanded expression = {_fmt(sp.expand(_safe_parse(_normalise(body))))}"
        elif op in {"factor", "factorise", "factorize"}: return f"Factored expression = {_fmt(sp.factor(_safe_parse(_normalise(body))))}"
        elif op == "simplify":
            if re.match(r"ratio\s+", body, re.I):
                raw = body
                low = raw.lower()
            else:
                expr = _safe_parse(_normalise(body))
                if re.search(r"\b(?:for real x|do not assume x is positive)\b", original_wording):
                    for symbol in expr.free_symbols:
                        expr = sp.refine(expr, sp.Q.real(symbol))
                simplified = sp.simplify(expr)
                if re.search(r"excluded value|domain restriction", original_wording):
                    denominator = sp.denom(sp.together(expr))
                    restrictions = []
                    if denominator != 1 and denominator.free_symbols:
                        symbol = sorted(denominator.free_symbols, key=str)[0]
                        roots = sp.solve(denominator, symbol)
                        if roots:
                            restrictions.append(f"{symbol} != " + ", ".join(_fmt(root) for root in roots))
                    suffix = "; original expression excludes " + ", ".join(restrictions) if restrictions else ""
                    return f"Simplified expression = {_fmt(simplified)}{suffix}"
                return f"Simplified expression = {_fmt(simplified)}"
        elif op == "evaluate": raw = body; low = raw.lower()
        elif op == "limit of":
            m = re.match(r"(.+?)\s+as\s+([a-z])\s*(?:approaches|->|tends to)\s*(-?\d+(?:\.\d+)?)", body, re.I)
            if not m: raise ValueError("Use: limit of (x^2-1)/(x-1) as x approaches 1.")
            expr, var, point = m.groups(); return f"Limit = {_fmt(sp.limit(_safe_parse(_normalise(expr)), sp.Symbol(var), sp.Rational(point)))}"

    # Common percentage-change and ratio requests.
    percent_change = re.search(r"(?:percentage|percent)\s+(increase|decrease|change)\s+(?:from\s+)?(-?\d+(?:\.\d+)?)\s+(?:to|by)\s+(-?\d+(?:\.\d+)?)", low)
    if percent_change:
        kind, start_value, end_value = percent_change.groups()
        start_value, end_value = sp.Rational(start_value), sp.Rational(end_value)
        if start_value == 0: raise ValueError("Percentage change from zero is undefined.")
        actual = (end_value-start_value)/abs(start_value)*100
        return f"Percentage change = {_fmt(actual)}% ({'increase' if actual > 0 else 'decrease' if actual < 0 else 'no change'})"
    ratio = re.search(r"(?:simplify\s+)?ratio\s+(-?\d+)\s*[:/]\s*(-?\d+)", low)
    if ratio:
        a, b = map(int, ratio.groups())
        if a == 0 and b == 0: raise ValueError("The ratio 0:0 is undefined.")
        gcd = math.gcd(a, b)
        return f"Simplified ratio = {a//gcd}:{b//gcd}"

    # Unit conversion runs on the raw text before normalisation mangles it.
    unit_answer = _unit_conversion(text)
    if unit_answer is not None: return unit_answer
    quadratic_answer = _quadratic_formula(raw)
    if quadratic_answer is not None: return quadratic_answer
    inequality_answer = _inequality(raw)
    if inequality_answer is not None: return inequality_answer
    binomial_answer = _binomial(raw)
    if binomial_answer is not None: return binomial_answer
    number_theory_answer = _number_theory(raw)
    if number_theory_answer is not None: return number_theory_answer
    summation_answer = _summation_product(raw)
    if summation_answer is not None: return summation_answer
    series_answer = _series_expansion(raw)
    if series_answer is not None: return series_answer
    polynomial_answer = _polynomial_ops(raw)
    if polynomial_answer is not None: return polynomial_answer
    complex_answer = _complex_number(raw)
    if complex_answer is not None: return complex_answer
    matrix_answer = _matrix_operation(raw)
    if matrix_answer is not None: return matrix_answer
    vector_answer = _vector_from_text(raw)
    if vector_answer is not None: return vector_answer
    geo_answer = _coordinate_geometry(raw)
    if geo_answer is not None: return geo_answer
    line_match = re.search(r"(?:equation of (?:a )?line|straight.line equation).*?slope\s*(?:is|=)?\s*(-?\d+(?:\.\d+)?).*?(?:y[- ]?intercept|intercept)\s*(?:is|=)?\s*(-?\d+(?:\.\d+)?)", low)
    if line_match:
        slope, intercept = map(sp.Rational, line_match.groups())
        return f"Line equation: y = {_fmt(slope)}*x + ({_fmt(intercept)})"
    comb_answer = _combinatorics(raw)
    if comb_answer is not None: return comb_answer
    stat_answer = _stats(raw)
    if stat_answer is not None: return stat_answer
    geometry_answer = _geometry(raw)
    if geometry_answer is not None: return geometry_answer
    probability_answer = _probability(raw)
    if probability_answer is not None: return probability_answer

    # AP/GP terms and sums. Remove the requested n before reading sequence values.
    ap = bool(re.search(r"\b(?:arithmetic progression|ap)\b", low))
    gp = bool(re.search(r"\b(?:geometric progression|gp)\b", low))
    if ap or gp:
        nmatch = re.search(r"(?:first\s+)?(\d+)\s+terms?", low)
        nthmatch = re.search(r"(?:\b(?:find|calculate|what is)?\s*)(\d+)(?:st|nd|rd|th)\s+term", low)
        cleaned = re.sub(r"(?:first\s+)?\d+\s+terms?", " ", low)
        # Prefer named a/d/r when supplied; otherwise infer the step/ratio from sequence values.
        if ap and re.search(r"common difference", low):
            a_match = re.search(r"first term\s*(?:is|=)?\s*(-?\d+(?:\.\d+)?)", low)
            d_match = re.search(r"common difference\s*(?:is|=)?\s*(-?\d+(?:\.\d+)?)", low)
            nums = [sp.Rational(a_match.group(1)), sp.Rational(d_match.group(1))] if a_match and d_match else _parse_numbers(cleaned)
        elif gp and re.search(r"common ratio", low):
            a_match = re.search(r"first term\s*(?:is|=)?\s*(-?\d+(?:\.\d+)?)", low)
            r_match = re.search(r"common ratio\s*(?:is|=)?\s*(-?\d+(?:\.\d+)?)", low)
            nums = [sp.Rational(a_match.group(1)), sp.Rational(r_match.group(1))] if a_match and r_match else _parse_numbers(cleaned)
        else:
            nums = _parse_numbers(cleaned)
        if len(nums) >= 2:
            a = nums[0]
            step = (nums[1] / nums[0] if gp and nums[0] != 0 else nums[1]) if len(nums) == 2 else (nums[1] / nums[0] if gp and nums[0] != 0 else nums[1]-nums[0])
            if nthmatch:
                n = int(nthmatch.group(1))
                if n < 1 or n > 1000000: raise ValueError("Term number must be between 1 and 1,000,000.")
                term = a*(step**(n-1)) if gp else a+(n-1)*step
                return f"{'GP' if gp else 'AP'} term {n} = {_fmt(term)}"
            if nmatch:
                n = int(nmatch.group(1))
                if n > 1000000: raise ValueError("Please limit progression calculations to one million terms.")
                total = a*n if gp and step == 1 else (a*(step**n-1)/(step-1) if gp else n*(2*a+(n-1)*step)/2)
                return f"{'GP' if gp else 'AP'} sum of first {n} terms = {_fmt(total)}"

    # Derivative/integral requests without command-first syntax.
    if re.search(r"\b(differentiate|derivative of|integrate|integral of)\b", low):
        derivative = bool(re.search(r"\b(differentiate|derivative of)\b", low))
        var_match = re.search(r"\bwith respect to\s+([a-z])\b", low)
        variable = sp.Symbol(var_match.group(1)) if var_match else sp.Symbol("x")
        expr_text = re.sub(r"\b(differentiate|derivative of|integrate|integral of)\b|\bwith respect to\s+[a-z]\b|\bd[xyz t]\b", "", _normalise(raw)).strip()
        expr = _safe_parse(expr_text)
        return f"Derivative = {_fmt(sp.diff(expr, variable))}" if derivative else f"Integral = {_fmt(sp.integrate(expr, variable))} + C"

    norm = _normalise(raw)
    # Remove only harmless command words. Do not silently drop unknown prose.
    norm = re.sub(r"\b(?:find|solve|the|of|please)\b", " ", norm).strip()
    if "=" in norm:
        equation_parts = re.split(r"\s+and\s+", norm)
        equations = []
        all_symbols = set()
        for part in equation_parts:
            if "=" not in part:
                raise ValueError("For simultaneous equations, separate complete equations with 'and'.")
            left, right = part.split("=", 1)
            left_expr, right_expr = _safe_parse(left.strip()), _safe_parse(right.strip())
            equations.append(sp.Eq(left_expr, right_expr))
            all_symbols.update((left_expr-right_expr).free_symbols)
        variables = sorted(all_symbols, key=str)
        if not variables:
            return "Equation check: " + ("true" if all(sp.simplify(eq.lhs-eq.rhs) == 0 for eq in equations) else "false")
        solutions = sp.solve(equations, variables, dict=True)
        complex_requested = bool(re.search(r"\b(?:complex numbers|over the complex|complex solutions)\b", original_wording))
        if not complex_requested:
            solutions = [sol for sol in solutions if all(
                not value.has(sp.I) and value.is_real is not False
                for value in sol.values()
            )]
        if not solutions:
            return "No real solutions found." if not complex_requested else "No solution found (the equations may be inconsistent)."
        if len(equations) > 1 or len(variables) > 1:
            answer = "Solutions: " + "; ".join(", ".join(f"{var} = {_fmt(sol[var])}" for var in variables if var in sol) for sol in solutions)
        else:
            vals = [sol[variables[0]] for sol in solutions if variables[0] in sol]
            answer = f"Solutions for {variables[0]}: " + (", ".join(_fmt(v) for v in vals) if vals else "no solution found")
        if re.search(r"domain restriction|excluded value", original_wording):
            excluded = []
            for part in equation_parts:
                left, right = part.split("=", 1)
                for side in (left, right):
                    denominator = sp.denom(sp.together(_safe_parse(side.strip())))
                    if denominator != 1 and denominator.free_symbols:
                        symbol = sorted(denominator.free_symbols, key=str)[0]
                        for root in sp.solve(denominator, symbol):
                            item = f"{symbol} != {_fmt(root)}"
                            if item not in excluded: excluded.append(item)
            if excluded: answer += "; domain restrictions: " + ", ".join(excluded)
        return answer
    expr = _safe_parse(norm)
    if expr.free_symbols: return f"Simplified expression = {_fmt(expr)}"
    result = sp.simplify(expr)
    if result.has(sp.zoo, sp.nan, sp.oo, -sp.oo): raise ValueError("The expression is undefined (for example, division by zero).")
    return f"Result = {_fmt(result)}"
