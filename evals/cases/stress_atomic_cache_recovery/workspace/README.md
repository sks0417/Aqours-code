# Local artifact cache recovery exercise

This repository implements a small, standard-library-only cache for local build
artifacts. The starter is runnable, but several related correctness defects remain.
Fix the implementation while preserving the contracts below.

## Implementation boundaries

Only these existing implementation files may be edited:

- `src/artifact_cache/key.py`;
- `src/artifact_cache/lock.py`;
- `src/artifact_cache/manifest.py`;
- `src/artifact_cache/store.py`;
- `src/artifact_cache/recovery.py`;
- `src/artifact_cache/service.py`.

Do not modify `README.md`, `pyproject.toml`, `src/artifact_cache/__init__.py`,
`src/artifact_cache/models.py`, or any file under `tests/`. Keep the implementation
standard-library-only. Do not use sleeps, network services, process-wide build
serialization, `eval`/`exec`, test-framework imports, test or grader detection,
environment or stack inspection for test-specific behavior, or any other dynamic
test coupling.

Preserve the starter's module responsibilities and these module-level declarations;
they are part of the required implementation structure and must not be renamed,
replaced with aliases, nested, inlined into another module, or converted between a
class and a function:

- `key.py`: function `cache_key`;
- `lock.py`: class `LeaseRegistry`;
- `manifest.py`: functions `artifact_digest`, `build_manifest`, `read_manifest`,
  and `write_manifest`;
- `store.py`: class `CacheStore`;
- `recovery.py`: class `RecoveryManager`;
- `service.py`: class `ArtifactCache`.

Refactoring methods inside those components is allowed. The six implementation
files must remain present, importable, syntactically valid, and free of any imports
from `pytest`, `unittest`, `evals`, or grader code. Run the complete public test suite
before finishing.

## Public API

The package exports `ArtifactCache`, the immutable request/result models, the public
exceptions, and the manifest constants listed in `artifact_cache.__all__`. Existing
export names, constant values, public dataclass field order, and public method
signatures are part of the API and must remain unchanged.

`artifact_cache.__all__` contains exactly the names below and no others. The
constant values are also exact:

```text
ARTIFACT_NAME = "artifact.bin"
MANIFEST_NAME = "manifest.json"
CURRENT_MANIFEST_VERSION = 2
LEGACY_MANIFEST_VERSION = 1
DEFAULT_NAMESPACE_VERSION = "artifact-cache-v1"

ArtifactCache
BuildRequest, BuildLease, CacheEntry, RecoveryReport
CacheError, BuildInProgress, InvalidArtifact, InvalidManifest,
InvalidRequest, StaleWriter
```

`BuildInProgress`, `InvalidArtifact`, `InvalidManifest`, `InvalidRequest`, and
`StaleWriter` remain subclasses of `CacheError`. The four public dataclasses remain
frozen, with exactly these fields in this order:

```text
BuildRequest: inputs, options, tool_version, namespace_version,
              artifact_format, scratch_dir
BuildLease:   cache_key, writer_id, generation, token, expires_at,
              staging_dir, artifact_format
CacheEntry:   cache_key, artifact, manifest, cache_hit
RecoveryReport: kept_entries, kept_active_builds, removed_staging_dirs,
                removed_versions, invalid_entries, removed_cache_keys,
                active_cache_keys
```

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

The exact public signatures are:

```text
ArtifactCache(root, *, clock=None, lease_seconds=30.0, fault_hook=None)
key_for(request)
get(request)
begin_build(request, *, writer_id)
commit(lease, artifact)
abort(lease)
get_or_build(request, builder, *, writer_id)
recover()
```

`root` is a string or path-like cache location shared durably by every
`ArtifactCache` instance opened on that location. `clock` is a zero-argument callable
returning a finite `int` or `float`; it defaults to a monotonic clock.
`lease_seconds` must be a finite, strictly positive `int` or `float`. Booleans are
not numeric clock or duration values. Invalid clocks or durations raise
`InvalidRequest`, including values whose conversion to a finite float would overflow.
Tests and callers may inject a clock, so the implementation must not sleep.
`fault_hook`, when supplied, is called as `fault_hook(stage, path)` at documented
publication boundaries (`artifact_staged`, `manifest_staged`, `before_publish`, and
`after_publish`). It may raise to simulate a crash or I/O failure.

