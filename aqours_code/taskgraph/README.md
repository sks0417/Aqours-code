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
| `EditSet` | `any_file` (default `false`; unrestricted files, single-node graphs only), `modify` (existing files), `create` (new files), optional `symbols` (functions/methods the node changes; an implement node listing a symbol here counts as its implementer for `requires_impl`, V12 and W5) |
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
| V7 | Every node lists at least one file to edit, unless `any_file` is true. |
| V8 | `requires`, `requires_impl`, `provides` and `edit_set.symbols` are well-formed symbols. |
| V9 | An `interface` edge starts at a `contract` node. |
| V10 | Unless `any_file` is true, every `edit_set.symbols` and `provides` entry belongs to a file in the same node's `modify` or `create` (a node cannot provide a symbol in a file it does not edit). |
| V11 | Each new file is created by exactly one node. |
| V12 | For every `requires_impl` symbol, every implementer is an ancestor (the others are reported together: missing edge?). With no implementer, the symbol must exist at the base commit; a symbol only declared by contract nodes, or not defined anywhere, is an error. *Needs an index.* |
| V13 | `edit_set.any_file: true` is allowed only in a graph with exactly one node. |
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
python -m aqours_code.taskgraph compare <planner_graph.json> <handwritten_graph.json> --repo <path> [--json <file>]
python -m aqours_code.taskgraph audit <runs_dir | run_dir> [...]
```

`plan`, `run` and `audit` are described in their own sections below. `compare` prints
a Markdown table of both graphs' structure: validation (error count, warning
codes), node counts by kind, files edited by contract nodes, critical path
(nodes on the longest chain), maximum parallel width (largest layer when
nodes are layered by longest-chain depth), test-only nodes (every edited file
under `tests/`), and, for each node of the second graph, the node of the first
with the highest Jaccard similarity of `modify ∪ create` outside `tests/`
(`no match` when no node shares a file with it; the mean counts it as 0).
Each graph is validated against `--repo` at its own `base_commit`.

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

## Generating a single-agent control

```bash
python -m aqours_code.taskgraph single <request.md> --repo <path> --out <graph.json>
    [--final-check "python -m pytest -q tests"]
```

This command uses only the original request and repository HEAD, without a
planner or reference solution. The single node has `any_file: true` and empty
file, symbol and context lists. Its goal preserves the full request and appends
"Keep the existing tests passing and add tests for the new behaviour."
The worker discovers the repository itself: it receives no context pack,
file restrictions or reading soft wall. Its `out_of_scope_files` is always empty.
Every worker, including this control, is instructed to follow the specification
when it disagrees with the goal and report the disagreement in its final answer.

Repeat `--final-check` for multiple commands; these become both the node checks
and `final_checks`. Without the option, `final_checks` is empty and the node has
the generic `git diff --check` check; the goal still asks the worker to run tests.
The graph records a manual generator and a revision-log entry for `single`.
The repository basename is stored as portable metadata.

## Running a graph

```bash
python -m aqours_code.taskgraph run <graph.json> --repo <path>
    [--out runs] [--workers 2] [--max-attempts 2]
    [--worker aqours|command] [--command-map <json>]
    [--worker-timeout 1800]
    [--hidden-tests <dir>]
    [--sandbox docker|none] [--sandbox-image aqours-code-eval:py311]
