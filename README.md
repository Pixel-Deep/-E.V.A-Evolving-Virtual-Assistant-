# EVA — Evolving Virtual Assistant

EVA is an extensible Python desktop assistant with a deterministic task router, mathematics tools, a project-oriented coding engine, and a cross-platform automation subsystem.

This version expands the automation layer while preserving the existing math, coding, provider, and conversation components. **Model selection and memory redesign are outside the scope of this automation update.**

## What is included

- A typed automation tool registry and executor with input validation, explicit permissions, risk/approval checks, structured results, verification hooks, cancellation context, and redacted audit logs.
- Deterministic YAML/JSON workflow loading, dependency validation, typed input/output references, previews, and dependency-aware execution that skips failed dependents while preserving independent branches.
- Approved-root filesystem tools for listing/searching, PDF discovery, SHA-256 duplicate reports, creating directories/text files, copying, and approval-gated moving without silent overwrites.
- Safe ZIP creation with cache/temp exclusions and archive verification; staged, traversal-safe ZIP extraction; bounded CSV/JSON conversion.
- A shared compound-orchestration path (`eva/automation/orchestration/`) for high-confidence multi-step requests. One analyzer classifies the objective, one planner builds a dependency-aware registry-backed plan, the existing workflow engine executes it with real `${steps...}` hand-offs, and one verifier checks the original outcome (source page read, grounded summary, exact Notepad read-back, selected result identity and playback, dated copy existence and SHA-256 equality) before EVA reports success. Legacy deterministic contracts remain in place for requests outside those analyzed shapes.
- An OpenCode-inspired local agent loop (`eva/automation/agent_runtime.py`) for unfamiliar tasks: it must declare a complete atomic plan before any action, uses registered tool alternatives and dependency IDs, executes one tool per turn, and observes real results before continuing. EVA rejects premature finalization and repairs plans that omit explicit compound actions or concrete deliverables. The complete tool-name inventory is paired with compact request-specific schemas; omitted registered schemas can be expanded before execution.
- Direct data handoff between workflow steps, unique-file lookup for unambiguous file-to-read workflows, real tool-result verification, bounded recovery/failure behavior, and explicit missing-input/capability responses instead of fabricated results.
- Persistent, deduplicated capability-gap records in `data/automation/capability_gaps.json`, available for review with `/gaps`; gap examples redact common personal identifiers and secrets.
- Read-only Git/project inspection, guarded URL reachability, browser heading extraction, and source-attributed multi-URL extraction reports.
- Windows-only process/resource details and side-by-side window arrangement, with result verification and explicit partial failure reporting.
- An isolated Playwright Chromium session for navigation, locator-based click/fill/read, and screenshots.
- Registered desktop/status/system/network/time/web-search tools and a deterministic `math.solve` adapter for compound workflows, plus the existing legacy desktop dispatcher.
- API-free public-web browsing: EVA fetches ordinary HTML search result pages and reads public pages through its isolated Playwright Chromium session. It needs internet access and browser runtime dependencies but no search-provider API, account token, external LLM key, or paid browsing service.
- Request-derived content contracts and a bounded local-model repair attempt that prevent unrelated drafts from being pasted. New Notepad content opens in a fresh blank document and must pass accessible blank-document verification before typing; existing Notepad content is not deliberately overwritten.
- CLI commands for `/tools`, `/workflows`, and `/run-workflow NAME [JSON_INPUTS]`.
- The existing EVA math and coding modules and their tests.

Single-tool operations remain deterministic. For high-confidence common tasks, EVA uses deterministic plans. For unfamiliar compound tasks, the local Ollama model must first declare all required atomic steps and dependencies. EVA checks explicit action/deliverable coverage, validates each proposed call against the live registry/schema/policy and the accepted step ledger, executes it through the existing executor, and returns the actual result to the next turn. EVA rejects finalization while required planned steps remain incomplete; a corrected successful retry resolves the same step while preserving the failure trace. If the loop fails before any action runs, the bounded structured-plan fallback may be attempted. Batch plans are capped at 12 steps and a 120-second workflow budget; the iterative loop is capped at 8 tool actions and 120 seconds. High/critical actions require approval of the exact previewed step or plan. The executor and registered tools—not the model—own actual results and verification. This bounded design is not a guarantee that every task is supported or that all bugs can be prevented. External account integrations such as sending email are not included unless a real supported tool is registered. Public browsing does not require an API key, but does require an internet connection and a working Chromium runtime. The existing local text-generation provider still talks to the local Ollama service on localhost; this is not a remote provider API. If Ollama is unavailable, browsing/search still work independently, but model-based writing and summarization may not. EVA does not currently expose a restricted project build/test/lint runner, automatic source patch application, dynamic workflow authoring/saving, or a pending-task connector; those gaps are reported rather than simulated.

## Requirements

- Python 3.11 or newer recommended. Confirm compatibility with the Python installation already used by your EVA project before changing interpreters.
- Install Python dependencies from the repository root:

  ```powershell
  python -m pip install -r requirements.txt
  ```