`BuildRequest` contains:

- `inputs`: a non-empty mapping whose logical input names are non-empty strings and
  whose content values are `bytes` or `str`;
- `options`: a mapping with string keys whose values may recursively contain
  mappings with string keys, lists, tuples, sets, frozensets, JSON scalar values,
  and bytes;
- `tool_version`, `namespace_version`, and `artifact_format`: non-empty strings;
- optional `scratch_dir`, an execution-only location that never changes output
  identity and therefore must not affect a cache key.

Unsupported request values, non-string option keys, empty required strings, and
non-finite numbers are invalid and raise `InvalidRequest`. `writer_id` must contain
at least one non-whitespace character; empty or whitespace-only values raise
`InvalidRequest`, as do non-string writer IDs. A non-callable builder passed to
`get_or_build` also raises `InvalidRequest`. Passing anything other than a
`BuildLease` to `commit` or `abort` raises `StaleWriter`.

`get` returns `None` for a missing or invalid cache entry. A returned `CacheEntry`
contains detached artifact bytes, a detached normalized manifest mapping, and a
`cache_hit` flag. `commit` returns `cache_hit=False`; reads and build reuse return
`cache_hit=True`. Mutating a returned manifest must not mutate stored state or affect
any later read, retry, or returned entry.

## Concurrency and lease fencing

`begin_build` creates an exclusive lease for one cache key. A live lease raises
`BuildInProgress` for another explicit writer. At or after expiry, a later writer may
acquire the next monotonically increasing generation. Explicit acquisition, abort,
commit, recovery mutation, and the builder portion of `get_or_build` may be
serialized by the existing per-key writer mutex. `ArtifactCache` instances that
refer to the same resolved root must coordinate those writers in the same process.
Writers for different keys must make true parallel progress; a process-wide build
mutex is forbidden.

Concurrent `get_or_build` calls for equal requests execute exactly one builder. A
waiting equal request re-reads and reuses the resulting entry with `cache_hit=True`
instead of running its builder. A completed entry is also reused across distinct
`ArtifactCache` instances opened on the same root.

Ordinary reads are deliberately outside writer serialization. `get` and the store
read path must not acquire or wait for the per-key writer mutex. In particular, if a
writer is paused in `before_publish` before the atomic current-pointer replacement,
a concurrent reader for that same key must return promptly with the previous
complete entry. After replacement, later readers return the new complete entry.

Only the exact current live lease may publish. Validation compares the supplied
lease with the authoritative active lock record, including the cache key, writer ID,
generation, opaque token, staging-directory identity, and artifact format. Any
inconsistent lease data is forged or stale. Liveness uses the authoritative lock
record's finite `expires_at`, not a caller-controlled value: the lease is live only
while `now < expires_at`.

A `commit` rejected with `StaleWriter` is a validation rejection and must perform no
state or filesystem mutation. This applies to expired, replaced, forged, and
cross-key leases, including a lease that is still recorded as the current lock owner
but has expired at the exact `expires_at` boundary. `expires_at == now` is expired.
The rejected lease's own staging directory must remain unchanged, as must every
other writer's staging directory, the cache entry, current pointer, and lock state.

A successful publication must finalize and remove its owned staging directory before
`commit` returns; recovery must not need to collect staging left by a successful
commit. Staging removal is permitted only through successful owned publication
finalization, an explicit owned `abort`, an owned build or publication failure after
successful lease validation, or recovery. `abort` is idempotent and may clean only
its own staging data. Repeating an abort leaves the terminal lock record byte-for-byte
unchanged. A stale, forged, replaced, or cross-key lease must never be able to delete
another lease's staging or rewrite its lock through `abort`.

A successful publication transitions the matching active record to terminal
`committed`, and an ordinary pre-publication failure or explicit abort transitions
it to terminal `aborted`.
Terminal records are durable history: retries and recovery must not reactivate or
rewrite them. Completing an owned publication or failure after successful validation
must still record its terminal state if the clock advances during the operation;
terminalization must not fail merely because the lease expires after validation.

