# jobrunner

A small background job runner. Jobs are submitted through a framework-free
REST API (`jobrunner.api.handle`), stored in SQLite (`jobrunner.store`),
executed one at a time by `jobrunner.runner.Runner.run_once`, and listed on an
HTML dashboard (`jobrunner.dashboard.render_jobs`).

The runner keeps pending jobs in an in-memory queue: `Runner._enqueue` puts a
job in the queue and `Runner.run_once` takes the next one out and runs it.
All times come from an injected `clock` function.

See `SPEC.md` for the required behaviour. Run the tests with:

```bash
python -m pytest -q tests
```