```

Run experiments on Linux or WSL2. Check commands, `final_checks`, and the
`--command-map` commands run through the system shell, so write them for a
POSIX shell. The process exits with `0` for `success`, `1` for any other
status or an invalid graph, and `2` for input or git errors.

Each run puts a full clone of the target repository under `--out` (default
`runs/` in the current directory; `runs/` is git-ignored in the Aqours
repository). Pass `--out` to keep run directories outside the repository,
for example `--out /tmp/tg-runs`; this also keeps the hidden-test run from
picking up the Aqours pytest configuration.

### Three schemes, one pipeline

| Scheme | How to run it |
| --- | --- |
| Single-agent baseline | Generate a one-node unrestricted graph with `single` |
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

### Worker sandbox

A worker must see only its own worktree. Its file tools (`read_file`,
`write_file`, `edit_file`, `glob`) already refuse paths outside it; its
`bash` commands run, with `--sandbox docker` (the default), in a Docker
container started from `--sandbox-image` that mounts only the worktree at
`/workspace`, with no network, a read-only root file system, no capabilities,
a non-root user, 2 CPUs, 2 GiB of memory and 256 processes. A command may run
up to 600 s (and never past the worker's deadline); a command that times out
gets a fresh container on the same worktree for the next command. The
container is removed when the worker ends, and the coordinator removes any
leftover after the worker process exits, however it ended. The worker prompt
tells the agent that bash runs in a Linux container at `/workspace`.

Inside the container git commands fail (the worktree's `.git` file points to
a host path). Workers must not change repository state anyway; the
coordinator commits on the host. Checks, post-merge checks, final checks and
hidden tests also run on the host, as before.

Before a run starts, `run` checks that Docker runs and that the image exists.
If not, it exits with code 2 and explains how to build the image:

```bash
docker build -f evals/docker/Dockerfile -t aqours-code-eval:py311 .
```

It never falls back to `--sandbox none`. `--sandbox none` runs worker
commands on the host and is only for unit tests and `--worker command`
(which always runs on the host); it prints a warning. `config.json` and
`summary.json` record `sandbox` (`docker` or `none`) and `sandbox_image`. The
planner needs no sandbox: it has no `bash`.

### Escape audit

After the hidden tests, the coordinator scans every attempt's
`trace_<n>.jsonl` for attempts to reach outside the workspace
(`aqours_code.taskgraph.escapes`):

- a file tool whose path is a host absolute path, an absolute path, or
  leaves the workspace through `..`, or that was refused with "Path escapes
  workspace";
- a `bash` command naming a host absolute path (`C:\`, `D:/`, `\\server\`,
  `/c/`), `/mnt/`, `/home/`, `/Users/`, or leaving the workspace (`cd ..`,
  `../` above the workspace root, `~`);
- file content written by `write_file` or `edit_file` naming a host absolute
  path, `/mnt/`, `/home/` or `/Users/`.

Absolute paths inside the node's own worktree and `/workspace` (the
container's mount) are allowed. Each node gets `escape_attempts` and
`escape_samples` (the first five: attempt, tool, the truncated command or
path, reasons) in `summary.json`, and the run gets `escape_attempts_total`.
`run` prints `escape attempts: N`; with `--sandbox none` and N > 0 it prints
a banner saying the run's results are invalid.

```bash
python -m aqours_code.taskgraph audit <runs_dir | run_dir> [...]
```

applies the same rules to finished runs, also those made before the audit
existed, and prints one row per node (run id, graph, node, escape attempts,
first sample) and a total. It exits with `1` when any escape attempt is
found, `0` when none is, and `2` when no run directory is found.

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
   `out_of_scope_files` but do not fail the node. For `any_file` nodes, the list
   stays empty.
5. **Final stage.** Run every `final_checks` command on the integration branch
   and record each result. With `--hidden-tests DIR`, check out the
   integration HEAD into `final/`, copy `DIR` to `final/_hidden_tests/`, and
   run `python -m pytest -q _hidden_tests -p no:cacheprovider --junitxml=...`.
   Workers never see these tests.
6. **Audit and clean up.** Scan the traces for escape attempts (see
   [Escape audit](#escape-audit)), then delete `final/_hidden_tests/` and
   `wt/`, so that no later worker can find this run's answers. `final/`
   keeps the final code; `nodes/` keeps the diffs, logs and traces.

The run status is `success` when every node is merged and every final check
passes, `partial` when some node is merged, and `failed` otherwise. Hidden
test results are reported separately and do not change the status.

Each node check command has the node's `check.timeout_s`; each final check
command has 1800 s, as do the hidden tests. A worker gets `--worker-timeout`
seconds; its process tree is killed 30 s after that if it has not stopped.

### Run directory

```text
runs/<run_id>/            run_id = UTC timestamp + 4 random hex characters
  config.json             run parameters, worker provider/model, validation warnings,
                          taskgraph_version, aqours_commit ({head, dirty} of the Aqours
                          checkout running the coordinator, or null)
  graph.json              the graph that was executed
  events.jsonl            event stream (for Gantt charts)
  summary.json            results
  final_checks.txt        output of final_checks
  hidden_tests.xml/.txt   hidden test results, if any (the tests themselves are deleted)
  repo/                   clone; tg/integration is checked out here
  wt/<node_id>/           node worktrees (removed when the node ends; wt/ when the run ends)
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
`final_checks_end`, `hidden_tests_end`, `escape_audit_end` (with `total`),
`run_end`.

