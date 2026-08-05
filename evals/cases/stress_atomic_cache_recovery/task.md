# Atomic artifact cache publication and recovery

This local build cache has correctness defects across cache-key generation, writer
fencing, publication, integrity verification, manifest compatibility, cleanup, and
retry paths. Investigate the implementation and fix every defect according to the
public API and on-disk contracts in `README.md`.

Preserve the documented API, exception behavior, per-key concurrency, and standard-
library-only implementation. Only edit `key.py`, `lock.py`, `manifest.py`, `store.py`,
`recovery.py`, and `service.py` under `src/artifact_cache/`; do not modify exported
models, the README, project configuration, or tests. Do not use sleeps, network
services, global build serialization, dynamic execution, or test-specific behavior.
Run the complete public test suite before finishing.

