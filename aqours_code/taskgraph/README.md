# Task Graph (Experimental)

Task graph format, repository symbol index, validator, and rule-based edge
derivation for multi-agent code task scheduling. A **Planner** produces a task
graph, this subpackage checks it, and a **Coordinator** later schedules workers
from it, using the stable single-agent path as the worker.

`aqours_code.taskgraph` is self-contained: it does not import the Aqours
runtime (agent loop, tools, Trace), and no existing module imports it. It
needs pydantic v2, installed by the `taskgraph` or `dev` extra, and `git` on
`PATH`:

```bash
pip install -e ".[taskgraph]"     # or ".[dev]" for the test suite
python -m pytest -q tests/taskgraph
```

## Task graph format

A graph describes a plan only; it never contains run state. The JSON Schema is
in [`schemas/taskgraph.schema.json`](schemas/taskgraph.schema.json) and
[`examples/toy_graph.json`](examples/toy_graph.json) is a complete example.
Unknown fields are rejected everywhere.

| Object | Fields |
| --- | --- |
| `Graph` | `request_id`, `request`, `repo`, `base_commit`, `final_checks` (default `[]`), `generator`, `nodes` (≥ 1), `edges` (default `[]`), `revision_log` (default `[]`) |
| `Generator` | `kind`: `manual` \| `planner`; optional `planner_version`, `model`, `revision_mode`: `llm` \| `rule_assisted` \| `none` |
| `Node` | `id` (`[A-Za-z0-9_-]+`), `title`, `kind`: `contract` \| `implement`, `goal` (non-empty), `edit_set`, `requires`, `requires_impl`, `provides`, `check`, `context_files`, optional `size`: `small` \| `medium` \| `large` |
| `EditSet` | `modify` (existing files), `create` (new files), optional `symbols` (functions/methods the node changes; an implement node listing a symbol here counts as its implementer for `requires_impl`, V12 and W5) |
| `Check` | `commands`, `timeout_s` (integer > 0, default 300) |
| `Edge` | `from`, `to`, `type`: `interface` \| `full` \| `order`, `source`: `manual` \| `llm` \| `derived`, `reason` |
| `RevisionEntry` | `action`: `merge` \| `split` \| `add_edge` \| `remove_edge` \| `add_node` \| `other`, `nodes`, optional `into`, `reason` |

In Python, `Edge.from` is `Edge.from_`; it is always serialized as `from`.
Use `load_graph(path)` and `dump_graph(graph, path)` for JSON I/O.

Within one node, `modify`, `create`, `context_files`, `requires`,
`requires_impl`, `provides` and `edit_set.symbols` must not repeat an entry,
no path may appear in both `modify` and `create`, and no symbol may appear in
both `requires` and `requires_impl`. These are schema errors.

The exported JSON Schema describes every field and, for the Planner, adds a
`pattern` for symbols and `uniqueItems` for these lists. The Python
validators remain authoritative.

### Execution semantics

Every edge, whatever its `type`, means that the downstream node starts only
after the upstream node has **finished and been merged**. The Coordinator must
follow this rule; the validator relies on it when it accepts a symbol provided
by any ancestor (V6) and when it accepts an edit conflict ordered by any edge
(V5). The edge type records *why* the order exists:

- `interface`: the downstream node uses an interface defined by a `contract`
  node (V9);
- `full`: the downstream node depends on the upstream implementation;
- `order`: only the order matters, for example two nodes editing the same
  file. If the downstream node requires a symbol the upstream node provides,
  the edge should be `interface` or `full` instead (W4).

### Interface versus implementation

A `contract` node writes a symbol's interface (signature, docstring, a
runnable default or fake body); an `implement` node writes the real logic.
Both list the symbol in `provides`. A node needs the symbol in one of two ways:

- `requires`: the interface is enough. Any ancestor that provides the symbol,
  contract or implement, satisfies it (V6), so the node can start as soon as
  the contract node is merged.
- `requires_impl`: a working implementation is needed, for example by an
  integration test that calls it. The node starts only after **every**
  implementer has finished and been merged (V12). An implementer is an
  `implement` node that lists the symbol in `provides` or `edit_set.symbols`.

Example: contract A and implement B both provide
`store.py::JobStore.load_unfinished`. Node C, which only calls it, lists it in
`requires` and gets `A -> C` (`interface`); integration test G lists it in
`requires_impl` and gets `B -> G` (`full`), so G waits for B.

