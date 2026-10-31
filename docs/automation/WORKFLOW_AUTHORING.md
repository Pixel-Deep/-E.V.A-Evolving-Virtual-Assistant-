# Authoring EVA automation workflows

Workflows live in `config/workflows/` and use YAML (`.yaml` / `.yml`) or JSON. A workflow can only invoke tools already registered in `AutomationService`; it cannot run Python, PowerShell, shell commands, or arbitrary expressions.

## Minimal example

```yaml
name: convert_csv_to_json
version: 1
description: Convert a bounded CSV file into JSON records.
inputs:
  source:
    type: string
    minLength: 1
    maxLength: 4096
    required: true
  destination:
    type: string
    minLength: 1
    maxLength: 4096
    required: true
steps:
  - id: convert
    tool: data.csv_to_json
    args:
      source: "${inputs.source}"
      destination: "${inputs.destination}"
```

## Current execution model

- Workflow names must be a simple identifier and the file must be directly inside the configured workflow directory.
- Workflows are limited to 100 steps and a maximum overall execution budget of 300 seconds.
- Every step has a unique `id`, a registered `tool`, optional `args`, and optional `depends_on` list.
- Dependencies are sorted deterministically; unknown tools, duplicate IDs, cycles, missing dependencies, invalid inputs, and bad references fail before execution.
- References use literal `${inputs.name}` or `${steps.step_id.data.field}` syntax. A structured result should occupy the entire argument value rather than be interpolated into a string.
- The engine stops further work after a tool fails. No arbitrary `if`, `while`, loop, shell, Python, or eval constructs are supported.
- High/critical tools need their exact tool name in the execution context's approved action set. The CLI previews a workflow and asks for explicit approval before running approval-gated steps.
- Tool handlers should verify postconditions and enforce their own network/process/browser timeouts. The engine cannot forcibly interrupt an arbitrary blocked synchronous Python handler.

## Validate a new workflow

1. Add a uniquely named file under `config/workflows/`.
2. Use only registered tool names shown by `/tools`.
3. Declare every external input and constrain it with `type`, length, and numeric bounds where appropriate.
4. Preview it with `/run-workflow NAME {"input": "value"}`. Preview does not run actions.
5. Add tests for a successful flow, invalid inputs, missing dependencies, permission denial, and at least one injected failure.
6. Run `python -m pytest -q` before sharing the workflow.
