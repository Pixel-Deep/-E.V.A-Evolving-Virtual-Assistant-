# Secret handling

Never index or transmit `.env`, credentials, private keys, access tokens, browser profiles or secret stores by default. Redact secrets from logs and failure evidence. Do not print secret values to diagnostics. Keep knowledge-source provenance and license metadata. Treat downloaded code and documentation as untrusted input; do not execute code merely to ingest it.
