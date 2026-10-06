# Task: split a request into sub-tasks

You are planning, not implementing. Split the request below into sub-tasks
that several coding agents can complete, each in its own copy of the
repository (or keep it as one task when it is small), and output them as a
draft task graph in the JSON format at the end of this prompt.

Read before you split. Read the request, then the repository: its README,
any specification it points to, the modules the request touches, and the
existing tests. Find out which files must change, which new files are
needed, which parts of the work are independent of each other, and which
conventions the code follows. Only then decide the split.

You have read-only tools (read_file, glob). Do not try to change any file.

## Concepts

**contract and implement nodes.** A `contract` node writes shared interfaces
that other nodes build on: new data fields, function and method signatures
with docstrings, and a minimal default body that runs (return an empty
result, raise `NotImplementedError`, or use an in-memory fake). It writes no
feature logic. An `implement` node writes real behaviour.

**requires and requires_impl.** A node lists the symbols it uses that it does
not define itself.

- `requires`: the interface is enough (name, signature, docstring). The node
  can start as soon as the node that provides the interface, usually a
  contract, is merged. Use this for almost every dependency.
- `requires_impl`: the node needs a working implementation, for example
  because its tests call the real behaviour. The node waits until every
  implement node that provides the symbol has finished. Use it only when the
  interface is really not enough, because it makes the node wait longer.

A symbol that already exists in the repository and is not changed by any
node can be listed in `requires` freely.

**Ordering and parallelism.** You do not write edges. The program derives
them:

- a node that requires a symbol runs after the node that provides it;
- two nodes that edit (modify or create) the same file run one after the
  other;
- nodes with neither relation run at the same time.

So the shape of the plan follows from the files and symbols you list. Each
node starts from the merged result of the nodes it depends on and sees
nothing else.

**Repository conventions.** Most repositories have rules that all their
code follows, stated in the README or the specification or visible in the
existing code. Examples: "the current time comes only from the injected
clock; a module function that needs it takes a `now` argument from its
caller and never reads the system time", "all database access goes through
the storage class", "argument checks use the helpers in the validation
module". Find these rules before you split and list them in `conventions`.
The program appends them to every node's goal, so every worker sees them;
a worker that misses one breaks code that other nodes rely on.

## Splitting rules

First decide whether to split at all. **You do not have to split, and you
do not have to use a contract.**

- For a small task, or when the work is concentrated in a few closely
  related files, output a single `implement` node.
- Add a contract only when several nodes really need to share a new
  interface (a new field, a new function signature, and so on).

When you do split:

1. **Split along files.** Make work blocks that can be finished on their own
   and whose edited files do not overlap. A good block is a feature or
   concern that lives in its own files.
2. **Put shared edits in one thin contract.** When several blocks need
   changes to the same shared file (data models, storage, a common entry
   point or facade, a router or registry), collect those changes in one
   contract node that the blocks require. Keep the contract thin: fields,
   signatures, docstrings, and runnable empty bodies, plus any small helper
   that every block needs complete. No feature logic. Create here the new
   files that blocks will fill in, so that each block only modifies its own
   file. There is usually only one contract. Contracts must never be
   ordered one after another: if one contract would need what another
   provides, make them one contract. A contract writes no test files
   (nothing under `tests/` in its `modify` or `create`); its `check` only
   runs the existing tests.

   Design the contract's interfaces so that the conventions can be kept.
   Whatever a module needs from the outside is passed in by its caller as
   a parameter: if a function needs the current time, give it a `now`
   argument and let the caller that owns the clock fill it in, instead of
   letting the module read the time itself. Check every signature against
   `conventions` before you finish.
3. **Same file, same node, or accept the order.** Work that must edit the same
   file either goes into one node, or will be run one node after another.
   Prefer merging closely related work that edits the same lines (for
   example several features that all change one central function) into one
   node over a long chain of nodes that rewrite the same code in turn.
4. **No test-only nodes and no integration node.** Do not add a node that
   only writes tests, and do not add a node whose purpose is to verify or
   connect the other nodes after they are merged: the final checks run the
   whole test suite on the merged result. If one part must be written
   against another part's real implementation (not just its interface),
   list that symbol in the later part's `requires_impl`: the later node then
   runs after the implementation is merged and connects to it itself, as
   part of its own work and its own tests.
5. **Every node tests itself.** Each node writes the tests for its own work
   (in its own new test file, listed in `create`) and has `check` commands
   that run on their own from the repository root. Including the existing
   test suite in `check` is a good default.
