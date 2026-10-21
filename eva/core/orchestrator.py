
"""Coordinate EVA's requests, router, tools, and language model."""

import re
import json
from eva.config import (
    SYSTEM_PROMPT,
    MAX_CONTEXT_TURNS,
    ONLINE_ENABLED,
)
from eva.providers.ollama_provider import OllamaProvider
from eva.memory.context import ConversationMemory
from eva.core.task_router import TaskRouter
from eva.tools.calculator import calculate_from_text
from eva.tools.math_engine import solve_math, _safe_parse, _normalise, _fmt
import sympy as sp
from eva.tools.system_tools import get_local_datetime
from eva.tools.math_solver import solve_progression
from eva.tools.web_search import search_web
from eva.coding.project_agent import ProjectAgent, ProjectGenerationError
from eva.automation.desktop import route_and_execute as run_desktop_automation


class EVAOrchestrator:
    """Coordinate request routing and response generation."""

    def __init__(self):
        self.provider = OllamaProvider()
        self.router = TaskRouter()
        self.memory = ConversationMemory(
            max_turns=MAX_CONTEXT_TURNS
        )

    def _remove_failed_user_message(self, user_input):
        """Remove the current user message after a failed request."""

        if not self.memory.messages:
            return

        last = self.memory.messages[-1]

        if (
            last["role"] == "user"
            and last["content"] == user_input
        ):
            self.memory.messages.pop()

    def _resolve_math_followup(self, user_input):
        """Resolve deterministic follow-ups against the correct prior math result."""
        cleaned = re.sub(r"[\s.!?\]\)]+$", "", user_input.strip().lower())
        messages = list(self.memory.messages)

        def assistant_expression(message, labels=("Simplified expression", "Expanded expression",
                                                   "Expanded and simplified expression", "Factored expression",
                                                   "Derivative", "Integral", "Result")):
            if message.get("role") != "assistant":
                return None
            content = message.get("content", "").strip()
            for label in labels:
                match = re.search(rf"^{re.escape(label)}\s*=\s*(.+?)(?:\s+\(approximately.*)?$", content, re.I)
                if match:
                    candidate = match.group(1).strip()
                    # Only accept a math-looking payload from a prior deterministic result.
                    if re.search(r"[0-9a-zA-Z]+|[+*/^()-]", candidate):
                        return candidate
            return None

        # Evaluate a derivative previously returned by EVA at a supplied point.
        m = re.fullmatch(r"(?:now\s+)?evaluate (?:the )?derivative at ([a-z])\s*=\s*(-?\d+(?:\.\d+)?)", cleaned)
        if m:
            var, point = m.groups()
            expr = next((assistant_expression(msg, ("Derivative",)) for msg in reversed(messages)
                         if assistant_expression(msg, ("Derivative",))), None)
            if expr is None:
                return "I don't have a previous derivative to evaluate. Ask me to differentiate an expression first."
            try:
                value = _safe_parse(_normalise(expr)).subs(sp.Symbol(var), sp.Rational(point))
                return f"Derivative evaluated at {var} = {point} is {_fmt(sp.simplify(value))}"
            except Exception as error:
                return f"I couldn't evaluate the previous derivative safely: {error}"

        # Select the current result or the first mathematical result in this session.
        wants_original = "original expression" in cleaned
        wants_result = "that result" in cleaned or "the result" in cleaned
        expressions = [(i, assistant_expression(msg)) for i, msg in enumerate(messages)]
        expressions = [(i, expr) for i, expr in expressions if expr]
        if wants_original:
            expr = expressions[0][1] if expressions else None
        else:
            expr = expressions[-1][1] if expressions else None

        # Explicit follow-up actions.
        if re.fullmatch(r"(?:then |now |please )*(?:differentiate|derive) (?:that result|it|that|this)", cleaned):
            if not expr:
                return None
            try:
                return solve_math(f"differentiate {expr}")
            except Exception as error:
                return f"I recognised the maths follow-up but couldn't differentiate it safely: {error}"

        if re.fullmatch(r"(?:then |now |please )*(?:expand|factor|factorise|factorize|simplify) (?:it|that|this|the original expression|the result)", cleaned):
            operation = re.search(r"\b(expand|factor|factorise|factorize|simplify)\b", cleaned).group(1)
            if not expr:
                return None
            try:
                return solve_math(f"{operation} {expr}")
            except Exception as error:
                return f"I recognised the maths follow-up but couldn't complete it safely: {error}"

        original_eq = re.fullmatch(r"(?:now )?solve the original expression equal to (-?\d+(?:\.\d+)?)", cleaned)
        if original_eq:
            if not expr:
                return None
            try:
                return solve_math(f"solve {expr} = {original_eq.group(1)}")
            except Exception as error:
                return f"I couldn't solve the original expression safely: {error}"

        match = re.fullmatch(
            r"(?:(?:then|now|please)\s+)*(?:(?:type|can you|could you)\s+)*("
            r"expand|differentiate|derive|integrate|factor|factorise|factorize|"
            r"simplify|solve|evaluate)"
            r"(?:\s+(?:it|that|this|the expression|the equation|the result))?",
            cleaned,
        )
        if not match:
            return None
        operation = match.group(1)
        if not expr:
            return None
        try:
            if operation == "solve":
                return solve_math(f"solve it after {expr}")
            return solve_math(f"{operation} {expr}")
        except Exception as error:
            if operation == "solve":
                return f"I need an equation to solve. {error}"
            return ("I recognised the follow-up as mathematics, but couldn't "
                    f"complete it safely: {error}")

    def respond_stream(self, user_input):
        """Route a request and yield its response progressively."""

        user_input = user_input.strip()

        if not user_input:
            yield "I'm listening. What's on your mind?"
            return

        # Short mathematical follow-ups ("expand it", "solve it", etc.)
        # must use the previous maths turn, not be guessed by the language model.
        followup_reply = self._resolve_math_followup(user_input)
        if followup_reply is not None:
            print("[DEBUG ROUTER] MATHEMATICS_FOLLOWUP -> math_engine")
            self.memory.add_message("user", user_input)
            self.memory.add_message("assistant", followup_reply)
            yield followup_reply
            return

        # Decide which tool or AI handler should process the request.
        decision = self.router.route(user_input)

        # Temporary diagnostic: shows which route EVA selected.
        print(
            f"[DEBUG ROUTER] {decision.category} "
            f"-> {decision.handler}"
        )

        self.memory.add_message("user", user_input)

        # Desktop automation uses an explicit allow-list rather than free-form shell execution.
        if decision.handler == "desktop_automation":
            try:
                result = run_desktop_automation(user_input)
                if result.get("action") == "multi_action":
                    details = {k: v for k, v in result.items() if k != "ok"}
                    detail_text = json.dumps(details, ensure_ascii=False, indent=2)
                    status = "completed" if result.get("ok") else "finished with one or more failures"
                    reply = f"Desktop automation {status}: multi_action\n{detail_text}"
                elif result.get("ok"):
                    details = {k: v for k, v in result.items() if k not in {"ok", "action"}}
                    detail_text = json.dumps(details, ensure_ascii=False, indent=2)
                    reply = f"Desktop automation completed: {result.get('action', 'action')}\n{detail_text}"
                else:
                    reply = "Desktop automation could not complete.\n" + str(result.get("error", "Unknown error"))
            except Exception as error:
                reply = f"Desktop automation failed safely: {type(error).__name__}: {error}"
            self.memory.add_message("assistant", reply)
            yield reply
            return

        # Open-ended coding uses a general-purpose project synthesis pipeline.
        # It is not limited to a registry of prewritten task templates and never
        # silently falls back to conversational prose if project generation fails.
        if decision.handler == "coding_engine":
            yield "[Working] EVA is building your project with bounded local-model usage.\n"
            try:
                result = ProjectAgent(self.provider).build(user_input)
                checks = result["validation"]["checks"]
                passed = sum(1 for item in checks if item["status"] == "passed")
                failed = [item for item in checks if item["status"] in {"failed", "error", "timeout"}]
                unchecked = [item for item in checks if item["status"] in {"unsupported", "skipped"}]
                lines = [
                    f"\nProject {'created' if result.get('status') == 'created' else 'validation failed; diagnostic project saved'}: {result['project_name']}",
                    f"Project folder: {result['project_path']}",
                    *([f"Downloadable archive: {result['archive_path']}"] if result.get("archive_path") else []),
                    f"Files created: {len(result['files'])}",
                    f"Validation status: {result['validation']['status']}",
                    f"Static checks passed: {passed}; failed: {len(failed)}; unavailable/skipped: {len(unchecked)}",
                    f"Local model calls: {result['generation']['calls']}; bounded repair attempts: {result['generation']['repair_attempts']}",
                ]
                if failed:
                    lines.append("\nValidation findings:")
                    lines.extend(f"- {item['file']}: {item['message']}" for item in failed[:8])
                elif unchecked:
                    lines.append("\nSome checks could not run because a required toolchain is unavailable. Generated source was not executed automatically.")
                else:
                    lines.append("\nAll required static checks and available bounded runtime checks passed.")
                if result.get("run_instructions"):
                    lines.append("\nRun instructions:")
                    lines.extend(f"- {instruction}" for instruction in result["run_instructions"][:8])
                if result.get("test_plan"):
                    lines.append("\nTest plan:")
                    lines.extend(f"- {item}" for item in result["test_plan"][:8])
                if result.get("assumptions"):
                    lines.append("\nAssumptions:")
                    lines.extend(f"- {item}" for item in result["assumptions"][:5])
                reply = "\n".join(lines)
            except (ProjectGenerationError, RuntimeError, OSError, ValueError) as error:
                reply = (
                    "\nEVA could not complete a validated project in this attempt. "
                    "No conversational code snippet was substituted for the requested project.\n"
                    f"Reason: {error}\n"
                    "No automatic retry was started, to protect your hardware."
                )
            except Exception as error:
                reply = (
                    "\nThe project pipeline stopped safely because of an unexpected error.\n"
                    f"{type(error).__name__}: {error}\n"
                    "No automatic retry was started."
                )
            self.memory.add_message("assistant", reply)
            yield reply
            return

        # 1. All detected mathematical requests use the deterministic engine.
        # Never delegate a maths problem to the language model.
        if decision.handler == "math_engine":
            try:
                reply = solve_math(user_input)
            except Exception as error:
                reply = (
                    "I recognized this as a mathematics problem, but the "
                    "deterministic engine could not safely solve it. "
                    f"Reason: {error}"
                )
            self.memory.add_message("assistant", reply)
            yield reply
            return

        # 1. Exact calculations
        if decision.handler == "calculator":
            try:
                reply = calculate_from_text(user_input)
            except Exception as error:
                self._remove_failed_user_message(user_input)
                yield f"I couldn't safely calculate that: {error}"
                return

            self.memory.add_message("assistant", reply)
            yield reply
            return

        # 2. Local date and time
        if decision.handler == "system_time":
            try:
                reply = get_local_datetime()
            except Exception as error:
                reply = f"I couldn't read the local date and time: {error}"

            self.memory.add_message("assistant", reply)
            yield reply
            return

        # 3. Supported arithmetic/geometric progression problems
        if decision.handler == "math_solver":
            try:
                reply = solve_progression(user_input)
            except Exception as error:
                reply = f"I couldn't solve that progression: {error}"

            self.memory.add_message("assistant", reply)
            yield reply
            return

        # 4. Online research
        if decision.handler == "web_search":
            if not ONLINE_ENABLED:
                reply = (
                    "Web research is currently disabled. "
                    "To enable it, set ONLINE_ENABLED = True "
                    "in eva/config.py."
                )
            else:
                try:
                    reply = search_web(user_input)
                except Exception as error:
                    reply = f"The web search failed: {error}"

            self.memory.add_message("assistant", reply)
            yield reply
            return

        # 5. Requests whose dedicated tools are not implemented
        if decision.handler == "unavailable":
            category = decision.category.lower().replace("_", " ")
            reply = (
                f"I recognized this as a {category} request, "
                "but its dedicated tool is not implemented yet. "
                f"{decision.reason}"
            )
            self.memory.add_message("assistant", reply)
            yield reply
            return

        # 6. Normal conversation and general knowledge
        messages = self.memory.get_messages(SYSTEM_PROMPT)
        reply_parts = []

        try:
            for chunk in self.provider.chat_stream(messages):
                reply_parts.append(chunk)
                yield chunk

        except Exception as error:
            self._remove_failed_user_message(user_input)
            yield f"\n[Connection error: {error}]"
            return

        reply = "".join(reply_parts).strip()

        if reply:
            self.memory.add_message("assistant", reply)

    def respond(self, user_input):
        """Return a complete response for non-streaming callers."""
        return "".join(self.respond_stream(user_input))

    def reset_conversation(self):
        """Clear short-term conversation memory."""
        self.memory.reset()

    def get_status(self):
        """Return basic session information."""
        return {
            "model": self.provider.model,
            "stored_messages": self.memory.message_count(),
            "online_enabled": ONLINE_ENABLED,
        }
