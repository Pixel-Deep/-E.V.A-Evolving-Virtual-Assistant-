# EVA public browsing without external APIs

## What the browser feature does

EVA now exposes two registered tools:

- `web.search`: fetches ordinary public HTML search-result pages and extracts titles, result URLs, and available snippets.
- `web.browse`: searches through those public HTML results or accepts one public HTTP/HTTPS URL, opens selected public pages in EVA's separate Playwright Chromium context, then extracts visible page text and headings.

Natural-language requests can be routed through the planner, for example:

- `Browse the web for official Windows keyboard shortcuts.`
- `Research the latest stable Python release and summarize the sources.`
- `Open https://www.python.org/downloads/ and summarize the page.`
- `Browse for free typing practice sites and save the findings to typing-sites.txt on Desktop.`

A research summary should retain source URLs. Search snippets are discovery aids; EVA should summarize page text it actually read and should not claim that a page was read when the browser did not return its text.

## No external API or key

The browsing implementation uses standard HTTPS requests to public HTML pages plus a controlled browser. It does not require a search-provider API key, SDK, subscription, or external model key. The Spotify Web API integration and external public-IP lookup were removed; Spotify actions use available native-app or public-website fallbacks. `pc.py doctor` checks connectivity using an ordinary public HTML page rather than an IP-lookup endpoint.

The existing local text-generation provider still uses the configured Ollama HTTP interface on `localhost` for content generation. This is a local model connection, not a cloud/third-party API. Public browsing does not depend on Ollama: if Ollama is stopped, direct search and page reading can still work, although model-based summarization/writing may fail without a local generator.

## Install and live-test

Install the Python dependencies, then (recommended) install Playwright's Chromium runtime in the same Python environment:

```powershell
python -m pip install -r requirements.txt
python -m playwright install chromium
```

If the managed Chromium download is not available, EVA tries an installed Chromium/Chrome/Edge/Brave executable in an isolated context. If no compatible browser is found, install Playwright's Chromium runtime and rerun the probe.

Run the integrated browsing probe on the target device:

```powershell
python scripts/eva_runtime_tool_audit.py --probe-web-browse --output work\EVA_tool_connection_audit_live.json
```

This probe runs the actual `web.browse` tool in query mode. It only passes if EVA gets a verified public page with non-empty readable text. It fails and returns a nonzero process exit code if the HTML search is blocked/changed, the network cannot connect, Chromium cannot launch, or no page body can be read. The build container used for this release has no working outbound network, so its live public-browse probe correctly reports a failure; that is not evidence of a failure on the Windows device.

You can run all supported opt-in probes together:

```powershell
python scripts/eva_runtime_tool_audit.py --probe-content-generation --probe-browser --probe-network --probe-web-browse --output work\EVA_tool_connection_audit_live.json
```

Read each probe status. `NOT_RUN` is not success, and a requested failed probe makes the audit exit unsuccessfully. The browser uses its own isolated context; it does not reuse normal Chrome/Edge cookies, bypass sign-in, solve CAPTCHAs, or bypass access restrictions.

## Security and reliability boundaries

- Only public HTTP/HTTPS URLs are accepted by the public browser tool; obvious local/private URL forms and embedded URL credentials are rejected.
- Page text is treated as untrusted source data, not as instructions to EVA.
- Results may be empty if public HTML changes or a provider blocks automated access. EVA should report the failure instead of fabricating links or a summary.
- Browsing makes ordinary outbound HTTPS requests, so internet access is required. No API key is required, but no offline implementation can fetch arbitrary current webpages without connectivity.
- Native email sending is not registered. EVA can draft an email, but must not claim to send it.
