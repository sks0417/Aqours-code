# Job platform task

A larger version of the [job runner task](../job_runner/README.md). One request
([`request.md`](request.md)) turns the job runner into a small job platform —
priorities with fair scheduling across tenants, delayed and recurring jobs,
dependencies between jobs, per-kind rate limits, webhook notifications, and an
audit log with statistics, each exposed through the REST API and the
dashboard. It is applied to two repositories with the same public interface
and different internal structure, to compare a single agent, a hand-written
graph run sequentially, and the same graph run in parallel.

Everything here is data: neither Aqours nor `aqours_code.taskgraph` imports it.
The two tasks are independent; `job_runner/` is unchanged.

## Why a larger task

The job runner demo answered its own question: on the coupled variant, a
single agent solved it (33/33 hidden tests) in about 170 s with about 200K input
tokens, while the hand-written graph took about 400 s and 900K input tokens.
The task was too small: about 200 changed lines fit comfortably in one agent's
context, so the graph only added per-node overhead (context reading, checks,
merges) and could not win on time.

This task changes about 1,250 lines of code per variant (plus tests), in six
features that are each about the size of the whole job runner task. A single
agent has to hold far more at once, and the graph has more independent work to
spread over workers, so the trade-off between the schemes becomes measurable.

```text
request.md          the request (both variants)
make_repo.py        builds a deterministic git repository from <variant>/base/
hidden_tests/       hidden tests (both variants; never shown to workers)
  regression/       the 33 job runner hidden tests, renamed test_jr_*.py
coupled/  modular/
  base/             repository content: jobrunner/, tests/, SPEC.md, README.md, pyproject.toml
  reference/        reference solution: only new or changed files, copied over base/
  graphs/
    single.json       one-node graph (single-agent baseline)
    handwritten.json  hand-written task graph
```

`SPEC.md` (identical in both variants) states the required behaviour of every
feature, the REST routes and their JSON, and the dashboard pages down to table
classes, columns, and number formats; sections marked "(new)" are what the
request adds. Hidden tests check only what it states, through the public
interface it lists. Times come only from an injected `clock` (no `sleep`, no
real clock, no network); webhooks go through an injected
`sender(url, payload)`; a restart is a new `JobStore` and `Runner` on the same
SQLite file; a crash is a `BaseException` that is not an `Exception`.

## The two bases

Both bases start from the job runner reference solution of the same variant
(cancellation, retries, and restart recovery work; all 33 job runner hidden
tests pass) and add `dashboard.render_page(runner, path)`, which serves the
jobs table at `/jobs`, so that both variants have the same public interface.

| | coupled | modular |
| --- | --- | --- |
| Choosing the next job | `Runner.run_once` pops an in-memory heap (`_enqueue`, `_next_due`) | `scheduler.pick_next` combines registered filters (`register_filter`) and orderings (`register_ordering`) |
| State changes | `job.status = ...; store.save(job)` scattered over `run_once`, `_record_failure`, `_finish_cancelled`, `cancel`, `_recover` | only through `transitions.create` / `transitions.change`, which publish an `Event` to subscribers (`transitions.subscribe`) inside the SQLite transaction of the change |
| Storage | `JobStore` has one method per query | `JobStore` plus `register_schema`, `execute`, `query`, `transaction` for a module's own tables |
| REST API | one `if` chain in `api.handle` | `@web.route` in the module that owns the route (`api.py`, `cancellation.py`) |
| Dashboard | one `if` chain in `render_page` | `@web.page` in the module that owns the page |

The modular base is a behaviour-preserving refactor: its public tests and the
33 job runner hidden tests pass, exactly as on the coupled base.

Lines of Python (`jobrunner/` + `tests/`): coupled base 461 + 248, modular base
867 + 377.

## Reference solutions

Lines changed in `jobrunner/` (`git diff --numstat`, added + deleted; tests not
counted):

| coupled | +/- | modular | +/- |
| --- | --- | --- | --- |
| `runner.py` | 413 / 55 | `runner.py` (delegation) | 145 / 10 |
| `store.py` | 236 / 23 | `store.py`, `transitions.py`, `web.py`, `api.py`, `errors.py` | 60 / 6 |
| `api.py` | 166 / 29 | `models.py` | 108 / 1 |
| `dashboard.py` | 157 / 7 | new `validation.py` | 40 / 0 |
| `models.py` | 109 / 2 | new `priority.py` | 110 / 0 |
| `errors.py` | 5 / 1 | new `recurring.py` | 174 / 0 |
| | | new `dependencies.py` | 96 / 0 |
| | | new `ratelimit.py` | 107 / 0 |
| | | new `notifications.py` | 238 / 0 |
| | | new `audit.py` | 143 / 0 |
| **total** | **1,123 / 117 = 1,240** | **total** | **1,262 / 17 = 1,279** |

