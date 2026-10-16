# Task: describe the work a request needs, grounded in the code

You are planning, not implementing. You get a request and a draft: the
pieces of work the request asks for, listed from the request text alone,
before anyone looked at the code. Look at the code and describe, for every
draft item, the work it needs: which existing files it changes and how much,
which new files it creates, what it provides to other work and what it uses
from it. Several coding agents will later do the work, each in its own copy
of the repository. Output the description in the JSON format at the end of
this prompt.

You describe the work; you do not design the task graph. The program
decides which units become nodes, which shared edits are made first or
last, what runs in parallel, and how each node is checked, from what you
report. Report truthfully, even when two units edit the same file or the
same function. When you are unsure whether an edit can be written on its
own, say standalone false.

Look before you describe. Go through the request and the draft items, then
the repository: its outline, its README, and any specification the README
or the request points to. Find out which files each item must change, which
new files are needed, what the items share, and which conventions the code
follows.

The outline below lists every definition in the source files with its
signature, line numbers and the first line of its docstring. Work from the
outline. Read a file only for a detail the outline does not show, such as
how a function works inside, and read only the lines you need. Do not read
whole source files to find out what they contain, and do not read test
files: their names in the file list are enough to choose where new tests go.

`read_file` reads part of a file with `offset` (how many lines to skip) and
`limit` (how many lines to return): for lines 120 to 160, pass `offset` 119
and `limit` 41. When you need several files, ask for all of them in one turn.

You have read-only tools (read_file, glob). Do not try to change any file.

## Concepts

**Units.** A unit is one piece of work. By default one draft item is one
unit. When an item contains several parts that are large, unrelated to each
other, and in different files, give it several units with the same `item`.
Every draft item has at least one unit. Do not split an item just because it
is big, and do not combine different items into one unit.

**Logic and registration edits.** An edit is a change to one existing file,
or to a new file that another unit creates.
A `logic` edit implements behaviour. A `registration` edit only enters a
name into a list or table: an export, `__all__`, a registry, a route table,
a few lines that name something defined elsewhere. Say in `note` what it
registers.

**Provides and uses.** `provides` lists the symbols a unit adds or changes
for other code to use, including the names it registers. `uses` lists the
symbols a unit needs that another unit provides, or that this unit itself
registers. A symbol that exists already and that no unit changes is not
listed.

- `need: "interface"`: the unit only needs the symbol to exist with its
  name and signature. This is the usual case: creating, reading or raising
  a class, a field or an exception, and calling a function or method whose
  result the unit's tests can replace with a small fake.
- `need: "behavior"`: the unit's code or tests depend on a result that only
  the provider's real code produces, so that a fake would make the tests
  meaningless. Say which result in `why`, in one sentence. If you cannot
  name such a result, the need is "interface".
- Name the method you call, not its class: `RecipeBook.rate`, not
  `RecipeBook`. A use of a class means its constructor and fields only.

When the unit's own code or tests import one of its own symbols through a
name the unit registers (a test writes `from package import Name`), list that
registered name in `uses` too.

**Standalone edits.** A `logic` edit is `standalone` when it can be written
using only the code already in the repository: it needs neither the unit's
other changes nor any other unit's work. When you are unsure, say `false`.

**Together.** Units that cannot be written or tested apart, not even with a
fake standing in for the other, are listed together with a reason. This is
rare. Editing the same file, or using another unit's fields, is not a
reason to list units together.

**Repository conventions.** Most repositories have rules that all their
code follows, stated in the README or the specification or visible in the
existing code. Examples: "the current time comes only from the injected
clock; a module function that needs it takes a `now` argument from its
caller and never reads the system time", "all database access goes through
the storage class", "argument checks use the helpers in the validation
module". Find these rules and list them in `conventions`. The program
appends them to every goal, so every worker sees them; a worker that misses
one breaks code that other work relies on.

**A short goal.** `goal`: the worker also receives the whole request, so do
not repeat it. Name the part of the request this unit implements (quote its
heading or item title), then add only what the request does not say: where
in the code the change goes, which existing functions or classes to use,
and decisions this unit shares with others. Keep it under 800 characters.
A specification file is not repeated either: no field types or meanings,
validation rules, defaults, return formats or error codes in `goal`. Point
to its sections with `SPEC.md#Heading` in `context_files`; the worker
receives those sections.

## Output format

Finish with your description as the last ```json code block in your final
answer. Earlier text and code blocks are ignored; only the last ```json block
is read.

