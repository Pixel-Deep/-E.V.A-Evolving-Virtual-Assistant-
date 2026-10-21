"""Deterministic classification of common compiler and test failures."""
import re

_RULES = [
    ("syntax_error", re.compile(r"SyntaxError|syntax error|unexpected token", re.I)),
    ("missing_import", re.compile(r"ModuleNotFoundError|ImportError|cannot find symbol.*import|cannot find module", re.I)),
    ("undefined_name", re.compile(r"NameError|is not defined|cannot find symbol", re.I)),
    ("type_error", re.compile(r"TypeError|incompatible types|type mismatch", re.I)),
    ("attribute_error", re.compile(r"AttributeError|has no attribute|undefined method", re.I)),
    ("assertion_failure", re.compile(r"AssertionError|assertion failed|expected .* but was", re.I)),
    ("timeout", re.compile(r"timed out|timeout|time limit exceeded", re.I)),
    ("permission_error", re.compile(r"PermissionError|permission denied|access is denied", re.I)),
    ("dependency_conflict", re.compile(r"dependency conflict|version conflict|resolution impossible", re.I)),
    ("security_warning", re.compile(r"SQL injection|command injection|hardcoded secret|unsafe deserialization", re.I)),
]


def classify_diagnostic(text: str) -> dict:
    matches = [name for name, pattern in _RULES if pattern.search(text or "")]
    category = matches[0] if matches else "unknown"
    return {"category": category, "matched_rules": matches, "needs_review": category == "unknown" or category == "security_warning",
            "automatically_fixable": False,
            "message": "Diagnostic classified by deterministic pattern matching; this is a lead, not proof of root cause."}
