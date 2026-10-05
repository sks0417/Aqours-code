# Job runner task

The main example task of the task graph project. One request
([`request.md`](request.md)) is applied to two repositories that offer the
same public interface but differ in internal structure:

- the same task runs as a single agent, as a single agent given the
  hand-written plan, as a hand-written graph executed sequentially, and as the
  same graph executed in parallel;
- the two variants show that **the same request on differently structured code
  leads to different task graphs**.

Everything here is data: neither Aqours nor `aqours_code.taskgraph` imports it.

```text
request.md          the request (both variants)
make_repo.py        builds a deterministic git repository from <variant>/base/
hidden_tests/       hidden tests (both variants; never shown to workers)
coupled/  modular/
  base/             repository content: jobrunner/, tests/, SPEC.md, README.md, pyproject.toml
  reference/        reference solution: only new or changed files, copied over base/
  graphs/
    single.json          one-node graph (single-agent baseline)
    single_planned.json  handwritten.json flattened into one node with the whole plan
    handwritten.json     hand-written task graph
```

`SPEC.md` (identical in both variants) states the required behaviour of
cancellation, retries with exponential backoff, restart recovery, the REST
API, and the dashboard. Workers and the Planner can read it; hidden tests check
only what it states. All times come from an injected clock; a process restart
is simulated by a new `JobStore` and `Runner` on the same SQLite file, and a
crash during a run by a handler raising a `BaseException` that is not an
`Exception`.

## The two variants

| | coupled | modular |
| --- | --- | --- |
| Choosing the next job | `Runner.run_once` pops an in-memory heap | `scheduler.pick_next` |
| Re-queueing | `Runner._enqueue`, shared by submit and (after the change) retries and recovery | none: the store is the queue |
| Failure handling | inline in `Runner._execute` | `retry.decide` hook (base: always `FAILED`) |
| Startup | `Runner.__init__` loads pending jobs with `_enqueue` | `recovery.recover` hook (base: no-op) |
| End of a run | `Runner._execute` saves the job | `JobStore.finish(job_id, outcome)` |
| Progress columns | not in the table | already in the table, unused |

Lines of Python (`jobrunner/` + `tests/`): coupled base 302 + 152, modular
base 386 + 175. The reference solutions change about 240 lines of code in
coupled and 170 in modular, plus 84 lines of tests each.

## Expected graphs

### coupled

| Node | Kind | Edits | Why it is a separate node |
| --- | --- | --- | --- |
| A | contract | `models.py`, `errors.py`, `store.py`, `runner.py` | defines `CANCELLED`, the job fields, `TransientError`, `InvalidTransition`, and `Runner.cancel`; keeps the new fields in memory so others can run checks before persistence |
| B | implement | `store.py` | persistence is real work here: the table has no progress columns |
| C | implement | `runner.py` | retries **and** restart recovery: both change `_enqueue` and `run_once`, so they are one node (property 1) |
| D | implement | `runner.py` | cancellation must be handled in `run_once` after the handler, so it is queued after C (property 3) |
| E | implement | `api.py` | cancel endpoint and status fields |
| F | implement | `dashboard.py` | new columns and cancel form |
| G | implement | `tests/test_end_to_end.py` (+ glue in any module) | integration |

Edges: `A→B`, `A→C`, `A→E`, `A→F` (interface); `C→D` (order: both edit
`runner.py`); `B→G`, `D→G`, `E→G`, `F→G` (full: G `requires_impl` their
symbols). After A, B, C, E, F run in parallel; D starts after C. Critical
path: A → C → D → G (4 nodes).

### modular

| Node | Kind | Edits | Why |
| --- | --- | --- | --- |
| A | contract | `models.py`, `errors.py`, `store.py`, `retry.py`, `runner.py`, new `cancellation.py` | as in coupled, plus mapping the existing progress columns to job fields, counting attempts in `claim`, the complete `backoff_delay` helper, and a default `request_cancel` |
| R | implement | `retry.py`, `scheduler.py` | retry policy and due-time ordering |
| S | implement | `recovery.py` | restart recovery has its own hook |
| D | implement | `cancellation.py`, `store.py` | cancellation never touches `run_once`: `JobStore.finish` already sees every run's end |
| E | implement | `api.py` | as in coupled |
| F | implement | `dashboard.py` | as in coupled |
| G | implement | `tests/test_end_to_end.py` (+ glue) | integration |

Edges: `A→R`, `A→S`, `A→D`, `A→E`, `A→F` (interface); `R→G`, `S→G`, `D→G`,
`E→G`, `F→G` (full). All five middle nodes run in parallel. Critical path:
A → (any) → G (3 nodes).

How it differs from coupled, and why:

