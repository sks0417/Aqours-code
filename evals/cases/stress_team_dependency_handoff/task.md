# Finish the document-index storage migration and dependent query adapter

The document index has two ordered pieces of work. Storage migration to record format v2 must be correct before the query adapter can be updated against the resulting representation.

Use the full collaboration workflow:

1. Create Shared Task A for v1/v2 storage reading and idempotent v2 migration.
2. Create Shared Task B for query compatibility, with `blockedBy` containing Task A.
3. Bind each task to its own Worktree and spawn two persistent Teammates.
4. Have the storage teammate claim A, implement and test it, commit, send Lead a concrete handoff describing the v2 record shape, and complete A.
5. Integrate A into the lead workspace before allowing the query teammate to claim B. A blocked claim attempt is not a substitute for respecting the dependency.
6. Send the handoff information to the query teammate. Have that different teammate claim B, implement/test/commit/complete it, then integrate B.

Read the README and `docs/storage_contract.md`. Preserve the stable API and serialized search result shape. Only edit Python files under `src/document_index`; run focused tests plus the full public suite.
