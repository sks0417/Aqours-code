# Document index

`build_application(initial_records)` accepts persisted v1 or v2 record dictionaries. The stable API is:

```python
app.api.migrate_storage()
app.api.search(query="", *, tag=None, page=1, page_size=20)
```

Search results are public dictionaries and must not leak the storage representation. Migration mutates the in-memory repository but must be idempotent and lossless. Detailed formats, ordering, filtering, and pagination are normative in `docs/storage_contract.md`.