- To enable the browser tools, install Playwright's managed Chromium runtime once:

  ```powershell
  python -m playwright install chromium
  ```

  If the managed download is unavailable, EVA can fall back to an installed Chromium, Chrome, Edge, or Brave executable. If neither the managed runtime nor a compatible system browser is available, the browser tools return an actionable startup error. Browser tools use a separate controlled context, not your normal Chrome/Edge profile; website sign-in state is not shared with your regular browser.

Some desktop actions need optional system capabilities or packages and will report an unsupported/dependency result when unavailable. No administrator privileges are required by default.

## Start EVA

From this repository's root:

```powershell
python main.py
```

Useful commands:

```text
/help
/tools
/gaps
/workflows
/run-workflow find_pdfs {"path":"Documents"}
/run-tool web.search {"query":"Windows UI Automation"}
/run-tool web.browse {"query":"official Windows keyboard shortcuts", "max_pages":2}
python scripts/eva_runtime_tool_audit.py --probe-web-browse
/automation
/status
/exit
```

### Public browsing without an external API

Browsing is implemented using the search engine's ordinary public HTML results page, standard HTTPS requests, and EVA's controlled Playwright browser. It does not call a search/news/weather API and requires no API key. The legacy macOS public-IP lookup and Spotify Web API credential integration were removed; Spotify can use its native app or public website fallback. The only model HTTP interface is the existing local Ollama service on localhost. Example natural-language requests:

```text
Browse the web for official Windows keyboard shortcuts.
Research online the latest stable Python release and summarize the sources.
Open https://www.python.org/downloads/ and summarize this page.
Browse the web for free typing practice sites and save the results to a file named typing-sites.txt.
```

Search is not a guarantee that a page is correct or up to date. EVA returns source URLs and bounded page text so the local model can summarize the pages actually read. If the HTML results page changes, blocks requests, the site blocks automated browsing, or Chromium is missing, EVA should report the failure rather than invent search results. This controlled browser uses its own isolated session and does not inherit sign-in from the user's normal Chrome/Edge profile.

Supported deterministic automation examples include:

```text
list files in Downloads
find files named *.pdf in Documents
create folder named research
inspect webpage https://example.com
copy file "Documents/source.txt" to "Documents/copy.txt"
move file "Documents/old.txt" to "Documents/new.txt"
convert CSV file "Documents/data.csv" to JSON file "Documents/data.json"
create zip archive from folder "Documents/report" to "Documents/report.zip"
extract zip file "Documents/report.zip" to "Documents/report-extracted"
```

`/run-tool TOOL_NAME JSON_INPUTS` is the direct interface for registered capabilities without a dedicated phrase mapping. It prints a validated preview first; high/critical tools require typing the exact `APPROVE tool.name` confirmation before execution.

File moves require a one-time confirmation: after EVA displays the exact source and destination, reply `approve move` as the next request. Free-form typing and generic key sequences also require `approve action` because they act on whichever application currently has focus. These approvals expire after 60 seconds and are cleared by any unrelated next request. The filesystem tools are restricted to configured roots and block credential/private-key paths.

## Configuration

- `config/automation.json`: trusted profile, filesystem roots, and default runtime settings.
- `config/permissions.json`: allowed/denied permissions and risk levels that require approval.
- `config/workflows/`: installed deterministic workflow definitions (`.yaml`, `.yml`, or `.json`). New LLM-created workflow files are not persisted by this revision.
- `data/automation/capability_gaps.json`: versioned engineering record of missing automation capabilities discovered during planning; inspect with `/gaps`.
- `EVA_AUTOMATION_AUDIT_PATH`: optional override for the JSONL automation audit log. By default it is written to `~/.eva/logs/automation.jsonl`.
- EVA connects to an Ollama server that is already running; it does not start another server process. The default native API base URL is `http://localhost:11434`, and the default model is `qwen2.5-coder:3b`.
- `EVA_OLLAMA_URL`: optional Ollama base URL override (for example, `http://127.0.0.1:11434`). `EVA_MODEL` selects the exact installed model name, and `EVA_OLLAMA_TIMEOUT` sets the bounded request timeout in seconds (1–300; default 120).

Use explicit roots narrower than the home directory when EVA should only access project or work folders. The trusted profile is not unrestricted shell access. Administrative, credentials, payments, security-disabling, destructive deletion, account modification, publishing, and messaging permissions are blocked by default.

## Testing

Run the full release gate from the repository root (compilation, both test trees, and the deterministic tool/workflow audit):

```powershell
python scripts/validate_release.py
```

The root `pytest.ini` explicitly includes both `tests/` and `eva/core/tests/`, so `python -m pytest -q` runs the automation suite and the core router/math regressions together.

The focused compound-orchestration contract suite uses temporary allowed roots and mocked browser/desktop handlers only:

```powershell
python -m pytest -q tests/automation/test_orchestration_contracts.py
```

Also useful:

```powershell
python -m unittest discover -s tests -v
python -m unittest discover -s eva/core/tests -v
python -m compileall -q eva main.py tests
```

