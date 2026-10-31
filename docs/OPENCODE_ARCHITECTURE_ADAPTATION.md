# OpenCode-Inspired Architecture Adaptation for EVA

## Objective

EVA adopts the architectural ideas that make an agent runtime reliable: a single source of truth for tools, structured tool contracts, constrained planning, explicit permissions, observation of real tool results, bounded recovery, and regression testing. This is an adaptation for EVA's Python/Windows assistant—not a claim that OpenCode itself is error-free, nor a line-for-line port of its TypeScript/Bun application.

Upstream references inspected on 2026-10-24:

- OpenCode source: <https://github.com/anomalyco/opencode>
- Agents and permissions: <https://opencode.ai/docs/agents/>
- Tool contracts and permissions: <https://opencode.ai/docs/tools/>
- Permissions: <https://opencode.ai/docs/permissions/>

The upstream repository identifies itself as MIT-licensed. EVA does not bundle the OpenCode application source or its runtime/dependency tree; the implementation below uses compatible architectural patterns while retaining EVA's Python modules and local-only provider constraint.

## Mapping to EVA

| Agent-system concern | EVA implementation | Enforcement point |
|---|---|---|
| Intent/routing | `eva/core/intent_understanding.py`, `eva/core/task_router.py`, `AutomationService.handle_text()` | Deterministic route precedence and regression tests |
| Tool discovery | `ToolRegistry`, `ToolSpec`, `AutomationService._tool_metadata()` | Live registry; unknown tools cannot execute |
| Structured planning | `TaskPlanner`, `_tool_rows()`, strict JSON output and bounded repair | Plan shape, tool names, schema, dependencies and reference validation |
| Iterative agent loop | `eva/automation/agent_runtime.py` for local providers advertising `supports_agent_loop` | One tool per turn, live-schema expansion, typed reference resolution, bounded steps/time, repeated-call prevention, per-call permissions, observed-result feedback |
| Agent/tool boundary | `ToolExecutor` | Argument schema, permission policy, error envelope, audit event |
| Multi-step execution | `WorkflowEngine` | Dependency DAG, template validation, time budget, stop/skip behavior |
| Permission model | `PermissionPolicy`, `config/permissions.json` | Fail-closed authorization and approval requirements |
| Tool feedback | `ToolResult` and workflow step reports | Actual handler result, error code and verification state |
| Capability discovery gaps | `CapabilityGapStore` | Only genuinely unregistered capabilities are recorded |
| Recovery | Planner's bounded JSON/schema repair and deterministic recovery paths | Replanning limits; invalid plans run no actions |
| Regression and release confidence | `pytest.ini`, tests in `tests/` and `eva/core/tests/`, runtime tool audit | One command now includes both test trees |

## Required invariants

1. The model proposes actions; it does not directly execute code, shell commands, or OS APIs.
2. Tool names, schemas, dependencies, allowed roots, and permission policy come from live deterministic code—not model assertions.
3. Validate the complete static plan before the first action. Resolve and validate dynamic references again at runtime.
4. A plan is not a result. A dispatch is not proof that a file, window, browser state, or external action reached its intended state.
5. Do not automatically retry a potentially completed side effect after timeout or uncertain verification. Inspect the state first.
6. Missing capability, missing essential input, denied permission, partial completion, and unverified success must remain distinct outcomes.
7. Keep local model inference on the configured local Ollama endpoint. Public web access uses ordinary HTML/controlled browser paths and does not require an external AI API.
8. Every discovered regression becomes a test. The test command must include both the automation suite and the core math/router suite.

## Changes in this release

- Added deterministic coordinate-geometry routing for tuple and named-point prompts, so a supported geometry problem is not diverted to the generic calculator or LLM route.
- Added quantitative word-problem routing for capacity/fill/percentage/set-overlap language so these tasks reach the symbolic math engine.
- Added variants to the routing regression suite.
- Added `pytest.ini` with both test trees explicitly listed. This prevents an apparently green automation-only run from hiding a failing core router test and avoids recursively collecting excluded backup/build directories.
- Added `scripts/validate_release.py` to run compilation, the complete pytest suite, and the deterministic runtime tool/workflow audit as one release gate.

## Limits that remain explicit

No architecture can guarantee zero bugs or unlimited task coverage. This runtime still depends on the quality and availability of the configured local model for unfamiliar language/plans, registered capabilities for actual actions, target-platform behavior for Windows UI control, and network/browser availability for public web tasks. The synchronous executor's timeout is checked after a handler returns; each blocking adapter must enforce its own transport/process timeout. The release audit therefore reports environment warnings separately from code-test failures.

Before a user-device release, run `python scripts/validate_release.py` on the target Windows machine, then execute the documented manual acceptance prompts. Linux mocks do not prove live Windows app focus, login state, or external network success.
