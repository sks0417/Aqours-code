# Storage and query contract

Legacy v1 records have `doc_id`, `title`, `body`, and `tags`. Version 2 records have `version: 2`, `id`, `fields: {title, body}`, and `labels`. Tags/labels preserve input order while removing duplicates. Unknown persisted fields may be ignored.

The repository must read both formats into the same immutable `Document` model. `migrate_all()` rewrites every record to canonical v2, returns the number of records actually rewritten, and returns zero on a second call. Migration must preserve ids, title/body text, and tags exactly after normalization.

Search is case-insensitive over title and body. Optional tag filtering is exact after trimming and happens before pagination. Results sort by case-folded title, then document id. Page and page_size are positive non-boolean integers; invalid values raise `QueryValidationError` before repository access.

Each search result contains exactly `document_id`, `title`, `snippet`, and `tags`. A snippet is the first 40 body characters. Tags are returned as a list. Results and repository snapshots are detached from caller mutation.
