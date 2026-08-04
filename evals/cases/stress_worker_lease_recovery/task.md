# Worker lease fencing and restart recovery

The worker queue accepts stale lease results, mishandles failure retries and
submission idempotency, leaves cancelled capabilities active, and corrupts durable
state during restart recovery. Investigate the implementation and fix every defect
according to the state-machine, idempotency, lease, cancellation, history, and
recovery contracts in `README.md`.

Preserve the public API and documented exception behavior. Only edit Python files
under `src/worker_queue/`; do not modify the README, project configuration, or tests.
Use explicit test times—do not add sleeps, network services, or wall-clock behavior.
Run the complete public test suite before finishing.