```json
{
  "conventions": ["A rule every unit must follow."],
  "units": [
    {
      "id": "A",
      "item": "1",
      "title": "Short name",
      "goal": "The part of the request, then where in the code it goes.",
      "size": 150,
      "edits": [
        {"file": "pkg/module.py", "kind": "logic",
         "symbols": ["pkg/module.py::Name"], "lines": 120, "standalone": false},
        {"file": "pkg/__init__.py", "kind": "registration",
         "symbols": ["pkg/__init__.py::Name"], "lines": 1, "note": "export Name from pkg"}
      ],
      "create": ["tests/test_name.py"],
      "provides": ["pkg/module.py::Name", "pkg/__init__.py::Name"],
      "uses": [{"symbol": "pkg/__init__.py::Name", "need": "interface"},
               {"symbol": "pkg/store.py::Store.total", "need": "behavior",
                "why": "The tests check the totals Store.total computes."}],
      "tests": ["tests/test_name.py"],
      "context_files": ["README.md"]
    }
  ],
  "together": [{"units": ["A", "B"], "reason": "Why one cannot stand without the other."}]
}
```

Field rules:

- `conventions`: the repository's rules, one short sentence each; empty if
  there are none.
- `id`: unique; letters, digits, `_` and `-`. `item`: the id of the draft
  item the unit belongs to. `title`: a few words.
- `size`: the number of lines the unit adds or changes, tests included.
- `edits`: changes to existing files (or to a new file that another unit
  creates, see `create`), one entry per file. `kind` is
  `logic` or `registration`. `symbols`: for `logic`, the classes and
  functions changed or added (so that two units changing the same function
  can be recognised); for `registration`, the registered names, written as
  symbols of the file they are registered in (`pkg/__init__.py::Name`).
  For a change inside a class, name the member (`path.py::Class.method`,
  `path.py::Class.field`), not the class. Name the class itself only when the
  whole class is new or rewritten.
  Every symbol of an edit is in that edit's file. `lines`: how many lines of
  that file change. `standalone`: only for `logic` (see above). `note`: for
  `registration`, what it registers.
- `create`: new files, tests included. They must not exist yet, and each new
  file is created by exactly one unit. When several units add code to the
  same new file, one unit lists it in `create` and the others list it in
  `edits`, with the symbols they add.
- `provides`: each symbol is in a file of the unit's `edits` or `create`; a
  registered name is also in the `symbols` of its registration edit.
- `uses`: each symbol is provided by some unit (this unit's registered
  names included); `need` is `"interface"` or `"behavior"`. `why` is
  required when `need` is `"behavior"`: one sentence naming the result that
  only the provider's real code produces.
- `tests`: the unit's own test files, each in its `create` or `edits`.
  Empty when the unit has no behaviour that can be observed on its own (pure
  wiring).
- `context_files`: context for the worker; each path must exist or be
  created by a unit. Use `SPEC.md#Exact heading` for the sections the unit
  needs (several headings from one file are allowed); headings must match
  exactly, without the Markdown `#` prefix. Python files outside the unit's
  own files are given as signatures and docstrings only.
- `together`: optional; each entry names existing units and gives a reason.
- Paths are relative to the repository root and use `/`. Symbols are
  `path/to/file.py::Name` or `path/to/file.py::Class.method` (only `.py`
  files); a class attribute or dataclass field is `path/to/file.py::Class.field`.
- Optional fields may be left out; lists default to empty.

## Example

A toy example, unrelated to your request. Request: "Let readers rate
recipes, list the best-rated recipes, export a shopping list, and add a
command line for rating and for the best-rated list." The draft items, from
the request alone:

- 1: **Star ratings**. Readers rate a recipe from one to five stars.
- 2: **Best-rated list**. The book lists recipes by their average rating.
- 3: **Shopping-list export**. Export the ingredients of chosen recipes as
  one shopping list.
- 4: **Command line**. `recipes rate` and `recipes best` commands.

The outline shows `recipes/models.py` (`Recipe`), `recipes/book.py` (the
`RecipeBook` facade) and `recipes/__init__.py`, which exports the public
functions. The README says that recipes are saved and loaded only through
`RecipeBook`, and that `RecipeBook` takes an injected `clock`.