`commit` accepts `bytes`, `bytearray`, `memoryview`, a string or `Path` naming a file
inside the lease's staging directory, or `None` when the builder already wrote
`artifact.bin` there. Relative paths resolve inside that staging directory. A path
outside it, an unreadable path, a missing `artifact.bin` for `None`, or any other
artifact form raises `InvalidArtifact`. A zero-length bytes-like artifact or staged
file is valid; there is no non-empty-artifact requirement. Builders passed to
`get_or_build` receive the private staging directory and return one of these same
forms.

## Cache keys

Keys are lowercase SHA-256 hex digests. They cover every output-affecting request
field: complete input names and content, normalized options, tool version, namespace
version, and artifact format. Mapping insertion order and set iteration order are not
semantic at any nesting depth. Set and frozenset iteration order is likewise not
semantic. List and tuple element order is semantic. The canonical form must retain
type identity: bytes and text, list and tuple, set and frozenset, booleans and
integers, integers and floats, `None` and text, and other unequal supported typed
values must not collide merely because their printed or JSON-like forms look alike.
`scratch_dir` and actual temporary paths are excluded. Changing only `scratch_dir`
therefore preserves the key, while changing any input name/content, option,
`tool_version`, `namespace_version`, or `artifact_format` changes it. Invalid,
unsupported, or non-finite values raise `InvalidRequest`; do not silently stringify
them.

## Publication and integrity

Builds use a private directory under `staging/` on the same filesystem as `entries/`.
An artifact and its manifest must be completely written and verified before a single
atomic current-pointer replacement makes that immutable version visible. Readers may
observe the old complete version or the new complete version, never a partial or
mixed pair. A failed replacement must preserve an older valid entry. Repeating an
already successful commit with the exact lease and bytes is idempotent; changed data
for that consumed lease is stale and has no side effects. The exact retry returns the
same artifact and manifest with `cache_hit=False` without rewriting the current
pointer or terminal lock record. This remains possible after successful staging
finalization, including an exact `None` retry when the original artifact came from
the lease's staged `artifact.bin`.

Publication follows this observable order:

1. Perform every lease check that can reject with `StaleWriter` before the first
   state or filesystem mutation. Once validation succeeds, do not perform a later
   expiry or identity check that can turn already-mutated work into a
   `StaleWriter` validation rejection.
2. Resolve the artifact bytes, write the owned staged artifact completely, then call
   `fault_hook("artifact_staged", staged_artifact_path)`.
3. Build and write the complete schema-2 manifest, then call
   `fault_hook("manifest_staged", staged_manifest_path)`.
4. Materialize the immutable version directory as
   `versions/g<generation>-<lease_token>/` and verify its artifact, manifest, cache
   key, digest, and size before making it visible.
5. Call `fault_hook("before_publish", current_pointer_path)` while the old pointer
   is still unchanged. Readers must remain able to read that old entry while this
   hook is blocked.
6. Write the exact new pointer to a temporary sibling and use one atomic
   `os.replace` of `current.json` as the publication point. If that replacement
   raises, propagate the error and leave the previous pointer and entry intact.
7. Call `fault_hook("after_publish", current_pointer_path)` only after the new
   pointer is durable at the required abstraction level, then terminalize the owned
   lock and finalize its staging as described below.

Each hook is called at most once for its boundary and receives the path named above.
No pointer may name a version until both files exist and pass full validation.

A fault raised at `after_publish` occurs after durable publication. The lease must
be recorded as terminally `committed` rather than remaining `active` or becoming
`aborted`. Retrying the exact lease and bytes returns the published entry without
rebuilding, while retrying the consumed lease with different bytes is stale and has
no side effects. Once the current pointer has been replaced, later hook behavior or
clock advancement cannot turn that durable publication into an aborted transaction.
The original `after_publish` exception still propagates to its caller, and the owned
staging directory is finalized rather than left for recovery. A later
`get_or_build` observes the durable hit and must not invoke its builder.

