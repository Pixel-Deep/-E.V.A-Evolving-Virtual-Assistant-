# EVA Tool-Registry and Task-Orchestration Architecture

## Scope

**Current hardening note (2026-10-22):** content output is now request-constrained and validated before dependent actions; an absent output body on a named-file request means create an empty file, not a model-generated substitute. See `CONTENT_INTENT_CONTRACTS_AND_RUNTIME_AUDIT.md`.

This design adds local-LLM task decomposition around EVA's existing deterministic automation system. It preserves the established math engine, coding engine, conversation memory, provider/model selection, and `desktop.py` compatibility dispatcher. It does not replace those components or grant the LLM direct operating-system access.

The governing rule is: **the model proposes a plan; EVA's registry, policy, workflow validator, and tools decide what can actually run and whether it succeeded.**

## Runtime flow

1. **Deterministic-first routing.** `TaskRouter` and `AutomationService.handle_text()` handle exact commands and supported single-tool requests first. Math, coding, registry help, file search, Git inspection, organization previews, and other known deterministic intents retain their established routes.
2. **Planner fallback for compound tasks.** When a request needs multiple actions or composition plus desktop/file actions and no direct handler can satisfy it, `AutomationService.plan_and_execute()` calls `TaskPlanner` through EVA's configured local provider. Request-specific deterministic operations (such as creating an explicitly named empty text file) can run without a provider. Tasks that genuinely require generated language fail or ask for missing inputs rather than inventing content when no provider is available.
3. **Structured plan only.** The planner asks for one JSON object containing a goal, at most 12 steps, dependencies, arguments, verification intent, required user inputs, and missing capabilities. The prompt supplies the complete live tool-name inventory and focused schemas for likely relevant tools, normally keeping the user payload well below 12,000 characters. If the iterative agent references a registered tool whose schema was not included, EVA adds that schema and asks again before execution (bounded to eight expansions and twelve model calls). The legacy batch planner expands its hidden-tool schemas in its own bounded repair loop before any step can run. A malformed, oversized, or unsupported plan runs nothing.
4. **Live-registry validation.** Every step must resolve to an actually registered tool and satisfy its input schema. A workflow preview validates the dependency graph, references and ordering. The planner cannot register tools, execute source code, invent shell commands, or bypass existing permission checks. A tool already in the registry cannot be recorded as missing just because the model says so.
5. **Capability-gap persistence.** A genuinely unavailable tool/capability is recorded in `data/automation/capability_gaps.json`, using schema version 1, atomic file replacement, deduplication, occurrence counts, timestamps, and bounded examples. User-supplied examples redact obvious email addresses, URLs, home paths, and inline secrets. `/gaps` lists recorded gaps. A failed JSON write is reported as a failure to persist; EVA does not claim it saved a gap that it could not write.
6. **Whole-plan preflight.** Every planned tool is checked against the central permission policy before execution starts. A blocked step prevents the safe prefix from running. High/critical or approval-required steps return a plan preview with exact arguments and require `approve plan` as the next request. That approval applies once to that exact stored plan and expires after 120 seconds or any unrelated request.
7. **Dependency-aware workflow execution.** The plan is passed to `WorkflowEngine` and `ToolExecutor`. Steps run in dependency order with a 120-second total workflow budget. Real structured outputs can be passed directly using `${steps.step_id.data.field}`; the clipboard is not required for internal handoff. If a step fails, only its dependent steps are skipped; independent feasible branches continue. The overall task still reports failure/partial completion if any required step failed.
8. **Truthful result formatting.** The final answer is derived from returned tool results, not the model's predicted outcome. Tools expose `verified` where they check a postcondition. An accepted launch request does not prove that a window opened or a user is signed in. EVA reports unverified actions as unverified.

## Planning and execution constraints

