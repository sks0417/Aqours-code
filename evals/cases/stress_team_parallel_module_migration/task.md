# Complete the parallel billing exporter migration

The CSV and JSON exporter modules were independently migrated to a v2 internal invoice model, and both adapters now violate parts of the stable external contract. Repair both modules while preserving `BillingExportAPI.export(records, *, format, schema_version="v2")`.

This task is also a collaboration-path diagnostic. You must:

1. Create one Shared Task for the CSV migration and one for the JSON migration.
2. Create and bind a separate Worktree to each Shared Task.
3. Spawn two persistent Teammates and have different teammates claim the two tasks.
4. Have each teammate inspect, implement, test, commit its isolated change, send a result/handoff message to Lead, and complete its Shared Task.
5. Integrate both Worktrees into the lead workspace. Do not reimplement teammate-owned changes in the lead workspace.

The two module changes are intentionally independent and may run in parallel. Read `README.md` and `docs/export_contract.md`; run focused module tests and the full public suite. Only edit Python files under `src/billing_export`.
