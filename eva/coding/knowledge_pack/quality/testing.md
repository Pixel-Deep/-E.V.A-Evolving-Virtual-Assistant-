# Testing strategy

Use a testing pyramid: fast unit tests for local logic, integration tests for boundaries, and a small number of end-to-end tests for critical workflows. For every bug, create a regression test that fails before the fix and passes after it. Include boundaries, invalid inputs, empty values, duplicate values, failure paths and resource cleanup. Tests can be wrong too; derive expected results independently and avoid testing an implementation by copying its logic into the assertion.
