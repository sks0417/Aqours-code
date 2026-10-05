# jobrunner

A small background job runner. Jobs are submitted through a framework-free
REST API (`jobrunner.api.handle`), stored in SQLite (`jobrunner.store`),
executed one at a time by `jobrunner.runner.Runner.run_once`, and shown on an
HTML dashboard (`jobrunner.dashboard.render_page`, which serves the jobs table
`render_jobs` at `/jobs`).

`Runner` does everything in one place: it keeps pending jobs in an in-memory
queue (`_enqueue` puts a job in, `_next_due` takes the next due one out),
`run_once` runs a job and saves each state change with `JobStore.save`, and
retries, cancellation, and restart recovery are handled inline. Failed jobs
are retried with exponential backoff, a restart (a new `Runner` on the same
database) recovers interrupted jobs, and running jobs can be cancelled. All
times come from an injected `clock` function.

See `SPEC.md` for the required behaviour. Run the tests with:

```bash
python -m pytest -q tests
```