### Paths and symbols

All paths are repository-relative POSIX paths: `/` separators, no leading `/`,
no drive letter, no empty, `.` or `..` segments, no segment that starts or ends
with whitespace, and no control characters.

A symbol is `path::Qualified.name`, where the path is a `.py` file and the
qualified name is one or more Python identifiers joined by `.`:

```text
models.py::JobStatus.CANCELLED
store.py::JobStore.list_unfinished
runner.py::run_loop
```

`parse_symbol()` parses and checks a symbol string. Restricting symbols to
`.py` files is a v0 limitation: only Python files are indexed.

## Repository index

`build_index(repo_path, commit)` reads a commit with `git ls-tree` and
`git show` without checking it out. `files` contains every file; `symbols` is
built from `.py` files with `ast`:

- module-level functions, classes, and assigned variables;
- methods, class-body assignments and annotated attributes (including Enum
  members and dataclass fields), recursively for nested classes;
- `self.xxx` assignments inside `__init__`, recorded as class attributes.

Assignments inside module- or class-level `if`, `try`, `with`, `for` and
`while` blocks count as definitions of that scope. Files that do not parse are
skipped and listed in `index.warnings`.

## Validation rules

`validate(graph, index=None)` returns a report with `errors`, `warnings`, and
`ok`. Ancestors are nodes that can reach a node along edges of any type.

| Code | Rule |
| --- | --- |
| V1 | Node ids are unique; edges reference existing nodes; no self-loops; no duplicate edge with the same `from`, `to` and `type`. |
| V2 | The graph, with all edge types, has no cycle. |
| V3 | Each `modify` file exists at the base commit or is created by an ancestor (a file created only by a non-ancestor is reported with its creator: missing edge?); `create` files do not exist at the base commit; each `context_files` file exists at the base commit or is created by an ancestor (one created by a non-ancestor, including the node itself, is reported with its creator: missing edge?). *Needs an index.* |
| V4 | Every node has at least one non-empty check command. |
| V5 | Two nodes that edit a common file (`modify ∪ create`) must be ordered: one is an ancestor of the other. |
| V6 | Every `requires` symbol exists at the base commit or is provided by an ancestor (contract or implement). A node's own `provides` does not count. *Needs an index.* |
| V7 | Every node edits at least one file. |
| V8 | `requires`, `requires_impl`, `provides` and `edit_set.symbols` are well-formed symbols. |
| V9 | An `interface` edge starts at a `contract` node. |
| V10 | Every `edit_set.symbols` and `provides` entry belongs to a file in the same node's `modify` or `create` (a node cannot provide a symbol in a file it does not edit). |
| V11 | Each new file is created by exactly one node. |
| V12 | For every `requires_impl` symbol, every implementer is an ancestor (the others are reported together: missing edge?). With no implementer, the symbol must exist at the base commit; a symbol only declared by contract nodes, or not defined anywhere, is an error. *Needs an index.* |
| W1 | A `small` node has exactly one distinct direct successor (consider merging). |
| W2 | A provided symbol is not in any other node's `requires` or `requires_impl`. |
| W3 | `final_checks` is empty. |
| W4 | A node's only direct edge from P is an `order` edge, yet it `requires` a symbol P provides or `requires_impl` a symbol P implements. |
| W5 | A node `requires` a symbol that exists at the base commit, but non-ancestor nodes list it in `provides` or `edit_set.symbols` (they change it, so the node may see the old version). `requires_impl` is covered by V12 instead. *Needs an index.* |

Without an index, V3, V6, V12 and W5 are skipped and reported as warnings. A single
node graph is the fallback plan and passes when V3, V4, V7, V8, V10 and V11
hold.

## Edge derivation

`derive_edges(graph, index)` returns a new graph and the revision entries it
appended to `revision_log`:

1. Dependency edges, in three passes:
   1. `requires`: for a symbol that is not in the repository and not provided
      by an ancestor, a single contract provider gets `contract -> node`
      (`interface`), even if implement nodes also provide it; with no contract
      provider, a single implement provider gets `implement -> node` (`full`).
      Several contract providers, or several implement providers and no
      contract, get no edge.
   2. `requires_impl`: every implementer of the symbol that is not yet an
      ancestor gets `implementer -> node` (`full`). With no implementer, no
      edge is added (V12 reports it).
   3. `context_files`: for a file that is not in the repository and is created
      by exactly one other node that is not yet an ancestor, add
      `creator -> node` (`interface` from a contract creator, otherwise
      `full`). A file created by the node itself or by several nodes gets no
      edge (V3 and V11 report them).
