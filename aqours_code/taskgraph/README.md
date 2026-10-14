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
| `Generator` | `kind`: `manual` \| `planner`; optional `planner_version`, `model`, `revision_mode`: `llm` \| `rule_assisted` \| `none`, `ablate` (planner v3: the properties `p1`-`p4` whose rules were turned off) |
| `Node` | `id` (`[A-Za-z0-9_-]+`), `title`, `kind`: `contract` \| `implement`, `goal` (non-empty), `edit_set`, `requires`, `requires_impl`, `provides`, `check`, `context_files`, optional `size`: `small` \| `medium` \| `large`, `run_after_failed_upstream` (default `false`; see [Flow](#flow)) |
| `EditSet` | `any_file` (default `false`; unrestricted files, single-node graphs only), `modify` (existing files), `create` (new files), optional `symbols` (functions/methods the node changes; an implement node listing a symbol here counts as its implementer for `requires_impl`, V12 and W5), optional `shared` (files of `modify` this node edits while other nodes edit other parts of them at the same time; see V5 and [Flow](#flow)) |
| `Check` | `commands`, `timeout_s` (integer > 0, default 300), optional `post_merge_commands` (the post-merge check; default: `commands`) |
| `Edge` | `from`, `to`, `type`: `interface` \| `full` \| `order`, `source`: `manual` \| `llm` \| `derived`, `reason` |
| `RevisionEntry` | `action`: `merge` \| `split` \| `add_edge` \| `remove_edge` \| `add_node` \| `other`, `nodes`, optional `into`, `reason` |

In Python, `Edge.from` is `Edge.from_`; it is always serialized as `from`.
Use `load_graph(path)` and `dump_graph(graph, path)` for JSON I/O.
`generator.ablate`, `check.post_merge_commands` and
`run_after_failed_upstream` are left out of the serialized graph while they
are unset (`null` or `false`), so a graph that does not use them reads and
writes exactly as before they existed.

Within one node, `modify`, `create`, `context_files`, `requires`,
`requires_impl`, `provides` and `edit_set.symbols` must not repeat an entry,
no path may appear in both `modify` and `create`, and no symbol may appear in
both `requires` and `requires_impl`. These are schema errors.

The exported JSON Schema describes every field and, for the Planner, adds a
`pattern` for symbols and `uniqueItems` for these lists. The Python
validators remain authoritative.

### Execution semantics

Every edge, whatever its `type`, means that the downstream node starts only
after the upstream node has **finished and been merged**. (The one exception
is a node with `run_after_failed_upstream`, which also starts after an
upstream node failed; see [Flow](#flow).) The Coordinator must follow this
rule; the validator relies on it when it accepts a symbol provided
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
| V4 | Every node has at least one non-empty check command; `check.post_merge_commands`, when given, too. |
| V5 | Two nodes that edit a common file (`modify ∪ create`) must be ordered: one is an ancestor of the other. A file both nodes list in `edit_set.shared` does not count: they edit different parts of it in parallel, and a conflict is resolved when merging. |
| V6 | Every `requires` symbol exists at the base commit or is provided by an ancestor (contract or implement). A node's own `provides` does not count. *Needs an index.* |
| V7 | Every node lists at least one file to edit, unless `any_file` is true. |
| V8 | `requires`, `requires_impl`, `provides` and `edit_set.symbols` are well-formed symbols. |
| V9 | An `interface` edge starts at a `contract` node. |
| V10 | Unless `any_file` is true, every `edit_set.symbols` and `provides` entry belongs to a file in the same node's `modify` or `create` (a node cannot provide a symbol in a file it does not edit). |
| V11 | Each new file is created by exactly one node. |
| V12 | For every `requires_impl` symbol, every implementer is an ancestor (the others are reported together: missing edge?). With no implementer, the symbol must exist at the base commit; a symbol only declared by contract nodes, or not defined anywhere, is an error. *Needs an index.* |
| V13 | `edit_set.any_file: true` is allowed only in a graph with exactly one node. |
| V14 | Every file in `edit_set.shared` is also in `edit_set.modify`. |
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
python -m aqours_code.taskgraph rescore <run_dir> --hidden-tests <dir> [--sandbox docker|none]
python -m aqours_code.taskgraph team <request.md> --repo <path> [...]
python -m aqours_code.taskgraph score <manifest.json> [--exclude <tests.txt>] [--out <dir>]
python -m aqours_code.taskgraph resynth <graph.json> --out <new_graph.json> [--ablate p1,p3]
```

`plan`, `resynth`, `run`, `audit`, `rescore`, `team` and `score` are described in their own sections below. `compare` prints
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
    [--merge-repairs 1] [--conflict-repairs 1]
    [--sandbox docker|none] [--sandbox-image aqours-code-eval:py311]
```

Check commands and `final_checks` run through `/bin/sh` in the sandbox
container (with `--sandbox none`, through the host's shell), so write them for
a POSIX shell; `--command-map` commands always run through the host's shell.
The process exits with `0` for `success`, `1` for any other status or an
invalid graph, and `2` for input or git errors.

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

### Sandbox

A worker must see only its own worktree, and nothing a worker wrote may run
on the host. Its file tools (`read_file`,
`write_file`, `edit_file`, `glob`) already refuse paths outside it; its
`bash` commands run, with `--sandbox docker` (the default), in a Docker
container started from `--sandbox-image` that mounts only the worktree at
`/workspace`, with no network, a read-only root file system, no capabilities,
a non-root user, 2 CPUs, 2 GiB of memory and 256 processes. A command may run
up to 600 s (and never past the worker's deadline); a command that times out
gets a fresh container on the same worktree for the next command. The
container is removed when the worker ends, and the coordinator removes any
leftover after the worker process exits, however it ended. The worker prompt
tells the agent that bash runs in a Linux container at `/workspace`, and to
use `/dev/null` and Linux commands, never `nul` or Windows commands.

A Windows habit such as `dir /b src 2>nul` creates a file named `nul` in the
container. On a Windows host that name is a device: git cannot commit the file
and a plain delete fails. So after every worker attempt, before the commit,
the coordinator deletes each file in the worktree whose name without its
extension is `CON`, `PRN`, `AUX`, `NUL`, `COM1`–`COM9` or `LPT1`–`LPT9` (in
any letter case, in any directory), on every host. No task needs such a file.
The deleted paths are recorded in the node's `removed_reserved_files` and in a
`reserved_files_removed` event.

Inside the container git commands fail (the worktree's `.git` file points to
a host path). Workers must not change repository state anyway; the
coordinator commits on the host.

Every check runs code that a worker wrote, so the checks run in the sandbox
too (`checks.py`). Each one gets a fresh container like a worker's: the same
image and limits, no network, and the directory being checked mounted at
`/workspace`. The container serves the commands of that one check and is then
removed. A command may run as long as the check's own timeout.

Inside the container every check command runs under `timeout`:
`timeout -k 5 <seconds> /bin/sh -c '<command>'`, with 15 seconds less than
the check's timeout (never less than half of it). A command that hangs is
then stopped from inside, and what it printed comes back: the result is a
timeout whose output ends with `[timed out after N s inside the sandbox]`
(`timeout` returned 124, or 137 when the command had to be killed). The
host's own timeout stays as the last resort; when only that one fires, the
container is removed and the output is usually lost. Checks on the host
(`--sandbox none`) are not wrapped.

| Check | Directory mounted at `/workspace` | `--sandbox docker` | `--sandbox none` |
| --- | --- | --- | --- |
| node check (after each attempt) | the node's worktree `wt/<id>/` | container | host |
| post-merge check | the integration clone `repo/` | container | host |
| `final_checks` | the integration clone `repo/` | container | host |
| hidden tests | the final checkout `final/` | container | host |

The hidden tests run as `python -m pytest`, with the image's Python in the
container and the Coordinator's own Python on the host. If the sandbox cannot
start or run a command, the check fails with `[sandbox error] ...` in its
output; it never falls back to the host. A check can only use what the image
provides: a test suite that needs a program the image lacks (a pager, a
compiler) fails there even on unchanged code, so run the repository's tests
in the image once before using it for a task.

Worker and planner processes start with `PYTHONUTF8=1` and
`PYTHONIOENCODING=utf-8`, and every output the Coordinator reads from a child
or from Docker is decoded as UTF-8 with undecodable bytes replaced, so a
character the host's default encoding lacks cannot fail a worker or planner
round.

Before a run starts, `run` checks that Docker runs and that the image exists.
If not, it exits with code 2 and explains how to build the image:

```bash
docker build -f evals/docker/Dockerfile -t aqours-code-eval:py311 .
```

It never falls back to `--sandbox none`. `--sandbox none` runs worker
commands and all checks on the host and is only for unit tests and
`--worker command` (which always runs on the host, and so do its checks); it
prints a warning. `config.json` and `summary.json` record `sandbox` (`docker`
or `none`), `sandbox_image`, and `checks_sandbox` (where the four checks ran:
`docker` or `none`). The planner needs no sandbox: it has no `bash`.

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
   order. A file both nodes list in `edit_set.shared` does not count as an
   overlap: such nodes edit different parts of it at the same time. When a
   node fails, its descendants are `skipped` (`upstream_failed`); unrelated
   nodes continue.

   **Running after a failed upstream node.** A node with
   `run_after_failed_upstream: true` is ready once every direct upstream node
   has *finished* (merged, failed or skipped), and is never skipped because
   of an upstream failure. Its prompt then ends with `Skip the registrations
   of these failed sub-tasks: <ids>`, naming the direct upstream nodes that
   were not merged. Planner v3 sets it only on `REG`, the node that registers
   the names other nodes defined (an export, a registry entry): a failed
   feature must not keep the others from being registered. Every other node
   behaves as before. Every git command on the
   shared clone runs under one lock; a merge, its post-merge check, and a
   possible undo run as one unit under a separate merge lock, so a slow
   post-merge check never blocks other nodes from starting or committing.
3. **Each node**, up to `--max-attempts` times (default 2): create
   `tg/node/<id>` and worktree `wt/<id>` from the last integration commit
   whose post-merge check passed (never from a merge still being checked, so
   an undone merge cannot leak into another branch), write the
   prompt, run the worker, delete reserved-name files (see
   [Sandbox](#sandbox)), put line endings back (below), and commit
   `attempt <n>`. The attempt fails on a
   worker error (`worker_error`), on a timeout without changes
   (`worker_timeout`), on no change since the start
   commit (`no_changes`), or on a failing check command run in the worktree
   (`check_failed`). A retry continues in the same worktree with the failure
   reason and the last 4000 characters of output in the prompt. A worker that
   times out after changing files may have finished its work, so its checks
   still run and decide the attempt. Each attempt's worker outcome is kept in
   `worker_reasons` (`""` when the worker ended normally).

   **Line endings.** An agent sometimes rewrites a file with `\r\n` line
   endings in a repository that uses `\n`; the diff is then the whole file,
   and it conflicts with any other change to that file. So after every worker
   attempt, in the same step that deletes reserved-name files, the
   Coordinator looks at the files that differ from the worktree's last
   commit. An existing text file that had no `\r\n` at the commit the node
   started from and has some now gets `\r\n` replaced by `\n`. So does a new
   text file, if no tracked text file at that commit has `\r\n`. A file that
   was CRLF stays CRLF, and a binary file (one with a NUL byte) is never
   touched. The restored paths are recorded in the node's
   `normalized_line_endings` and in a `line_endings_normalized` event.

   **A check that times out is run once more.** A timeout is often a stuck
   environment and not the code: the same commands on the same files pass
   seconds later. When a node check, a post-merge check or the final checks
   time out (a command did not finish; not when a test fails), the same check
   runs again at once, with nothing changed, and the second run decides. A
   post-merge check is run again while the merge lock is still held. Each
   check is run again at most once. If it times out again, the usual rule
   applies (a new attempt, a merge repair, or a failed node), and the prompt
   of that attempt or repair says that the check timed out and shows what it
   printed. A failed check is never run again. Each rerun adds one to the
   node's `check_timeout_retries` (the final checks only count in
   `check_timeout_retries_total`) and gives a `check_timeout_retry` event
   with `node`, `kind` (`node`, `post_merge` or `final`) and `ok` (whether
   the second run passed). The check's log file keeps both runs.
4. **Merge.** Squash-merge the node branch into `tg/integration` as one commit
   `[taskgraph] <id>: <title>`. A conflict resets the branch and, if the node
   has a conflict resolution left (below), is resolved once; otherwise the
   node fails (`merge_conflict`). The node's checks then run again on the integration
   branch, and a failure undoes the merge, so the integration branch stays
   clean. The post-merge check runs `check.post_merge_commands` when the node
   has them, and `check.commands` otherwise (planner v3 gives a node a narrow
   node check and the whole suite after merging when its effect is hard to
   predict). A failed post-merge check gets a repair round (below).

   **Conflict resolution.** Nodes that edit one file in parallel (`shared`)
   can conflict when the second one merges. Then, **still holding the merge
   lock**, the Coordinator merges the integration branch into the node's
   worktree with the conflict markers kept (the node's changes are measured
   from that integration commit from now on), and runs the same worker once
   more, its prompt naming the conflicting files and asking to keep both
   sides and run the tests that cover its work (the text is team's
   `CONFLICT_SECTION`, shared in `prompting.py`, with "other nodes" for
   "teammates"). Markers left in a file fail the node with `merge_conflict`,
   a worker failure with the worker's reason, and a failing node check with
   `conflict_check_failed`. Otherwise the merge is committed and the node is
   squash-merged again, which cannot conflict now, and its post-merge check
   runs as usual (a failure there gets the usual repair round). Holding the
   lock means the resolution starts from the newest integration state and
   nothing else merges meanwhile, so no second conflict can follow; the
   price is that other nodes' merges wait for that round. A node gets at
   most `--conflict-repairs` resolutions (default 1; 0 fails the node at
   once, as before). The round's worker time and tokens count for the node. Changed files outside `modify ∪ create` are recorded as
   `out_of_scope_files` but do not fail the node. For `any_file` nodes, the list
   stays empty.

   **Merge repair.** Two nodes can each be correct and still break together,
   on files that do not overlap, so no plan can foresee it (one registers a
   global listener, and the other's tests do not set up what the listener
   needs). When a node's post-merge check fails, the merge is undone and, if
   the node has a repair left (`--merge-repairs`, default 1; 0 fails the node
   at once with `post_merge_check_failed`):
   - the current integration branch is merged into the node's branch, so its
     worktree holds the other nodes' finished work plus its own: the state
     whose check just failed. A conflict here fails the node
     (`merge_conflict`);
   - the worker runs once more (the attempt number continues) with the
     post-merge check output under `# Your work broke after merging`: fix it
     in your own files, leave the other sub-tasks' code alone. The context
     pack is rebuilt from the new worktree; the soft wall applies as usual;
   - the node's own check runs in the worktree. If it fails, the node fails
     with `repair_check_failed`. If it passes, the node is squash-merged and
     checked again; another failure is `post_merge_check_failed`.

   The repair runs outside the merge lock, so other nodes keep merging; the
   node stays `running` and keeps its files. Repairs are separate from
   `--max-attempts`, which only covers failures of the node's own check. A
   repair happens only when it is needed, unlike an integration node, which
   would run every time and sit on the critical path. After a repair the
   node's `changed_files` and `diff.patch` are measured against the
   integration commit that was merged into its branch.
5. **Final stage.** Run every `final_checks` command on the integration branch
   and record each result. With `--hidden-tests DIR`, check out the
   integration HEAD into `final/`, copy `DIR` to `final/_hidden_tests/`, and
   run `python -m pytest -q _hidden_tests -p no:cacheprovider
   --junitxml=_hidden_tests.xml` there; the report is then moved to
   `hidden_tests.xml` in the run directory. Workers never see these tests.
   Like every check, both run in the sandbox (see [Sandbox](#sandbox)).
6. **Audit and clean up.** Scan the traces for escape attempts (see
   [Escape audit](#escape-audit)) and write `summary.json`. Only then delete
   `final/_hidden_tests/` and `wt/`, so that no later worker can find this
   run's answers. `final/` keeps the final code; `nodes/` keeps the diffs,
   logs and traces. A path that cannot be deleted does not fail the run: it
   gives a `cleanup_warning` event and a `warning: cleanup:` line on stderr.
   Delete that path by hand before the next run. `wall_time_s` does not
   include this clean-up.

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
  hidden_tests_rescored.xml/.txt   results of the last `rescore`, if any
  repo/                   clone; tg/integration is checked out here
  wt/<node_id>/           node worktrees (removed when the node ends; wt/ when the run ends)
  nodes/<node_id>/        prompt_<n>.md, worker_<n>.json, worker_<n>_config.json,
                          worker_<n>_stdout.txt, trace_<n>.jsonl, aqours_<n>/,
                          check_<n>.txt, post_merge_check.txt,
                          post_merge_check_repair_<k>.txt, diff.patch
  final/                  clean checkout of the integration HEAD for hidden tests
```

### events.jsonl

One JSON object per line with `ts` (ISO 8601, UTC), `t` (seconds since the
run started), `type`, and `node` where it applies. Types: `run_start`,
`node_ready`, `node_start`, `worker_start`, `worker_end` (with `ok`,
`reason`, `duration_s`, `model_calls`, `input_tokens`, `output_tokens`),
`check_start`, `check_end`, `merge_start`, `merge_end`,
`post_merge_check_end`, `merge_conflict` (with `files`),
`merge_conflict_resolved` (with `ok`), `merge_repair_start` (with `attempt`),
`merge_repair_end` (with `ok`: whether the node was merged after the repair),
`reserved_files_removed` (with `attempt`, `files`),
`line_endings_normalized` (with `attempt`, `files`),
`check_timeout_retry` (with `kind`, `ok`),
`node_merged`, `node_failed`, `node_skipped`,
`final_checks_end`, `hidden_tests_end`, `escape_audit_end` (with `total`),
`cleanup_warning` (with `path`, `error`), `run_end`.

### summary.json

- `run_id`, `status`, `wall_time_s`, `config`, `integration_commit`;
- `nodes.<id>`: `status`, `reason`, `attempts`, `worker_reasons`, `start_t`, `end_t`,
  `worker_time_s`, `check_time_s` (node and post-merge checks),
  `model_calls`, `input_tokens`, `output_tokens`, `changed_files`,
  `out_of_scope_files`, `merge_commit`, `error`, `escape_attempts`,
  `escape_samples`, `merge_repairs` (repair rounds that ran), `merge_repair_ok`
  (`true` if the node was merged after a repair, `false` if it still failed,
  `null` without a repair), `removed_reserved_files`,
  `check_timeout_retries` (node and post-merge checks that timed out and were
  run once more), `normalized_line_endings` (files put back to LF),
  `merge_conflicts` (merges that conflicted) and `conflict_resolved`
  (`true`/`false`, `null` without a conflict);
- `totals`: `model_calls`, `input_tokens`, `output_tokens`;
- `final_checks`: one entry per command (`command`, `ok`, `exit_code`,
  `timed_out`, `duration_s`);
- `hidden_tests`: `passed`, `failed`, `errors`, `skipped`, `exit_code`,
  `timed_out`, `duration_s`, or `null`; `parse_error` when pytest left no
  report (the counts are then all 0);
- `hidden_tests_rescored`: added by `rescore`: the same fields plus `sandbox`,
  `sandbox_image`, `hidden_tests` (the directory used) and `rescored_at`;
- `sandbox`, `sandbox_image`, `checks_sandbox`, `escape_attempts_total`,
  `merge_repairs_total`, `merge_conflicts_total`, `conflicts_resolved_total`,
  `check_timeout_retries_total` (all nodes plus the
  final checks).

### Rescoring the hidden tests

```bash
python -m aqours_code.taskgraph rescore <run directory> --hidden-tests <dir>
    [--sandbox docker|none] [--sandbox-image <image>]
```

runs the hidden tests again on the `final/` of a finished run, for a run
whose hidden tests could not run (a crashed interpreter, a missing image) or
when the tests themselves were corrected. It copies `<dir>` to
`final/_hidden_tests/`, runs pytest there exactly as `run` does, and deletes
the copy. The default is `--sandbox docker`, with the image the run used (or
`--sandbox-image`); without Docker it exits with `2` and runs nothing, unless
`--sandbox none` is given.

The results go to `hidden_tests_rescored.txt` and `.xml` and to a new
`hidden_tests_rescored` entry in `summary.json`. The run's own `hidden_tests`
entry and files are never changed; a second `rescore` replaces the first. It
prints the counts and exits with `0` when pytest produced a report, whatever
the tests' results, and with `1` when it did not (the output file says why).

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
    [--planner v3|v2] [--ablate p1,p2,p3,p4]
    [--final-check "python -m pytest -q tests"] [--timeout 1800] [--request-id <id>]
    [--fast-path-lines 500] [--no-fast-path] [--no-merge]
```

`--planner v3` (the default, `planner_version` `planner-v3.1`) is described in
[Planner v3](#planner-v3-the-model-reports-facts-the-program-decides-the-structure);
`--planner v2` runs planner v2, described in the rest of this section,
exactly as before (its graphs and reports still say `planner-v2`).
`--ablate` is for v3 only and `--no-merge` for v2 only; the other options
apply to both.

Planner v2 builds the graph in the three steps of the project proposal: a
draft from the request alone, grounding in the code, and a rule-based
revision. The model makes the decisions that need reading (what the request
asks for, which files each piece touches, whether a piece must be split,
whether an interface is missing); the program makes the decisions that follow
from the files (which nodes are ordered, which nodes are merged).

v2 changes only what Step 2 costs, not what it decides. Planning took a
large part of a run (about 240 of 520 s on the click batch, 235 of 670 s on
the job platform): on the click batch Step 2 read the source files one by
one, in full, and every one of its 26 calls carried all of them again (about
980,000 input tokens); on the job platform it wrote goals of about 1,700
characters that repeated the request. v2 gives Step 2 an
[outline of the code](#the-repository-outline) to plan from and asks for
[short goals](#goals). The fast path and its threshold, Step 1, the checks
P1-P7 and the program revision of Step 3 are exactly those of v1, so what is
split, and when, is decided as before.

| Proposal property | Where it is decided |
| --- | --- |
| Property 1, split: independent work is separate | Step 1 (one item per feature) and Step 2 (split an item the code shows to be two independent pieces) |
| Property 1, merge: coupled work is one node | Step 3, rule M1 (never by the model) |
| Property 2: a missing shared interface gets a contract | Step 2 (the model adds a contract node, with a `reason`) |
| Property 3: work on the same code is ordered | `derive_edges()` (order edges), then Step 3 merges what can only queue |

`plan` clones the HEAD of `--repo` into a temporary directory (uncommitted
changes are not seen; the original repository is not touched); `base_commit`
is that HEAD. Both model steps use the worker's child-process runner
(`python -m aqours_code.taskgraph.planner_entry`, `CountingClient`, the same
model configuration) on the clone.

### Step 1: draft (model, request only)

One agent with no file tools (`planner_entry --stage draft`; only `compact`
is allowed) gets [`planner_draft_prompt.md`](planner_draft_prompt.md), the
request, and the list of repository files with their line counts, never
their content. It answers with the pieces of work the request names and an
estimate of the lines the whole request will add or change:

```json
{"estimated_changed_lines": 1200,
 "items": [{"id": "1", "title": "Short name", "description": "What it must achieve."}]}
```

(The prompt itself shows `<number>` instead of a figure, so that the model
does not copy an example value near the fast-path threshold.)

`estimated_changed_lines` must be a non-negative integer and `items` must
have unique ids. An answer that cannot be read is retried once with the
errors; a second failure fails the whole `plan` (exit code 1, the report is
written, no graph).

**Fast path.** When the estimate is below `--fast-path-lines` (default 500),
`plan` skips Steps 2 and 3 and writes the single-agent graph: the same graph
as the `single` command (goal = the request plus the generic sentence,
`any_file: true`, no context pack), built by the same function. Only the
metadata differ: `generator` is `{"kind": "planner", "planner_version":
"planner-v2", ...}` and `revision_log` has one `other` entry `fast path:
estimated N changed lines < threshold T`. The reason: every node costs about
60 s of fixed overhead (context reading, checks, merge) and planning itself
about 2 minutes, so splitting a change of a few hundred lines cannot beat one
agent. `--no-fast-path` turns it off. This estimate is the only one the
planner makes; nodes carry no size or cost estimates.

### Step 2: grounding (model, read-only tools)

A second agent (`read_file`, `glob`, `compact`) gets
[`planner_prompt.md`](planner_prompt.md), the request, the draft items (a
`# Draft items` section), the file list, the repository outline (a
`# Repository outline` section) and the final checks. It plans from the
outline and answers with a draft graph: only nodes, no edges; the last
```` ```json ```` block of the final answer is the draft.

#### The repository outline

`outline.py` lists, with `ast` and without a model, what every source file
defines. A source file is a Python file that is not a test: not under a
`tests/` directory, not named `test_*.py`, and not `conftest.py`.

```text
## src/click/types.py (1437 lines)
_ValueT = ... (L29)
class ParamType(t.Generic[_ValueT_co, _InputT_contra], abc.ABC) (L65-L239): Represents the type of a parameter. Validates and converts values
  fields: is_composite, arity, name, envvar_list_splitter (L94-L106)
  def to_info_dict(self) -> ParamTypeInfoDict (L122): Gather information that could be useful for a tool generating
  def fail(self, message: str, param: Parameter | None=None, ctx: Context | None=None) -> t.NoReturn (L215): Helper method to fail with an invalid value message.
class DateTime(ParamType[datetime]) (L522-L590): The DateTime type converts date strings into `datetime` objects.
def convert_type(ty: t.Any | None=None, default: t.Any | None=None) -> ParamType[t.Any] (L1356): Find the most appropriate :class:`ParamType` for the given Python
```

- A file's header has its path and line count. A file that cannot be parsed
  has only the header and `(could not be parsed)`.
- Top-level classes with their bases and line range, their methods, top-level
  functions, and the names assigned at the top level, each with its line
  number. Signatures (parameters, defaults, annotations, return type) come
  from `ast.unparse`. A docstring gives its first line, cut to 100
  characters.
- A class also gets one `fields:` line with the names assigned in its body
  (dataclass fields, enum members, class attributes), because the symbols a
  contract adds are often such fields.
- Definitions inside `if`/`try` blocks at the top level are listed;
  `typing.overload` stubs, nested functions and nested classes are not.

The outline is at most 60,000 characters. A longer one is shortened in
steps, each including the ones before it, until it fits:

| `outline_reduction` | Left out |
| --- | --- |
| `none` | nothing |
| `no_docstrings` | the docstring lines |
| `no_private` | names that start with `_` (not `__init__` and other dunder names) |
| `no_methods` | methods, `fields:` lines and top-level names: only classes and top-level functions remain |
| `truncated` | whole files from the end of the list; a last line names the files that are not listed |

A shortened outline starts with a line that says what is left out. The
report records `outline_chars`, `outline_reduction` and
`outline_omitted_files`, and `plan` prints the first two.

#### Reading the code

The prompt tells the agent to plan from the outline, to read a file only for
a detail the outline does not show (how a function works inside), to read
only the lines it needs, and not to read test files: their names in the file
list are enough to choose where new tests go. Two facts about the runtime
make this cheap, and the prompt states both:

- `read_file` reads part of a file: `offset` is the number of lines to skip
  and `limit` the number of lines to return, so lines 120 to 160 are `offset`
  119 and `limit` 41;
- every tool call of one answer is executed before the next model call, so
  the agent is told to ask for all the files it needs in one turn.

```json
{"conventions": ["..."],
 "nodes": [{"id": "A", "title": "...", "kind": "contract | implement", "item": "1",
            "reason": "...", "goal": "...",
            "modify": [], "create": [], "provides": [], "requires": [],
            "requires_impl": [], "check": ["python -m pytest -q tests"],
            "context_files": []}]}
```

- `item`: the draft item an `implement` node belongs to. By default one item
  is one node. The model may **split** an item when the code shows a large,
  independent piece of it, and may **add a contract** when several nodes need
  a shared new interface, each with a `reason`. It never merges two items,
  even when they edit the same function: it lists truthfully which files each
  node edits, and Step 3 decides.
- `item` and `reason` do not enter the graph's nodes (the schema is
  unchanged). They become `revision_log` entries, placed first: `split` for an
  item with several implement nodes, `add_node` for each contract.
- `conventions` (optional) lists the repository's rules that every node must
  follow, for example "the current time comes only from the injected clock:
  functions that need it take a `now` argument". The program appends them to
  every node's goal as a `Repository conventions:` section.

The program completes the draft into a graph (`generator` `{"kind":
"planner", "planner_version": "planner-v2", "model": ...}`), runs
`derive_edges()` and `validate()`, and applies the planner-only checks
(`planner_checks()`, `item_checks()`, `implementation_checks()` and
`contract_checks()`; errors like V1-V13, not part of
`validate()`, so hand-written graphs are not held to them):

| Code | Error |
| --- | --- |
| `P1` | a contract node is an ancestor of another contract node (contracts are not ordered: merge them) |
| `P2` | every file a node modifies or creates is under `tests/` (a test-only node: remove it or fold its work into the related nodes) |
| `P3` | a contract node creates or modifies a file under `tests/` (a contract's check only runs the existing tests) |
| `P4` | a draft item has no `implement` node; an `implement` node has no `item` or one that is not a draft item; a `contract` node has an `item` |
| `P5` | an item is split into several nodes, or there is a `contract` node, without a `reason` |
| `P6` | an `implement` node's `requires_impl` makes it wait for implement nodes of two or more other draft items (it has become an integration node) |
| `P7` | a `contract` node creates a file (outside `tests/`) that no `implement` node has in its `modify` (the contract would have to implement it) |

P6 keeps a node that only exposes other features (REST routes, pages, a CLI)
parallel with them: it depends on the contract's interfaces (`requires`) and
tests itself against a fake, as E and F do in the proposal's example. Waiting
for the implementation of one other item (statistics built on an audit log,
say) is allowed, and so is waiting for another node of the same item. P6 is
not checked again after Step 3, where merged nodes have no item.

P7 keeps the contract thin. A contract writes interfaces only: fields,
signatures with docstrings, and bodies that run but do nothing useful. It
never completes an implementation, not even of a small shared helper: the
helper gets its signature in the contract, one implement node writes its
body, and the others `require` it. A contract that also implements shared
modules is slow, and every other node waits for it. The prompt also asks for
an owner of every existing entry point the request changes (a route or
function that must accept new fields): its file must be in some node's
`modify`. P7 is not checked after Step 3 either (M2 may have merged the
contract away).

On any error the agent runs again with its previous draft and the errors (a
fresh agent; the prompt repeats the request and the draft items), at most
twice. If errors remain, the run fails: the last graph that could be built
and the report are still written, and the exit code is 1.

#### Goals

A worker's prompt already has the whole request (`# Overall request`), so a
goal does not repeat it. It names the part of the request the node
implements, by quoting its heading or item title, and adds only what the
request does not say: where in the code the change goes, which existing
functions or classes to use, and decisions the node shares with other nodes;
under 800 characters. A specification file is not retold either: its
sections go in `context_files` as `SPEC.md#Heading`. The goals of the
prompt's examples are written this way, for instance `"Star ratings": fill in
recipes/ratings.py::rate, which RecipeBook.rate already calls. ...`.

A long goal is never an error. The report's `goal_chars` maps node ids to
the final goals' character counts, including appended conventions, and
`long_goals` holds those over 1,500, and `plan` prints a note for each of them
(`goal repeats the request; the worker already receives it`).

### Step 3: revision (program, `revise.py`)

`revise_graph()` is a pure function on the valid graph of Step 2. It applies
two rules until neither fits, scanning edges and nodes in list order, so the
result is deterministic:

- **M1**: for an edge X -> Y, X and Y become one node when all four hold:
  1. X and Y are both `implement` nodes;
  2. they edit a common file (`modify ∪ create`);
  3. every other direct predecessor of Y is an ancestor of X (this keeps the
     graph acyclic and never makes X's work start later);
  4. every other direct successor of X is already a descendant of Y, so it
     had to wait for Y anyway. Without this, a node W that only needs X would
     have to wait for Y's work as well.

  When 1 and 2 hold and 3 or 4 does not, the nodes stay apart: an `other`
  entry gives the reason, and the report's `not_merged` lists the pair with
  `blocked_by` (the predecessors of condition 3) and `delayed` (the
  successors of condition 4).
- **M2**: a `contract` node C whose only direct successor is Y, where every
  other direct predecessor of Y is an ancestor of C, is merged into Y; the
  result is an `implement` node.

The merged node (members in order, X before Y):

| Field | Merged as |
| --- | --- |
| `id` | member ids joined by `_` (`C_D`), with a numeric suffix on a clash |
| `title` | member titles joined by ` + ` |
| `goal` | `This sub-task combines N parts. Do them all, in this order.` and then `Part k (<id>: <title>):` with each member's own goal; the conventions appear once, at the end |
| `edit_set.create`, `symbols`, `provides` | unions |
| `edit_set.modify` | union, without files a member creates |
| `requires` / `requires_impl` | unions, without symbols a member provides; a symbol in both stays in `requires_impl` |
| `check.commands` | union in order; `timeout_s` is the maximum |
| `context_files` | union in order, without files a member creates (a node cannot have its own new file as context) |

Edges to or from a member point to the merged node, edges between members
disappear, and of several edges between the same two nodes the strongest
stays (`full` > `interface` > `order`). The merged node takes the place of
its first member. Every merge is a `merge` entry in `revision_log` (`nodes`:
the members, `into`: the new id, `reason`: the rule and the shared files).

After the merges the graph is validated again (V1-V13 and P1-P3). An error
there is a bug in the rules, not something the model can fix: the graph from
before the merge is written, the report gets `REVISE` errors, and the exit
code is 1. `generator.revision_mode` is `rule_assisted` when something was
merged, otherwise `llm` after a revision round and `none` without one.

### Output

- `--out`: the final graph;
- `<out>.unmerged.json`: the graph before Step 3 (edges derived), for
  comparison. `--no-merge` skips Step 3: `--out` is then that graph and no
  `.unmerged.json` is written;
- `<out>.report.json`: `draft_items`, `estimated_changed_lines`, `fast_path`
  (`taken`, `estimated_changed_lines`, `threshold`; `threshold` is null with
  `--no-fast-path`), `stage1` (every Step 1 round: answer, errors, agent
  statistics), `rounds` (every Step 2 round: answer, draft JSON, conventions,
  errors, warnings, agent statistics), `node_items` (each node's `item` and
  `reason`), `merges` (members, new id, rule, shared files) and `not_merged`,
  `nodes_before_merge` and `nodes`, `errors` (of the step that failed),
  `goal_chars` and `long_goals`, `outline_chars`, `outline_reduction` and
  `outline_omitted_files`, `totals` and `duration_s` (Step 1 included) and
  `stage_totals` (Step 1 and Step 2 separately);
- `<out>.logs/` (Step 2) and `<out>.logs/draft/` (Step 1): each round's
  config, trace and stdout.

The terminal summary shows the number of draft items and the estimate,
whether the fast path was taken, the node count before and after merging,
and the members of each merge. `--timeout` applies to each model round. Exit
codes: `0` success, `1` errors remained, `2` input or git errors. Error codes
in the report besides V1-V13 and P1-P7: `FORMAT` (no JSON block, invalid
JSON, or a draft or schema mismatch), `AGENT` (a planner agent failed or
timed out) and `REVISE`.

`experiments/taskgraph/planner_eval/` runs the planner on the experiment
repositories and compares the result with the hand-written graphs.

### Planner v3: the model reports facts, the program decides the structure

The four properties of the project proposal are meant to **decide the shape
of the graph from the features of the task**. In v2 every structural choice
is the model's (Step 2, from its prompt and examples), and the program only
checks that the graph is valid. The result was the same template for every
task, "build a heavy contract, then run the workers in parallel":

- on the click batch the contract was not needed: four nodes waited for it,
  at a cost of more than 400,000 tokens;
- on the job platform the contract ran 208-305 s, the longest stretch of the
  critical path;
- every node check and every post-merge check ran the whole test suite.

v3 splits the work differently:

- **The model (Step 2) reports task facts and makes local judgements:** where
  each piece of work changes the code, what it provides and uses, whether it
  needs an interface or the real behaviour, how large it is, which pieces
  belong together. These judgements can still be wrong; the program checks
  them, and takes the conservative choice where they are uncertain.
- **The program (Step 3, `synthesize.py`) decides the structure** from those
  facts, by the rules of the four properties. The same rules give a single
  node, plain parallel nodes, interfaces first and then parallel nodes, or
  nodes that wait for each other. Every decision records its property, its
  rule and its grounds, and every property can be turned off.

**Core principle: first satisfy the real dependencies, then use rules to cut
the coordination cost.** The thresholds below are initial assumptions, not
proven optima.

Step 1 (draft items, fast path) is the same as in v2. v3 does not run v2's
merges M1 and M2.

#### Step 2: task facts (`facts.py`, `planner_facts_prompt.md`)

The agent (read-only tools, as in v2) gets
[`planner_facts_prompt.md`](planner_facts_prompt.md), the request, the draft
items, the file list and the outline. The prompt keeps v2's instructions on
reading (plan from the outline, read parts of files with `offset`/`limit`,
several files per turn), conventions and short goals, and drops every
structural rule (how to build a contract, when to split, which checks to
run, "no integration node"). Instead it says: *You describe the work; you
do not design the task graph. The program decides which units become nodes,
which shared edits are made first or last, what runs in parallel, and how
each node is checked, from what you report.* Its example (a toy recipe book)
shows facts only, no structure.

```json
{
  "conventions": ["A rule every unit must follow."],
  "units": [{
    "id": "D", "item": "1", "title": "Duration parameter type",
    "goal": "The part of the request, then where in the code it goes (under 800 characters).",
    "size": 150,
    "edits": [
      {"file": "src/click/types.py", "kind": "logic",
       "symbols": ["src/click/types.py::Duration"], "lines": 120, "standalone": false},
      {"file": "src/click/__init__.py", "kind": "registration",
       "symbols": ["src/click/__init__.py::Duration"], "lines": 1,
       "note": "export Duration from click"}],
    "create": ["tests/test_types/test_duration.py"],
    "provides": ["src/click/types.py::Duration", "src/click/__init__.py::Duration"],
    "uses": [{"symbol": "src/click/__init__.py::Duration", "need": "interface"}],
    "tests": ["tests/test_types/test_duration.py"],
    "context_files": ["README.md"]}],
  "together": [{"units": ["P1", "P2"], "reason": "Why one cannot stand without the other."}]
}
```

| Field | Meaning |
| --- | --- |
| unit | one piece of work. By default one draft item is one unit; an item with large, unrelated parts in different files may have several units (same `item`). Every draft item has at least one. |
| `size` | estimated changed lines, tests included |
| `edits` | changes to **existing** files, one per file. `kind`: `logic` (implements behaviour) or `registration` (only enters a name into a list or table: an export, `__all__`, a registry, a route table). `symbols`: for `logic`, the classes and functions changed or added (to see whether two units change the same function); a change inside a class names the member (`Class.method`, `Class.field`), and the class itself only when the whole class is new or rewritten; for `registration`, the registered names (`src/click/__init__.py::Duration`), which other units may `use`. `lines`: lines changed in that file. `standalone` (`logic` only): the edit can be written with only the code already in the repository, without the unit's other edits or other units; `false` when unsure. `note`: what a registration registers. |
| `create` | new files, tests included |
| `provides` | symbols added or changed for others, registered names included |
| `uses` | symbols another unit provides, or this unit registers. `need: "interface"`: the name and signature are enough; this is the usual case (creating, reading or raising a class, a field or an exception, calling a function whose result a small fake can stand in for). `need: "behavior"`: the unit's code or tests depend on a result only the provider's real code produces, so a fake would make the tests meaningless; `why` names that result in one sentence (no result to name: the need is `interface`). A use names the method called, not its class; a use of a class means its constructor and fields. A unit that imports its own symbol through its registered name (a test with `from click import Duration`) lists that name too. Facts written before `why` existed still read. |
| `tests` | the unit's own test files; empty when nothing can be observed on its own (pure wiring) |
| `together` | units that are one piece of work logically, with a reason; editing the same file is not a reason |

**Checks of the facts** (errors go back to the model in the usual revision
rounds, at most `MAX_REVISIONS`):

| Code | Error |
| --- | --- |
| `F1` | a unit's `item` is not a draft item, or a draft item has no unit |
| `F2` | a unit id is repeated |
| `F3` | an edit names a file that does not exist, the same file twice, or a symbol of another file; a unit edits and creates nothing |
| `F4` | a created file exists, is created by several units, or is also in `edits` |
| `F5` | a provided symbol is not in a file of the unit's `edits` or `create`; a registered name is not in the `symbols` of its registration edit |
| `F6` | `need` is neither `interface` nor `behavior`; a used symbol is provided by no unit |
| `F7` | a test file is neither in the unit's `create` nor in its `edits` |
| `F8` | `together` names an unknown unit |
| `F9` | a context file neither exists nor is created by a unit; a context heading does not exist |

One correction the program makes itself and records: a `registration` edit
of more than 5 lines is treated as `logic` with `standalone: false`
(P3/`registration_too_large`).

#### Step 3: the graph (`synthesize.py`)

Input: the facts, the repository index, `final_checks` and the imports of
the existing test files (`affected.py`). Output: the graph and `decisions`,
one per structural choice: `property` (P1-P4), `rule`, the units and nodes
involved, the numbers used, and a reason.

The thresholds are module constants. They are **initial assumptions**. Their
basis: a worker's fixed cost (starting, reading the code, checking) is about
a minute, as much as writing a few dozen lines directly.

| Constant | Value | Use |
| --- | --- | --- |
| `SMALL_UNIT_LINES` | 30 | P1: a smaller unit joins a related one |
| `WAIT_LINES` | 60 | P2: when the provider or the user is smaller, the user waits instead of putting the interface first |
| `SMALL_SHARED_EDIT_LINES` | 20 | P3: the largest shared edit that may be made first |
| `AFFECTED_TESTS_MAX` | 30 | P4: more affected test files run the whole suite |

**Step 0: contradictions.** The `uses` form a graph: B uses what A provides
gives A -> B, for both needs. A cycle made only of `behavior` uses (each needs
the other's real result) means the units cannot stand on their own: they are
merged (P1/`mutual_behavior_merged`). A cycle with an `interface` use is left
to step 2, where that interface goes first and opens the cycle. This step is
run again after the merges of step 1, since merges can close new cycles.

**Step 1, property 1: which nodes.**

1. Every unit starts as its own group.
2. Groups named in `together` are merged (P1/`together`).
3. A group smaller than `SMALL_UNIT_LINES` that is related to other groups
   (a shared file, or uses between them) joins the one it is most related to
   (shared files plus distinct symbols used between them; the first in order
   on a tie) (P1/`small_merged`); a small group related to none stays alone
   (P1/`small_kept`). A file on which both groups only make `registration`
   edits is not a shared file here: an export next to another export is no
   reason to do two pieces of work together.
4. Groups whose `logic` edits name the same symbol are merged: two workers
   cannot change one function apart (P3/`same_symbol_merged`; done here
   because it decides the nodes).
5. One group left: the single-agent graph of the fast path (`solve`, any
   file), and nothing else is done (P1/`single_unit`).

A merged group has its edits by file, its sizes added, and its goals one
after the other (`merged_goal`, as in v2). From here on, "unit" means group.

**A behavior use of a data class (P2).** Before step 0, the program checks
one kind of `behavior` use: a use of a class (a qualified name without a
dot, starting with an upper-case letter and having a lower-case one:
`RateLimit` and `NotFound`, not `SCHEMA`) that only holds data, because no
unit lists one of its methods or attributes (`Class.name`) in `provides` or
in an edit's `symbols`. Using such a class means its constructor and fields,
which exist as soon as its interface does, so the use counts as `interface`
(P2/`class_use_interface`). When some unit lists a method, the class's
behaviour lives there and the model's `behavior` stands. A `behavior` use
without a `why` is only listed in the report (`behavior_without_why`); it is
no error and starts no revision round. `--ablate p2` skips this check.

**Step 2, property 2: real dependencies first.** For each use (user B,
symbol s, provider A):

| Case | Structure | Record |
| --- | --- | --- |
| `need: "behavior"` | B waits for A (`requires_impl`, a `full` edge) | P2/`wait_for_behavior` |
| `need: "interface"`, A or B smaller than `WAIT_LINES` | B waits for A | P2/`wait_small` |
| `need: "interface"`, otherwise | the interface of s (signature, docstring, a body that runs) goes into the node `PRE`; A and B both start after `PRE` | P2/`interface_first` |

An `interface` use that lies on a cycle of uses never waits (waiting would
close the cycle): its interface goes into `PRE` whatever the sizes. That is
how step 2 opens the cycles step 0 left.

For a registered name r (A registers r for its symbol s, found as A's symbol
with the same last name in another file), **who needs r decides where the
registration goes**, not the kind of edit:

| Who needs r | Structure | Record |
| --- | --- | --- |
| nobody | **last**: the registration leaves A for the node `REG`, which runs after A | P2/`registration_last` |
| only A's own group, or other units that wait for A anyway (behavior, or small) | **kept**: A registers it itself | P2/`registration_kept` |
| another unit that does not wait for A | **first**: the registration and the interface of s go into `PRE` (registering a name that does not exist yet would break importing the package, so the interface comes along); A and the users start after `PRE` | P2/`registration_first` |

A registration that is all of its group's work, or whose symbol s cannot be
found, is kept, and its users wait for A (P2/`registration_kept`,
P2/`registration_target_unknown`).

**Step 2c (property 3): registrations several nodes keep in one file.** A
registration kept in its node (its own tests, or users that wait for it,
need the name) is ordered with every other node that edits the same file,
and the order holds back the *whole* node, not just its few lines. So when
two or more nodes keep registrations in one file, each of those
registrations goes into `PRE` together with the interface of what it
registers, exactly as for `registration_first`, and the node starts after
`PRE` instead of queuing (P3/`registration_first_shared`). A registration
whose symbol cannot be found (no interface can go first), or that is all of
its node's work, stays and is ordered; the record says that the node queues
as a whole (P3/`registration_kept_ordered`). A file on which only one node
keeps registrations is left as it is. With `p2` or `p3` turned off this step
does not run.

After step 2, a dependency that still forms a cycle (through waits, `PRE`,
`REG`, and context files created by another unit) is a contradiction in the
facts: the model revises them, with the units on the cycle and what each uses
(`CONFLICT`).

**Step 3, property 3: shared edits.** For each existing file that two or
more groups still edit:

1. same function: already merged in step 1;
2. registrations that several groups still keep in the file after step 2c
   (symbol unknown, or all of the group's work) queue one after the other
   (P3/`ordered`);
3. a `logic` edit moves into `PRE` only when all three hold: at most
   `SMALL_SHARED_EDIT_LINES` lines, `standalone: true`, and another group
   also edits the file (P3/`small_shared_edit_first`). It stays when it is
   all of its group's work. **Few lines alone are no reason to go first;**
4. **different functions of one file run in parallel** (step 3b): a file
   two or more nodes (not `PRE` or `REG`) still edit is edited in parallel
   when every edit there is `logic`, lists its `symbols`, and no two nodes'
   symbols overlap (the same symbol, or a class and one of its members:
   `runner.py::Runner` and `runner.py::Runner.subscribe` overlap). The file
   goes into each node's `edit_set.shared`, and each goal says which parts
   the other nodes change at the same time and that a merge conflict will be
   handed back to resolve (P3/`parallel_different_symbols`). Ordering such
   nodes would make whole nodes queue for a file they touch in different
   places; a rare conflict is cheaper, and the Coordinator resolves it once
   (see [Flow](#flow)). `PRE` and `REG` keep their order with the others;
   the same function is still one node (step 1);
5. everything else (a registration, an edit without `symbols`, overlapping
   symbols) stays ordered: `derive_edges()` orders the nodes on the file
   (P3/`ordered`; the reason names which case it is).

**`PRE` and `REG`** exist only when something is put into them, at most one
of each.

- `PRE` (`kind: "contract"`, first in the graph) holds only what steps 2
  and 3 moved there: the chosen interfaces, the registrations that go first
  (also those of step 2c), the small shared edits. It writes no stubs for other symbols, no tests,
  and creates no module of a unit, except the module of a chosen interface
  (`PRE` then creates the file and the unit `modify`s it). Its goal is
  written by the program: one item per interface, registration or edit, with
  who implements it and who uses it.
- `REG` (`kind: "implement"`, last) holds only the registrations that go
  last; its goal lists them with their notes. It has
  `run_after_failed_upstream`, so a failed feature does not keep the others
  from being registered (the Coordinator names the failed ones in its
  prompt).
- A unit whose registration moved to `REG` gets a sentence at the end of its
  goal: `Another step registers <name> in <file>; do not edit that file, and
  import from <module> in your tests.` A unit whose edit moved to `PRE` is
  told that an earlier step makes it.

**Step 4, property 4: how each node is checked.**

- Affected tests: `affected.py` reads the `import` statements of every
  Python file of the repository with `ast` (also imports inside functions,
  and relative imports). Module names are mapped to files from the
  repository root (a top-level package) and `src/` (the `src` layout); a
  file under `tests/` also resolves names from its own directory, as pytest
  does, while a source module does not (`import types` in
  `src/click/core.py` is the standard library). A test file is affected by
  a node when it imports, **directly or indirectly**, a file the node
  changes: the imports are followed from the test file through the source
  modules, `__init__.py` included, so `import click` reaches every module
  `src/click/__init__.py` imports (import cycles are followed once). A
  `conftest.py` counts as imported by the test files in its directory and
  below, and a changed test file affects itself.
- Node check (`check.commands`): a node with tests of its own runs
  `python -m pytest -q <own tests> <affected tests>`; any other node
  (`PRE` and `REG` too) runs `python -m pytest -q <affected tests>`, or,
  without affected tests, `python -m compileall -q <its .py files>`. More
  than `AFFECTED_TESTS_MAX` affected test files, or any Python file (test or
  source) whose imports cannot be read because it does not parse, run
  `final_checks` (P4/`full_suite_fallback`): which tests a change reaches is
  then unknown. The other
  cases are recorded as P4/`affected_tests` and P4/`compile_only`.
- Post-merge check (`check.post_merge_commands`): a node that edits a file
  another node also edits, and `PRE` and `REG`, run `final_checks`, because
  the effect of their merge is the hardest to predict
  (P4/`shared_file_full_check`). Every other node runs its node check again
  (no `post_merge_commands`).
- The final checks are unchanged: the whole suite.

When `--final-check` is not given, `git diff --check` stands in for the
whole suite, as in the `single` command.

**Step 5: the graph.** Nodes get their unit's id (a merged group:
`_new_id`, member ids joined by `_`); `PRE` and `REG` come first and last.
The decisions become explicit edges (`PRE -> X` `interface`, waits `full`,
`X -> REG` `full`; all `source: "derived"`), `derive_edges()` adds the
rest (the order on shared files, context files), and `validate()` checks
the graph. Every goal ends with the conventions, as in v2.

**Step 6, property 1: parallel enough?** Splitting pays only when enough of
the work can run at the same time. Once the graph is built and valid, the
program measures it:

- a node's work: its units' `size`, less the `lines` of their edits that
  moved to `PRE` or `REG`; `PRE`: the lines moved into it plus
  `INTERFACE_LINES` = 5 per interface (an initial assumption); `REG`: the
  lines of the registrations moved into it;
- the **critical path**: the chain of nodes along the edges (`interface`,
  `full` and `order` alike) whose work adds up to the most. However many
  workers run, the graph takes at least that long;
- the **ratio**: critical path / all the work. Near 1 the graph is a chain.

When the ratio is above `PARALLEL_PATH_MAX` = 0.7, the single-agent graph is
used instead (P1/`low_parallelism_single`); the reason names the chain, both
numbers, the ratio and the threshold. Otherwise P1/`parallel_enough`
records the same numbers. 0.7 is an initial assumption: at 0.7 the split
graph is at most 1 / 0.7, about 1.43 times, faster than one agent, which is
about what the extra nodes' fixed costs (starting, reading the code,
checking, merging) take away. The decisions recorded before a
`low_parallelism_single` describe the split graph that was not used.
`--ablate p1` keeps the graph split (the decision is `ablated`, with the same
numbers).

**Where a problem comes from** decides who fixes it:

- the program broke one of its own invariants while moving edits or making
  dependencies (a file or new file nobody is responsible for, a symbol nobody
  provides any more, a unit in no group): `synthesize()` checks the
  invariants after every step and raises `SynthesisError`, naming the
  invariant and the latest decisions. The model is not asked; the run fails
  with a `SYNTH` error;
- the facts themselves cause it (a cycle left after step 2, or an edge
  `derive_edges()` skipped for a cycle so that `validate()` fails): the
  model revises the facts (`CONFLICT`).

#### Ablation

`plan --ablate p1,p2,p3,p4` (any of them) turns properties off. Each falls
back to its simplest behaviour, its decisions are recorded with the rule
`ablated`, and the graph says so in `generator.ablate` (and the report in
`ablate`):

| Off | Fallback |
| --- | --- |
| `p1` | `together` is ignored, small units are not merged, and a graph that is not parallel enough stays split (cycles of `behavior` uses are still merged, or the graph could not hold) |
| `p2` | every use waits (no class check); every registration stays with its unit; no `PRE` for interfaces. A cycle of uses of any need is then merged like a `behavior` cycle, since no interface can open it |
| `p3` | no merge of units changing the same function, no shared edit moved, no parallel edits of one file: the same file is only ordered |
| `p4` | every node check and every post-merge check runs the whole suite |

#### Output

The graph (`generator`: `planner_version` `planner-v3.1`, `revision_mode`
`llm` after a revision round, else `none`, and `ablate`), and
`<out>.report.json` with, besides the fields v2 also writes (`draft_items`,
`fast_path`, `stage1`, `rounds`, `errors`, `goal_chars`, `long_goals`, the
outline fields, `totals`, `stage_totals`, `duration_s`):

- `planner` (`v3`), `facts` (the facts of the round that built the graph),
  `units`, `conventions`;
- `decisions` (the full list) and `decision_counts` (per property);
- `node_units` (node id -> the units it is made of; for `PRE` and `REG`, the
  units whose parts they took over), `pre_node` and `reg_node`;
- `ablate`;
- `parallelism` (step 6): `node_work`, `total`, `critical`, `ratio`, `path`,
  `threshold` and `collapsed`, also when the graph became the single-agent
  graph (the figures are those of the split graph); `null` when the units
  formed one group before any graph was built;
- `behavior_without_why`: the `behavior` uses (unit, symbol) the model gave
  no `why` for;
- `resynth_of` (only from `resynth`): the graph whose facts were used;
- `revision_rounds` and `revision_reasons` (the error codes of every round
  that had to be revised);
- `test_imports` (`files`: test files read, `sources`: other Python files
  read, `unresolved`: files whose imports could not be read).

There is no `.unmerged.json`: v3 does not merge after the fact. The terminal
summary adds the node and edge counts, whether there is a `PRE` and a `REG`,
the number of decisions per property, and the parallelism of step 6:

```text
nodes: 7  edges: 16  PRE: PRE  REG: no  revision rounds: 0  success: yes
units: 8  decisions: P1: 2  P2: 55  P3: 6  P4: 12
parallelism: critical path 1890 / 2840 lines (0.67 <= 0.70) -> split
```

Error codes besides those of v2: `F1`-`F9`, `CONFLICT` and `SYNTH`.

The rules of step 3 listed by name: `registration_too_large`,
`same_symbol_merged`, `registration_first_shared`,
`registration_kept_ordered`, `small_shared_edit_first`,
`parallel_different_symbols` and `ordered`. Of P1, besides those of step 1:
`low_parallelism_single` and `parallel_enough`; of P2, besides those of step
2: `class_use_interface`.

#### Building a graph again from saved facts (`resynth`)

```bash
python -m aqours_code.taskgraph resynth <graph.json> --out <new_graph.json> [--ablate p1,p3]
```

reads the facts from `<graph.json>.report.json` and the request,
repository, base commit and final checks from the graph, clones the
repository at that commit as `plan` does, indexes it and reads its imports,
checks the facts again (F1-F9, against the saved draft items) and runs
`synthesize()` with the current rules. No model is called: the same facts
can be built under other rules or another `--ablate` at no planning cost.
It writes the new graph and a report with the fields of a v3 `plan` report
(model calls and tokens 0, no rounds) plus `resynth_of`, and prints the
same summary. Facts saved before `why` existed read as before. Exit codes:
`0` success, `1` the facts no longer pass the checks or contradict each
other, `2` a missing graph, report or facts, or a git failure.

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

## Team baseline (`team`)

A control for the task graph method: the established fixed workflow in which
a planner hands out tasks and workers do them in parallel. Its best-known
forms are Claude Code's agent teams
(<https://code.claude.com/docs/en/agent-teams>) and Cursor's planner/worker
agents (<https://cursor.com/blog/scaling-agents>). Neither can be run in
batch as open source, so `team.py` re-implements their **published workflow
and best practices** in this runtime: the same model, the same worker
(`AqoursWorker`), the same Docker sandbox, the same final checks and hidden
tests. The only variable is the structure.

```bash
python -m aqours_code.taskgraph team <request.md> --repo <path> [--out runs] [--workers 4]
    [--max-attempts 2] [--worker-timeout 1800] [--lead-timeout 1800]
    [--final-check "python -m pytest -q tests"] [--hidden-tests <dir>]
    [--sandbox docker|none] [--sandbox-image aqours-code-eval:py311]
```

The options mean what they mean for `run` and `plan`; `--final-check` can be
repeated, and `--lead-timeout` bounds each of the lead's two calls. The base
commit is the repository's `HEAD`. Exit codes are those of `run`.

### Flow, and what each step copies

| Step | Here | In the original systems |
| --- | --- | --- |
| 1. The lead plans | One agent with read-only tools (`read_file`, `glob`) on the base commit answers with a JSON task list: `id`, `title`, `description`, `files`, `blocked_by`. A list that cannot be used (not JSON, 0 or more than 20 tasks, duplicate ids, a `blocked_by` that names no task, itself, or a cycle, an empty description, a path outside the repository) is asked for once more with the problems listed; after two unusable answers the run fails with `lead_plan_invalid` and no teammate starts | Agent teams: the lead "creates tasks" in a shared task list, and tasks "can also depend on other tasks". Cursor: planners "explore the codebase and create tasks" |
| The guidelines in the lead's prompt | Self-contained tasks with a clear deliverable; a different set of files for each teammate; sequential, same-file or heavily dependent work stays in one task (one task is a valid plan); neither too small nor too large; `blocked_by` only when needed; a description that says everything, because teammates do not see the lead's conversation | Agent teams, best practices: "Size tasks appropriately", "Avoid file conflicts" ("Two teammates editing the same file leads to overwrites. Break the work so each teammate owns a different set of files"), "Give teammates enough context" (the lead's conversation "does not carry over"), and "For sequential tasks, same-file edits, or work with many dependencies, a single session ... [is] more effective" |
| 2. Teammates work in parallel | Up to `--workers` teammates at once (default 4). A task starts when every task in its `blocked_by` has finished, in list order. Each teammate is an ordinary worker with its own worktree at the current head of the integration branch; its prompt has the request, the task list with every task's status, the final answers of the teammates that finished (at most 1,500 characters each), and its own task and files | Agent teams: teammates "self-claim" the next unblocked task; each has "its own context window"; "start with 3-5 teammates"; a teammate that stops notifies the lead with "its final answer". Cursor: workers "take assigned tasks and focus only on finishing them" |
| No locks on files | Two tasks may edit the same file at the same time; nothing checks `files` before a task starts. A file changed outside a task's `files` is recorded (`out_of_scope_files`) and merged like the rest | Agent teams lock only the claiming of a task. Cursor found that file locks cut throughput and replaced them with optimistic concurrency |
| A teammate that fails | A worker error, a timeout, or no change at all: a new teammate tries again in the same worktree, told why (`--max-attempts`, default 2). Then the task is `failed`; tasks blocked by it still run and see `failed` in the task list | Agent teams: "Spawn a replacement teammate to continue the work" |
| Merging, and conflicts | A finished task is squash-merged into the integration branch at once. On a conflict, the integration branch is merged into the task's branch with the conflict markers left in the files, and the same task gets one more teammate round to resolve them. Markers still there, or a second conflict: the task is `failed` with `merge_conflict` | Cursor: an integrator role "created more bottlenecks than it solved"; "workers were already capable of handling conflicts themselves" |
| 3. The lead wraps up | After the last task: the final checks run on the merged work (`wrapup_checks.txt`), then one agent with the ordinary tools gets the request, every task's status and report, and the check output, and finishes or fixes what is missing or broken. Its changes are merged. It runs once | Agent teams: the lead "synthesiz[es] results". Cursor: a judge at the end of each cycle (here: one cycle) |
| 4. Final stage | As in `run`: `final_checks`, hidden tests, escape audit, `summary.json`, clean-up | |

The rules that keep a run of `run` steady hold here too, so that both
methods run under the same conditions: after every teammate and after the
lead's wrap-up, reserved-name files are deleted and line endings put back;
the checks before the wrap-up and the final checks run in the sandbox under
`timeout`, and one that times out is run once more (`check_timeout_retry`
with `kind` `wrapup` or `final`).

Deliberate simplifications of the originals: a task is done by a fresh agent
(a teammate does not keep its context from task to task); teammates do not
message each other while they work (they share only their final answers);
a task waits for its blockers to finish, completed or failed, so a failed
task cannot stall the run (agent teams require the dependency to be
completed); and each teammate has its own git worktree, so two edits of one
file meet as a merge conflict instead of one overwriting the other.

### What it does not have

These are the task graph method's parts, left out on purpose:

- no contract nodes, and no distinction between needing an interface and
  needing an implementation: `blocked_by` is the only dependency;
- no planner checks (P1-P7), no validation of the plan against the
  repository index, and no program revision of the plan;
- no context pack and no soft wall: a teammate reads what it wants;
- no check of its own for a task: a teammate that ends normally with changes
  is done;
- no post-merge check and no merge repair;
- splitting and conflict avoidance are rules of thumb in the lead's prompt,
  not rules a program enforces; a conflict is resolved afterwards by the
  teammate that hit it.

### Run directory and summary.json

The run directory has the layout of a graph run, so `rescore`, `audit` and
the escape audit work on it: `config.json` (`"method": "team"`),
`events.jsonl`, `repo/`, `nodes/<id>/` (prompt, worker result, trace, diff of
each agent; the ids are `lead_plan`, the task ids, and `lead_wrapup`),
`final/`, `final_checks.txt`, `hidden_tests.*`, `summary.json`, plus
`lead_plan.json` (the task list and every planning round) and
`wrapup_checks.txt`.

`summary.json` has the fields of a graph run that `score` and `rescore` use:
`run_id`, `status`, `wall_time_s` (from the start of the run to its end,
including both lead calls), `config`, `nodes` (the fields of a graph node;
`status` is `completed` or `failed`), `totals` (`model_calls`,
`input_tokens`, `output_tokens`, including the lead), `integration_commit`,
`final_checks`, `hidden_tests`, `sandbox`, `sandbox_image`, `checks_sandbox`,
`escape_attempts_total`, `check_timeout_retries_total`. `status` follows the rule of a graph run: `success`
when every task is completed and every final check passes, `failed` when no
task is completed (with `"reason": "lead_plan_invalid"` when there was no
plan), `partial` otherwise.

It adds `method` (`"team"`) and `team`: `tasks` (`id`, `title`, `files`,
`blocked_by`, `status`, `reason`, and whether it had a conflict), `n_tasks`,
`max_parallel` (the most tasks that ran at the same time), `conflicts`,
`conflicts_resolved`, `out_of_scope_total`, `failed_tasks`,
`wrapup_changed_files`.

## Scoring runs (`score`)

```bash
python -m aqours_code.taskgraph score <manifest.json> [--exclude <tests.txt>] [--out <dir>]
```

One number per method and task, from quality, time and cost, with the single
agent as the baseline:

```text
S = Q × (T_single / T) ^ 0.5 × (C_single / C) ^ 0.5
```

The **manifest** is a JSON list of objects: `task`, `method` (any name; one
must be `single`), `run` (a run directory of `run` or `team`), and optionally
`plan_report` (the planner's `*.report.json`, whose time and tokens are then
counted). Relative paths are relative to the manifest. Several entries with
the same task and method are repeated runs.

For each run:

- **Results**: `hidden_tests_rescored.xml` if the run has one, else
  `hidden_tests.xml`.
- **Quality Q** = pass rate of the functional tests × pass rate of the
  regression tests. A regression test is one from the hidden tests'
  `regression/` directory: pytest writes its JUnit `classname` as the dotted
  path of the test file (`_hidden_tests.regression.test_utils.test_echo`), so
  it is a test with `regression` among the parts of its `classname`. All
  other tests are functional. A pass rate is passed / (passed + failed +
  errors); skipped tests are not counted, and a kind without tests has the
  rate 1.
- `--exclude` names a file of pytest node ids, one per line (`#` starts a
  comment), such as `test_priority.py::test_tenants_take_turns_within_a_priority`.
  An id matches a test whose path ends with the id's path, so the directories
  in front (`_hidden_tests/`) can be left out. An excluded test counts
  neither as passed nor as failed.
- **Time T** = `wall_time_s` + the plan report's `duration_s`.
- **Cost C** = input tokens + 4 × output tokens, the plan report's included.

`T_single` and `C_single` are the means over the `single` runs of the same
task, so the single agent's S is its own Q. With repeated runs, a method's
Q, T and C are the means of its runs and its S is computed from those means;
the range shown beside S is that of the single runs' own S. A task without a
valid `single` run is an error (exit code 2).

A run is **invalid** and left out, but listed, when it ran with
`--sandbox none` and has escape attempts, or when it has no readable hidden
test results.

The output (printed, and with `--out` also written to `score.md` and
`score.json`):

1. by task: one row per task and method: runs, functional and regression
   pass rate, Q, T, input and output tokens, C, S;
2. by method: the number of tasks, mean Q, mean S, and the tasks on which it
   has the highest S;
3. the oracle: the mean S when each task takes its best method.
