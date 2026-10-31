# Automation engine contracts

- `models.py`: tool schema validation, result objects, risk levels, and run context.
- `registry.py`: unique tool identifiers and handler/verifier/undo bindings.
- `policy.py`: fail-closed permissions and approval checks.
- `executor.py`: tool validation, execution boundary, verification, stable errors, and audit.
- `workflows.py`: safe YAML/JSON loading, DAG validation, literal reference interpolation, dry-run preview, and stop-on-failure execution.
- `audit.py`: minimal redacted JSONL execution audit.

All workflows are deterministic. They can only call registered tools. A high/critical action runs only when its exact tool name exists in the run context's approval set. Tools that execute subprocesses or network operations must enforce their own operation-specific timeouts.
