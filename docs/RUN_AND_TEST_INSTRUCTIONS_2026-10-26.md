# EVA Run and Test Instructions — 2026-10-26

These instructions apply to the compound-orchestration build in the supplied `EVA` folder. Run all commands from the repository root.

## 1. Requirements

- Windows 10/11 (the current acceptance target) or the existing macOS compatibility layer.
- Python 3.11 or newer. The validation environment used Python 3.13.0.
- Install Python dependencies:

```powershell
python -m pip install -r requirements.txt
```

- For browser-backed workflows, install the managed Chromium runtime once:

```powershell
python -m playwright install chromium
```

- EVA's local text generation uses the existing Ollama service on `localhost`. Start Ollama and make sure the configured model is installed. The validation environment used `qwen2.5-coder:3b`; model selection itself is unchanged by this work.
- Confirm filesystem roots in `config/automation.json` include the folders used by a task (for dated-file tests, the folder containing the report).
- Close or save unrelated Notepad windows before a Notepad acceptance test. EVA uses a fresh document and does not deliberately overwrite an existing note, but a real desktop test should still be isolated.

## 2. Start EVA

```powershell
python main.py
```

Useful commands inside EVA:

```text
/help
/tools
/workflows
/gaps
/status
/exit
```

Approval-gated actions require the exact confirmation shown by EVA. Do not pre-approve a plan that you have not reviewed.

## 3. Automated tests

### Focused orchestration contract suite

```powershell
python -m pytest -q tests/automation/test_orchestration_contracts.py
```

This is the primary evidence for the rebuilt path. It uses temporary allowed roots, the real registry/engine, and deterministic doubles for live browser, desktop, and model handlers. It does not open a visible app or visit a website.

Expected current result:

```text
17 passed, 5 subtests passed
```

### Full test suite and release gate

```powershell
python scripts/validate_release.py
```

This runs, in order:

1. `python -m compileall -q eva main.py tests scripts`
2. `python -m pytest -q`
3. `python scripts/eva_runtime_tool_audit.py`
4. `python scripts/audit_tool_orchestration.py --check-only`

Expected current result:

```text
572 passed, 1 skipped, 412 subtests passed
EVA Runtime Tool Audit: PASS_WITH_WARNINGS
Tools: 97 | contracts: PASS | top-level routes: PASS | routing shortlist: PASS
PASS: compilation, all configured tests, deterministic tool/workflow audit, and one-by-one registry/shortlist audit.
```

The runtime audit's warning is expected: native email sending is not registered. The audit intentionally does not run live browser, public-network, or content-generation probes by default.

### Individual commands

```powershell
python -m compileall -q eva
python -m pytest -q
python scripts/eva_runtime_tool_audit.py
python scripts/audit_tool_orchestration.py --check-only
```

### Optional local probes

These are opt-in and are **not** part of the current acceptance evidence:

```powershell
# Headless Chromium renders a local blank page; no visible desktop, no website.
python scripts/eva_runtime_tool_audit.py --probe-browser

# One bounded local-model request; requires Ollama and the configured model.
python scripts/eva_runtime_tool_audit.py --probe-content-generation

# One request to https://example.com; requires external network access.
python scripts/eva_runtime_tool_audit.py --probe-network

# Exercises the integrated public HTML search + browser reader; requires network.
python scripts/eva_runtime_tool_audit.py --probe-web-browse
```

Use a separate `--output` path if you do not want to overwrite `work/EVA_tool_connection_audit.json`:

```powershell
python scripts/eva_runtime_tool_audit.py --probe-browser --output work/browser_probe.json
```

## 4. What the test commands guarantee

| Command | Guarantee |
|---|---|
| Focused suite | The five mandatory Nehru phrasings, Kalam, first/ranked result identity and playback, Python downloads failure/recovery, dated copy/open/ambiguity/missing source, legacy leave application, simple file creation, capability gaps, cancellation, timeout, and 12/13-step limits behave as described in the report. Live external systems are mocked. |
| Full suite | Existing routing, planner, browser, filesystem, document, content-contract, math, memory, and core tests still pass alongside the new contracts. |
| Compilation | All Python sources under `eva`, the root `main.py`, tests, and scripts compile. |
| Runtime audit | Registered tools, bundled workflows, temporary-directory file/document round-trips, local time tool, and Ollama read-only reachability. It does not prove live website or visible-UI behavior. |
| Contract audit | One-by-one tool schema/contract checks, top-level route recognition, and routing-shortlist coverage. It is read-only and does not execute high-impact tools. |

