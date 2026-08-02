# Deterministic Dependency Resolver

This project is an in-memory package resolver for a normalized registry. It
combines transitive Semantic Versioning constraints, platform and feature
dependencies, declared conflicts, strict lock data, deterministic installation
order, memoization, and atomic registry replacement.

Run the public suite from the workspace root with `python -m pytest -q`.

## Public API

`dependency_resolver.bootstrap.build_application(registry)` returns a
`ResolverApplication`. Its `api` facade is supported:

```python
api.resolve(requirements, *, platform, features=None, lock=None)
api.replace_registry(registry)
api.cache_size()
```

Repositories are exposed only for diagnostics and adapter tests. Results are
fresh JSON-compatible copies. Domain exceptions are preserved.

## Registry normalization

The registry is a non-empty mapping of trimmed, non-empty, case-sensitive
package names to non-empty mappings of version string to metadata. Versions are
valid Semantic Versioning 2.0.0 strings and normalized without build metadata;
two raw versions that normalize to the same value are invalid.

Version metadata is a mapping with:

- `digest`: exactly 64 lowercase hexadecimal characters;
- optional boolean `yanked` (default false);
- optional `dependencies`: package-to-constraint mapping;
- optional `optional_dependencies`: feature name to dependency mapping;
- optional `platform_dependencies`: keys `linux`, `win32`, or `darwin`, each
  containing a dependency mapping;
- optional `conflicts`: package-to-constraint mapping.

Unknown fields are ignored. Package/feature names and dependency names are
trimmed non-empty strings. All referenced packages must exist. An unknown
feature requested during resolution raises `ValidationError(field="features")`.

Invalid initial or replacement data raises `ValidationError(field=...)`.
Failed replacement leaves registry revision, cache, and all old behavior
unchanged. Successful replacement increments revision once and clears cache.

## Constraints and SemVer

Requirements is a non-empty package-to-constraint mapping. A constraint is one
of:

- exact `1.2.3` (optional leading `=`);
- wildcard `1.2.*`;
- caret `^1.2.3`;
- tilde `~1.2.3`;
- a comma-separated AND of comparators using `>`, `>=`, `<`, or `<=`.

Whitespace around comma-separated terms is ignored. Empty terms and other
syntax are invalid. Caret upper bounds are the next major when major is
non-zero, next minor when major is zero and minor is non-zero, otherwise next
patch. Tilde upper bound is the next minor.

SemVer precedence follows 2.0.0: numeric core comparison, release after
prerelease, numeric prerelease identifiers numerically and before non-numeric,
and build metadata ignored. Prerelease versions are excluded unless at least
one active constraint for that package explicitly contains a prerelease.

Every constraint accumulated for a package is an AND requirement. Booleans are
never versions or numeric identifiers.

## Resolution

The resolver must search, not greedily commit. For an unresolved package,
candidate releases are considered by descending SemVer precedence. Yanked
versions are never selected. When a candidate later makes another package
unsatisfiable or violates a declared conflict, the resolver backtracks to the
next candidate. If no complete solution exists, raise
`UnsatisfiedConstraints(package, constraints)` and leave cache unchanged.

Normal dependencies are always active. Optional dependencies activate only for
features explicitly requested for the owning package. Platform dependencies
activate only for the requested platform. Features is an optional mapping of
package name to a list of unique feature names; order is irrelevant.

A selected release's `conflicts` mapping prohibits another selected package
when its selected version matches that constraint. Conflict checks are
bidirectional across selected releases. If all candidate solutions conflict,
raise `PackageConflict(package, other)`.

Dependency cycles raise `DependencyCycle(path)`, where `path` begins and ends
with the repeated package. Otherwise the installation plan is a deterministic
topological order: every dependency precedes its dependents and alphabetic
package name breaks ties between currently ready packages.

## Strict lock input and output

An optional lock is a mapping from package name to exactly useful fields
`version` and `digest`. When supplied it is strict:

- every resolved package has exactly one lock entry and no extra entry exists;
- each pin exists, is not yanked, and satisfies all active constraints;
- every digest exactly equals registry metadata.

Any violation raises `LockError(package, reason)` and does not populate cache.
Lock pins constrain search rather than being checked only after a different
solution is chosen.

The result shape is:

```python
{
    "platform": "linux",
    "packages": [
        {"name": "core", "version": "1.4.0", "digest": "..."},
        {"name": "app", "version": "2.0.0", "digest": "..."},
    ],
    "lock": {
        "app": {"version": "2.0.0", "digest": "..."},
        "core": {"version": "1.4.0", "digest": "..."},
    },
}
```

Lock mapping keys are alphabetic. Package list follows installation order.

## Cache isolation

The cache fingerprint includes normalized requirements, platform, normalized
features, strict lock content, and registry revision. Equivalent mapping/list
orders share an entry; any semantic difference does not. Failed resolutions
create no entry. `cache_size()` reports entry count.

Both newly resolved and cached results are deep fresh copies: callers cannot
mutate versions, package rows, or nested lock data retained by the service.

## Exceptions and architecture

All errors inherit `DependencyResolverError` and are exported:
`ValidationError`, `UnknownPackage`, `UnsatisfiedConstraints`,
`DependencyCycle`, `PackageConflict`, and `LockError`.

The facade delegates to `ResolverService`; it must not reach into repository
storage. Avoid test-specific branches, dynamic execution, or coupling
production code to grader paths.