Current manifests use schema version 2 and contain exactly `schema_version`,
`cache_key`, `digest`, `size`, `artifact_format`, `generation`, `writer_id`,
`lease_token`, and `created_at`. The schema version is the integer `2`; `cache_key`
and `digest` are lowercase SHA-256 hex strings; `size` and `generation` are
non-negative integers; `artifact_format`, `writer_id`, and `lease_token` are
non-empty strings; and `created_at` is finite. Booleans are not valid numeric field
values. The two SHA-256 fields contain exactly 64 lowercase hexadecimal characters;
uppercase, short, non-hex, or non-string values are invalid. Numeric validation must
also reject values whose finite-number conversion overflows rather than leaking an
`OverflowError`. Every cache read, including a legacy read, verifies digest and size
before returning a hit. Corrupt, truncated, replaced, malformed, or mismatched
entries are safe misses returned as `None` by `get`, without damaging other keys.
The current pointer has the JSON shape `{"version": "<version-directory>"}` and must
be a JSON object with exactly that one field and no extras. Its value must be a safe
single directory name with no absolute path, separator, `.`/`..`, or traversal, and
must resolve below that cache key's own `versions/` directory. The pointed directory
must contain both a readable artifact and a valid matching manifest. A malformed or
extra-field pointer, unsafe version name, missing file, incomplete version,
cache-key mismatch, digest mismatch, or size mismatch must never become a cache hit.

The durable layout is:

```text
entries/<cache-key>/
    current.json                  # {"version": "<version-directory>"}
    versions/g<generation>-<lease-token>/   # every new schema-2 publication
        artifact.bin
        manifest.json
locks/<cache-key>.json
staging/<cache-key>.g<generation>.<token>/
    lease.json
    ... build files ...
```

Temporary JSON files may be used beside their targets and replaced atomically.
Complex platform-specific `fsync` guarantees are out of scope.

### Lease state authority

`locks/<cache-key>.json` is the authoritative current lease record. Its `state`,
`expires_at`, `writer_id`, `generation`, `token`, `staging_dir`, and
`artifact_format` describe the lease that recovery must evaluate at the time it
runs; the record also contains its matching `cache_key`. Active records use
`state="active"`; owned completion records `committed` or `aborted`, and recovery
may mark an expired active record `abandoned`. Lock writes use a temporary sibling
and atomic replacement so readers never observe a partially written JSON record.

Each successful `begin_build` creates both the active lock record and its private
directory named exactly
`staging/<cache-key>.g<generation>.<token>/`. Before `begin_build` returns, that
directory contains `lease.json` with the lease's `cache_key`, `writer_id`,
`generation`, `token`, creation-time `expires_at`, and `artifact_format`. If staging
creation or marker writing fails after acquisition, the writer is released as an
owned failure and its partial private staging is removed so another writer can retry
immediately.

The `lease.json` inside a staging directory is an immutable creation-time ownership
marker. Recovery may use its cache key, writer, generation, token, and artifact
format to verify that the directory belongs to the current lease. Its `expires_at`
is only the expiry snapshot written when staging was created; it is not the current
source of truth and does not have to equal the authoritative lock record's current
expiry.

A staging directory is live when its ownership identity and directory name match the
current `active` lock record for that key and that lock record has `expires_at > now`.
Recovery must evaluate each key from its own lock record. An expired or invalid key
must not cause another key's live staging directory to be removed. Missing,
malformed, boolean, non-finite, or unrepresentable authoritative expiry values are
not live.

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
be verified. A schema 1 manifest returned through `CacheEntry.manifest` is normalized
to exactly `schema_version`, `cache_key`, `digest`, `size`, `artifact_format`, and
`generation`; the on-disk `sha256` field is exposed as `digest` and is not retained
as an additional returned field. Unknown schemas, missing required fields, unsafe
version names, extra fields, boolean schema versions, and values that cannot be
interpreted unambiguously are rejected. All new writes use the exact complete
schema-2 shape and preserve the request's `artifact_format`.

## Recovery