### summary.json

- `run_id`, `status`, `wall_time_s`, `config`, `integration_commit`;
- `nodes.<id>`: `status`, `reason`, `attempts`, `worker_reasons`, `start_t`, `end_t`,
  `worker_time_s`, `check_time_s` (node and post-merge checks),
  `model_calls`, `input_tokens`, `output_tokens`, `changed_files`,
  `out_of_scope_files`, `merge_commit`, `error`, `escape_attempts`,
  `escape_samples`;
- `totals`: `model_calls`, `input_tokens`, `output_tokens`;
- `final_checks`: one entry per command (`command`, `ok`, `exit_code`,
  `timed_out`, `duration_s`);
- `hidden_tests`: `passed`, `failed`, `errors`, `skipped`, `exit_code`,
  `timed_out`, `duration_s`, or `null`;
- `sandbox`, `sandbox_image`, `escape_attempts_total`.

### Smoke test on the toy repository

With a model configured in the Aqours `.env`, recreate the toy repository
(its commit matches `base_commit` in the example) and run the example:

```bash
python -c "import sys; sys.path.insert(0, 'tests/taskgraph'); from pathlib import Path; from taskgraph_support import TOY_FILES, commit_files, git; repo = Path('toy-repo'); repo.mkdir(); git(repo, 'init', '-q'); print(commit_files(repo, TOY_FILES, 'toy repository'))"
python -m aqours_code.taskgraph run aqours_code/taskgraph/examples/toy_graph.json --repo toy-repo --workers 2
```

## Planner

```bash
python -m aqours_code.taskgraph plan <request.md> --repo <path> --out <graph.json>
    [--final-check "python -m pytest -q tests"] [--timeout 1800] [--request-id <id>]
```

Planner v0 only splits: it always outputs the split it thinks best, without
deciding whether to split or estimating cost.

1. Clone the HEAD of `--repo` into a temporary directory (uncommitted changes
   are not seen; the original repository is not touched). `base_commit` is
   that HEAD.
2. Run one planner agent: the worker's child-process runner
   (`python -m aqours_code.taskgraph.planner_entry`, `CountingClient`, the
   same model configuration) on the clone, with read-only tools
   (`read_file`, `glob`, `compact`). The prompt is
   [`planner_prompt.md`](planner_prompt.md) plus the request, the
   repository's file list and the final checks. The agent answers with a
   draft: only nodes, no edges. The last ```` ```json ```` block of the final
   answer is the draft.
3. Complete the draft into a graph: `request` is the request file's text,
   `repo` the repository path, `final_checks` the `--final-check` commands,
   `generator` `{"kind": "planner", "planner_version": "planner-v0", "model":
   ...}`, and no edges.
4. `derive_edges()`, then `validate()`, then the planner-only checks below.
5. If the draft does not parse, does not match the draft format, or the graph
   has validation or planner-check errors, run the agent again with the previous draft and the
   errors (a fresh agent; the prompt repeats the request), at most twice.
   A revised graph has `generator.revision_mode = "llm"`, otherwise `"none"`.
   If errors remain after two revisions, the run fails: the last graph that
   could be built and the report are still written, and the exit code is 1.

Draft format (fields as in the graph schema; missing lists are empty, and
`check` becomes `check.commands` with the default timeout):

```json
{"conventions": ["..."],
 "nodes": [{"id": "A", "title": "...", "kind": "contract | implement", "goal": "...",
            "modify": [], "create": [], "provides": [], "requires": [],
            "requires_impl": [], "check": ["python -m pytest -q tests"],
            "context_files": []}]}
