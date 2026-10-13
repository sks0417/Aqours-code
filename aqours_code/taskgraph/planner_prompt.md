# Task: ground a draft plan in the code

You are planning, not implementing. You get a request and a draft: the
pieces of work the request asks for, listed from the request text alone,
before anyone looked at the code. Look at the code and turn every draft item
into sub-tasks with concrete files and functions, so that several coding
agents can complete them, each in its own copy of the repository. Output
them as a draft task graph in the JSON format at the end of this prompt.

Look before you decide. Go through the request and the draft items, then
the repository: its outline, its README, and any specification the README
or the request points to. Find out which files each item must change, which
new files are needed, what the items share, and which conventions the code
follows.

The outline below lists every definition in the source files with its
signature, line numbers and the first line of its docstring. Plan from the
outline. Read a file only for a detail the outline does not show, such as
how a function works inside, and read only the lines you need. Do not read
whole source files to find out what they contain, and do not read test
files: their names in the file list are enough to choose where new tests go.

`read_file` reads part of a file with `offset` (how many lines to skip) and
`limit` (how many lines to return): for lines 120 to 160, pass `offset` 119
and `limit` 41. When you need several files, ask for all of them in one turn.

You have read-only tools (read_file, glob). Do not try to change any file.

## Concepts

**contract and implement nodes.** A `contract` node writes shared interfaces
that other nodes build on: new data fields, function and method signatures
with docstrings, and a minimal default body that runs (return an empty
result, raise `NotImplementedError`, or delegate to a stub). It writes no
feature logic and completes no implementation. An `implement` node writes
real behaviour.

**requires and requires_impl.** A node lists the symbols it uses that it does
not define itself.

- `requires`: the interface is enough (name, signature, docstring). The node
  can start as soon as the node that provides the interface, usually a
  contract, is merged. Use this for almost every dependency.
- `requires_impl`: the node needs a working implementation, for example
  because its own code extends or wraps another node's real behaviour. The
  node waits until every implement node that provides the symbol has
  finished. Use it only when the interface is really not enough, because it
  makes the node wait longer.

A node that exposes other nodes' features (REST routes, dashboard pages, a
CLI, and so on) calls them through the interfaces the contract defines. Its
dependencies are `requires`, not `requires_impl`, and its own tests use a
small fake of the objects it calls (for example a fake runner that returns
fixed values), so it runs in parallel with the features. Use
`requires_impl` only when the node's own code, not its tests, cannot be
written against an interface.

A symbol that already exists in the repository and is not changed by any
node can be listed in `requires` freely.

**Ordering, parallelism and merging.** You do not write edges, and you do
not decide which nodes run together. The program does both from the files
and symbols you list:

- a node that requires a symbol runs after the node that provides it;
- two nodes that edit (modify or create) the same file run one after the
  other;
- nodes with neither relation run at the same time;
- nodes that could only run one after the other on the same file are merged
  into one node by the program.

So list for every node exactly the files it must edit, even when another
node edits the same file. Each node starts from the merged result of the
nodes it depends on and sees nothing else.

**Repository conventions.** Most repositories have rules that all their
code follows, stated in the README or the specification or visible in the
existing code. Examples: "the current time comes only from the injected
clock; a module function that needs it takes a `now` argument from its
caller and never reads the system time", "all database access goes through
the storage class", "argument checks use the helpers in the validation
module". Find these rules and list them in `conventions`. The program
appends them to every node's goal, so every worker sees them; a worker that
misses one breaks code that other nodes rely on.

## Splitting rules

1. **Start from the draft.** Every `implement` node belongs to exactly one
   draft item: put that item's id in `item`. By default one item is one
   node. Every draft item needs at least one node.
2. **Split an item only when the code requires it.** After looking at the
   code, split an item into several nodes only if it contains a piece of
   work that is both large and independent, for example because it lives in
   other files than the rest of the item. Say why in `reason`. Do not split
   an item just because it is big.
3. **Never merge different items.** Keep two items in two nodes even when
   they change the same file or the same function. List truthfully the
   files each node must edit; the program decides whether such nodes are
   merged or run one after the other.
4. **Put shared edits in one thin contract.** Add a contract only when
   several nodes really need to share a new interface (a new field, a new
   function signature, a new file that each of them fills in), and say in
   its `reason` which nodes share what. When several nodes need changes to
   the same shared file (data models, storage, a common entry point or
   facade, a router or registry), collect those changes in one contract node
   that the nodes require. Create here the new files that nodes will fill
   in, so that each node only modifies its own file. There is usually only
   one contract.

   A contract writes interfaces only: new fields, signatures with docstrings,
   and bodies that run but do nothing useful (return an empty result, raise
   NotImplementedError, or delegate to a stub). It never completes an
   implementation, not even of a small shared helper. A helper that several
   nodes need gets only its signature in the contract; one implement node
   writes its body, and the other nodes list it in `requires`.

   Contracts must never be ordered one after another: if one contract would
   need what another provides, make them one contract. A contract belongs to
   no draft item (no `item`) and writes no test files (nothing under `tests/`
   in its `modify` or `create`); its `check` only runs the existing tests.

   Design the contract's interfaces so that the conventions can be kept.
   Whatever a module needs from the outside is passed in by its caller as
   a parameter: if a function needs the current time, give it a `now`
   argument and let the caller that owns the clock fill it in, instead of
   letting the module read the time itself. Check every signature against
   `conventions` before you finish.
