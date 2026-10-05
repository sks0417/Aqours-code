# jobrunner

A small background job runner. Jobs are submitted through a framework-free
REST API (`jobrunner.api.handle`), stored in SQLite (`jobrunner.store`),
executed one at a time by `jobrunner.runner.Runner.run_once`, and shown on an
HTML dashboard (`jobrunner.dashboard.render_page`, which serves the jobs table
`render_jobs` at `/jobs`). Failed jobs are retried with exponential backoff, a
restart (a new `Runner` on the same database) recovers interrupted jobs, and
running jobs can be cancelled. All times come from an injected `clock`.

The runner is assembled from small modules, each with an extension point:

| Module | Role | Extension point |
| --- | --- | --- |
| `transitions` | every status change (`create`, `change`) | `subscribe(listener)`: `listener(store, event)` sees every change, inside the same SQLite transaction |
| `scheduler` | `pick_next(store, now)` | `register_filter(name, rule)` and `register_ordering(name, rank, rule)` |
| `store` | the `jobs` table | `register_schema(sql)` for a module's own tables; `execute`, `query`, `transaction` |
| `web` | route and page registries | `@web.route(method, pattern)` for REST routes, `@web.page(pattern)` for dashboard pages |
| `retry`, `recovery`, `cancellation` | failure policy, startup recovery, cancellation | — |

`api.py` registers the core job routes and `dashboard.py` the jobs page;
`cancellation.py` registers `POST /jobs/<id>/cancel` itself. A module's
registrations happen when it is imported, and `runner.py` imports every
module, so `jobrunner.api.handle` and `jobrunner.dashboard.render_page` see
them all.

See `SPEC.md` for the required behaviour. Run the tests with:

```bash
python -m pytest -q tests
```
