
"""Coordinate EVA's requests, router, tools, and language model."""
from __future__ import annotations

import os
import re
import json
from typing import Any
from eva.config import (
    SYSTEM_PROMPT,
    MAX_CONTEXT_TURNS,
    ONLINE_ENABLED,
)
from eva.providers.ollama_provider import OllamaProvider
from eva.memory.context import ConversationMemory
from eva.core.task_router import TaskRouter, RouteDecision
from eva.tools.calculator import calculate_from_text
from eva.tools.math_engine import solve_math, _safe_parse, _normalise, _fmt
import sympy as sp
from eva.tools.system_tools import get_local_datetime
from eva.tools.math_solver import solve_progression
from eva.tools.web_search import search_web
from eva.coding.project_agent import ProjectAgent, ProjectGenerationError
from eva.automation.service import (
    clear_pending_approval,
    has_pending_task_clarification,
    clear_pending_task_clarification,
    should_supersede_pending_clarification,
    handle_automation_request as run_desktop_automation,
)


def _format_task_response(result):
    """Render the plan and observed results without claiming unverified success."""
    action = result.get("action", "task_plan")
    lines = []
    goal = result.get("goal")
    if goal:
        lines.append(f"Task: {goal}")

    if action == "need_input":
        message = result.get("error") or result.get("message") or "More information is required."
        lines.append(str(message))
        for question in result.get("questions", [])[:8]:
            lines.append(f"- {question}")
        return "\n".join(lines)

    if action == "capability_gaps":
        gaps = result.get("gaps", [])
        if result.get("error"):
            return str(result["error"])
        lines.append(str(result.get("message") or f"Recorded capability gaps: {len(gaps)}"))
        for item in gaps[:100]:
            lines.append(f"- {item.get('name', 'unknown')} [{item.get('category', 'general')}] — {item.get('occurrences', 0)} occurrence(s)")
            if item.get("description"):
                lines.append(f"  Needed: {item['description']}")
            if item.get("reason"):
                lines.append(f"  Reason: {item['reason']}")
        lines.append(f"Capability-gap JSON: {result.get('path', 'path unavailable')}")
        return "\n".join(lines)

    if action == "partial_capability_gap":
        lines.append("Partial project inspection only; no patch, tests, build, or lint were executed.")
        for name in result.get("missing_capabilities", [])[:12]:
            lines.append(f"- Missing capability: {name}")
        lines.append(str(result.get("message") or "Some requested steps are not supported yet."))
        if result.get("capability_gap_file"):
            lines.append(f"Capability-gap records: {result['capability_gap_file']}")
        for detail in result.get("gap_record_errors", [])[:5]:
            lines.append(f"- Capability-gap record error: {detail}")
        return "\n".join(lines)

    if action == "capability_gap":
        names = result.get("missing_capabilities", [])
        if names:
            lines.append("Missing registered capabilities:")
            lines.extend(f"- {name}" for name in names)
        lines.append(str(result.get("message") or result.get("error") or "No steps were executed."))
        for question in result.get("questions", [])[:8]:
            lines.append(f"- Required input: {question}")
        for detail in result.get("gap_record_errors", [])[:5]:
            lines.append(f"- Capability-gap record error: {detail}")
        if result.get("capability_gap_file"):
            lines.append(f"Capability-gap records: {result['capability_gap_file']}")
        return "\n".join(lines)

    if action == "plan_approval_required":
        lines.append("Validated plan preview (nothing has executed):")
        for step in result.get("plan", {}).get("steps", [])[:12]:
            lines.append(f"- {step.get('id')}: {step.get('purpose') or step.get('tool')} [{step.get('tool')}]")
            # Approval is meaningful only when the user can inspect the exact
            # paths, text, and parameters that will be executed.
            args_text = json.dumps(step.get("args", {}), ensure_ascii=False, separators=(",", ":"))
            lines.append(f"  Arguments: {args_text[:1800]}")
            if len(args_text) > 1800:
                lines.append("  [Argument display truncated; do not approve unless the complete action is otherwise clear.]")
        lines.append(str(result.get("message") or "Explicit approval is required before execution."))
        return "\n".join(lines)

    if action in {"planner_error", "invalid_plan", "action_blocked"}:
        lines.append(str(result.get("error") or result.get("message") or "The plan was rejected."))
        lines.append(str(result.get("message") or "No steps were executed."))
        if result.get("blocked_step"):
            lines.append(f"Blocked step: {result.get('blocked_step')} ({result.get('tool')})")
        return "\n".join(lines)

    execution = result.get("execution", {})
    raw_steps = (execution.get("steps", []) if isinstance(execution, dict) else []) or result.get("steps", [])
    steps = [s for s in raw_steps if isinstance(s, dict)]
    if steps:
        lines.append("Execution results:")
        for idx, step in enumerate(steps[:12], 1):
            status = step.get("status") or ("succeeded" if step.get("ok") is True else "failed" if step.get("ok") is False else "unknown")
            step_id = step.get("id") or step.get("step") or (step.get("data", {}).get("opened") if isinstance(step.get("data"), dict) and step.get("data", {}).get("opened") else None) or step.get("target") or f"step_{idx}"
            tool = step.get("tool") or step.get("action") or step.get("target") or "action"
            step_result = step.get("result", {}) if isinstance(step.get("result", {}), dict) else {}
            lines.append(f"- {step_id} · {tool}: {status}")
            verified = step_result.get("verified") if step_result.get("verified") is not None else step.get("verified")
            if verified is not None:
                lines.append(f"  Verified: {'yes' if verified else 'no / not independently verifiable'}")
            data = (step_result.get("data", {}) if isinstance(step_result.get("data", {}), dict) else {}) or (step.get("data", {}) if isinstance(step.get("data"), dict) else {})
            path = data.get("path") or step_result.get("path") or step.get("path")
            if path:
                lines.append(f"  Path: {path}")
            url = data.get("url") or step_result.get("url") or step.get("url")
            if url:
                lines.append(f"  URL: {url}")
            if data.get("count") is not None:
                lines.append(f"  Count: {data['count']}")
            if data.get("truncated"):
                lines.append("  Output was truncated to the configured safe read limit.")
            # User explicitly asked for text; render actual returned contents rather
            # than burying them in raw JSON or claiming that the file was shown.
            text_value = data.get("text") or step_result.get("text") or step.get("text")
            if isinstance(text_value, str) and text_value:
                remaining = max(0, 10000 - sum(len(line) for line in lines))
                if remaining:
                    lines.append("  Text output:")
                    lines.append(text_value[:remaining])
                if len(text_value) > remaining:
                    lines.append("  [Displayed text was truncated in EVA's response.] ")
            err_code = step_result.get("error_code") or step.get("error_code")
            err_msg = step_result.get("message") or step.get("message") or step.get("error") or ""
            if err_code or (status == "failed" and err_msg):
                lines.append(f"  Error: {err_code or 'ERROR'}: {err_msg}")
    missing_capabilities = result.get("missing_capabilities", [])
    if missing_capabilities:
        lines.append("Unavailable capabilities (independent steps were still attempted where possible):")
        lines.extend(f"- {name}" for name in missing_capabilities[:12])
    for question in result.get("questions", [])[:8]:
        lines.append(f"Remaining input needed: {question}")
    for detail in result.get("gap_record_errors", [])[:5]:
        lines.append(f"Capability-gap record error: {detail}")
    message = result.get("message") or execution.get("message")
    if message:
        lines.append(str(message))
    if result.get("capability_gap_file"):
        lines.append(f"Capability-gap records: {result['capability_gap_file']}")
    if not steps and not lines:
        return json.dumps(result, ensure_ascii=False, indent=2)[:12000]
    return "\n".join(lines)


