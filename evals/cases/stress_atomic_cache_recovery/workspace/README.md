# Local artifact cache recovery exercise

This repository implements a small, standard-library-only cache for local build
artifacts. The starter is runnable, but several related correctness defects remain.
Fix the implementation while preserving the contracts below.

## Public API

The package exports `ArtifactCache`, the immutable request/result models, the public
exceptions, and the manifest constants listed in `artifact_cache.__all__`. Existing
export names, constant values, public dataclass field order, and public method
signatures are part of the API and must remain unchanged.

```python
cache = ArtifactCache(
    root,
    clock=None,
    lease_seconds=30.0,
    fault_hook=None,
)
key = cache.key_for(request)
entry_or_none = cache.get(request)
lease = cache.begin_build(request, writer_id="worker-a")
entry = cache.commit(lease, artifact)
cache.abort(lease)
entry = cache.get_or_build(request, builder, writer_id="worker-a")
report = cache.recover()
```

`root` is a path. `clock` is a zero-argument callable returning a finite numeric
time; it defaults to a monotonic clock. `lease_seconds` must be finite and positive;
booleans are not accepted as numeric clock or duration values. Tests and callers may
inject a clock, so the implementation must not sleep.
`fault_hook`, when supplied, is called as `fault_hook(stage, path)` at documented
publication boundaries (`artifact_staged`, `manifest_staged`, `before_publish`, and
`after_publish`). It may raise to simulate a crash or I/O failure.

`BuildRequest` contains:

- `inputs`: a non-empty mapping whose logical input names are non-empty strings and
  whose content values are `bytes` or `str`;
- `options`: a nested value made from mappings with string keys, lists, tuples,
  sets/frozensets, JSON scalar values, and bytes;
- `tool_version`, `namespace_version`, and `artifact_format`: non-empty strings;
- optional `scratch_dir`, an execution-only location that never changes output
  identity and therefore must not affect a cache key.

Unsupported request values, non-string option keys, empty required strings, and
non-finite numbers are invalid and raise `InvalidRequest`. `writer_id` must likewise
be a non-empty string.

`get` returns `None` for a missing or invalid cache entry. A returned `CacheEntry`
contains detached artifact bytes, a detached normalized manifest mapping, and a
`cache_hit` flag. `commit` returns `cache_hit=False`; reads and build reuse return
`cache_hit=True`.

`begin_build` creates an exclusive lease for one cache key. A live lease raises
`BuildInProgress` for another explicit writer. At or after expiry, a later writer may
acquire the next generation. Only the exact current key, writer, generation, and
opaque token may publish. Expired, replaced, forged, or cross-key leases raise
`StaleWriter` without changing cache data, lock state, or another writer's staging
directory. `abort` is idempotent and may clean only its own staging data.

Lease expiry uses a strict live interval: a lease is live only while the current time
is less than `expires_at`; equality is already expired. A successful publication
transitions the matching active record to terminal `committed`, and an ordinary
pre-publication failure or explicit abort transitions it to terminal `aborted`.
Terminal records are durable history: retries and recovery must not reactivate or
rewrite them.

`commit` accepts bytes-like data, a path inside the lease's staging directory, or
`None` when the builder already wrote `artifact.bin` there. A path outside that
directory is invalid. Builders passed to `get_or_build` receive the private staging
directory and return one of the same artifact forms. Concurrent `get_or_build` calls
for an equal request execute one builder and reuse its result. Different cache keys
must be able to build concurrently; a process-wide build lock is not acceptable.

## Cache keys

Keys are lowercase SHA-256 hex digests. They cover every output-affecting request
field: complete input names and content, normalized options, tool version, namespace
version, and artifact format. Mapping insertion order and set iteration order are not
semantic. List and tuple order is semantic. Types that would otherwise collide must
remain distinguishable. `scratch_dir` and actual temporary paths are excluded.
Invalid or non-finite values raise `InvalidRequest`.

## Publication and integrity

Builds use a private directory under `staging/` on the same filesystem as `entries/`.
An artifact and its manifest must be completely written and verified before a single
atomic current-pointer replacement makes that immutable version visible. Readers may
observe the old complete version or the new complete version, never a partial or
mixed pair. A failed replacement must preserve an older valid entry. Repeating an
already successful commit with the exact lease and bytes is idempotent; changed data
for that consumed lease is stale and has no side effects.