Tests should use temporary files and controlled/mock web responses. Avoid testing destructive actions against real personal files or accounts.

## Browser automation

EVA's controlled Playwright browser supports public web research, page snapshots, grounded element refs, safe form interactions, condition waits, tab/history control and bounded diagnostics. It does not use an external AI API for browsing. Its browser context is isolated from the user's normal Chrome/Edge profile. Local/private URLs are blocked by default; see `docs/automation/OPENCODE_BROWSER_INTEGRATION.md` before enabling local development browsing.

## OpenCode-inspired architecture adaptation

EVA adapts OpenCode's agent/tool/permission patterns to its Python, Windows-automation, and local-only constraints. The implementation mapping, invariants, and current limitations are documented in [`docs/OPENCODE_ARCHITECTURE_ADAPTATION.md`](docs/OPENCODE_ARCHITECTURE_ADAPTATION.md). No architecture can guarantee zero errors; the release gate and regression suite are designed to catch known regressions and expose environment-dependent limits.

## Automation architecture

See [`docs/automation/ARCHITECTURE.md`](docs/automation/ARCHITECTURE.md) for the frozen deterministic-first planning model and trust boundaries, [`docs/automation/WORKFLOW_AUTHORING.md`](docs/automation/WORKFLOW_AUTHORING.md) for workflow definitions, [`docs/automation/CONNECTOR_GUIDE.md`](docs/automation/CONNECTOR_GUIDE.md) for adding tools/connectors, [`docs/automation/PERMISSIONS.md`](docs/automation/PERMISSIONS.md) for permission rules, and [`docs/automation/IMPLEMENTATION_REPORT.md`](docs/automation/IMPLEMENTATION_REPORT.md) for shipped capabilities, test results, limitations, and follow-up work. See [`CHANGELOG_AUTOMATION.md`](CHANGELOG_AUTOMATION.md) for this update's change summary and [`docs/automation/REGRESSION_SMOKE_TEST_REPORT.md`](docs/automation/REGRESSION_SMOKE_TEST_REPORT.md) for all 20 tested prompts and the observed outcomes.

The compound-orchestration rebuild is documented separately: [`docs/ORCHESTRATION_REBUILD_REPORT_2026-10-26.md`](docs/ORCHESTRATION_REBUILD_REPORT_2026-10-26.md) covers the architecture, scenario-by-scenario test guarantees, actual command results, and limitations; [`docs/WINDOWS_ACCEPTANCE_CHECKLIST_2026-10-26.md`](docs/WINDOWS_ACCEPTANCE_CHECKLIST_2026-10-26.md) is the manual acceptance script for a real Windows desktop; and [`docs/RUN_AND_TEST_INSTRUCTIONS_2026-10-26.md`](docs/RUN_AND_TEST_INSTRUCTIONS_2026-10-26.md) lists setup, run, and test commands.

## Tool and orchestration audit

The registry includes deterministic coding helpers (`coding.scaffold_project` and `coding.validate_file`) so the local agent can create reviewed starter projects and statically validate generated source without executing it. Generic foreground typing (`desktop.type`) requires approval; Notepad workflows use the scoped `desktop.type_notepad` tool.

Run the full local release checks with:

```powershell
python scripts/validate_release.py
```

For the read-only one-by-one registry/schema/routing-shortlist audit alone:

```powershell
python scripts/audit_tool_orchestration.py --check-only
```

The detailed audit is in `docs/TOOL_ORCHESTRATION_AUDIT_2026-10-24.md` and `.json`. The latest routing-completeness findings and limits are in `docs/FINAL_ROUTING_COMPLETENESS_REPAIR_2026-10-24.md`. The contract audit does not launch live Windows apps, call the external internet, or prove that the configured Ollama model will choose correctly for every possible request.

## Important limitations

- Tool time budgets are checked after a synchronous handler returns; Python cannot safely terminate every blocked thread. Network/browser/subprocess adapters must enforce real transport/process timeouts internally.
- Cancellation is cooperative. The workflow engine checks cancellation between steps and cooperative tools check within long operations.
- Browser automation does not bypass CAPTCHA, MFA, login controls, or service permissions.
- Workflow execution stops when a step fails. Persistent checkpoints, generic compensation/rollback, arbitrary branches/loops, and a universal app/API connector catalogue are not claimed as implemented.
- Audit logs redact known sensitive fields, but operators should still review which actions are appropriate to log and where logs are stored.


## Tool contracts and task planning

EVA's automation planner uses the live tool registry as its source of truth. Tool metadata describes purpose, when to use the tool, input schema, dependencies, output shape, side effects, permissions and risk. Compound requests are decomposed into dependency-aware steps and executed through the existing workflow engine. The local LLM is also a registered writing tool; its output is passed directly to later tools. File lookup tries the supplied filename exactly first, then reasonable extension variants, and DOCX text can be extracted with the registered document reader. Independent steps continue when an unrelated step fails; dependent steps are skipped and the final report describes partial completion honestly. See `docs/automation/ARCHITECTURE.md`.