2. For each pair of nodes that edit a common file and are not ordered, add an
   `order` edge. The direction, by priority:
   1. if one node creates an overlapping file that the other modifies, the
      creator goes first;
   2. otherwise a `contract` node goes first;
   3. otherwise the earlier node in `nodes` goes first.

   If each node creates an overlapping file that the other modifies, no edge
   is added and an `other` revision explains the conflicting creation
   direction.

Ancestry is recomputed after every edge. An edge that would create a cycle is
not added and is recorded as an `other` revision. After both steps, each
multi-provider symbol from step 1.1 is checked against the final graph: if no
ancestor of the requiring node provides it, an `other` revision lists the node
and every candidate provider with the reason "multiple providers, edge not
derived". These entries come after all edge entries. Run
`validate()` on the result to decide whether it is usable. W5 situations get
no derived edge.

## CLI

```bash
python -m aqours_code.taskgraph validate <graph.json> [--repo <path>]
python -m aqours_code.taskgraph derive <graph.json> --repo <path> --out <new_graph.json>
python -m aqours_code.taskgraph index --repo <path> --commit <sha>
python -m aqours_code.taskgraph export-schema [--out <path>]
```

`--repo` indexes the repository at the graph's `base_commit`. Exit codes:
`0` valid, `1` validation errors (for `derive`: the derived graph is still
invalid), `2` unreadable input, schema mismatch, an unwritable `--out`, or a
git failure (including a missing `git` executable or an unknown commit). Exit
code `2` errors go to stderr as one `error:` line; a schema mismatch is
followed by pydantic's error details. Output lines
look like:

```text
errors (1):
[V5] C, D: both edit runner.py but neither is an ancestor of the other
warnings (0):
```

## Running a graph

```bash
python -m aqours_code.taskgraph run <graph.json> --repo <path>
    [--out runs] [--workers 2] [--max-attempts 2]
    [--worker aqours|command] [--command-map <json>]
    [--worker-timeout 1800]
    [--hidden-tests <dir>]
```

Run experiments on Linux or WSL2. Check commands, `final_checks`, and the
`--command-map` commands run through the system shell, so write them for a
POSIX shell. The process exits with `0` for `success`, `1` for any other
status or an invalid graph, and `2` for input or git errors.

### Three schemes, one pipeline

| Scheme | How to run it |
| --- | --- |
| Single-agent baseline | A graph with one node that edits every file |
| Sequential | `--workers 1` |
| Parallel | `--workers N` |

All schemes use the same worker: `AqoursWorker` runs
`aqours_code.agent_loop.run_agent_task()` in a child process
(`python -m aqours_code.taskgraph.worker_entry --config <file>`) with one fixed
tool policy (`bash`, `read_file`, `write_file`, `edit_file`, `glob`,
`todo_write`, `compact`; no MCP, memory, skills, teammates, or background
tasks). A worker has no model-call limit: it runs exactly like an ordinary
Aqours single-agent run and is bounded only by `--worker-timeout`. Its model
calls and tokens are counted for the cost data. The model comes from the
Aqours `.env` and environment. `--worker command` runs a fixed
shell command per node instead (tests and debugging only).

### Flow

1. **Prepare.** Index `base_commit` and run `validate()`; any error stops the
   run before anything executes. Clone the repository to `repo/`, create
   `tg/integration` at `base_commit`, and add the Aqours runtime directories
   and Python caches to `.git/info/exclude` so workers' runtime files are never
   committed.
2. **Schedule.** A node is ready when every direct upstream node is merged. A
   ready node starts when fewer than `--workers` nodes are running and its
   `modify ∪ create` does not overlap a running node; ties start in `nodes`
   order. When a node fails, its descendants are `skipped`
   (`upstream_failed`); unrelated nodes continue. Every git command on the
   shared clone runs under one lock; a merge, its post-merge check, and a
   possible undo run as one unit under a separate merge lock, so a slow
   post-merge check never blocks other nodes from starting or committing.