A fault raised at `after_publish` occurs after durable publication. The lease must
be recorded as terminally `committed` rather than remaining `active` or becoming
`aborted`. Retrying the exact lease and bytes returns the published entry without
rebuilding, while retrying the consumed lease with different bytes is stale and has
no side effects. Once the current pointer has been replaced, later hook behavior or
clock advancement cannot turn that durable publication into an aborted transaction.

Current manifests use schema version 2 and contain exactly `schema_version`,
`cache_key`, `digest`, `size`, `artifact_format`, `generation`, `writer_id`,
`lease_token`, and `created_at`. The schema version is the integer `2`; `cache_key`
and `digest` are lowercase SHA-256 hex strings; `size` and `generation` are
non-negative integers; `artifact_format`, `writer_id`, and `lease_token` are
non-empty strings; and `created_at` is finite. Booleans are not valid numeric field
values. Every cache read, including a legacy read, verifies digest and size before
returning a hit. Corrupt, truncated, replaced, malformed, or mismatched entries are
safe misses.
The current pointer has the JSON shape `{"version": "<version-directory>"}` and must
name a safe version directory belonging to the same cache key. The pointed directory
must contain both a readable artifact and a valid matching manifest. A malformed
pointer, unsafe version name, missing file, incomplete version, cache-key mismatch,
digest mismatch, or size mismatch must never become a cache hit.

The durable layout is:

```text
entries/<cache-key>/
    current.json                  # {"version": "<version-directory>"}
    versions/<version-directory>/
        artifact.bin
        manifest.json
locks/<cache-key>.json
staging/<cache-key>.g<generation>.<token>/
    lease.json
    ... build files ...
```

Temporary JSON files may be used beside their targets and replaced atomically.
Complex platform-specific `fsync` guarantees are out of scope.

## Legacy manifest compatibility

Schema version 1 remains readable inside the same version-directory layout. Its
exact manifest shape is:

```json
{
  "schema_version": 1,
  "cache_key": "<64 lowercase hex characters>",
  "sha256": "<64 lowercase hex characters>"
}
```

For schema 1 only, size is derived from `artifact.bin`, `artifact_format` defaults to
`"binary"`, and generation defaults to zero. The digest is still mandatory and must
be verified. Unknown schemas, missing required fields, unsafe version names, and
values that cannot be interpreted unambiguously are rejected. All new writes use
schema 2.

## Recovery

`recover` is repeatable. It preserves valid committed entries, live writers and their
staging directories, terminal lock records, and unrelated files. It removes expired
or abandoned cache-owned staging directories, unreachable cache-owned version
directories, and invalid current pointers/pointed entries. An expired active lock may
be marked abandoned, but committed/aborted records must not become active again.
Keys are recovered independently.

Recovery applies the following observable rules independently to each cache key:

- A valid current pointer and complete matching version remain readable and increment
  `kept_entries`.
- A malformed or unsafe current pointer, a pointer to a missing version, or a pointed
  version with a missing, malformed, corrupt, or mismatched artifact/manifest is one
  invalid entry. Recovery removes the current pointer, removes the invalid pointed
  version when it is a safe cache-owned directory, increments `invalid_entries`,
  increments `removed_versions` when that directory is removed, and includes the key
  in `removed_cache_keys`. The invalid data must remain a cache miss before and after
  recovery; recovery must never promote a partial version into a hit.
- A cache-owned version not named by a valid current pointer is unreachable and is
  removed and counted in `removed_versions`, while the valid current version survives.
- Staging belonging to an exact live active lease is preserved and reported through
  `kept_active_builds` and `active_cache_keys`. Expired, abandoned, or orphaned
  cache-owned staging is removed and counted in `removed_staging_dirs` and
  `removed_cache_keys`.
- Existing terminal `committed` and `aborted` lock records are left unchanged. Files
  unrelated to the cache layout are left untouched.

`RecoveryReport` reports `kept_entries`, `kept_active_builds`,
`removed_staging_dirs`, `removed_versions`, `invalid_entries`, and sorted tuples of
the affected cache keys. Counts describe filesystem actions performed by that run;
the key tuples are sorted and contain no duplicates. Running recovery again without
external changes reports the still-kept valid/live state but no new removals or new
invalidations.

## Failure behavior

Builder, staging-write, manifest-write, pre-publication, and pointer-publication
failures release the current writer and clean its private staging data without
damaging an existing valid entry. If `after_publish` raises, publication remains a
valid success on disk and a client retry returns that entry without rebuilding.
Failures for one key do not block or modify another key.

Run the public tests with:

```bash
python -m pytest -q
```
