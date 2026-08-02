# Progressive Delivery Evaluation Engine

This project is an in-memory boundary for evaluating feature flags against
user context. It supports ordered rules, reusable segments, prerequisites,
stable percentage rollouts, exactly-once exposure recording, and atomic
configuration replacement.

Run the public suite from the workspace root with `python -m pytest -q`.

## Public API

`rollout_engine.bootstrap.build_application(flags, segments=None)` returns a
`RolloutApplication`. Its `api` facade is the supported interface:

```python
api.evaluate(flag_key, context, *, request_id)
api.exposures()
api.replace_configuration(flags, segments=None)
```

Repositories are exposed on the application only for diagnostics and adapter
tests. Returned values are fresh JSON-compatible copies. Domain exceptions are
never translated to built-ins.

## Configuration normalization

`flags` is a non-empty mapping keyed by trimmed, non-empty, case-sensitive flag
keys. Unknown fields are ignored. Every flag contains:

- boolean `enabled`;
- non-empty string `off_variation`, `default_variation`, and `salt`;
- a non-empty `variations` mapping whose trimmed names are unique and whose
  values are JSON-compatible;
- optional `targets`, `prerequisites`, `rules`, and `rollout`.

The off/default variation and all referenced variations must exist.
`targets.users` and `targets.tenants` map trimmed IDs to variation names.
Prerequisites contain exactly useful fields `flag` and `variation`; the
referenced flag and variation must exist. The prerequisite graph must be
acyclic.

Each rule has a unique non-empty `id`, an integer `priority` (booleans invalid),
a non-empty `conditions` list, and exactly one of `variation` or `rollout`.
Rules are evaluated by ascending `(priority, original position)`.

A rollout is a non-empty list of `{variation, weight}` mappings. Weight is a
positive integer (booleans invalid), all variations exist, and weights total
exactly 10,000.

`segments` is a mapping of segment names to `{include, exclude, conditions}`.
Include/exclude are lists of trimmed user IDs. Conditions use the same shape as
rule conditions. Segment references may be nested, but the segment graph must
be acyclic. Exclusion always wins; otherwise explicit inclusion wins; otherwise
all segment conditions must match. A segment without an inclusion or conditions
matches nobody.

Configuration failures raise `ConfigurationError(field=...)` and leave the
entire previous configuration usable.

## Context and predicates

Context is a mapping with required trimmed `user_id`, optional trimmed
`tenant_id`, and optional `attributes` mapping. Attribute names are trimmed
non-empty strings and values must be JSON scalars (`null`, bool, number, or
string). Unknown context fields are ignored. The built-in attributes `user_id`
and `tenant_id` cannot be overridden.

Conditions have `attribute`, `operator`, and `value`. Supported operators are:

- `eq`, `neq`: scalar equality/inequality, with booleans distinct from numbers;
- `in`, `not_in`: membership in a non-empty list of scalars;
- `contains`: substring for two strings;
- `semver_gte`: compare a context string as Semantic Versioning 2.0.0;
- `segment`: value is a segment name and the subject is the context user.

All conditions in a rule or segment must match. Missing attributes do not match,
except that missing attributes satisfy `neq` and `not_in`. Invalid semantic
versions simply do not match.

Semantic versions compare numeric major/minor/patch identifiers, then
prerelease identifiers per SemVer: a release is newer than its prerelease,
numeric prerelease identifiers compare numerically and sort before non-numeric
identifiers, and build metadata is ignored.

## Evaluation order and rollouts

Evaluation uses this exact precedence:

1. a disabled flag returns `off_variation`;
2. prerequisites are evaluated recursively in listed order; a prerequisite
   passes only when its **variation name** equals the required variation, and
   any failed prerequisite returns the current flag's off variation;
3. user target, then tenant target;
4. first matching rule by ascending priority;
5. flag-level rollout, when present;
6. `default_variation`.

Prerequisite cycles encountered at runtime raise `EvaluationCycle` and produce
no exposures. A prerequisite evaluation is exposed before its dependent. Each
flag is exposed at most once per request even if reached by several dependency
paths.

Rollout bucketing is process-independent. Hash the UTF-8 string
`"{flag_key}:{user_id}:{salt}"` with SHA-256, interpret the first eight
hexadecimal characters as an integer, and take modulo 10,000. A bucket selects
the first cumulative weight strictly greater than the bucket. Rollout order is
configuration order.

The result shape is:

```python
{
    "flag_key": "checkout",
    "variation": "on",
    "value": true,
    "reason": "rule:staff",
}
```

Reasons are `disabled`, `prerequisite`, `target:user`, `target:tenant`,
`rule:<id>`, `rollout`, or `default`.

## Exactly-once requests and exposures

`request_id` is a trimmed non-empty string of at most 128 characters containing
only letters, digits, `.`, `_`, `:`, and `-`. A request fingerprint includes
the normalized top-level flag key and the complete normalized context,
including every attribute.

Reusing a request ID with the same normalized request returns the original
result without adding exposures. Reusing it for a different flag or context
raises `RequestConflict(request_id)` and changes nothing. Failed evaluations
bind no request and add no exposure.

Each committed exposure is:

```python
{
    "request_id": "req-1",
    "flag_key": "checkout",
    "variation": "on",
    "reason": "rule:staff",
}
```

`exposures()` returns durable order. Successful configuration replacement does
not erase requests or exposures. Existing request IDs still replay their
original result; new requests use the new configuration.

## Exceptions and architecture

All errors inherit from `RolloutEngineError` and are exported:
`ConfigurationError`, `ContextError`, `UnknownFlag`, `RequestConflict`, and
`EvaluationCycle`.

The facade delegates to `RolloutService`. It must not reach into repository
storage. Avoid test-specific branches, dynamic execution, or coupling
production code to grader paths.