- Maximum 12 plan steps and a 120-second workflow budget; individual tools retain their own timeouts and size limits.
- The local model handles planning and bounded content drafting as separate tool calls. It cannot directly invoke operating-system APIs, perform arbitrary shell execution, or generate and load new live tools.
- The model must not assume private account state. Browser tools use EVA's separate Playwright context rather than attaching to the user's everyday browser profile. A direct Gmail Spam-folder request opens `https://mail.google.com/mail/u/0/#spam` through the system browser; EVA reports that launch, not Gmail authentication or page rendering, as verified.
- The registry is the source of truth for each tool's name, purpose, `when_to_use`, input schema, dependencies, output contract, side effects, permissions, risk, timeout, and verification behavior. `/tools` and `/tool NAME` expose this contract. The planner sees the complete live name inventory and compact contracts/schemas for likely relevant tools; if it needs an omitted schema, EVA expands the catalogue and replans before execution.
- `content.generate_text` is a first-class local-LLM tool, distinct from the planner. It writes actual content and returns it as data; later steps consume that exact output. It does not send or publish. Content genre and topic are hard constraints extracted from the current request and checked before any dependent file/UI action. An essential subject or formal-letter reason is clarified, not invented. Optional identity fields may use placeholders; the current local date is added only when relevant to the requested content. Incorrect genre/topic output is rejected before it is typed or saved.
- Writing into an application follows content generation -> application launch -> direct text entry when supported. Clipboard use is optional, not the default. File workflows resolve the filename exactly as supplied first, then try sensible extension variants/basename within approved roots; they ask about extension only when ambiguity cannot be safely resolved. `files.read_document` extracts DOCX text and reads supported plain text; PDF extraction requires `pypdf`. A draft-only email is not sent. Reading a file and showing its contents in EVA's current CLI returns the actual text in the task response without requiring a separate terminal-display tool.
- External messages are not sent or published by the text-generation tool. Gmail read/send APIs are not registered in this revision. Sending an external message would require an approved provider integration and explicit approval of the final recipient and content.
- File operations remain under approved roots, use bounded path traversal, and reject disallowed/sensitive paths. Bulk organization still previews first; file moves remain approval-gated.
- The audit trail redacts content, prompt, body, recipient, query, clipboard, and other configured sensitive argument keys.

## Current registered tool families

- `files.*`: listing, search, filename resolution with extension fallback, bounded text/document reading (including DOCX), unique text-file creation, directory creation, copy/move, and read-only duplicate SHA-256 scanning.
- `content.generate_text`: bounded local-model drafting only; it cannot send or publish.
- `browser.*`: controlled Playwright navigation, text/headings, click/fill, screenshot, download, and upload attachment through the isolated EVA browser context.
- `data.*`: CSV/JSON conversion.
- `archive.*`: safe ZIP creation/extraction with integrity and path checks.
- `desktop.*`: selected existing desktop actions, system/app information, clipboard, app/browser/file-editor launch, and typing/keys (with existing approval rules).
- `windows.*` (Windows only): UI Automation controls and side-by-side window placement.
- `developer.*`: read-only Git status and project inventory/Python syntax inspection.
- `network.*`, `system.*`, `web.*`: guarded URL checks, local time, public HTML search, and public-page reading through a controlled browser. Public-IP lookup via an external lookup service is intentionally omitted.

The `/tools` and `/tool TOOL_NAME` interfaces derive their data from the live registry; platform-specific Windows tools are only present on supported hosts.

## Commands

- `/tools` — list the current registered automation tools and metadata.
- `/tool files.search` — show the registered schema and limits for one tool.
- `/workflows` — list installed workflows.
- `/run-workflow NAME {"input":"value"}` — preview/execute an installed workflow according to policy.
- `/gaps` — review persisted missing capabilities.
- `approve plan` — execute the exact unexpired high-impact plan previewed immediately before it.

## Runtime audit and acceptance tests

Run `python scripts/eva_runtime_tool_audit.py` to validate every registered tool schema/metadata record, preflight bundled workflows, and exercise safe filesystem/data/archive/time operations in a temporary directory. The audit optionally supports `--probe-content-generation`, `--probe-browser`, `--probe-network`, and `--probe-web-browse`. The last option tests the integrated ordinary-HTML search plus controlled Chromium page-reading route; no external search API or API key is involved. Review `docs/automation/CONTENT_INTENT_CONTRACTS_AND_RUNTIME_AUDIT.md` for the full scope and limitations. The audit does not click through every visible UI, send email, or validate credentials by design.

## Explicit gaps, not simulated features

- Gmail-specific message listing, reading, drafting into an authenticated Gmail account, and sending are not registered. Direct folder navigation is not an authenticated Gmail API.
- There is no restricted build/test/lint runner, automatic source patch-apply tool, dynamic workflow authoring/validation/saving tool, or pending-task connector.
- Project inspection is read-only and must not be described as having executed tests or changed source.
- Durable resumable checkpoints, full compensation/rollback, arbitrary loops/conditionals, and parallel step execution are not implemented. Independent steps run sequentially after a failure; there is no parallel executor.
- Real Windows app/clipboard/window interactions and live external network behavior require acceptance testing on the target Windows machine. Passing Linux tests does not claim those host integrations were exercised.

## Trust boundary

Treat plans as untrusted model output. The registry, JSON schema validation, workflow DAG validator, permission preflight, tool-specific safety constraints, run-scoped approval, and postcondition checks are the enforcement points. The planner is never a substitute for them.
