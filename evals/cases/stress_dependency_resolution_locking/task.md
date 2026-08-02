# Dependency resolution and lock integrity

Builds select incompatible releases, leak optional dependencies across
platforms, and reuse stale or forged lock results after registry updates.
Investigate the resolver and fix every defect according to the constraint,
backtracking, graph, lock, cache, and replacement contracts in `README.md`.

Preserve the public API and documented exception behavior. Do not modify the
README, project configuration, or tests. Run the complete public suite before
finishing.
