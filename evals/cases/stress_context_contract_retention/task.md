# Repair notification delivery without breaking its contract

The notification dispatcher recently regressed around channel fallbacks, failure classification, rate limits, and retry deduplication. Repair the implementation under `src/notification_dispatcher`.

Start by reading `README.md` and both documents in `docs/`; important requirements are deliberately distributed across them. Inspect the implementation and run the public tests. Preserve the documented public API, exception hierarchy, receipt shape, and forward-compatible treatment of unknown payload fields.

Only edit Python files under `src/notification_dispatcher`. Do not edit tests, project configuration, README, or contract/operations documentation. Run focused tests while diagnosing and the complete public suite before finishing.
