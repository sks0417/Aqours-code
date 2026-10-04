# Job runner specification

This document describes the behaviour the job runner must have once
cancellation, automatic retries, and restart recovery are added. The current
code implements only submitting and running jobs.

## Public interface

Only these names are public. Tests use nothing else.

| Module | Names |
| --- | --- |
| `jobrunner.models` | `JobStatus`, `Job` |
| `jobrunner.store` | `JobStore(path)` — a SQLite database at `path` |
| `jobrunner.runner` | `Runner(store, handlers, clock, max_attempts=3, base_delay=1.0)` with `submit(kind, payload) -> Job`, `run_once() -> Job \| None`, `cancel(job_id) -> Job`, `get(job_id) -> Job` |
| `jobrunner.errors` | `TransientError`, `JobNotFound`, `InvalidTransition` |
| `jobrunner.api` | `handle(runner, method, path, body=None) -> (status_code, dict)` |
| `jobrunner.dashboard` | `render_jobs(jobs) -> str` |

- `handlers` maps a job kind to a function `handler(payload) -> result`. The
  result must be JSON-serializable.
- `clock` is a function returning the current time in seconds (a float). The
  runner never reads the real clock; all times come from `clock()`.
- `submit` raises `ValueError` for a kind without a handler.
- `get` and `cancel` raise `JobNotFound` for an unknown id.

## Job

`JobStatus` has the values `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`, and
`CANCELLED` (string values `"pending"`, `"running"`, `"succeeded"`,
`"failed"`, `"cancelled"`). `SUCCEEDED`, `FAILED`, and `CANCELLED` are final.

`Job` has the fields `id` (int), `kind`, `payload`, `status`, `result`,
`created_at`, and:

| Field | Meaning |
| --- | --- |
| `attempts` | how many times the job has started running (0 for a new job) |
| `max_attempts` | the runner's `max_attempts` when the job was submitted |
| `next_run_at` | earliest time the job may run; `clock()` at submission |
| `last_error` | message of the most recent failure, or `None` |
| `cancel_requested` | `True` once cancellation was requested while running |

## Running jobs

`run_once()` runs at most one job and returns it (with its new state), or
returns `None` when no job is due. It only picks a job whose status is
`PENDING` and whose `next_run_at <= clock()`; among those it picks the
smallest `next_run_at`, then the oldest job (smallest id).

Before calling the handler, the runner stores the job as `RUNNING` and adds 1
to `attempts`. Then:

- the handler returns: the job becomes `SUCCEEDED` and `result` is stored;
- the handler raises `TransientError`: a retryable failure (see Retries);
- the handler raises any other `Exception`: the job becomes `FAILED` at once
  and is not retried; `last_error` is the exception message;
- the handler raises a `BaseException` that is not an `Exception` (for
  example `KeyboardInterrupt`): it propagates out of `run_once()` and the job
  stays `RUNNING` in the database. This is how a process crash looks.

## Retries

After the n-th failure (n counts from 1, so n equals `attempts`):

- if `attempts < max_attempts`, the job goes back to `PENDING` with
  `next_run_at = now + base_delay * 2 ** (n - 1)`, where `now` is `clock()`
  when the failure is handled;
- otherwise the job becomes `FAILED`.

`last_error` always holds the message of the most recent failure.

## Cancellation

`cancel(job_id)`:

- a `PENDING` job becomes `CANCELLED` immediately, including a job waiting
  for a retry;
- a `RUNNING` job gets `cancel_requested = True` and stays `RUNNING`. When
  its handler finishes, whether it succeeded or failed, the job becomes
  `CANCELLED`; its result is not stored and it is not retried;
- a job that is already `SUCCEEDED`, `FAILED`, or `CANCELLED` raises
  `InvalidTransition`.

A `CANCELLED` job never runs again. `cancel` returns the job's new state.

## Restart recovery

Creating a new `Runner` on the same database file simulates a process
restart. When it is created:

- `PENDING` jobs can run as before, still respecting `next_run_at`;
- a `RUNNING` job was interrupted. If `cancel_requested` is set it becomes
  `CANCELLED`. Otherwise the interruption counts as one failure and the
  Retries rules apply, with `now = clock()` at construction; `last_error`
  says the job was interrupted.

`attempts`, `max_attempts`, `next_run_at`, `last_error`, and
`cancel_requested` are stored in SQLite, so they survive a restart.

## REST API

`handle(runner, method, path, body=None)` returns `(status_code, dict)`:

| Request | Response |
| --- | --- |
| `POST /jobs` with body `{"kind": ..., "payload": {...}}` | `201` and the job; `400` for a missing or unknown kind |
| `GET /jobs/<id>` | `200` and the job, or `404` |
| `GET /jobs` or `GET /jobs?status=<status>` | `200` and `{"jobs": [...]}` ordered by id; `400` for an unknown status |
| `POST /jobs/<id>/cancel` | `200` and the job; `404` for an unknown id; `409` if the job cannot be cancelled |

Errors are returned as `{"error": "<message>"}`. Any other method or path
returns `404`.

A job is returned as a JSON object with at least `id`, `kind`, `payload`,
`status` (the string value), `result`, `attempts`, `max_attempts`,
`next_run_at`, `last_error`, and `cancel_requested`.

## Dashboard

`render_jobs(jobs)` returns an HTML `<table>` with one row per job, in the
order given, and the columns `ID`, `Kind`, `Status`, `Attempts`,
`Next retry`, and `Actions`:

- `Status` is the status string value;
- `Attempts` is `attempts/max_attempts`, for example `2/3`;
- `Next retry` is `next_run_at` with one decimal for a `PENDING` job that
  has failed before (`attempts > 0`), and `-` otherwise;
- `Actions` holds, for a job that can be cancelled (`PENDING` or
  `RUNNING`), the form
  `<form method="post" action="/jobs/<id>/cancel"><button type="submit">Cancel</button></form>`,
  and is empty otherwise.

All text taken from a job is HTML-escaped.