- **No persistence node.** The table already stores the progress fields; A
  only maps them to `Job` fields, a few lines.
- **Retry and recovery are two parallel nodes** instead of one, because they
  live in separate hooks (`retry.decide`, `recovery.recover`) instead of a
  shared `_enqueue`.
- **Cancellation runs in parallel** instead of after retries, because it is
  implemented in `cancellation.py` and `JobStore.finish`, not in `run_once`.
- **Recovery and retry share only the backoff rule.** A provides the complete
  three-line `backoff_delay`, so S `requires` it from A instead of waiting for
  R. Because only a contract provides it, the worker prompt labels it
  "interface only"; S's goal states that the helper is complete.

### Single-node graphs

`single.json` has one implement node whose goal is the request plus "Follow
SPEC.md", whose edit set is every file the reference solution touches, and
whose check is `python -m pytest -q tests`.

`single_planned.json` is `handwritten.json` flattened by
`python -m aqours_code.taskgraph flatten` (see the
[task graph README](../../../aqours_code/taskgraph/README.md#flattening)): one
node whose goal is the request followed by every hand-written node as a step,
in topological order, with the union of their files and checks. It gives a
single agent the same detailed plan as the graph, so a difference between
`single` and `single_planned` measures the plan, and a difference between
`single_planned` and `seq`/`par` measures splitting the work. It validates
with no errors; its W2 warnings (a provided symbol that no other node
requires) are inherent to a one-node graph.

### Validation

The single and hand-written graphs of both variants pass `validate()` on
their generated repository with no errors and **no warnings**, and `derive_edges()` adds no edge to either
hand-written graph. `tests/taskgraph/test_job_runner_task.py` checks this.

## Hidden tests

`hidden_tests/` has `conftest.py` (a fake clock, a temporary database, a
scripted handler, and the `Crash` exception) and six files:
`test_cancellation.py`, `test_retry.py`, `test_recovery.py`, `test_api.py`,
`test_dashboard.py`, `test_end_to_end.py`. They use only the public interface
in SPEC.md, so they apply to both variants.

Rules:

- copy them to `_hidden_tests/` in the repository root and run
  `python -m pytest -q _hidden_tests` there (this is what the Coordinator
  does with `--hidden-tests`);
- on `base`, every cancellation, retry, and recovery test fails (one API test,
  "unknown job is 404", passes because unknown routes were already 404);
- on `base` + `reference`, every hidden and public test passes;
- public tests pass on both.

## Generating a repository

```bash
python experiments/taskgraph/job_runner/make_repo.py coupled /tmp/jr-coupled
python experiments/taskgraph/job_runner/make_repo.py modular /tmp/jr-modular --with-reference
```

The command prints the commit hash, which equals `base_commit` in the graphs on
every platform (files are written with LF, git runs with `core.autocrlf=false`
and a fixed author, date, and message). `--with-reference` copies the reference
solution over the working tree after the commit, without committing it.

## Running the four configurations

With the Coordinator (task 2) on `main`, on Linux or WSL2, from the Aqours
repository root (use an `--out` directory outside the repository):

```bash
python experiments/taskgraph/job_runner/make_repo.py coupled /tmp/jr-coupled
H=experiments/taskgraph/job_runner/hidden_tests
G=experiments/taskgraph/job_runner/coupled/graphs

# single: single agent, request only
python -m aqours_code.taskgraph run $G/single.json --repo /tmp/jr-coupled --workers 1 --hidden-tests $H --out /tmp/jr-runs
# single_planned: single agent, the whole plan
python -m aqours_code.taskgraph run $G/single_planned.json --repo /tmp/jr-coupled --workers 1 --hidden-tests $H --out /tmp/jr-runs
# seq: hand-written graph, sequential
python -m aqours_code.taskgraph run $G/handwritten.json --repo /tmp/jr-coupled --workers 1 --hidden-tests $H --out /tmp/jr-runs
# par: hand-written graph, parallel
python -m aqours_code.taskgraph run $G/handwritten.json --repo /tmp/jr-coupled --workers 4 --hidden-tests $H --out /tmp/jr-runs
```

| Configuration | Graph | `--workers` |
| --- | --- | --- |
| `single` | `single.json` | 1 |
| `single_planned` | `single_planned.json` | 1 |
| `seq` | `handwritten.json` | 1 |
| `par` | `handwritten.json` | 4 |

After changing `handwritten.json`, regenerate `single_planned.json` with
`python -m aqours_code.taskgraph flatten $G/handwritten.json --out $G/single_planned.json`;
`tests/taskgraph/test_flatten.py` fails while the committed file is stale.

Replace `coupled` with `modular` for the other variant. Each run clones the
repository, so one generated repository serves every run.