Each reference also adds `tests/test_platform.py` (144 lines, the same file in
both variants).

In coupled, every feature lands in `runner.py` (`_next_job` for selection,
`_changed` for the audit entry, outbox rows, and dependency cascade of every
state change, `_create_recurring_jobs` at the start of `run_once`), `store.py`
(one table and a few methods per feature), `api.py`, and `dashboard.py`. In
modular, each feature is one module that registers its own scheduler rules,
transition subscribers, tables, routes, and pages; the core files only gain
the new job fields and one-line `Runner` delegations.

## Hand-written graphs

### coupled

```text
              ┌──► E (api.py) ──────────────────────────────┐
              ├──► F (dashboard.py) ────────────────────────┤
  A ──────────┴──► S ──► R ──► N ──► H ─────────────────────┴──► G
contract     selection recurring notify audit                   integration
```

| Node | Kind | Edits | Why |
| --- | --- | --- | --- |
| A | contract | `models.py`, `errors.py`, `store.py`, `runner.py` | new job fields and record classes, `NotFound`, the complete `JobStore.transaction()` helper, and every new `Runner` method as a signature with a runnable default (readers return empty results, writers raise `NotImplementedError`) |
| S | implement | `runner.py`, `store.py` | **priorities, tenant rotation, dependencies, and rate limits merged**: all three change how `run_once` picks a job, which means replacing the heap with a selection over stored jobs. Splitting them would give three nodes rewriting the same twenty lines one after the other (property 1). Dependency cascades are here too, since they hook into the same failure and cancellation paths |
| R | implement | `runner.py`, `store.py` | recurring jobs: another change at the start of `run_once`, plus job creation through S's `JobStore.add`; queued after S (same files, and it needs S's job columns: `full` edge) |
| N | implement | `runner.py`, `store.py` | notifications: an outbox write at **every** state change, scattered over `runner.py`, so queued after S and R, which add state changes (`order` edge, property 3) |
| H | implement | `runner.py`, `store.py` | the audit log: an entry at the same state changes as N; queued after N (`order` edge, property 3) |
| E | implement | `api.py` | every new REST route; only needs A's method signatures, so it runs in parallel with the chain |
| F | implement | `dashboard.py` | every new dashboard page; same reason |
| G | implement | `tests/test_platform.py` (+ glue in any module) | end-to-end tests |

Edges: `A→S`, `A→E`, `A→F` (interface); `S→R` (full); `R→N`, `N→H` (order);
`H→G`, `E→G`, `F→G` (full). Critical path: A → S → R → N → H → G (6 nodes).
At most three nodes ever run at once (the chain, E, F), and once E and F are
done the rest is sequential.

### modular

```text
          ┌──► P priority.py ──────┐
          ├──► R recurring.py ─────┤
          ├──► D dependencies.py ──┤
  A ──────┼──► L ratelimit.py ─────┼──► G
contract  ├──► N notifications.py ─┤  integration
          └──► H audit.py ─────────┘
```

| Node | Kind | Edits | Why |
| --- | --- | --- | --- |
| A | contract | `models.py`, `errors.py`, `store.py`, `transitions.py`, `web.py`, `runner.py`, `api.py`; creates `validation.py` and the six feature modules | new job fields and records, `NotFound`, complete shared helpers (`validation.py`, `web.fields/number/time/heading`), the six feature modules as stubs with their final signatures, and complete one-line `Runner` delegations to them; `submit` takes the new arguments and `run_once` calls `recurring.create_due` |
| P | implement | `priority.py` | two orderings, a turn-recording subscriber, `set_priority`, `queue`, `tenants`, their routes and page |
| R | implement | `recurring.py` | definitions, `create_due`, routes, page |
| D | implement | `dependencies.py` | a filter, a cascade subscriber, routes, page |
| L | implement | `ratelimit.py` | a filter, a start-recording subscriber, routes, page |
| N | implement | `notifications.py` | an outbox subscriber, delivery, routes, page |
| H | implement | `audit.py` | an audit subscriber, history and stats, routes, pages |
| G | implement | `tests/test_platform.py` (+ glue) | end-to-end tests |

Edges: `A→P/R/D/L/N/H` (interface); `P/R/D/L/N/H→G` (full). The six feature
nodes have no path between them and edit disjoint files, each owning its own
API routes and dashboard pages. Critical path: A → (any feature) → G (3 nodes).

How it differs from coupled, and why:

- **Selection is three nodes, not one.** Priority (orderings), dependencies
  (a filter), and rate limits (a filter) are separate rules that
  `scheduler.pick_next` combines; none touches `run_once`.
