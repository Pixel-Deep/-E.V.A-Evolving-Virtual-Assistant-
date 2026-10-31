# EVA Automation

EVA's automation package has three compatible layers:

1. **Compound orchestration** under `eva/automation/orchestration/`: one analyzer, plan builder, and outcome verifier for high-confidence multi-step requests. It uses the same registry and workflow contract as every other plan.
2. **Typed automation engine** under `eva/automation/engine/`: tool contracts, registry, policy, execution, redacted audit, and deterministic DAG workflows.
3. **Existing desktop bridge** in `desktop.py`: established allow-listed Windows/macOS operations, retained as a compatibility dispatcher.

The deterministic request handler resolves known automation intent before the generic chat/coding fallbacks. Analyzed compound requests are planned as one workflow, validated before execution, executed with dependency-aware hand-offs, and checked against the requested outcome. The handler answers registry/help questions from the live registry and will ask for missing input or name an unavailable capability rather than fabricate a result.

See [`docs/ORCHESTRATION_REBUILD_REPORT_2026-10-26.md`](../../docs/ORCHESTRATION_REBUILD_REPORT_2026-10-26.md) for the design, test evidence, and limitations. The new date-aware filesystem tools are `files.find_by_date` and `files.copy_with_date`; both remain restricted to approved roots and fail closed on ambiguity.

## Registered tool families

- **Filesystem:** bounded listing/search, PDF search, create directory/text file, copy/move, SHA-256 duplicate scanning, CSV/JSON conversion.
- **Archives:** ZIP creation with integrity checking and cache/temp exclusions; safe ZIP extraction.
- **Browser/web:** controlled Playwright navigation, click/fill/text extraction, headings extraction, screenshots, approval-gated link downloads/file attachment, web search, URL reachability checks, and multi-URL source reports.
- **Desktop/system:** selected app launch, text-editor launch, clipboard and keyboard operations, basic system/network/time information, and process/resource listing where the host backend supports it.
- **Windows UI:** available on Windows when `pywinauto` is installed; enumerate/focus windows, inspect accessible controls, click eligible controls with approval, and arrange two uniquely identified windows side by side.
- **Developer inspection:** read-only Git branch/status/diff summaries and bounded Python source/syntax inventory.

Each registered tool exposes an input schema and tool metadata. `/tools` lists the current registry; `/tool TOOL_NAME` shows one tool's schema, risk, permissions, dependencies and example. `/workflows` lists installed workflows, and existing workflow preview/run commands remain available.

## Safety and current limitations

- Organization by file type is preview-first; nothing moves until the exact preview is explicitly approved. Destination conflicts are skipped instead of overwritten.
- Duplicate scans are read-only. They compare file sizes first and SHA-256 hashes for candidate files; no files are deleted.
- URL checks accept public HTTP/HTTPS targets only and re-check redirects to block private/local destinations. The outbound network must be available.
- Browser tools use an isolated Playwright Chromium session. User login state is not shared from the normal browser profile. Install Chromium with `python -m playwright install chromium`.
- Filesystem tools remain confined to configured approved roots and never overwrite existing files.
- EVA does **not** currently register a restricted project test/build/lint runner, a new-workflow authoring/saving tool, a pending-tasks connector, or an automatic project patch-apply tool. For these prompts it reports the gap; it does not claim to have run commands, changed source, or created a workflow.
- Windows process metrics and side-by-side tiling require integration testing on a real Windows desktop. Linux smoke tests cannot verify visible Windows UI state. The macOS backend does not currently provide detailed process metrics.
- The controlled browser/network tools depend on installed Playwright/Chromium and outbound DNS/HTTP access. External results are not fabricated when those dependencies are unavailable.

Read [the architecture and safety guide](../../docs/automation/ARCHITECTURE.md) before adding a tool. Do not introduce unrestricted shell execution or workflows that interpret arbitrary Python or shell text.