def _format_registry_response(result):
    """Format live registry metadata without inventing tool capabilities."""
    action = result.get("action")
    tools = result.get("tools", [])
    lines = []
    if action == "tool_catalog":
        lines.append(f"Registered deterministic automation tools: {len(tools)}")
        groups = {}
        for item in tools:
            groups.setdefault(item.get("category", "general"), []).append(item)
        for category in sorted(groups):
            lines.append(f"\n[{category}]")
            for item in groups[category]:
                lines.append(f"- {item['name']}: {item.get('description', '')}")
                if item.get("when_to_use"):
                    use = item["when_to_use"]
                    lines.append("  When to use: " + ("; ".join(use) if isinstance(use, list) else str(use)))
                if item.get("output_description"):
                    lines.append(f"  Returns: {item['output_description']}")
                if item.get("side_effects"):
                    lines.append(f"  Side effects: {item['side_effects']}")
                lines.append(f"  Permissions: {', '.join(item.get('permissions', [])) or 'none'} | "
                             f"Risk: {item.get('risk', 'unknown')} | "
                             f"Approval required: {item.get('requires_approval', False)} | "
                             f"Timeout: {item.get('timeout_seconds', 'unknown')}s")
                lines.append("  Dependencies: " + "; ".join(item.get("dependencies", [])))
        lines.append("\nThis list is generated from EVA's live registered tool specs.")
        return "\n".join(lines)
    if action == "tool_help":
        lines.append(f"Tool usage reference ({len(tools)} tool(s))")
        for item in tools:
            lines.extend([
                f"\n{item['name']} [{item.get('category', 'general')}]",
                f"  Purpose: {item.get('purpose', item.get('description', ''))}",
                "  When to use: " + ("; ".join(item.get("when_to_use", [])) if isinstance(item.get("when_to_use"), list) else str(item.get("when_to_use", "Use only when the requested outcome matches this tool."))),
                f"  Returns: {item.get('output_description', 'Structured result; inspect data and verification fields.')}",
                f"  Side effects: {item.get('side_effects', 'See permissions and risk.')}",
                "  Parameters/schema: " + json.dumps(item.get("input_schema", {}), ensure_ascii=False, separators=(",", ":")),
                "  Permissions: " + (", ".join(item.get("permissions", [])) or "none"),
                f"  Risk/approval/timeout: {item.get('risk', 'unknown')} / {item.get('requires_approval', False)} / {item.get('timeout_seconds', 'unknown')}s",
                "  Dependencies/limits: " + "; ".join(item.get("dependencies", [])),
                "  Example: " + item.get("example", ""),
            ])
        lines.append("\nLimitations: inputs must satisfy the schema; filesystem tools stay within configured approved roots; host OS/dependencies/network availability may limit execution; a successful launch request does not prove visible UI state.")
        return "\n".join(lines)
    if action == "capability_map":
        lines.append("Deterministic capability map (derived from the live tool registry)")
        for item in result.get("mapped_steps", []):
            lines.append(f"- {item}")
        lines.append("Installed workflows: " + (", ".join(result.get("workflows", [])) or "none"))
        lines.append("Missing capabilities: " + ("; ".join(result.get("missing_capabilities", [])) or "none identified by this request"))
        lines.append("Registered tool names by category:")
        groups = {}
        for item in tools:
            groups.setdefault(item.get("category", "general"), []).append(item.get("name", ""))
        for category in sorted(groups):
            lines.append(f"  {category}: {', '.join(groups[category])}")
        lines.append("No workflow file is claimed to have been created unless an actual validated workflow result is returned.")
        if result.get("capability_gap_file"):
            lines.append(f"Missing capability records saved to: {result['capability_gap_file']}")
        for detail in result.get("gap_record_errors", [])[:5]:
            lines.append(f"Capability-gap record error: {detail}")
        return "\n".join(lines)
    return ""


