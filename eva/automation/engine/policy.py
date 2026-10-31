"""Central policy checks for trusted, bounded automation."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from .models import AutomationError, ExecutionContext, RiskLevel, ToolSpec

DEFAULT_ALLOWED_PERMISSIONS = frozenset({
    "filesystem.read", "filesystem.write", "filesystem.copy", "filesystem.move",
    "document.read", "document.write", "system.inspect", "app.launch",
    "desktop.input", "clipboard.read", "clipboard.write", "browser.navigate",
    "browser.read", "browser.interact", "network.http", "developer.read", "developer.test",
})
DEFAULT_DENIED_PERMISSIONS = frozenset({
    "system.admin", "credentials.read", "payments", "security.disable",
    "filesystem.delete", "account.modify", "content.publish", "message.send",
})


class PermissionPolicy:
    """Apply a single fail-closed permission and confirmation policy."""

    def __init__(
        self,
        allowed_permissions: set[str] | frozenset[str] | None = None,
        denied_permissions: set[str] | frozenset[str] | None = None,
        approval_required_risks: set[str] | frozenset[str] = frozenset({"high", "critical"}),
    ):
        self.allowed_permissions = frozenset(allowed_permissions if allowed_permissions is not None else DEFAULT_ALLOWED_PERMISSIONS)
        self.denied_permissions = frozenset(denied_permissions if denied_permissions is not None else DEFAULT_DENIED_PERMISSIONS)
        self.approval_required_risks = frozenset(str(x).lower() for x in approval_required_risks)

    @classmethod
    def from_json(cls, path: str | Path) -> "PermissionPolicy":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("Permission configuration must be a JSON object.")
        allowed = payload.get("allowed_permissions", [])
        denied = payload.get("denied_permissions", [])
        risks = payload.get("approval_required_risks", ["high", "critical"])
        for label, values in (("allowed_permissions", allowed), ("denied_permissions", denied), ("approval_required_risks", risks)):
            if not isinstance(values, list) or not all(isinstance(item, str) for item in values):
                raise ValueError(f"{label} must be a list of strings.")
        return cls(set(allowed), set(denied), set(risks))

    def authorize(self, spec: ToolSpec, context: ExecutionContext, *, check_approval: bool = True) -> None:
        denied = sorted(set(spec.permissions) & self.denied_permissions)
        if denied:
            raise AutomationError("PERMISSION_DENIED", f"Tool '{spec.name}' requires blocked permission(s): {', '.join(denied)}.")
        missing = sorted(set(spec.permissions) - self.allowed_permissions)
        if missing:
            raise AutomationError("PERMISSION_DENIED", f"Tool '{spec.name}' requires permission(s) not enabled: {', '.join(missing)}.")
        risk_requires_approval = spec.risk.value in self.approval_required_risks
        if check_approval and (spec.requires_approval or risk_requires_approval) and spec.name not in context.approved_actions:
            raise AutomationError("APPROVAL_REQUIRED", f"Tool '{spec.name}' requires explicit approval for this run.")
