# Worker Queue lease and recovery exercise

This repository contains a small, synchronous Python job queue. It is intentionally
implemented with an in-memory durable snapshot rather than a database so that lease
fencing, retry idempotency, cancellation, and restart recovery can be tested without
network calls or wall-clock sleeps. Some correctness defects remain in the
implementation. Fix the implementation while preserving this contract.

## Public API

The package exports `build_application`, `build_api`, the public exception classes,
and `JobStatus`. `build_application(snapshot=None, *, lease_seconds=30.0)` returns a
`QueueApplication` with an `api` attribute and a JSON-compatible `snapshot()` method.
`build_api` accepts the same arguments and returns only the API.

The API methods are:

```python
submit(request, *, request_id, now)
claim(worker_id, *, now)
complete(job_id, lease_token, result, *, now)
fail(job_id, lease_token, error, *, retry_at, now)
cancel(job_id, *, now)
recover(*, now)
get_job(job_id)
list_jobs()
history(job_id=None)
```

All times are explicit finite numbers. A caller controls time; the queue must not read
the system clock or sleep. Completion results, like payloads, must be JSON-compatible.
Returned jobs, claims, snapshots, results, payloads, and history entries are defensive
copies. A missing runnable job makes `claim` return `None`. A missing job raises
`JobNotFound`.

## Submission and request idempotency

A request is a mapping with exactly `task`, optional `payload` (a JSON-compatible
mapping, default `{}`), and optional `max_attempts` (an integer from 1 through 10,
default 3). Leading and trailing whitespace is removed from `task`; identifiers are
also trimmed. Unknown fields and invalid values raise `ValidationError`.

`request_id` is an idempotency key for the entire normalized request, including the
task, the complete nested payload, and `max_attempts`. Repeating an equivalent request
returns the original job. It must not allocate another ID or append another history
entry. Reusing the key for any different normalized content raises
`IdempotencyConflict`, again without changing jobs, ID sequences, the request map, or
history. Dictionary key order is not significant; array order and values are.

Jobs are ordered by first successful submission. A rejected or idempotent submission
does not alter that order.

## Claims, leases, and fencing

Only `pending` jobs and `retry_waiting` jobs whose `retry_at <= now` are runnable.
Claims choose the first runnable job in original submission order. A claim increments
`attempt` exactly once, increments `lease_generation`, assigns the worker, and returns
an opaque `lease_token` with an expiry of `now + lease_seconds`.

An unexpired lease is held exclusively. At or after its expiry, the job becomes
runnable and another worker may claim it. Expiry is processed either by `claim` or by
`recover`, and produces one `lease_expired` history entry per expired lease generation.
Every completion or failure must present the exact currently stored token before its
expiry. A token from an expired, replaced, or cancelled lease raises `StaleLease` and
has no side effects—even while a newer generation is leased for the same job.

## Completion, failure, and retries

A valid completion moves the job to `completed`, stores a defensive copy of the
result, clears all active lease fields, and appends one `completed` event. An exact
retry of the same completion request returns the current job without another event;
changing the result for that same operation key raises `OperationConflict`.

`attempt` counts leases issued, not API calls. A valid failure does not increment it.
If `attempt < max_attempts`, failure moves the job to `retry_waiting`, stores `retry_at`
and the error, clears the lease, and appends one `retry_scheduled` event. It cannot be
claimed before `retry_at`. If `attempt >= max_attempts`, failure instead moves it to
terminal `failed`, clears `retry_at` and the lease, and appends one `failed` event.
Terminal jobs are never claimable.

An exact retry of `fail` with the same job, token, error, and normalized `retry_at`
returns the current job without changing attempt, retry time, or history. Changed
failure content for that operation key raises `OperationConflict`. A different
operation using an old token is stale.

## Cancellation

`pending`, `retry_waiting`, and `leased` jobs may be cancelled. Cancellation clears
retry and every lease field, invalidating the worker's token, then appends one
`cancelled` event. Repeating cancellation of a cancelled job is an idempotent no-op.
Cancelling an already `completed` or `failed` job raises `InvalidStateTransition` with
no state or history changes. Cancelled jobs are terminal and never claimable.

## Snapshot and restart recovery

`QueueApplication.snapshot()` captures jobs in submission order, ID and lease
sequences, request bindings, operation receipts, and ordered history. Passing that
snapshot to `build_application` simulates a process restart. The caller then invokes
`recover(now=...)` before serving work.

Recovery changes only expired `leased` jobs: each becomes `pending`, has its lease
fields cleared, and receives one `lease_expired` event. Unexpired leases remain held.
Existing pending jobs and retry-waiting jobs retain their states; future retries remain
unavailable until due. Completed, failed, and cancelled jobs remain terminal. Attempts,
lease generations, results, errors, request bindings, submission order, event order,
and ID sequences are preserved. Running recovery repeatedly causes no additional state
or history mutation once all expired leases have been processed.

The recovery report contains the number of leases expired in that call, currently
runnable jobs, held unexpired leases, terminal jobs, and the IDs whose expired leases
were released. `runnable_jobs` includes pending jobs plus due retry-waiting jobs.

## History

History is globally ordered by contiguous integer `sequence` and can be filtered by
job. State-changing operations append exactly one event. Validation errors, conflicts,
stale operations, and exact idempotent retries append none. Event attempts and statuses
describe state immediately after their operation. Snapshot/restart must continue the
existing sequence without gaps or duplicates.

Run the public tests with:

```bash
pytest -q
```