- **Notifications and audit run in parallel** instead of queueing behind every
  feature that adds a state change: each subscribes to `transitions` and
  sees all changes, including those other features cause.
- **API and dashboard are not separate nodes**: each feature registers its own
  routes and pages, so the node that knows the feature also exposes it.
- **The contract is larger** (about 500 lines: records, helpers, stubs for six
  modules, and the `Runner` delegations, against about 300 in coupled), which is the price of making the six nodes
  independent.

### Validation

All four graphs pass `validate()` on their generated repository with no errors
and **no warnings**, and `derive_edges()` adds no edge to either hand-written
graph. `requires` holds interface dependencies (satisfied by the contract
node); `requires_impl` holds implementation dependencies (R on S's
`JobStore.add` in coupled; G on every feature in both).
`tests/taskgraph/test_job_platform_task.py` checks this, the commit hashes,
the hidden test results below, and the graph shapes.

### Expected execution differences

- **Single agent**: one context for about 1,250 changed lines in six (coupled)
  or fourteen (modular) files. Expect long runs, context compaction, and
  missed details in the strict SPEC formats.
- **Graph, sequential (`--workers 1`)**: the same total work as the single agent
  plus per-node overhead; worth it only if smaller contexts make nodes more
  accurate.
- **Graph, parallel**: modular can run six features at once (with
  `--workers 4`, two rounds), so wall time is about A + two feature nodes + G.
  Coupled gains only the overlap of E and F with the chain; its wall time stays
  close to the sequential run. Comparing the two variants under the same
  scheme isolates the effect of code structure on the graph.
- In coupled, E and F work against stub `Runner` methods, and N and H must find
  every state change that S and R wrote, so mismatches surface late, in G.

## Hidden tests

`hidden_tests/conftest.py` provides a fake clock, a temporary database with
restart support (`make_runner`), a scripted handler, a recording webhook
sender, and HTML table helpers. Groups (test files) and results:

| Group | Tests | base (both variants) | base + reference (both variants) |
| --- | --- | --- | --- |
| `regression/` (job runner) | 33 | 33 pass | 33 pass |
| `test_priority.py` | 13 | 0 pass | 13 pass |
| `test_recurring.py` | 15 | 0 pass | 15 pass |
| `test_dependencies.py` | 12 | 0 pass | 12 pass |
| `test_rate_limit.py` | 10 | 0 pass | 10 pass |
| `test_notifications.py` | 15 | 0 pass | 15 pass |
| `test_audit_stats.py` | 13 | 0 pass | 13 pass |
| `test_api.py` | 12 | 1 pass (unknown routes are 404) | 12 pass |
| `test_dashboard.py` | 10 | 1 pass (`/jobs` and unknown pages) | 10 pass |
| `test_end_to_end.py` | 8 | 0 pass | 8 pass |

108 new tests plus 33 regression tests. Public tests pass on both base and
base + reference. Copy the directory to `_hidden_tests/` in the repository and
run `python -m pytest -q _hidden_tests` there (the Coordinator does this with
`--hidden-tests`).

## Generating a repository

```bash
python experiments/taskgraph/job_platform/make_repo.py coupled /tmp/jp-coupled
python experiments/taskgraph/job_platform/make_repo.py modular /tmp/jp-modular --with-reference
```

The command prints the commit hash, which equals `base_commit` in the graphs on
every platform (LF files, `core.autocrlf=false`, fixed author, date, and
message). `--with-reference` copies the reference solution over the working
tree after the commit, without committing it.

## Running the three schemes

Workers make long model calls on this task. Set the request timeout in `.env`
before running, or requests time out mid-node:

```bash
AQOURS_CODE_REQUEST_TIMEOUT=300
```

With the Coordinator, on Linux or WSL2, from the Aqours repository root (use
an `--out` directory outside the repository):

```bash
python experiments/taskgraph/job_platform/make_repo.py coupled /tmp/jp-coupled
H=experiments/taskgraph/job_platform/hidden_tests
G=experiments/taskgraph/job_platform/coupled/graphs

# single agent
python -m aqours_code.taskgraph run $G/single.json --repo /tmp/jp-coupled --workers 1 --hidden-tests $H --out /tmp/jp-runs
# hand-written graph, sequential
python -m aqours_code.taskgraph run $G/handwritten.json --repo /tmp/jp-coupled --workers 1 --hidden-tests $H --out /tmp/jp-runs
# hand-written graph, parallel
python -m aqours_code.taskgraph run $G/handwritten.json --repo /tmp/jp-coupled --workers 4 --hidden-tests $H --out /tmp/jp-runs
```

Replace `coupled` with `modular` for the other variant. Each run clones the
repository, so one generated repository serves every run. The default
`--worker-timeout` is 1800 s per node; the single-agent node may need more
(`--worker-timeout 3600`).
