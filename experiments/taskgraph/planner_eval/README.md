# Planner evaluation

Does the planner split a request the way a person would? Planner v1
(`python -m aqours_code.taskgraph plan`) drafts the pieces of work from the
request alone, grounds them in the code, and lets the program merge the
nodes that can only queue on one file; a small request takes the fast path
and stays a single-agent graph. This evaluation runs it several times on the
job runner and job platform tasks and compares the **structure** of each
generated graph with the hand-written graph of the same repository.

The planner never sees the hand-written graphs or the reference solutions:
it reads only the request and a clone of the generated repository (which
contains the base code, its tests, `SPEC.md`, and `README.md`), and neither
`aqours_code/taskgraph/planner_draft_prompt.md` nor `planner_prompt.md` says
anything about either task.

## Running

Generate the repositories once (any directory outside the Aqours repository):

```bash
python experiments/taskgraph/job_platform/make_repo.py modular C:\tg\jp-modular
python experiments/taskgraph/job_platform/make_repo.py coupled C:\tg\jp-coupled
python experiments/taskgraph/job_runner/make_repo.py modular C:\tg\jr-modular
python experiments/taskgraph/job_runner/make_repo.py coupled C:\tg\jr-coupled
```

Then, from the Aqours repository root, with the model configured in `.env`
as for the Coordinator (set `AQOURS_CODE_REQUEST_TIMEOUT=300` too):

```bash
python experiments/taskgraph/planner_eval/run_eval.py --out C:\tg\planner_eval --repeat 3 --case jp-modular=C:\tg\jp-modular --case jp-coupled=C:\tg\jp-coupled --case jr-modular=C:\tg\jr-modular --case jr-coupled=C:\tg\jr-coupled
```

Options: `--repeat N` (default 3), `--final-check` (default
`python -m pytest -q tests`), `--timeout` (seconds per planner round, default
1800), and `--no-fast-path`, `--fast-path-lines N`, `--no-merge`, which are
passed on to `plan` (use `--no-merge` to compare the graph with and without
the rule-based merge). Pass only the cases you want. The request and the hand-written graph of
each case are fixed in the script:

| Case | Request | Hand-written graph |
| --- | --- | --- |
| `jp-modular` | `job_platform/request.md` | `job_platform/modular/graphs/handwritten.json` |
| `jp-coupled` | `job_platform/request.md` | `job_platform/coupled/graphs/handwritten.json` |
| `jr-modular` | `job_runner/request.md` | `job_runner/modular/graphs/handwritten.json` |
| `jr-coupled` | `job_runner/request.md` | `job_runner/coupled/graphs/handwritten.json` |

Output, per run in `<out>/<case>/run<N>/`:

```text
graph.json               the planner's graph
graph.json.unmerged.json the graph before the rule-based merge (not on the fast path)
graph.json.report.json   draft items, every round's draft and errors, merges, totals
graph.json.logs/         planner agent configs, traces and stdout per round (draft/ for step 1)
plan_stdout.txt          output of the plan command
compare.md, compare.json the comparison with the hand-written graph
```

and `<out>/summary.md` / `summary.json`: one row per run with the case,
success, draft items, whether the fast path was taken, revision rounds, nodes
before the merge and after it, critical path, maximum parallel width,
test-only nodes, mean node match, model calls, and tokens. A failed run gets
a row with a note; the remaining runs continue.

A single comparison can be repeated by hand:

```bash
python -m aqours_code.taskgraph compare C:\tg\planner_eval\jp-modular\run1\graph.json experiments/taskgraph/job_platform/modular/graphs/handwritten.json --repo C:\tg\jp-modular
```

## What `compare` reports

| Item | Meaning |
| --- | --- |
| Validation | error count; warning codes with counts |
| Nodes | total, and contract / implement |
| Contract files | files edited by contract nodes |
| Critical path | nodes on the longest dependency chain |
| Max parallel width | largest layer when nodes are layered by longest-chain depth |
| Test-only nodes | nodes whose modified and created files are all under `tests/` |
| Node match | for each hand-written node, the planner node with the highest Jaccard similarity of edited files (`modify ∪ create`, ignoring `tests/`), and the mean |

## Expectations

| Repository | Expected |
| --- | --- |
| job platform, modular | one thin contract followed by at least four feature nodes that can run in parallel; feature nodes do not touch `runner.py`, `api.py` or `dashboard.py`; critical path 2–3; no test-only nodes |
| job platform, coupled | the features that all edit `runner.py` are separate nodes before the merge and one node after it (rule M1); the API and dashboard nodes stay parallel |
| job runner (both) | validates; if the estimate is under 500 lines, the fast path gives the single-agent graph |

The planner does not have to reproduce the hand-written graphs. They are
ideal plans written after reading the reference solutions; the planner only
has to capture the same structural points. A low node-match score with the
right shape (a thin contract, independent features in parallel, no test-only
nodes) is fine; a high score with test-only nodes or a long chain on modular
is not.