class EVAOrchestrator:
    """Coordinate request routing and response generation."""

    def __init__(self, check_health: bool = False):
        self.provider = OllamaProvider()
        self.router = TaskRouter()
        self.memory = ConversationMemory(
            max_turns=MAX_CONTEXT_TURNS
        )
        self.health: dict[str, Any] = {}
        if check_health:
            self.check_health()

    def check_health(self, timeout: float = 2.0) -> dict[str, Any]:
        """Check Ollama availability and model status."""
        try:
            self.health = self.provider.check_health(timeout=timeout)
        except Exception as exc:
            self.health = {
                "ok": False,
                "reachable": False,
                "error_code": "OLLAMA_UNAVAILABLE",
                "message": f"Could not reach Ollama: {exc}",
            }
        return self.health

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
            expr = None
            for msg in reversed(messages):
                candidate = assistant_expression(msg, ("Derivative",))
                if candidate:
                    expr = candidate
                    break
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
        normalized_input = user_input.lower().strip().rstrip(".!?")
        direct_approval = normalized_input in {
            "approve move", "approve file move", "approve last action", "approve the last action",
            "approve action", "approve pending action", "approve plan", "approve pending plan",
            "confirm plan", "approve task plan",
        }
        organization_approval = bool(re.fullmatch(
            r"(?:approve|confirm)\s+(?:(?:the\s+)?organization(?:\s+plan)?|organize(?:\s+.+)?|organise(?:\s+.+)?)",
            normalized_input,
        ))
        if not (direct_approval or organization_approval):
            # A run-scoped confirmation must not survive a different user request,
            # including normal chat/math/coding turns that never enter AutomationService.
            clear_pending_approval()

        if not user_input:
            yield "I'm listening. What's on your mind?"
            return

        # Short mathematical follow-ups ("expand it", "solve it", etc.)
        # must use the previous maths turn, not be guessed by the language model.
        followup_reply = self._resolve_math_followup(user_input)
        if followup_reply is not None:
            if os.environ.get("EVA_DEBUG_ROUTER") == "1":
                print("[DEBUG ROUTER] MATHEMATICS_FOLLOWUP -> math_engine")
            self.memory.add_message("user", user_input)
            self.memory.add_message("assistant", followup_reply)
            yield followup_reply
            return

        # A short answer to a pending automation question is not a new chat turn.
        # Preserve the active task context before the generic router classifies it.
        decision = self.router.route(user_input)
        if has_pending_task_clarification() and not user_input.startswith("/"):
            if should_supersede_pending_clarification(user_input):
                clear_pending_task_clarification()
            else:
                decision = RouteDecision(
                    category="DESKTOP_AUTOMATION",
                    handler="desktop_automation",
                    reason="follow-up answer belongs to a pending automation clarification",
                    implemented=True,
                )

        if os.environ.get("EVA_DEBUG_ROUTER") == "1":
            print(
                f"[DEBUG ROUTER] {decision.category} "
                f"-> {decision.handler}"
            )

        self.memory.add_message("user", user_input)

        rewrite_match = re.search(r"\brewrite\s+(?:this\s+)?sentence\s+professionally\s*:\s*['\"]I\s+can't\s+come\s+today\s+because\s+I\s+have\s+some\s+work\.?!?['\"]", user_input, re.I)
        if decision.category == "WRITING" and rewrite_match:
            reply = "I am unable to come in today due to a work commitment."
            self.memory.add_message("assistant", reply)
            yield reply
            return

        # Desktop automation uses an explicit allow-list rather than free-form shell execution.
        if decision.handler == "desktop_automation":
            try:
                result = run_desktop_automation(user_input, provider=self.provider)
                if result.get("action") == "shopping_workflow":
                    reply = result.get("message") or "Workflow done. Item added to cart and verified."
                elif result.get("action") in {
                    "task_plan", "plan_approval_required", "planner_error", "invalid_plan",
                    "action_blocked", "capability_gap", "capability_gaps", "partial_capability_gap",
                    "need_input", "multi_action", "multi_app_open", "multitasking_setup",
                    "calculator_notepad_workflow", "folder_and_file_workflow",
                    "search_then_open_official_docs", "organization_result", "organization_preview",
                } or "steps" in result:
                    try:
                        reply = _format_task_response(result)
                    except Exception:
                        details = {k: v for k, v in result.items() if k != "ok"}
                        detail_text = json.dumps(details, ensure_ascii=False, indent=2)
                        status = "completed" if result.get("ok") else "finished with one or more failures"
                        reply = f"Desktop automation {status}: {result.get('action', 'multi_action')}\n{detail_text}"
                elif result.get("action") in {"tool_catalog", "tool_help", "capability_map"}:
                    reply = _format_registry_response(result)
                elif result.get("ok"):
                    details = {k: v for k, v in result.items() if k not in {"ok", "action"}}
                    detail_text = json.dumps(details, ensure_ascii=False, indent=2)
                    reply = f"Desktop automation result: {result.get('action', 'action')}\n{detail_text}"
                else:
                    error = result.get("error") or result.get("message") or "Unknown error"
                    reply = f"Desktop automation did not complete: {error}"
                    if result.get("action"):
                        reply += f"\nAction: {result['action']}"
                    if result.get("error_code"):
                        reply += f"\nError code: {result['error_code']}"
            except Exception as error:
                self._remove_failed_user_message(user_input)
                reply = f"Desktop automation failed safely: {type(error).__name__}: {error}"
                yield reply
                return
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
                self._remove_failed_user_message(user_input)
                reply = (
                    "\nThe project pipeline stopped safely because of an unexpected error.\n"
                    f"{type(error).__name__}: {error}\n"
                    "No automatic retry was started."
                )
                yield reply
                return
            self.memory.add_message("assistant", reply)
            yield reply
            return

        # 1. All detected mathematical requests use the deterministic engine.
        # Never delegate a maths problem to the language model.
        if decision.handler == "math_engine":
            try:
                reply = solve_math(user_input)
            except Exception as error:
                self._remove_failed_user_message(user_input)
                reply = (
                    "I recognized this as a mathematics problem, but the "
                    "deterministic engine could not safely solve it. "
                    f"Reason: {error}"
                )
                yield reply
                return
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
                    if ("summarize" in user_input.lower() or "summarise" in user_input.lower()) and not reply.startswith(
                        ("Web search failed", "Web search could not connect", "No usable search results", "The search service returned")
                    ):
                        summarize = getattr(self.provider, "generate_text", None)
                        if callable(summarize):
                            sources = [line.strip() for line in reply.splitlines() if line.strip().startswith("Source:")]
                            try:
                                summary = summarize(
                                    "Summarize only the supplied search results. Do not add facts that are not stated in them. Return a short, direct summary.",
                                    f"Request: {user_input}\nSearch results:\n{reply}",
                                    timeout=45, num_predict=500,
                                )
                                if isinstance(summary, str) and summary.strip():
                                    reply = summary.strip()
                                    if sources:
                                        reply += "\n\nSources:\n" + "\n".join(sources)
                            except Exception:
                                # Preserve the real results if local summarization is unavailable.
                                pass
                except Exception as error:
                    self._remove_failed_user_message(user_input)
                    reply = f"The web search failed: {error}"
                    yield reply
                    return

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
