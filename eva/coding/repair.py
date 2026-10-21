"""Evidence-based repair suggestions; deliberately does not auto-edit source."""
from __future__ import annotations
import re
from eva.coding.diagnostics import classify_diagnostic

_HINTS = [
    (re.compile(r"NameError: name ['\"]([^'\"]+)['\"] is not defined"), "undefined_name",
     "Check spelling and scope; confirm the name is defined before use and imported if external.",
     ["Run the relevant test", "Run static analysis", "Check all call sites"]),
    (re.compile(r"ModuleNotFoundError: No module named ['\"]([^'\"]+)['\"]"), "missing_import",
     "Check whether the module is part of the standard library, the project, or an explicitly declared dependency. Do not install automatically.",
     ["Inspect project dependency manifest", "Check active interpreter/environment", "Run tests in the intended environment"]),
    (re.compile(r"IndentationError|unexpected indent|unindent does not match"), "syntax_error",
     "Inspect the indicated block and make indentation consistent with the surrounding syntax; avoid global whitespace replacement.",
     ["Compile/parse the file", "Run nearby tests", "Inspect the resulting diff"]),
    (re.compile(r"AssertionError|assertion failed", re.I), "assertion_failure",
     "Compare expected and actual values; determine whether the implementation or the test expectation is wrong before changing either.",
     ["Reproduce the failing test", "Add a minimal regression case", "Run the full relevant suite"]),
    (re.compile(r"TypeError: .* (?:missing .* required positional argument|unsupported operand type|not callable)", re.I), "type_error",
     "Inspect the complete traceback, argument count, and operand types; validate the intended API contract before editing.",
     ["Check function signature and call sites", "Add boundary/type tests", "Run static analysis"]),
]

def suggest_repairs(diagnostic: str) -> dict:
    base = classify_diagnostic(diagnostic)
    for pattern, category, guidance, checks in _HINTS:
        match = pattern.search(diagnostic or "")
        if match:
            return {"category": category, "matched_detail": match.group(0), "guidance": guidance,
                    "verification_steps": checks, "automatic_edit": False,
                    "confidence": "pattern_match_only", "needs_review": True}
    return {**base, "guidance": "No specific repair rule matched. Preserve the failure and collect a minimal reproducible example.",
            "verification_steps": ["Reproduce the failure", "Capture full diagnostic and environment", "Create a minimal regression test"],
            "confidence": "unknown", "automatic_edit": False}