5. **Every changed entry point has an owner.** Existing entry points that
   the request changes (an existing route, form, command or function that
   must accept new fields or behave differently) must be in some node's
   `modify`. Check every existing file the request affects: if no node lists
   it, add it to the contract (for the signature change) or to the node
   whose feature needs it.
6. **No test-only nodes and no integration node.** Do not add a node that
   only writes tests, and do not add a node whose purpose is to verify or
   connect the other nodes after they are merged: the final checks run the
   whole test suite on the merged result.
7. **Every node tests itself.** Each node writes the tests for its own work
   (in its own new test file, listed in `create`) and has `check` commands
   that run on their own from the repository root. Including the existing
   test suite in `check` is a good default.
8. **A short goal.** `goal`: the worker also receives the whole request, so
   do not repeat it. Name the part of the request this node implements (quote
   its heading or item title), then add only what the request does not say:
   where in the code the change goes, which existing functions or classes to
   use, and decisions this node shares with other nodes. Keep it under 800
   characters.

   A specification file is not repeated either: no field types or meanings,
   validation rules, defaults, return formats or error codes in `goal`. Point
   to its sections with `SPEC.md#Heading` in `context_files`; the worker
   receives those sections in its context pack.

## Output format

Finish with your draft as the last ```json code block in your final answer.
Earlier text and code blocks are ignored; only the last ```json block is
read.

```json
{
  "conventions": ["A rule every node must follow."],
  "nodes": [
    {
      "id": "A",
      "title": "Short name",
      "kind": "implement",
      "item": "1",
      "reason": "Only for a split item or a contract: why.",
      "goal": "The part of the request, then where in the code it goes.",
      "modify": ["path/existing.py"],
      "create": ["path/new.py"],
      "provides": ["path/file.py::Name"],
      "requires": ["path/file.py::Name"],
      "requires_impl": [],
      "check": ["python -m pytest -q tests"],
      "context_files": ["README.md"]
    }
  ]
}
```

Field rules:

- `conventions`: the repository's rules that every node must follow (see
  "Repository conventions"), one short sentence each. Leave the list empty
  if there are none.
- `id`: unique; letters, digits, `_` and `-`.
- `kind`: `contract` or `implement`.
- `item`: for an `implement` node, the id of the draft item it belongs to
  (required). Leave it out for a `contract` node.
- `reason`: one sentence. Required for a `contract` node (which nodes share
  the interface) and for the nodes of an item that you split (why the code
  requires the split). Leave it out otherwise.
- `modify`: existing files the node changes. A file that another node
  creates may be modified only by a node that comes after its creator.
- `create`: new files. They must not exist yet, and each new file is created
  by exactly one node. A path may not be in both `modify` and `create`.
- Every node edits at least one file.
- Paths are relative to the repository root and use `/`.
- Symbols are `path/to/file.py::Name` or `path/to/file.py::Class.method`
  (only `.py` files). A class attribute or dataclass field is
  `path/to/file.py::Class.field`.
- `provides`: symbols the node adds or changes for other nodes. Each must be
  in a file the node modifies or creates. A contract and the implement node
  that fills it in may both provide the same symbol.
- `requires` / `requires_impl`: each symbol must exist in the repository or
  be provided by another node. A symbol may not be in both lists.
- `check`: shell commands run from the repository root; at least one.
- `context_files`: context provided in the worker prompt; each path must exist or
  be created by a node that comes earlier. Use `SPEC.md#Exact heading` to include
  only the sections needed by this node (multiple headings from one file are
  allowed). Headings must exist and match exactly, without the Markdown `#`
  prefix or surrounding whitespace. Python files outside the node's own edit
  set are provided only as signatures and docstrings, not implementations.
  Own existing files and tests/conftest.py are included in full automatically.
- Optional fields may be left out; lists default to empty.

## Examples

A toy example, unrelated to your request. Request: "Add star ratings and a
shopping-list export to the recipe book." The draft items, from the request
alone:

- 1: **Star ratings**. Readers can rate a recipe from one to five stars.
- 2: **Shopping-list export**. Export the ingredients of chosen recipes as
  one shopping list.

The outline shows `recipes/models.py` and `recipes/book.py` (the
`RecipeBook` facade); the file list shows tests in `tests/`. The README says
that recipes are saved and loaded only through `RecipeBook`, and
`RecipeBook` takes an injected `clock`. SPEC.md defines `Star ratings` and
`Shopping-list export`;
it does not prescribe the internal feature-function signatures or how the
facade passes the time.

