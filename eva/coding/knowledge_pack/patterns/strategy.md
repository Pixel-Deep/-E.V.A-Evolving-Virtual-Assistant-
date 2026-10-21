# Strategy pattern

Encapsulate interchangeable behaviours behind a stable interface and inject the chosen strategy into the client. Use when selection varies independently from the client. Benefits: explicit contracts and testable alternatives. Costs: extra types/indirection. Test every strategy against the shared contract. Avoid introducing a strategy hierarchy for a single trivial branch unless extension is expected.
