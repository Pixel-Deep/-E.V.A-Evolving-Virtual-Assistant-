# EVA automation permissions

Permission defaults are configured in `config/permissions.json`. The policy is fail-closed: a registered tool that asks for a permission not in `allowed_permissions`, or one explicitly listed in `denied_permissions`, will not run.

## Profiles and approval

`config/automation.json` currently selects the `trusted` profile and limits filesystem tools to paths under the current user's home directory. This is broad for convenience; a stricter installation should change `allowed_roots` to specific project and working folders.

High- and critical-risk tools, as well as tools marked `requires_approval`, must receive the exact registered tool name in the current run's `approved_actions`. The CLI previews registered tool calls and workflows before asking for approval. Conversational file moves use a one-time approval for the exact source/destination request. Free-form typing and generic key sequences through the legacy dispatcher use a short-lived one-time `approve action` step.

## Permissions denied by default

The starter policy denies administrative operations, credential reading, payments, security disabling, filesystem deletion, account modification, content publishing, and message sending. There are no general-purpose shell/PowerShell tools in the registered tool catalogue.

## Audit

The JSONL log is written to `~/.eva/logs/automation.jsonl` by default and can be redirected with `EVA_AUTOMATION_AUDIT_PATH`. Rotation keeps one previous 5 MiB segment. Known secret-like fields and URL query/fragment values are redacted. Audit logs are not conversation memory, but they still contain operational metadata; store them in a protected location.
