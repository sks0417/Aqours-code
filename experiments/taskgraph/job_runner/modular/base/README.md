# jobrunner

A small background job runner. Jobs are submitted through a framework-free
REST API (`jobrunner.api.handle`), stored in SQLite (`jobrunner.store`),
executed one at a time by `jobrunner.runner.Runner.run_once`, and listed on an
HTML dashboard (`jobrunner.dashboard.render_jobs`).

`run_once` is assembled from small modules:

- `jobrunner.scheduler.pick_next` chooses the next job;
- `jobrunner.retry.decide` turns a handler failure into an outcome (today:
  always `FAILED`);
- `jobrunner.store.JobStore.finish` stores every outcome;
- `jobrunner.recovery.recover` runs once when a `Runner` is created (today:
  does nothing).

The `jobs` table already has `attempts`, `max_attempts`, `next_run_at`,
`last_error`, and `cancel_requested` columns (`JobStore.progress` /
`update_progress`), but nothing uses them yet. All times come from an
injected `clock` function.

See `SPEC.md` for the required behaviour. Run the tests with:

```bash
python -m pytest -q tests
```
