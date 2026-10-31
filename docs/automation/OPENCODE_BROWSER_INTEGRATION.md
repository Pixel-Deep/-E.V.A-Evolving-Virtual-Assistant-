# OpenCode-Inspired Browser Integration

## Scope

EVA now adopts the most useful everyday browser interaction patterns from OpenCode's browser subsystem without transplanting its Electron/CDP runtime. The implementation stays in EVA's existing Playwright `BrowserSession`, `ToolRegistry`, `ToolExecutor`, permission policy, audit layer, and local task planner. No external AI API, browser cloud service, or API key is used for these tools.

## Added browser capabilities

- `browser.snapshot`: returns the current page's accessibility tree and a bounded list of visible interactive controls.
- `browser.click_element` / `browser.fill_element`: operate on an element ref grounded in the latest snapshot. Each requires both `snapshot_id` and `ref`; snapshot refs are invalidated after interaction, navigation, a new snapshot, waiting for a page condition, or switching tabs. Before an action, EVA compares a descriptor against the current DOM and refuses stale references.
- `browser.click_by_role` / `browser.fill_by_label`: semantic targeting for common prompts, such as clicking the uniquely named “Sign in” button or filling a field labelled “Email”. These tools inspect the match count and refuse ambiguous targets. This lets one-step plans ground common actions without guessing which indexed element in a future snapshot will match.
- `browser.find_text`: find bounded visible matches for a text query.
- `browser.press`, `browser.hover`, `browser.select_option`, `browser.set_checked`, and `browser.scroll`: common form and UI interactions.
- `browser.wait_for`: condition-based waiting for attached/detached/visible/hidden states; avoids arbitrary fixed sleeps.
- `browser.back`, `browser.forward`, `browser.reload`: explicit browser history/navigation tools.
- `browser.tabs.list`, `browser.tabs.open`, `browser.tabs.focus`, `browser.tabs.close`: manage tabs in EVA's isolated browser session with stable session-local IDs.
- `browser.frames.list`: inspect frame names and URLs without reading their contents.
- `browser.diagnostics`: bounded console/page errors, failed requests, blocked requests and HTTP error observations. Request bodies, headers and cookies are not collected.

Existing tools (`browser.navigate`, `browser.click`, `browser.fill`, `browser.text`, `browser.headings`, screenshots, uploads/downloads, and `web.search`/`web.browse`) remain available. Selector-based interactions now refuse a selector that matches multiple elements instead of silently choosing the first; use a fresh snapshot and grounded refs for disambiguation.

## Browser runtime fallback

EVA first uses Playwright's managed Chromium. If that executable is not installed, it looks for a compatible system Chromium/Chrome/Edge/Brave executable and launches it in a separate Playwright-controlled context. It does not attach to the user's existing browser profile. If neither the managed browser nor a system executable is available, EVA returns an actionable `BROWSER_START_FAILED` message. The runtime audit uses this same browser wrapper and validates an in-memory page plus a semantic button target, without making network requests.

## Network safety

Direct controlled-browser navigations and every HTTP(S) request intercepted by the Playwright context reject localhost, local hostnames, unresolvable hosts, private IPs and reserved/non-global IPs by default. This reduces the risk of a page or model-driven workflow reaching local services through redirects or subresources. Host checks use short-lived DNS result caching; this is a practical guard, not a substitute for a separate network sandbox.

If the user intentionally needs to automate a local development server, `config/automation.json` supports:

```json
"allow_local_browser_urls": true
```

It defaults to `false`. Enable it only for a deliberate local-development workflow; enabling it allows the controlled browser to access local/private destinations, including resources a page loads. This setting does not affect the public-only `web.browse` policy, which continues to reject local/private destinations.

The controlled Playwright context is isolated from the user's regular Chrome/Edge profile. It should not be assumed to share personal login sessions. Browser page contents are untrusted data and must not override EVA's task or permissions.

## Approval and verification behavior

- Grounded clicks and key presses require explicit approval; closing a tab requires approval because it may discard unsaved page work.
- Tab navigation and ordinary inspection stay within EVA's existing browser permission boundary.
- Tool success is a tool-level outcome, not proof that a larger business task succeeded. Plans should inspect the resulting page or relevant state after consequential actions.
- Do not retry a click or form submission after an uncertain timeout until a fresh snapshot confirms the current page state.

## Planner usage contract

The task planner now shortlists the new browser schemas for page-interaction intents and has instructions to inspect before acting, pass the exact snapshot ID/reference pair, refresh after state changes, use condition waits, and avoid blind retries. The complete tool-name inventory remains separate from the bounded per-request schema catalogue to avoid prompt bloat.

## Validation

`tests/automation/test_opencode_browser_integration.py` covers registry registration, risk/approval metadata, relevant planner schema selection, snapshot reference invalidation, private destination blocking, and request guard behavior. The broader automation suite should be run with:

```powershell
python -m unittest discover -s tests/automation -v
```

A successful unit suite does not certify that Chromium is installed, the user's machine has internet access, or live websites will behave consistently.