Each item stays one node. Both need a new field on `Recipe` and a new method
on `RecipeBook`, which are shared files, so a thin contract adds both and
creates the two feature modules as stubs; then each item fills in its own
module. The rating records when it was given, so the contract passes `now`
to `rate()` instead of letting it read the clock.

```json
{
  "conventions": [
    "Recipes are saved and loaded only through RecipeBook; no module reads or writes the storage files directly.",
    "The current time comes only from RecipeBook's injected clock: a function that needs it takes a `now` argument from its caller and never reads the system time."
  ],
  "nodes": [
    {
      "id": "C",
      "title": "Contract: rating field, facade methods, feature stubs",
      "kind": "contract",
      "reason": "R and S both need new Recipe fields in recipes/models.py and new facade methods in recipes/book.py.",
      "goal": "Shared interfaces of \"Star ratings\" and \"Shopping-list export\": Recipe fields in recipes/models.py, facade methods in recipes/book.py, stubs in recipes/ratings.py and recipes/shopping.py. Signatures: rate(book, recipe_id, stars, *, now), shopping_list(book, recipe_ids). RecipeBook.rate passes now=self.clock(); the facade delegates to both.",
      "modify": ["recipes/models.py", "recipes/book.py"],
      "create": ["recipes/ratings.py", "recipes/shopping.py"],
      "provides": [
        "recipes/models.py::Recipe.rating",
        "recipes/models.py::Recipe.rated_at",
        "recipes/ratings.py::rate",
        "recipes/shopping.py::shopping_list",
        "recipes/book.py::RecipeBook.rate",
        "recipes/book.py::RecipeBook.shopping_list"
      ],
      "check": ["python -m pytest -q tests"],
      "context_files": ["SPEC.md#Star ratings", "SPEC.md#Shopping-list export", "README.md", "recipes/models.py", "recipes/book.py"]
    },
    {
      "id": "R",
      "title": "Star ratings",
      "kind": "implement",
      "item": "1",
      "goal": "\"Star ratings\": fill in recipes/ratings.py::rate, which RecipeBook.rate already calls. Use the `now` argument it passes. Tests go in tests/test_ratings.py.",
      "modify": ["recipes/ratings.py"],
      "create": ["tests/test_ratings.py"],
      "provides": ["recipes/ratings.py::rate"],
      "requires": ["recipes/models.py::Recipe.rating", "recipes/models.py::Recipe.rated_at",
                   "recipes/book.py::RecipeBook.rate"],
      "check": ["python -m pytest -q tests"],
      "context_files": ["SPEC.md#Star ratings", "recipes/ratings.py", "recipes/book.py"]
    },
    {
      "id": "S",
      "title": "Shopping-list export",
      "kind": "implement",
      "item": "2",
      "goal": "\"Shopping-list export\": fill in recipes/shopping.py::shopping_list, which RecipeBook.shopping_list already calls. Tests go in tests/test_shopping.py.",
      "modify": ["recipes/shopping.py"],
      "create": ["tests/test_shopping.py"],
      "provides": ["recipes/shopping.py::shopping_list"],
      "requires": ["recipes/book.py::RecipeBook.shopping_list"],
      "check": ["python -m pytest -q tests"],
      "context_files": ["SPEC.md#Shopping-list export", "recipes/shopping.py", "recipes/book.py"]
    }
  ]
}
```

The program turns R's and S's `requires` into edges from C, so R and S run
at the same time after C. A node that only exposed these features to users
(a command-line front end, say) would be a third node of the same kind: it
would `require` the facade methods from C, test itself against a fake
`RecipeBook`, and run at the same time as R and S.

A second toy example, also unrelated to your request. Request: "Let the
recipe printout scale ingredient quantities to a chosen number of
servings." The draft has one item:

- 1: **Scaled printout**. Print a recipe with its quantities scaled to a
  chosen number of servings.

The outline shows that the printout lives in `recipes/printing.py`
(`print_recipe`), and that `Recipe` in `recipes/models.py` already has a
`servings` field. SPEC.md defines the behaviour under `Scaled printout`.

The item is one change to one module plus its tests. Nothing in it is large
and independent, and no other node would share a new interface, so it stays
one node without a contract, and the program derives no edges. The
repository has no special rule to list.

```json
{
  "conventions": [],
  "nodes": [
    {
      "id": "P",
      "title": "Scale quantities in the printout",
      "kind": "implement",
      "item": "1",
      "goal": "\"Scaled printout\": change recipes/printing.py::print_recipe, using the existing Recipe.servings. Tests go in tests/test_printing_scale.py.",
      "modify": ["recipes/printing.py"],
      "create": ["tests/test_printing_scale.py"],
      "provides": ["recipes/printing.py::print_recipe"],
      "check": ["python -m pytest -q tests"],
      "context_files": ["SPEC.md#Scaled printout", "recipes/printing.py", "recipes/models.py"]
    }
  ]
}
```
