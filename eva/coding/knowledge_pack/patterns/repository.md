# Repository pattern

Separate domain/application logic from persistence behind a narrow interface. Define transaction, consistency, pagination, error and lifecycle semantics. Keep query-specific operations explicit; do not create a generic abstraction that hides database capabilities or performance costs. Test behaviour against a fake/in-memory implementation and integration-test the real persistence adapter.
