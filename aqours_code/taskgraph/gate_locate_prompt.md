# Task: say which files each feature of a request changes

You are planning, not implementing. Your part is only to say where in the
code each feature of the request below is done; how to do it comes later.

You get the request, its features (listed from the request text alone), an
outline of the repository's source files (each file with its number of
lines and the names of its top-level classes and functions; a function's
name ends in `()`), and for each feature the source files a program search
found for the names in its text. The search results are hints: they may miss
files or include unrelated ones. You cannot open files; work from the
outline and the search results.

For each feature, list:

- `modify`: the existing source files the feature must change to be done,
  including a module that must register or export what the feature adds.
  Do not list test files, and do not list files that do not exist yet.
- `reference`: at most 3 other existing source files that someone doing the
  feature must read to do it (code it calls, extends or copies as an
  example). Leave it empty when no other file is needed.

Write only paths that are in the outline, exactly as written there. Do not
describe how to implement anything.

Answer as the last ```json block of your final answer:

```json
{"features": [{"id": "1", "modify": ["pkg/module.py"], "reference": ["pkg/other.py"]}]}
```

The example shows the format only. Give one entry per feature, with the
feature's `id`.
