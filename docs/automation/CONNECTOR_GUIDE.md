# Adding an EVA automation connector

A connector wraps one application, operating-system capability, or supported service. Keep it small, deterministic, and independent of task interpretation.

## Recommended steps

1. Identify the supported API/SDK or platform interface and the minimum permission scope.
2. Put code in a focused module under `eva/automation/tools/` (or a provider-specific subpackage).
3. Register each operation with `ToolSpec`: stable dotted name, clear description, strict input schema, permissions, category, timeout, idempotency, and risk.
4. Implement handlers using `(context, arguments)`. Check `context.check_cancelled()` in bounded work loops and use actual I/O/process/browser timeouts.
5. Return `ToolResult` with a stable error code and verified postcondition when feasible.
6. Use the central `PermissionPolicy`; never bypass it for a registered action. Mark high-impact operations high/critical or set `requires_approval=True`.
7. Redact secrets from errors and logs. Never include raw passwords, access tokens, clipboard contents, typed text, private keys, or API request bodies in the audit trail.
8. Add unit tests with mocks, integration tests against disposable/local test resources, security tests, and documentation.

## Handler shape

```python
from eva.automation.engine import RiskLevel, ToolResult, ToolSpec


def register_example(registry, handler):
    registry.register(
        ToolSpec(
            name="example.inspect",
            description="Inspect a bounded resource without modifying it.",
            input_schema={
                "type": "object",
                "properties": {"resource_id": {"type": "string", "minLength": 1, "maxLength": 200}},
                "required": ["resource_id"],
                "additionalProperties": False,
            },
            permissions=("system.inspect",),
            category="example",
            timeout_seconds=10,
            idempotent=True,
            risk=RiskLevel.LOW,
        ),
        handler,
    )
```

The sample is a shape, not a working connector; the actual handler must validate provider responses and return a `ToolResult`.

## Hard boundaries

- Do not expose general-purpose shell execution to user text.
- Do not silently delete, overwrite, publish, send, pay, or change account/credential/security settings.
- Treat pages and downloaded files as untrusted data, never as instructions to run.
- Avoid automatic retries of non-idempotent operations.
- If an operation cannot be undone, say so in its description and report partial completion honestly.
- Keep provider credentials in OS-backed secure storage, never workflow files or source code.
