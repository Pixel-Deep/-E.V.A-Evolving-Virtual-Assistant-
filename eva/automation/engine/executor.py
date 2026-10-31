"""Single-tool execution, validation, verification, and auditing."""
from __future__ import annotations

import concurrent.futures
from time import monotonic
from typing import Any

from .audit import AuditLogger
from .models import AutomationError, ExecutionContext, RiskLevel, ToolResult, validate_arguments
from .policy import PermissionPolicy
from .registry import ToolRegistry


class ToolExecutor:
    def __init__(self, registry: ToolRegistry, policy: PermissionPolicy | None = None, audit: AuditLogger | None = None):
        self.registry = registry
        self.policy = policy or PermissionPolicy()
        self.audit = audit or AuditLogger()

    def execute(self, name: str, arguments: dict[str, Any] | None = None, *, context: ExecutionContext | None = None) -> ToolResult:
        context = context or ExecutionContext()
        arguments = {} if arguments is None else arguments
        started = monotonic()
        binding = self.registry.get(name)
        if binding is None:
            result = ToolResult(False, name, error_code="TOOL_NOT_FOUND", message=f"Unknown automation tool: {name}")
            self._audit(context, name, arguments, result)
            return result

        spec = binding.spec
        try:
            context.check_cancelled()
            validate_arguments(spec.input_schema, arguments)
            self.policy.authorize(spec, context, check_approval=not context.dry_run)
            if context.dry_run:
                result = ToolResult(
                    True, spec.name,
                    data={"dry_run": True, "arguments": arguments, "permissions": list(spec.permissions)},
                    message="Validated preview only; no action was executed.", verified=True,
                )
            else:
                read_only_permissions = frozenset({
                    "filesystem.read", "system.inspect", "browser.read",
                    "clipboard.read", "developer.read", "document.read"
                })
                side_effects_possible = not (spec.idempotent and set(spec.permissions).issubset(read_only_permissions))

                # Fail-closed for high/critical risk actions if the audit sink is non-writable
                if spec.risk in (RiskLevel.HIGH, RiskLevel.CRITICAL):
                    if hasattr(self.audit, "is_writable") and not self.audit.is_writable():
                        raise AutomationError("AUDIT_UNAVAILABLE", "Audit logging destination is unwritable; high-risk actions cannot proceed unaudited.")

                tool_timeout = float(spec.timeout_seconds)
                now = monotonic()
                if context.deadline is not None and context.deadline > (now - 1000.0):
                    remaining = context.deadline - now
                    if remaining <= 0:
                        context.cancel()
                        result = ToolResult(
                            False, spec.name,
                            error_code="TIMEOUT",
                            message=f"Execution deadline already exceeded before starting tool '{spec.name}'.",
                            data={
                                "timeout": True,
                                "deadline_exceeded": True,
                                "side_effects_uncertain": side_effects_possible,
                                "side_effects_confirmed_absent": not side_effects_possible,
                            },
                            verified=False,
                        )
                        self._audit(context, spec.name, arguments, result)
                        return result
                    timeout_limit = max(0.01, min(tool_timeout, remaining))
                else:
                    timeout_limit = max(0.2, tool_timeout)

                tool_context = context.child_context()
                pool = concurrent.futures.ThreadPoolExecutor(max_workers=1)
                try:
                    future = pool.submit(binding.handler, tool_context, arguments)
                    try:
                        raw = future.result(timeout=timeout_limit)
                    except concurrent.futures.TimeoutError:
                        tool_context.cancel()
                        deadline_exceeded = context.deadline is not None and context.deadline > (started - 1000.0) and monotonic() >= context.deadline
                        result = ToolResult(
                            False, spec.name,
                            error_code="TIMEOUT",
                            message=(
                                (f"Execution deadline exceeded after {timeout_limit:g}s budget. " if deadline_exceeded else f"Tool execution timed out after {spec.timeout_seconds:g}s execution budget. ")
                                + ("Active processes and operations were signalled cancellation. Inspect the target state before retrying; side effects may already have occurred or may still occur."
                                   if side_effects_possible else
                                   "Side effects are confirmed absent (read-only/idempotent tool); retry is safe.")
                            ),
                            data={
                                "timeout": True,
                                "deadline_exceeded": deadline_exceeded,
                                "side_effects_uncertain": side_effects_possible,
                                "side_effects_confirmed_absent": not side_effects_possible,
                                "cancellation_requested": True,
                                "cancellation_signalled": True,
                                "thread_forcibly_terminated": False,
                            },
                            verified=False,
                        )
                    except (ValueError, TypeError) as exc:
                        result = ToolResult(
                            False, spec.name,
                            error_code="TOOL_ERROR",
                            message=f"Internal tool error ({type(exc).__name__}): {exc}",
                        )
                    else:
                        result = self._normalise_result(spec.name, raw)
                        elapsed_seconds = monotonic() - started
                        deadline_exceeded = context.deadline is not None and context.deadline > (started - 1000.0) and monotonic() >= context.deadline
                        if elapsed_seconds > spec.timeout_seconds or deadline_exceeded:
                            result.ok = False
                            result.error_code = "TIMEOUT"
                            result.message = (
                                (f"Tool exceeded its execution deadline ({timeout_limit:g}s). " if deadline_exceeded else f"Tool exceeded its {spec.timeout_seconds:g}s budget. ")
                                + ("The handler returned late; inspect the target state before retrying."
                                   if side_effects_possible else
                                   "The handler returned late; side effects are confirmed absent.")
                            )
                            result.data = result.data or {}
                            result.data["timeout"] = True
                            result.data["deadline_exceeded"] = deadline_exceeded
                            result.data["side_effects_uncertain"] = side_effects_possible
                            result.data["side_effects_confirmed_absent"] = not side_effects_possible
                            result.verified = False
                        elif result.ok and binding.verifier is not None:
                            try:
                                result.verified = bool(binding.verifier(context, arguments, raw))
                            except Exception as exc:
                                result.verified = False
                                result.data = result.data or {}
                                result.data["verifier_error"] = f"{type(exc).__name__}: {exc}"
                            if not result.verified:
                                result.ok = False
                                result.error_code = "VERIFICATION_FAILED"
                                result.message = (
                                    f"The action returned, but verification encountered an error: {result.data.get('verifier_error')}"
                                    if result.data and result.data.get("verifier_error")
                                    else "The action returned, but its postcondition could not be verified."
                                )
                        result.elapsed_ms = int((monotonic() - started) * 1000)
                finally:
                    pool.shutdown(wait=False, cancel_futures=True)
        except AutomationError as exc:
            result = ToolResult(False, spec.name, error_code=exc.code, message=exc.message)
        except (ValueError, TypeError) as exc:
            result = ToolResult(False, spec.name, error_code="INVALID_ARGUMENT", message=str(exc))
        except Exception as exc:  # Tool boundaries must not crash EVA's main loop.
            result = ToolResult(False, spec.name, error_code="TOOL_ERROR", message=f"{type(exc).__name__}: {exc}")
        result.elapsed_ms = int((monotonic() - started) * 1000)
        self._audit(context, spec.name, arguments, result)
        return result

    @staticmethod
    def _normalise_result(name: str, raw: Any) -> ToolResult:
        if isinstance(raw, ToolResult):
            if not raw.tool:
                raw.tool = name
            return raw
        if isinstance(raw, dict):
            ok = bool(raw.get("ok", True))
            reserved = {"ok", "tool", "message", "error", "error_code", "verified", "elapsed_ms"}
            data = dict(raw.get("data", {})) if isinstance(raw.get("data"), dict) else {}
            data.update({k: v for k, v in raw.items() if k not in reserved and k != "data"})
            return ToolResult(
                ok=ok,
                tool=str(raw.get("tool") or name),
                data=data,
                message=str(raw.get("message") or raw.get("error") or ("Completed." if ok else "Tool failed.")),
                error_code=raw.get("error_code"),
                verified=bool(raw.get("verified", False)),
                elapsed_ms=int(raw.get("elapsed_ms", 0) or 0),
            )
        return ToolResult(True, name, data={"result": raw}, message="Completed.")

    def _audit(self, context: ExecutionContext, name: str, arguments: dict[str, Any], result: ToolResult) -> None:
        self.audit.write({
            "event": "tool_execution",
            "run_id": context.run_id,
            "tool": name,
            "arguments": arguments,
            "ok": result.ok,
            "error_code": result.error_code,
            "verified": result.verified,
            "elapsed_ms": result.elapsed_ms,
        })
