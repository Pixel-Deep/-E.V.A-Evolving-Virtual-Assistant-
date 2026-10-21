# Evidence-based repair loop

1. Preserve the failing input, environment, command, exit code and full diagnostic with secrets redacted.
2. Reproduce the failure with a minimal test where possible.
3. Classify the diagnostic as a hypothesis, not a root-cause proof.
4. Inspect nearby code, interfaces, dependencies and recent changes.
5. Propose one minimal reversible change and state why it addresses the evidence.
6. Run the original failing test, relevant regression tests, then the broader suite.
7. Compare unrelated tests and public interfaces for regressions.
8. Store both successful and failed attempts. Promote a reusable rule only after review and evidence. Bound retries and stop when evidence is insufficient.
