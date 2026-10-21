"""Static validation adapters. Source files are never executed by validate_file()."""
from __future__ import annotations
import json
import re
import shutil
import subprocess
import tempfile
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from pathlib import Path


def _result(path: Path, language: str, status: str, message: str, command=None) -> dict:
    return {"path": str(path), "language": language, "status": status, "message": message,
            "command": command, "executed_source": False}


def _validate_css(source: str) -> str | None:
    """Catch common CSS syntax defects without pretending to implement all CSS grammar."""
    stack: list[tuple[str, int]] = []
    quote: str | None = None
    escaped = False
    comment = False
    line = 1
    i = 0
    pairs = {')': '(', ']': '[', '}': '{'}
    while i < len(source):
        ch = source[i]
        nxt = source[i + 1] if i + 1 < len(source) else ''
        if ch == '\n':
            line += 1
        if comment:
            if ch == '*' and nxt == '/':
                comment = False
                i += 2
                continue
            i += 1
            continue
        if quote:
            if escaped:
                escaped = False
            elif ch == '\\':
                escaped = True
            elif ch == quote:
                quote = None
            i += 1
            continue
        if ch == '/' and nxt == '*':
            comment = True
            i += 2
            continue
        if ch in ('"', "'"):
            quote = ch
        elif ch in '([{':
            stack.append((ch, line))
        elif ch in ')]}':
            if not stack or stack[-1][0] != pairs[ch]:
                return f"CSS syntax error on line {line}: unexpected '{ch}'."
            stack.pop()
        i += 1
    if comment:
        return "CSS syntax error: unterminated /* comment."
    if quote:
        return f"CSS syntax error: unterminated {quote} string."
    if stack:
        token, opened_line = stack[-1]
        return f"CSS syntax error: unclosed '{token}' opened on line {opened_line}."
    # A declaration block with a property but no colon is almost always a typo.
    for match in re.finditer(r'\{([^{}]*)\}', source, re.S):
        body = re.sub(r'/\*.*?\*/', '', match.group(1), flags=re.S).strip()
        if not body or body.startswith('@'):
            continue
        for declaration in body.split(';'):
            declaration = declaration.strip()
            if declaration and ':' not in declaration and not declaration.startswith('@'):
                return f"CSS syntax error: declaration is missing ':' near {declaration[:80]!r}."
    return None



def _java_formatter_error(source: str) -> str | None:
    """Detect common invalid Java Formatter conversions inside string literals.

    Java accepts format strings at compile time, so javac alone cannot catch
    runtime failures such as %ld. This intentionally targets printf/format calls.
    """
    allowed = set("bBhHsScCdoxXeEfgGaAtT%n")
    # Inspect only the first string argument to a Formatter call. Other string
    # literals may contain examples or arbitrary text such as the characters %ld.
    calls = re.finditer(r'(?:\.|\b)(?:printf|format)\s*\(\s*("(?:\\.|[^"\\])*")', source, re.S)
    for call in calls:
        literal = call.group(1)
        i = 0
        while i < len(literal):
            if literal[i] != "%":
                i += 1; continue
            if i + 1 < len(literal) and literal[i + 1] == "%":
                i += 2; continue
            j = i + 1
            while j < len(literal) and literal[j] in "0123456789$#+-( <":
                j += 1
            while j < len(literal) and literal[j].isdigit():
                j += 1
            if j < len(literal) and literal[j] == ".":
                j += 1
                while j < len(literal) and literal[j].isdigit():
                    j += 1
            if j < len(literal) and literal[j] in "tT":
                j += 1
            if j >= len(literal):
                return "Java format string ends with an incomplete '%' conversion."
            conversion = literal[j]
            if conversion not in allowed:
                return f"Unsupported Java Formatter conversion '%{conversion}' in a string literal; Java uses %d for long integers, not %ld."
            i = j + 1
    return None


def _java_input_error(source: str, request: str) -> str | None:
    """Check stdin reads when the user's request clearly requires interactive input."""
    prompt = request.lower()
    requires_input = any(term in prompt for term in (
        "user input", "user entered", "entered number", "enter a number",
        "input from user", "read input", "take input", "accept input",
        "input a number", "factorial of a number", "number entered",
        "menu driven", "menu-driven", "ask the user",
    ))
    if not requires_input:
        return None
    input_patterns = (r'\bScanner\b', r'\bSystem\s*\.\s*in\b',
                      r'\bBufferedReader\b', r'\bConsole\b',
                      r'\breadLine\s*\(', r'\bnext(?:Int|Long|Double|Line|Float|Boolean)\s*\(')
    # Catch common hardcoding instead of reading the value after prompting.
    if re.search(r'\bint\s+n\s*=\s*\d+\s*;', source) and not re.search(r'\b(?:nextInt|parseInt|readLine)\s*\(', source):
        return "Java source appears to hardcode the input number instead of reading it from the user."
    if not any(re.search(pattern, source) for pattern in input_patterns):
        return "This request requires interactive input, but the Java source does not appear to read from stdin (for example Scanner(System.in) or BufferedReader)."
    return None

