# Diagnose and repair inventory batch imports

The public integration suite for the inventory import pipeline now produces a very large failure report. Find the useful root cause in that output, narrow the failure with focused commands, and repair the implementation under `src/inventory_import_pipeline`.

Read the README and import contract before editing. Preserve the public API, exception attributes, atomic batch behavior, idempotent retries, and forward compatibility. Only edit Python files under `src/inventory_import_pipeline`; do not modify tests, documentation, or project configuration.

Run the complete public test suite after focused verification. Avoid repeatedly running an unchanged broad failing command when a narrower test or selector can answer the next question.
