# Task: list the pieces of work in a request

You are planning, not implementing. Another planner will read the code
later; your part is to read the request.

List the separate pieces of work that the request below asks for, using the
request text. Make one item per feature or concern that the request names.
Do not guess how the code is organised and do not name files in the items;
that comes later, after the code is read. If the request asks for one thing,
output one item. Do not add items for testing, documentation, or integration
unless the request names them as work of their own.

The list of repository files with their line counts is given only so that
you can judge the size of the work. You cannot open the files. Estimate how
many lines of code (including tests) the whole request will add or change.

Answer as the last ```json block of your final answer:

```json
{"estimated_changed_lines": <number>,
 "items": [{"id": "1", "title": "Short name",
            "description": "What this item must achieve, in one or two sentences."}]}
```

The example shows the format only; replace <number> with your own estimate.

- `estimated_changed_lines`: one non-negative whole number for the whole
  request, not per item.
- `items`: at least one. `id` is unique (`"1"`, `"2"`, ...); `title` is a
  few words; `description` says what the item must achieve, not how.