def validate_file(path: str | Path, *, timeout_seconds: int = 10) -> dict:
    p = Path(path).expanduser().resolve(strict=True)
    if not p.is_file():
        raise ValueError("Validation target must be a file.")
    if p.stat().st_size > 2_000_000:
        return _result(p, "unknown", "skipped", "File exceeds 2 MB validation limit.")
    ext = p.suffix.lower()
    try:
        source = p.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as exc:
        return _result(p, ext.lstrip('.') or "unknown", "failed", f"Source is not valid UTF-8: {exc}")
    # Empty source files must never pass validation. Some compilers (notably
    # javac) can return success for an empty .java file without producing a
    # class, which previously allowed a blank generated program to be reported
    # as a successful build.
    source_exts = {".py", ".pyi", ".java", ".js", ".mjs", ".cjs", ".c", ".h",
                   ".cpp", ".cc", ".cxx", ".hpp", ".css", ".html", ".htm",
                   ".json", ".xml", ".svg", ".ts", ".tsx", ".rs", ".go"}
    if ext in source_exts and not source.strip():
        return _result(p, ext.lstrip('.') or "unknown", "failed", "Source file is empty or contains only whitespace.")
    if ext == ".java":
        format_error = _java_formatter_error(source)
        if format_error:
            return _result(p, "Java", "failed", format_error)
    if ext in {".py", ".pyi"}:
        try:
            compile(source, str(p), "exec", dont_inherit=True)
            return _result(p, "Python", "passed", "Python syntax compilation passed; code was not executed.")
        except SyntaxError as exc:
            line = getattr(exc, "lineno", None)
            return _result(p, "Python", "failed", f"{type(exc).__name__}: {exc}" + (f" (line {line})" if line else ""))
    if ext == ".json":
        try:
            json.loads(source)
            return _result(p, "JSON", "passed", "JSON parsing passed.")
        except json.JSONDecodeError as exc:
            return _result(p, "JSON", "failed", f"JSON syntax error on line {exc.lineno}, column {exc.colno}: {exc.msg}")
    if ext in {".xml", ".svg"}:
        try:
            ET.fromstring(source)
            return _result(p, "XML", "passed", "XML well-formedness check passed.")
        except ET.ParseError as exc:
            return _result(p, "XML", "failed", f"XML syntax error: {exc}")
    if ext == ".css":
        error = _validate_css(source)
        return _result(p, "CSS", "failed" if error else "passed", error or "CSS delimiter/string/declaration checks passed (not a full CSS specification validator).")
    if ext in {".html", ".htm"}:
        class ParseCheck(HTMLParser):
            def __init__(self):
                super().__init__(convert_charrefs=True)
                self.error = None
            def feed_checked(self, text):
                try:
                    self.feed(text)
                    self.close()
                except Exception as exc:
                    self.error = f"HTML parser error: {type(exc).__name__}: {exc}"
        parser = ParseCheck()
        parser.feed_checked(source)
        return _result(p, "HTML", "failed" if parser.error else "passed", parser.error or "HTML parser accepted the document; browser conformance is not fully checked.")
    if ext in {".js", ".mjs", ".cjs"}:
        node = shutil.which("node")
        if not node:
            return _result(p, "JavaScript", "unsupported", "Node.js is unavailable; JavaScript syntax was not checked.")
        command = [node, "--check", str(p)]
    elif ext == ".java":
        javac = shutil.which("javac")
        if not javac:
            return _result(p, "Java", "unsupported", "javac is unavailable; Java syntax was not checked.")
        with tempfile.TemporaryDirectory(prefix="eva-javac-") as out_dir:
            command = [javac, "-proc:none", "-Xlint:none", "-d", out_dir, str(p)]
            try:
                completed = subprocess.run(command, cwd=str(p.parent), capture_output=True, text=True,
                                           timeout=max(1, min(timeout_seconds, 30)), shell=False)
                output = (completed.stderr or completed.stdout or "").strip()
                return _result(p, "Java", "passed" if completed.returncode == 0 else "failed",
                               output[:8000] or ("Compilation passed; output artifacts were isolated in a temporary directory." if completed.returncode == 0 else "Compiler failed without diagnostics."), command)
            except subprocess.TimeoutExpired:
                return _result(p, "Java", "timeout", "Static validator timed out.", command)
            except OSError as exc:
                return _result(p, "Java", "error", str(exc), command)
    elif ext in {".c", ".h"} and shutil.which("gcc"):
        command = [shutil.which("gcc"), "-x", "c", "-fsyntax-only", str(p)]
    elif ext in {".cpp", ".cc", ".cxx", ".hpp"} and shutil.which("g++"):
        command = [shutil.which("g++"), "-x", "c++", "-fsyntax-only", str(p)]
    else:
        language = {".ts": "TypeScript", ".tsx": "TypeScript/TSX", ".rs": "Rust", ".go": "Go"}.get(ext, "unknown")
        return _result(p, language, "unsupported", "No configured static validator is available for this file type on this machine.")
    try:
        completed = subprocess.run(command, cwd=str(p.parent), capture_output=True, text=True,
                                   timeout=max(1, min(timeout_seconds, 30)), shell=False)
        output = (completed.stderr or completed.stdout or "").strip()
        return _result(p, ext[1:], "passed" if completed.returncode == 0 else "failed",
                       output[:8000] or ("Syntax check passed." if completed.returncode == 0 else "Validator failed without diagnostics."), command)
    except subprocess.TimeoutExpired:
        return _result(p, ext[1:], "timeout", "Static validator timed out.", command)
    except OSError as exc:
        return _result(p, ext[1:], "error", str(exc), command)
