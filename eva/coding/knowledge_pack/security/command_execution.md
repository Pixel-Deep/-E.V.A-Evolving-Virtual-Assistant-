# Safe command execution

Use argument arrays with shell disabled; avoid interpolating untrusted text into command strings. Set working directory, environment allowlist, timeout and output-size limits. Require approval for dependency installation, destructive operations, network access or writes outside the selected workspace. A subprocess timeout and command allowlist are not a full sandbox; use a disposable VM/container or OS-level restrictions for untrusted code.