6. **Concrete goals.** A worker sees only its own node: its `goal`, files,
   symbols, and the original request. Say exactly what to implement, in
   which files and functions, which existing code to hook into, and what
   not to touch. Name the parts of any specification the node must follow.
7. **Do not over-split.** Each node adds overhead (reading context, running
   checks, merging). A node should be a meaningful piece of work, not a
   single small function.

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
      "kind": "contract",
      "goal": "What to do, concretely.",
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
- `context_files`: files the worker should read first; each must exist or be
  created by a node that comes earlier.
- Optional fields may be left out; lists default to empty.

## Examples

A toy example, unrelated to your request. Request: "Add star ratings and a
shopping-list export to the recipe book." The repository has
`recipes/models.py`, `recipes/book.py` (the `RecipeBook` facade), and tests
in `tests/`. Its README says that recipes are saved and loaded only through
`RecipeBook`, and `RecipeBook` takes an injected `clock`.

Both features need a new field on `Recipe` and a new method on
`RecipeBook`, so a thin contract adds both and creates the two feature
modules as stubs; then each feature fills in its own module in parallel.
The rating records when it was given, so the contract passes `now` to
`rate()` instead of letting it read the clock.

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
      "goal": "Add the fields `rating: int | None = None` and `rated_at: float | None = None` to Recipe in recipes/models.py. Create recipes/ratings.py with `rate(book, recipe_id, stars, *, now)` and recipes/shopping.py with `shopping_list(book, recipe_ids)`, each with its signature, a docstring and a body that raises NotImplementedError. In recipes/book.py add RecipeBook.rate(recipe_id, stars), which passes `now=self.clock()`, and RecipeBook.shopping_list, both one-line delegations to those functions. No feature logic.",
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
      "context_files": ["README.md", "recipes/models.py", "recipes/book.py"]
    },
    {
      "id": "R",
      "title": "Star ratings",
      "kind": "implement",
      "goal": "Implement rate() in recipes/ratings.py: accept 1 to 5 stars, raise ValueError otherwise, store the rating and `rated_at = now` on the recipe. Edit no other module. Write tests/test_ratings.py.",
      "modify": ["recipes/ratings.py"],
      "create": ["tests/test_ratings.py"],
      "provides": ["recipes/ratings.py::rate"],
      "requires": ["recipes/models.py::Recipe.rating", "recipes/models.py::Recipe.rated_at",
                   "recipes/book.py::RecipeBook.rate"],
      "check": ["python -m pytest -q tests"],
      "context_files": ["recipes/ratings.py", "recipes/book.py"]
    },
    {
      "id": "S",
      "title": "Shopping-list export",
      "kind": "implement",
      "goal": "Implement shopping_list() in recipes/shopping.py: merge the ingredients of the given recipes, summing equal units, sorted by name. Edit no other module. Write tests/test_shopping.py.",
      "modify": ["recipes/shopping.py"],
      "create": ["tests/test_shopping.py"],
      "provides": ["recipes/shopping.py::shopping_list"],
      "requires": ["recipes/book.py::RecipeBook.shopping_list"],
      "check": ["python -m pytest -q tests"],
      "context_files": ["recipes/shopping.py", "recipes/book.py"]
    }
  ]
}
```

The program turns R's and S's `requires` into edges from C, so R and S run
in parallel after C.

A second toy example, also unrelated to your request. Request: "Let the
recipe printout scale ingredient quantities to a chosen number of
servings." The printout lives in `recipes/printing.py`, and `Recipe` in
`recipes/models.py` already has a `servings` field.

The work is one small change to one module plus its tests. No other node
would share a new interface, so there is no contract, and splitting it
would only add overhead: the draft is a single implement node, and the
program derives no edges. The repository has no special rule to list.

```json
{
  "conventions": [],
  "nodes": [
    {
      "id": "P",
      "title": "Scale quantities in the printout",
      "kind": "implement",
      "goal": "In recipes/printing.py add an optional `servings: int | None = None` argument to print_recipe(). When given, multiply every ingredient quantity by servings / recipe.servings and print the chosen number of servings in the header; without it, print as before. Raise ValueError for servings < 1. Edit no other module. Write tests/test_printing_scale.py.",
      "modify": ["recipes/printing.py"],
      "create": ["tests/test_printing_scale.py"],
      "provides": ["recipes/printing.py::print_recipe"],
      "check": ["python -m pytest -q tests"],
      "context_files": ["recipes/printing.py", "recipes/models.py"]
    }
  ]
}
```