3. **Each node**, up to `--max-attempts` times (default 2): create
   `tg/node/<id>` and worktree `wt/<id>` from the last integration commit
   whose post-merge check passed (never from a merge still being checked, so
   an undone merge cannot leak into another branch), write the
   prompt, run the worker, and commit `attempt <n>`. The attempt fails on a
   worker error (`worker_error`), on a timeout without changes
   (`worker_timeout`), on no change since the start
   commit (`no_changes`), or on a failing check command run in the worktree
   (`check_failed`). A retry continues in the same worktree with the failure
   reason and the last 4000 characters of output in the prompt. A worker that
   times out after changing files may have finished its work, so its checks
   still run and decide the attempt. Each attempt's worker outcome is kept in
   `worker_reasons` (`""` when the worker ended normally).
4. **Merge.** Squash-merge the node branch into `tg/integration` as one commit
   `[taskgraph] <id>: <title>`. A conflict resets the branch and fails the node
   (`merge_conflict`); the node's checks then run again on the integration
   branch, and a failure undoes the merge (`post_merge_check_failed`). Neither
   is retried. Changed files outside `modify ∪ create` are recorded as
   `out_of_scope_files` but do not fail the node.
5. **Final stage.** Run every `final_checks` command on the integration branch
   and record each result. With `--hidden-tests DIR`, check out the
   integration HEAD into `final/`, copy `DIR` to `final/_hidden_tests/`, and
   run `python -m pytest -q _hidden_tests -p no:cacheprovider --junitxml=...`.
   Workers never see these tests.

The run status is `success` when every node is merged and every final check
passes, `partial` when some node is merged, and `failed` otherwise. Hidden
test results are reported separately and do not change the status.

Each node check command has the node's `check.timeout_s`; each final check
command has 1800 s, as do the hidden tests. A worker gets `--worker-timeout`
seconds; its process tree is killed 30 s after that if it has not stopped.

### Run directory

```text
runs/<run_id>/            run_id = UTC timestamp + 4 random hex characters
  config.json             run parameters, worker provider/model, validation warnings
  graph.json              the graph that was executed
  events.jsonl            event stream (for Gantt charts)
  summary.json            results
  final_checks.txt        output of final_checks
  hidden_tests.xml/.txt   hidden test results, if any
  repo/                   clone; tg/integration is checked out here
  wt/<node_id>/           node worktrees (removed when the node ends)
  nodes/<node_id>/        prompt_<n>.md, worker_<n>.json, worker_<n>_config.json,
                          worker_<n>_stdout.txt, trace_<n>.jsonl, aqours_<n>/,
                          check_<n>.txt, post_merge_check.txt, diff.patch
  final/                  clean checkout of the integration HEAD for hidden tests
```

### events.jsonl

One JSON object per line with `ts` (ISO 8601, UTC), `t` (seconds since the
run started), `type`, and `node` where it applies. Types: `run_start`,
`node_ready`, `node_start`, `worker_start`, `worker_end` (with `ok`,
`reason`, `duration_s`, `model_calls`, `input_tokens`, `output_tokens`),
`check_start`, `check_end`, `merge_start`, `merge_end`,
`post_merge_check_end`, `node_merged`, `node_failed`, `node_skipped`,
`final_checks_end`, `hidden_tests_end`, `run_end`.

### summary.json

- `run_id`, `status`, `wall_time_s`, `config`, `integration_commit`;
- `nodes.<id>`: `status`, `reason`, `attempts`, `worker_reasons`, `start_t`, `end_t`,
  `worker_time_s`, `check_time_s` (node and post-merge checks),
  `model_calls`, `input_tokens`, `output_tokens`, `changed_files`,
  `out_of_scope_files`, `merge_commit`, `error`;
- `totals`: `model_calls`, `input_tokens`, `output_tokens`;
- `final_checks`: one entry per command (`command`, `ok`, `exit_code`,
  `timed_out`, `duration_s`);
- `hidden_tests`: `passed`, `failed`, `errors`, `skipped`, `exit_code`,
  `timed_out`, `duration_s`, or `null`.

### Smoke test on the toy repository

With a model configured in the Aqours `.env`, recreate the toy repository
(its commit matches `base_commit` in the example) and run the example:

```bash
python -c "import sys; sys.path.insert(0, 'tests/taskgraph'); from pathlib import Path; from taskgraph_support import TOY_FILES, commit_files, git; repo = Path('toy-repo'); repo.mkdir(); git(repo, 'init', '-q'); print(commit_files(repo, TOY_FILES, 'toy repository'))"
python -m aqours_code.taskgraph run aqours_code/taskgraph/examples/toy_graph.json --repo toy-repo --workers 2
```
