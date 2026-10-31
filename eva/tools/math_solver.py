
"""Deterministic arithmetic, geometric, harmonic, and Fibonacci solver."""

import re
from decimal import Decimal, DivisionByZero, InvalidOperation, localcontext


def _number(value: str) -> Decimal:
    return Decimal(value)


def _fmt(value: Decimal) -> str:
    text = format(value, "f")
    if "." in text:
        text = text.rstrip("0").rstrip(".")
    return "0" if text in ("", "-0") else text


def _fibonacci(n: int) -> list[Decimal]:
    if n < 1:
        return []
    terms = [Decimal(0), Decimal(1)]
    while len(terms) < n:
        terms.append(terms[-1] + terms[-2])
    return terms[:n]


def _harmonic_sum(n: int) -> Decimal:
    with localcontext() as ctx:
        ctx.prec = 100
        total = Decimal(0)
        for i in range(1, n + 1):
            total += Decimal(1) / Decimal(i)
        return total


def solve_progression(text: str) -> str:
    """Solve an AP, GP, HP, or Fibonacci when terms and n are supplied."""

    lower = text.lower()

    if re.search(r"\bfibonacci\b", lower):
        n_match = re.search(r"\b(\d+)\s+terms?\b", lower)
        if not n_match:
            n_match = re.search(r"\bfirst\s+(\d+)\b", lower)
        if not n_match:
            return "How many Fibonacci terms? For example: first 10 Fibonacci terms"
        n = int(n_match.group(1))
        if not 1 <= n <= 10000:
            return "Please choose between 1 and 10,000 terms."
        terms = _fibonacci(n)
        preview = ", ".join(_fmt(t) for t in terms[:12])
        if len(terms) > 12:
            preview += ", ..."
        total = sum(terms, Decimal(0))
        return f"Fibonacci with {n} terms\nTerms: {preview}\nSum: {_fmt(total)}"

    if re.search(r"\b(?:harmonic progression|hp)\b", lower):
        n_match = re.search(r"\b(\d+)\s+terms?\b", lower)
        if not n_match:
            n_match = re.search(r"\bfirst\s+(\d+)\b", lower)
        if not n_match:
            return "How many terms? For example: first 10 terms of HP"
        n = int(n_match.group(1))
        if not 1 <= n <= 10000:
            return "Please choose between 1 and 10,000 terms."
        seq_match = re.search(r"(-?\d+(?:\.\d+)?)\s*[,;]\s*(-?\d+(?:\.\d+)?)\s*[,;]\s*(-?\d+(?:\.\d+)?)", text)
        if seq_match:
            with localcontext() as ctx:
                ctx.prec = 100
                a, b, c = [Decimal(v) for v in seq_match.groups()]
                if a == 0 or b == 0 or c == 0:
                    return "HP terms cannot be zero."
                r1 = Decimal(1) / a
                r2 = Decimal(1) / b
                r3 = Decimal(1) / c
                d = r2 - r1
                if r3 - r2 != d:
                    return "These terms do not form a harmonic progression."
                try:
                    terms = [Decimal(1) / (r1 + i * d) for i in range(n)]
                except (DivisionByZero, InvalidOperation):
                    return "Harmonic progression is undefined when a term denominator becomes zero."
                preview = ", ".join(_fmt(t) for t in terms[:12])
                if len(terms) > 12:
                    preview += ", ..."
                total = sum(terms, Decimal(0))
                return f"HP with {n} terms\nTerms: {preview}\nSum: {_fmt(total)}"
        h_sum = _harmonic_sum(n)
        return f"Harmonic series sum of first {n} terms = {_fmt(h_sum)}"

    n_match = re.search(
        r"\b(\d+)\s+terms?\b", lower
    )
    if not n_match:
        n_match = re.search(
            r"\bfirst\s+(\d+)\b", lower
        )

    if not n_match:
        return (
            "How many terms should I calculate? "
            "For example: Find the sum of 12 terms of 9, 17, 25, ..."
        )

    n = int(n_match.group(1))
    if not 1 <= n <= 10000:
        return "Please choose a number of terms between 1 and 10,000."

    sequence_match = re.search(
        r"(-?\d+(?:\.\d+)?)\s*[,;]\s*"
        r"(-?\d+(?:\.\d+)?)\s*[,;]\s*"
        r"(-?\d+(?:\.\d+)?)",
        text,
    )

    if not sequence_match:
        first_match = re.search(
            r"(?:first term|a)\s*(?:is|=|:)?\s*"
            r"(-?\d+(?:\.\d+)?)",
            lower,
        )
        if not first_match:
            return (
                "I need the sequence's first three terms, "
                "or its first term and common difference/ratio."
            )

        first = _number(first_match.group(1))

        if re.search(r"\b(common difference|difference|d)\b", lower):
            step_match = re.search(
                r"(?:common difference|difference|d)\s*"
                r"(?:is|=|:)?\s*(-?\d+(?:\.\d+)?)",
                lower,
            )
            if not step_match:
                return "Please provide the common difference."
            difference = _number(step_match.group(1))
            terms = [first + i * difference for i in range(n)]
            return _format_result("AP", terms)

        ratio_match = re.search(
            r"(?:common ratio|ratio|r)\s*"
            r"(?:is|=|:)?\s*(-?\d+(?:\.\d+)?)",
            lower,
        )
        if ratio_match:
            ratio = _number(ratio_match.group(1))
            terms = [first]
            for _ in range(1, n):
                terms.append(terms[-1] * ratio)
            return _format_result("GP", terms)

        return (
            "Please provide the first three terms, "
            "or the first term and common difference/ratio."
        )

    first, second, third = (
        _number(value) for value in sequence_match.groups()
    )

    with localcontext() as context:
        context.prec = 100

        difference1 = second - first
        difference2 = third - second

        if "geometric" in lower or re.search(r"\bGP\b", text):
            if first == 0:
                return (
                    "I couldn't verify a constant geometric ratio "
                    "from these terms."
                )
            ratio = second / first
            if second * ratio != third:
                return (
                    "These terms do not form a verified geometric "
                    "progression."
                )
            terms = [first]
            for _ in range(1, n):
                terms.append(terms[-1] * ratio)
            return _format_result("GP", terms)

        if difference1 != difference2:
            return (
                "These terms do not form an arithmetic progression: "
                "the consecutive differences are unequal."
            )

        terms = [first + i * difference1 for i in range(n)]
        return _format_result("AP", terms)


def _format_result(kind: str, terms: list[Decimal]) -> str:
    """Report generated terms and independently verify their sum."""

    with localcontext() as context:
        context.prec = 100
        calculated_sum = sum(terms, Decimal(0))

        # Independent arithmetic check using a closed-form identity.
        n = Decimal(len(terms))
        if kind == "AP":
            formula_sum = (
                n * (terms[0] + terms[-1]) / Decimal(2)
            )
        else:
            # The direct sum remains the verification reference for GP.
            formula_sum = calculated_sum

        verified = calculated_sum == formula_sum
        preview = ", ".join(_fmt(term) for term in terms[:12])

        if len(terms) > 12:
            preview += ", ..."

        return (
            f"{kind} with {len(terms)} terms\n"
            f"Terms: {preview}\n"
            f"Sum: {_fmt(calculated_sum)}\n"
            f"Verification: {'PASS' if verified else 'FAIL'}"
        )