# Time-zone Series Scheduler

This project is an in-memory scheduling boundary for recurring appointments on
named resources. Series are defined in resource-local wall time, expanded into
UTC occurrences, checked for conflicts, and support cancellation, rescheduling,
and exactly-once seat bookings.

Run the public suite from the workspace root with `python -m pytest -q`.

## Public API

`series_scheduler.bootstrap.build_application(resources)` returns a
`SchedulerApplication`. Its `api` facade is the supported interface:

```python
api.create_series(payload, *, idempotency_key)
api.occurrences(series_id, window_start, window_end)
api.cancel_occurrence(series_id, original_start)
api.reschedule_occurrence(series_id, original_start, new_start)
api.book(series_id, occurrence_start, seats, *, request_id)
api.bookings()
```

Repositories are exposed only for diagnostics and adapter tests. Returned
values are fresh JSON-compatible copies. Domain exceptions are preserved.

## Resource and series normalization

`resources` is a non-empty mapping keyed by trimmed, non-empty, case-sensitive
resource IDs. Each value is a mapping with:

- IANA `timezone` accepted by `zoneinfo.ZoneInfo`;
- positive integer `capacity` (booleans invalid).

Unknown fields are ignored.

A series payload is a mapping containing trimmed non-empty `series_id` and
`resource_id`, local `start`, positive integer `duration_minutes`, and a
`recurrence` mapping. Unknown fields are ignored. `start` is exactly a naive
minute-precision ISO local time (`YYYY-MM-DDTHH:MM`); aware values and seconds
are invalid.

Recurrence contains:

- `frequency`: `DAILY` or `WEEKLY` (case-sensitive);
- positive integer `interval`;
- positive integer `count`, at most 100;
- for `WEEKLY`, a non-empty unique `weekdays` list using `MO` through `SU`.

For DAILY, candidate `n` is local start plus `n * interval` days. For WEEKLY,
weeks are anchored to the Monday containing local start. Selected weekdays are
processed Monday through Sunday within every `interval`th week; candidates
before local start in the first week are ignored. `count` counts local
candidates before daylight-saving filtering.

Optional `exdates` is a list of unique local timestamps in the same exact
format. An exdate suppresses a matching original local candidate. It may name a
candidate outside the generated count and is then harmless.

Validation failures raise `ValidationError(field=...)`. An unknown resource
raises `UnknownResource(resource_id)`. A duplicate series ID under a different
key raises `DuplicateSeries(series_id)`.

## Local time and recurrence

Every candidate preserves its local wall-clock time across offset changes.
Local times are resolved in the resource timezone. A nonexistent time in a
spring-forward gap is omitted; it still consumes one recurrence candidate.
For an ambiguous fall-back time, use `fold=0` (the earlier instant).

Occurrence timestamps are canonical UTC strings ending in `Z`, at
minute precision. An occurrence is identified forever by its **original UTC
start**, even after it is rescheduled.

`occurrences(series_id, window_start, window_end)` requires canonical or
offset-aware ISO instants and a non-empty half-open UTC window
`[window_start, window_end)`. It returns active occurrences whose effective
start is inside the window, sorted by `(start, original_start)`. Window end is
exclusive. Each item is:

```python
{
    "series_id": "clinic",
    "resource_id": "room-a",
    "original_start": "2026-03-01T14:00Z",
    "start": "2026-03-01T14:00Z",
    "end": "2026-03-01T14:30Z",
    "capacity": 3,
    "reserved": 0,
}
```

## Conflicts, cancellations, and rescheduling

All effective occurrences on one resource use half-open intervals. Touching
endpoints do not conflict. Creating a series is all-or-nothing: every generated
active occurrence is checked against every other active series. Any overlap
raises `ScheduleConflict(resource_id, start)` and changes no state.

Cancellation and rescheduling take an original UTC start. A missing original
occurrence raises `UnknownOccurrence(series_id, original_start)`. Canceling an
already canceled occurrence is idempotent. A canceled occurrence is absent from
queries and cannot be booked. Its existing bookings remain in history.

Rescheduling changes only effective start and end, preserves original identity
and reservations, and checks conflicts against other active occurrences
(excluding itself). Moving to its current effective start is idempotent.
Rescheduling a canceled occurrence raises `OccurrenceCancelled`. An aware
`new_start` is interpreted as an absolute instant and canonicalized to UTC.

## Exactly-once creation and booking

Creation idempotency keys and booking request IDs are trimmed non-empty strings,
at most 128 characters, containing letters, digits, `.`, `_`, `:`, and `-`.
The creation fingerprint includes every normalized series field, recurrence
field, and exdate. The booking fingerprint includes series ID, canonical
effective occurrence start, and seats.

Reusing a key with the same normalized operation returns its original result
without changes. Reusing it with different input raises
`IdempotencyConflict(key)` or `RequestConflict(request_id)`.
Failed operations bind nothing.

`seats` is a positive integer (booleans invalid). Capacity is cumulative across
all bookings for an occurrence. An over-capacity attempt raises
`CapacityExceeded(series_id, occurrence_start, requested, available)` and
changes no repositories or booking ID. A successful booking receives
`booking-000001`, `booking-000002`, and so on.

Booking uses the current effective start. A stale pre-reschedule effective start
does not resolve. Booking results include `booking_id`, `series_id`,
`occurrence_start`, and `seats`. `bookings()` returns durable booking order.

## Exceptions and architecture

All errors inherit `SchedulerError` and are exported: `ValidationError`,
`UnknownResource`, `DuplicateSeries`, `UnknownSeries`, `UnknownOccurrence`,
`OccurrenceCancelled`, `ScheduleConflict`, `CapacityExceeded`,
`IdempotencyConflict`, and `RequestConflict`.

The facade delegates to `SchedulerService` and must not reach into repository
storage. Avoid test-specific branches, dynamic execution, or coupling
production code to grader paths.