```json
{
  "conventions": [
    "Recipes are saved and loaded only through RecipeBook; no module reads or writes the storage files directly.",
    "The current time comes only from RecipeBook's injected clock: a function that needs it takes a `now` argument from its caller."
  ],
  "units": [
    {
      "id": "R", "item": "1", "title": "Star ratings",
      "goal": "\"Star ratings\": a ratings list on Recipe in recipes/models.py, and rate(book, recipe_id, stars, *, now) in a new recipes/ratings.py that stores through RecipeBook. Tests in tests/test_ratings.py import rate from recipes.",
      "size": 140,
      "edits": [
        {"file": "recipes/models.py", "kind": "logic", "symbols": ["recipes/models.py::Recipe.ratings"], "lines": 15, "standalone": true},
        {"file": "recipes/__init__.py", "kind": "registration", "symbols": ["recipes/__init__.py::rate"], "lines": 1, "note": "export rate from recipes"}
      ],
      "create": ["recipes/ratings.py", "tests/test_ratings.py"],
      "provides": ["recipes/models.py::Recipe.ratings", "recipes/ratings.py::rate", "recipes/__init__.py::rate"],
      "uses": [{"symbol": "recipes/__init__.py::rate", "need": "interface"}],
      "tests": ["tests/test_ratings.py"],
      "context_files": ["SPEC.md#Star ratings", "recipes/book.py"]
    },
    {
      "id": "B", "item": "2", "title": "Best-rated list",
      "goal": "\"Best-rated list\": RecipeBook.best_rated in recipes/book.py, ranking by the mean of Recipe.ratings. Tests in tests/test_best_rated.py build rated recipes themselves.",
      "size": 90,
      "edits": [
        {"file": "recipes/book.py", "kind": "logic", "symbols": ["recipes/book.py::RecipeBook.best_rated"], "lines": 40, "standalone": false}
      ],
      "create": ["tests/test_best_rated.py"],
      "provides": ["recipes/book.py::RecipeBook.best_rated"],
      "uses": [{"symbol": "recipes/models.py::Recipe.ratings", "need": "interface"}],
      "tests": ["tests/test_best_rated.py"],
      "context_files": ["SPEC.md#Best-rated list"]
    },
    {
      "id": "S", "item": "3", "title": "Shopping-list export",
      "goal": "\"Shopping-list export\": shopping_list(book, recipe_ids) in a new recipes/shopping.py. Tests in tests/test_shopping.py import it from recipes.shopping.",
      "size": 120,
      "edits": [
        {"file": "recipes/__init__.py", "kind": "registration", "symbols": ["recipes/__init__.py::shopping_list"], "lines": 1, "note": "export shopping_list from recipes"}
      ],
      "create": ["recipes/shopping.py", "tests/test_shopping.py"],
      "provides": ["recipes/shopping.py::shopping_list", "recipes/__init__.py::shopping_list"],
      "tests": ["tests/test_shopping.py"],
      "context_files": ["SPEC.md#Shopping-list export"]
    },
    {
      "id": "C", "item": "4", "title": "Command line",
      "goal": "\"Command line\": a new recipes/cli.py with the rate and best commands. rate calls recipes.rate with now from the book's clock; its tests use a fake rate. The best command's tests check the printed ranking of real ratings. Tests in tests/test_cli.py.",
      "size": 160,
      "create": ["recipes/cli.py", "tests/test_cli.py"],
      "provides": ["recipes/cli.py::main"],
      "uses": [
        {"symbol": "recipes/__init__.py::rate", "need": "interface"},
        {"symbol": "recipes/book.py::RecipeBook.best_rated", "need": "behavior",
         "why": "C's tests check the scores best_rated computes from stored ratings"}
      ],
      "tests": ["tests/test_cli.py"],
      "context_files": ["SPEC.md#Command line"]
    }
  ]
}
```

What the facts say, and why:

- R registers `rate` in `recipes/__init__.py`, and its own tests import
  `rate` through that name, so R lists `recipes/__init__.py::rate` in `uses`.
  C also uses that name. Both only call `rate`, so `"interface"` is enough.
- S registers `shopping_list`, but nobody imports it through the package:
  S's tests import it from `recipes.shopping`.
- B only reads the `Recipe.ratings` field, and its tests build rated
  recipes themselves, so it says `"interface"`.
- C's tests check the scores `best_rated` computes from stored ratings, a
  result only B's real code produces, so C says `"behavior"` and names that
  result in `why`.
- R's change to `Recipe` adds one field with only the existing code, so it
  is `standalone`; B's ranking reads that new field, so it is not.
- R and B are not listed `together`: B can be written and tested with
  recipes it rates itself. `together` is only for units that cannot be
  written or tested apart even with a fake, which is rare.
