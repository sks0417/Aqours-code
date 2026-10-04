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
| `Node` | `id` (`[A-Za-z0-9_-]+`), `title`, `kind`: `contract` \| `implement`, `goal` (non-empty), `edit_set`, `requires`, `provides`, `check`, `context_files`, optional `size`: `small` \| `medium` \| `large` |
| `EditSet` | `modify` (existing files), `create` (new files), optional `symbols` (functions/methods the node changes; not used for scheduling) |
| `Check` | `commands`, `timeout_s` (integer > 0, default 300) |
| `Edge` | `from`, `to`, `type`: `interface` \| `full` \| `order`, `source`: `manual` \| `llm` \| `derived`, `reason` |
| `RevisionEntry` | `action`: `merge` \| `split` \| `add_edge` \| `remove_edge` \| `add_node` \| `other`, `nodes`, optional `into`, `reason` |

In Python, `Edge.from` is `Edge.from_`; it is always serialized as `from`.
Use `load_graph(path)` and `dump_graph(graph, path)` for JSON I/O.

Within one node, `modify`, `create`, `context_files`, `requires`, `provides`
and `edit_set.symbols` must not repeat an entry, and no path may appear in
both `modify` and `create`. These are schema errors.

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
| V3 | Each `modify` file exists at the base commit or is created by an ancestor (a file created only by a non-ancestor is reported with its creator: missing edge?); `create` files do not exist at the base commit; `context_files` exist or are created by some node. *Needs an index.* |
| V4 | Every node has at least one non-empty check command. |
| V5 | Two nodes that edit a common file (`modify ∪ create`) must be ordered: one is an ancestor of the other. |
| V6 | Every `requires` symbol exists at the base commit or is provided by an ancestor. A node's own `provides` does not count. *Needs an index.* |
| V7 | Every node edits at least one file. |
| V8 | `requires`, `provides` and `edit_set.symbols` are well-formed symbols. |
| V9 | An `interface` edge starts at a `contract` node. |
| V10 | Every `edit_set.symbols` and `provides` entry belongs to a file in the same node's `modify` or `create` (a node cannot provide a symbol in a file it does not edit). |
| V11 | Each new file is created by exactly one node. |
| W1 | A `small` node has exactly one distinct direct successor (consider merging). |
| W2 | A provided symbol is not required by any other node. |
| W3 | `final_checks` is empty. |
| W4 | A node requires a symbol provided by a node whose only direct edge to it is an `order` edge. |
| W5 | A node requires a symbol that exists at the base commit, but non-ancestor nodes list it in `provides` (they change it, so the node may see the old version). *Needs an index.* |

Without an index, V3, V6 and W5 are skipped and reported as warnings. A single
node graph is the fallback plan and passes when V3, V4, V7, V8, V10 and V11
hold.

## Edge derivation

`derive_edges(graph, index)` returns a new graph and the revision entries it
appended to `revision_log`:

1. For each `requires` symbol that is not in the repository and has exactly one
   provider that is not yet an ancestor, add `provider -> node`
   (`interface` from a `contract` provider, otherwise `full`). When several
   providers exist and none is an ancestor, no edge is added.
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
multi-provider symbol from step 1 is checked against the final graph: if none
of its providers is an ancestor of the requiring node, an `other` revision
lists the node and every candidate provider with the reason "multiple
providers, edge not derived". These entries come after all edge entries. Run
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