`recover` is repeatable. It preserves valid committed entries, live writers and their
staging directories, terminal lock records, and unrelated files. It removes expired
or abandoned cache-owned staging directories, unreachable cache-owned version
directories, and invalid current pointers/pointed entries. An expired active lock may
be marked abandoned, but committed/aborted records must not become active again.
Keys are recovered independently under their own writer coordination. Cache-owned
keys are lowercase 64-character hexadecimal cache-key directories or records in the
documented layout; arbitrary sibling files and directories are unrelated data and
must not be deleted or interpreted as cache state.

Recovery applies the following observable rules independently to each cache key:

- A valid current pointer and complete matching version remain readable and increment
  `kept_entries` on every recovery run. A successful commit has already finalized
  its staging, so recovery immediately after that commit reports no staging removal.
- A malformed or unsafe current pointer, a pointer to a missing version, or a pointed
  version with a missing, malformed, corrupt, or mismatched artifact/manifest is one
  invalid entry. Recovery removes the current pointer, removes the invalid pointed
  version when it is a safe cache-owned directory, increments `invalid_entries`,
  increments `removed_versions` when that directory is removed, and includes the key
  in `removed_cache_keys`. The invalid data must remain a cache miss before and after
  recovery; recovery must never promote a partial version into a hit.
- A cache-owned version not named by a valid current pointer is unreachable and is
  removed and counted in `removed_versions`, while the valid current version survives.
  Removing unreachable versions alone does not add that key to `removed_cache_keys`.
- Staging belonging to an exact live active lease is preserved and reported through
  `kept_active_builds` and `active_cache_keys`. Exact ownership requires the staging
  directory name and marker's cache key, writer ID, generation, token, and artifact
  format to match the authoritative active lock; only the lock's current expiry
  controls liveness. Expired, abandoned, identity-mismatched, or orphaned cache-owned
  staging is removed and counted in `removed_staging_dirs`. A recoverable cache key
  is added to `removed_cache_keys`; malformed orphan names with no valid cache key
  cannot add a fabricated key.
- Existing terminal `committed` and `aborted` lock records are left byte-for-byte
  unchanged. Files unrelated to the cache layout are left untouched. Recovery of one
  invalid or expired key must not remove, block, mark abandoned, or otherwise modify
  another key's entry, lock, or live staging.

`RecoveryReport` reports `kept_entries`, `kept_active_builds`,
`removed_staging_dirs`, `removed_versions`, `invalid_entries`, and sorted tuples of
the affected cache keys. `active_cache_keys` contains exactly the sorted live keys;
`removed_cache_keys` contains exactly the sorted valid keys whose invalid current
entry was removed or whose staging was removed in that run. Counts describe actual
filesystem actions performed by that run, and both tuples contain no duplicates.
Running recovery again without external changes reports the still-kept valid/live
state but no new removals, removed keys, or new invalidations.

## Failure behavior

Failures propagate their original exception to the caller after required state and
cleanup handling. Their observable outcomes are:

- A builder exception, staging-creation/write failure, manifest-write failure,
  `artifact_staged`, `manifest_staged`, or `before_publish` hook failure, or failed
  current-pointer replacement after successful lease validation records the exact
  owned lock as terminal `aborted`, removes only that lease's staging, and preserves
  any previous valid entry. Another writer can retry immediately and receives the
  next generation.
- A `StaleWriter` validation rejection is not an owned failure and performs none of
  that abort or cleanup handling; its lock, staging, versions, pointer, and entry
  remain byte-for-byte unchanged.
- Once the pointer replacement succeeds, publication is durable. An
  `after_publish` exception records the exact lease as terminal `committed`, finalizes
  its staging, leaves the new entry readable, and then propagates. Exact retry returns
  that entry without rebuilding; a retry with changed content raises `StaleWriter`
  and changes nothing.
- A failure for one key does not block or modify another key. In particular, a
  healthy entry remains readable while another key aborts or is recovered.

Run the public tests with:

```bash
python -m pytest -q
```

A clean run of the complete public suite is required before finishing, but it is not
a substitute for reviewing every contract above, including failure branches,
non-blocking reader visibility, exact on-disk shapes, recovery counters, and the
required module structure.