The tests prove the orchestration contracts and verifier logic. They do not prove:

- that a particular live website still has the expected markup;
- that Google/YouTube anti-bot or sign-in rules permit the action;
- that the local model will produce a high-quality summary;
- that the current Windows Notepad build exposes document text through UI Automation;
- that an OS association will visibly open the copied file.

Those are the manual acceptance items.

## 5. Manual Windows acceptance

Follow `docs/WINDOWS_ACCEPTANCE_CHECKLIST_2026-10-26.md`. It contains:

- exact prompts;
- expected plan/action shape where useful;
- visible pass criteria;
- evidence to capture;
- failure interpretation and cleanup.

Do not mark a checklist item passed because the mocked test passed. Record the actual visible result and the returned fields (`ok`, `verified`, `verification`, `unverified_steps`, `launch_only_steps`).

## 6. Inspecting an orchestration result

For an analyzed request, `plan_and_execute` returns a `task_plan` response with:

```json
{
  "action": "task_plan",
  "plan": {"steps": []},
  "execution": {"steps": []},
  "verification": {
    "ok": true,
    "verified": true,
    "checks": [],
    "issues": [],
    "unverified_steps": [],
    "launch_only_steps": []
  },
  "verified": true,
  "unverified_steps": [],
  "launch_only_steps": [],
  "ok": true,
  "message": "..."
}
```

The exact fields depend on the workflow and whether the request is orchestration-owned. Interpret them as follows:

- `execution.steps[].status`: what happened to each plan step (`succeeded`, `failed`, `skipped`).
- `verification.checks[]`: independent outcome checks and their details.
- `unverified_steps`: required outcomes that succeeded at the tool level but lacked independent proof.
- `launch_only_steps`: launch requests that are intentionally not treated as visible-state proof.
- `verified`: true only when all required outcomes have strict proof and execution completed.
- `ok`: false when the requested objective failed, even if some tools returned success.

For legacy deterministic requests (for example the leave application), there may be no `verification` object. That means the strict orchestration verifier did not claim the request; it does not mean the action was verified.

## 7. Troubleshooting

### Disk space during compile/tests

A nearly full drive can make `compileall` or pytest fail while writing bytecode/temp files. Free space and rerun. The validation run reported roughly 1 GB free and completed successfully; no code change is needed for that condition.

### `ModuleNotFoundError` or missing optional tools

Reinstall requirements and check `/tools` for the actual registry in this installation. A capability gap is the correct response when a required registered tool is unavailable; do not edit the registry to make a test pass.

### Browser workflow fails at the first step

- Confirm Chromium is installed.
- Confirm outbound network/DNS access.
- Check whether the site is showing a CAPTCHA, sign-in wall, or anti-bot page. EVA is designed to report the failure rather than bypass it.
- Run the optional browser probe with a local page to separate a browser-runtime problem from a website problem.

### Summary is unverified or missing

- Confirm Ollama is running and the configured model exists.
- Check `verification.checks` for `source_page_read`, `summary_generated`, and `summary_typed_and_read_back`.
- If the source page returned only navigation/infobox material, the content contract may return `INSUFFICIENT_SOURCE_TEXT`; the task should fail rather than type page chrome.

### Notepad typing occurred but EVA says unverified

UI Automation may not expose Notepad's document text in this Windows build. The task is intentionally reported as unverified. Check the visible text, record the limitation, and do not report the automated result as verified.

### Dated file search finds nothing

- Confirm the file's local creation/modification date is actually the requested day.
- Confirm the file is under an approved root.
- Narrow the folder or use a filename term.
- If the folder has more entries than the scan limit, narrow it; EVA fails closed on an incomplete scan.

### Multiple matches

EVA returns `need_input` with candidate paths. Answer with a number or filename through the existing clarification flow. No copy should exist from the failed attempt.

## 8. Cleanup after manual acceptance

- Close Notepad and browser windows opened by EVA.
- Delete temporary acceptance reports and dated copies created for the test.
- Do not delete the original source report.
- Preserve `work/windows_acceptance` logs if you want an audit trail; they are not part of the source package.

## 9. Reporting a failure

Include:

1. the exact user prompt;
2. the returned `action`, `ok`, `verified`, `message`, and `verification`/`execution` fields;
3. the visible application/browser state;
4. whether the website required sign-in/CAPTCHA;
5. the Python version, EVA source revision/working tree state, and model name;
6. a screenshot or short screen recording for visible-state failures.

Do not describe a mocked test pass as a live acceptance pass.