```

`conventions` (optional, default empty) lists the repository's rules that
every node must follow, for example "the current time comes only from the
injected clock: functions that need it take a `now` argument". The program
appends them to every node's goal as a `Repository conventions:` section, so
every worker sees them; without conventions the goals are unchanged. The
prompt asks the planner to find these rules first, to design the contract's
interfaces so they can be kept (a module gets what it needs, such as `now`,
from its caller), and not to add an integration node: the final checks
verify the merged result, and a part that must be written against another
part's implementation uses `requires_impl` and connects to it itself.

Planner-only checks (`planner_checks()`, errors like V1-V13, applied only to
planner graphs and not part of `validate()`, so hand-written graphs are not
held to them):

| Code | Error |
| --- | --- |
| `P1` | a contract node is an ancestor of another contract node (contracts are not ordered: merge them) |
| `P2` | every file a node modifies or creates is under `tests/` (a test-only node: remove it or fold its work into the related nodes) |
| `P3` | a contract node creates or modifies a file under `tests/` (a contract's check only runs the existing tests) |

Goals describe implementation scope and integration points, generally within
600 characters; specification details belong in `SPEC.md#Heading` context
references, not in paraphrased goals. Only cross-node decisions absent from the
specification belong in goals. The report's `goal_chars` maps node IDs to final
goal character counts, including appended conventions. A goal longer than
1200 characters produces a terminal note, without failing planning.

Output: `--out` (the graph), `<out>.report.json` (every round's answer,
draft JSON, conventions, errors and warnings; the draft and conventions of
the written graph; the agent's calls, tokens and time; the number of
revision rounds, success, totals, and wall time), and `<out>.logs/` (the
planner agent's config, trace and stdout per round). `--timeout` applies to
each round. Exit codes: `0` success, `1` errors remained, `2` input or git
errors. Error codes in the report besides V1-V13 and P1-P3: `FORMAT` (no JSON block,
invalid JSON, or a draft or schema mismatch) and `AGENT` (the planner agent
failed or timed out).

`experiments/taskgraph/planner_eval/` runs the planner on the experiment
repositories and compares the result with the hand-written graphs.

### Worker reading soft wall

For graphs with more than one node, the context intro says that the supplied
pack already performs the requested source/contract inspection. A final
`# Before you start` reminder follows the rules and any retry details. Single
node graphs omit these reminders and register no soft wall. Unrestricted
single-agent controls also omit the context section entirely.

The coordinator passes `soft_wall.enabled`, `own_files`, `full_files`, and the
per-attempt log path through `WorkerRequest` and `AqoursWorker.config_for()`.
Only `worker_entry.register_soft_wall()` imports and uses the public Aqours
`register_hook` and `recoverable_tool_rejection` APIs. It calls `bootstrap()`
first: that function is idempotent (`aqours_code.__init__._BOOTSTRAPPED`), and
`run_agent_task()`'s runtime state/isolated collection lists do not include
`hooks.HOOKS`. Registrations therefore survive the task. A real subprocess
integration test verifies the rejection reaches the model; a lifetime test
also verifies the callbacks remain registered. They become inactive in
`run_worker()`'s `finally` block, so in-process scripted tests cannot leave an
active wall affecting subsequent worker or planner runs. Production workers
exit after one attempt. There is no import-time registration or planner hook.

`SoftWall.decide()` blocks the first `read_file` request per canonical existing
repository path, then permits subsequent requests. Own files, full-pack files,
and files successfully written during this attempt pass immediately. Host and
Docker workspace paths and Windows separators normalize to the same identity.
`bash_read_paths()` handles explicit readers and path globs conservatively;
exact command strings have independent confirmation state. Listings, pytest,
redirections, in-place edits, and commands with uncertain write effects pass.
This is a reading nudge, not a security sandbox; arbitrary shell programs are
not fully interpreted. Existing Aqours permissions still apply.

A `PostToolUse` callback records successful confirmations and tracks file-tool
writes. For Bash, metadata snapshots recognize created/changed files without
reading their contents; `ObservedExecutor` delegates the public executor
interface and supplies exit/timeout status for confirmation accounting.

`nodes/<id>/soft_wall_<attempt>.jsonl` records UTC time, tool-call id, tool,
path/command, canonical paths, and `blocked`/`allowed` decisions. Allowed events
are written only after successful tool completion. Each node's summary has
`soft_wall_blocked` and an ordered, unique `confirmed_reads` path list; totals
sum blocked calls and the lengths of those per-node lists. `reads_outside_pack`
counts successful confirmed file reads, including paths in recognized Bash
commands, with repeated reads counted again. Blocked, failed, and unfinished
reads do not count. With no soft-wall log, trace accounting requires a
successful `tool_result` paired with its `read_file` call. First-write model
call counting retains its existing semantics.
