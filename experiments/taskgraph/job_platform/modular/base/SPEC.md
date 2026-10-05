# Job platform specification

This document describes the behaviour of the job platform. The current code
implements the job runner: submitting and running jobs, retries, cancellation,
restart recovery, the job routes of the REST API, and the jobs page of the
dashboard (sections "Jobs" to "Dashboard" without "(new)"). Every section
marked **(new)** still has to be added.

## Public interface

Only these names are public. Tests use nothing else.

| Module | Names |
| --- | --- |
| `jobrunner.models` | `JobStatus`, `Job`; (new) `RecurringJob`, `RateLimit`, `Subscription`, `Notification`, `AuditEntry` |
| `jobrunner.store` | `JobStore(path)` — a SQLite database at `path`, with `close()` |
| `jobrunner.runner` | `Runner(store, handlers, clock, max_attempts=3, base_delay=1.0)` with the methods below |
| `jobrunner.errors` | `JobRunnerError`, `JobNotFound`, `InvalidTransition`, `TransientError`; (new) `NotFound` |
| `jobrunner.api` | `handle(runner, method, path, body=None) -> (status_code, dict)` |
| `jobrunner.dashboard` | `render_jobs(jobs) -> str`, `render_page(runner, path) -> (status_code, str)` |

`Runner` methods:

| Area | Methods |
| --- | --- |
| jobs | `submit(kind, payload) -> Job` (new keyword arguments: `*, priority=0, tenant="default", run_at=None, depends_on=()`), `get(job_id) -> Job`, `list(status=None) -> list[Job]`, `run_once() -> Job \| None`, `cancel(job_id) -> Job` |
| priorities (new) | `set_priority(job_id, priority) -> Job`, `queue() -> list[Job]`, `tenants() -> list[dict]` |
| recurring jobs (new) | `schedule_recurring(kind, payload, interval_s, start_at=None, *, priority=0, tenant="default") -> int`, `get_recurring(definition_id) -> RecurringJob`, `list_recurring() -> list[RecurringJob]`, `pause_recurring(definition_id) -> RecurringJob`, `resume_recurring(definition_id) -> RecurringJob`, `delete_recurring(definition_id) -> None`, `recurring_jobs(definition_id) -> list[Job]`, `update_recurring(definition_id, *, payload=None, interval_s=None, priority=None, tenant=None) -> RecurringJob` |
| dependencies (new) | `dependents(job_id) -> list[Job]` |
| rate limits (new) | `set_rate_limit(kind, max_starts, window_s) -> RateLimit`, `clear_rate_limit(kind) -> None`, `list_rate_limits() -> list[RateLimit]` |
| notifications (new) | `subscribe(url, kinds=None, statuses=None) -> Subscription`, `unsubscribe(subscription_id) -> None`, `list_subscriptions() -> list[Subscription]`, `list_notifications(status=None, job_id=None) -> list[Notification]`, `deliver_notifications(sender) -> int`, `retry_notification(notification_id) -> Notification` |
| audit and statistics (new) | `history(job_id) -> list[AuditEntry]`, `audit_log(since=None) -> list[AuditEntry]`, `stats(window_s=None) -> dict` |

- `handlers` maps a job kind to a function `handler(payload) -> result`. The
  result must be JSON-serializable.
- `clock` is a function returning the current time in seconds (a float). The
  platform never reads the real clock and never sleeps; all times come from
  `clock()`, and "now" below means `clock()` at the time of the call.
- Invalid arguments raise `ValueError`. Wherever an `int` or a number is
  expected, a `bool` is not accepted. An unknown job id raises
  `JobNotFound`; an unknown recurring definition, subscription, or rate limit
  raises `NotFound`. `JobNotFound` is a subclass of `NotFound`, which is a
  subclass of `JobRunnerError`.
- Every model class has `to_dict()`, the JSON form used by the REST API.
  Statuses appear there as their string values.
- Everything the platform knows is stored in the SQLite database: creating a
  new `JobStore` and `Runner` on the same file (a "restart") loses nothing
  except what a handler or sender was doing when the process stopped.

## Jobs

`JobStatus` has the values `PENDING`, `RUNNING`, `SUCCEEDED`, `FAILED`, and
`CANCELLED` (string values `"pending"`, `"running"`, `"succeeded"`,
`"failed"`, `"cancelled"`). `SUCCEEDED`, `FAILED`, and `CANCELLED` are final.

`Job` fields:

| Field | Meaning |
| --- | --- |
| `id` | integer, increasing in creation order |
| `kind`, `payload` | the handler name and its argument (a dict) |
| `status`, `result` | current status; the handler's return value once `SUCCEEDED` |
| `created_at` | now at submission |
| `attempts` | how many times the job has started running (0 for a new job) |
| `max_attempts` | the runner's `max_attempts` when the job was created |
| `next_run_at` | earliest time the job may run: `run_at`, or now at submission |
| `last_error` | message of the most recent failure, or `None` |
| `cancel_requested` | `True` once cancellation was requested while running |
| `priority` (new) | integer, default `0`; higher runs first |
| `tenant` (new) | non-empty string, default `"default"` |
| `depends_on` (new) | list of job ids this job waits for, default `[]` |
| `definition_id` (new) | the recurring definition that created the job, or `None` |
| `cancel_reason` (new) | `None`, or for a `CANCELLED` job `"cancel_requested"` or `"dependency_failed"` |

`submit(kind, payload, *, priority=0, tenant="default", run_at=None,
depends_on=())` creates a `PENDING` job and returns it. It raises `ValueError`
for a kind without a handler, a `priority` that is not an `int` (a `bool` is
not accepted), a `tenant` that is not a non-empty string, a `run_at` that is
not an `int` or `float`, a `depends_on` that is not a list or tuple of ints,
or a dependency id that does not exist. Repeated dependency ids are kept once,
in first-seen order.

## Running jobs

`run_once()` runs at most one job and returns it with its new state, or
returns `None` when no job can run. It first creates the jobs of due recurring
definitions (see Recurring jobs). Then a job *can run* when all of these hold:

- its status is `PENDING` and `next_run_at <= now`;
- (new) every job in `depends_on` is `SUCCEEDED`;
- (new) its kind is not at its rate limit.

Among the jobs that can run, `run_once` picks, in this order of preference:

1. (new) the highest `priority`;
2. (new) among equal priorities, the job of the tenant whose most recent
   start is the oldest; a tenant none of whose jobs has ever started comes
   first;
3. the smallest `next_run_at`;
4. the oldest job (smallest id).

A job that cannot run never blocks another one: a higher-priority job waiting
for a dependency or a rate limit does not stop a lower-priority job.

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
  `next_run_at = now + base_delay * 2 ** (n - 1)`;
- otherwise the job becomes `FAILED`.

`last_error` always holds the message of the most recent failure.

## Cancellation

`cancel(job_id)`:

- a `PENDING` job becomes `CANCELLED` immediately, including a job waiting
  for a retry;
- a `RUNNING` job gets `cancel_requested = True` and stays `RUNNING`. When
  its handler finishes, whether it succeeded or failed, the job becomes
  `CANCELLED`; its result is not stored and it is not retried;
- a job that is already final raises `InvalidTransition`.

A `CANCELLED` job never runs again. `cancel` returns the job's new state.
(new) A job cancelled this way has `cancel_reason == "cancel_requested"`.

## Restart recovery

Creating a new `Runner` on the same database file simulates a process
restart. When it is created, a `RUNNING` job was interrupted. If
`cancel_requested` is set it becomes `CANCELLED`. Otherwise the interruption
counts as one failure and the Retries rules apply; `last_error` is
`"interrupted by a process restart"`.

## Priorities and fair scheduling (new)

- `priority` and `tenant` are set by `submit` and used by `run_once` as
  described in Running jobs. A tenant's most recent start counts starts of any
  of its jobs, at any priority, and survives a restart.
- `set_priority(job_id, priority)` changes the priority of a `PENDING` job
  and returns it. It raises `ValueError` for a priority that is not an `int`,
  `JobNotFound` for an unknown id, and `InvalidTransition` for a job that is
  not `PENDING`.
- `queue()` returns every `PENDING` job ordered by `priority` (highest
  first), then `next_run_at`, then id. It ignores tenants, dependencies, and
  rate limits.
- `tenants()` returns one dict per tenant that has jobs, ordered by tenant:
  `{"tenant", "pending", "running", "finished"}`, the numbers of its jobs
  that are `PENDING`, `RUNNING`, and final.

## Delayed and recurring jobs (new)

- `submit(..., run_at=t)` sets `next_run_at = t`: the job does not run before
  `t`. A `run_at` in the past is allowed (the job is due at once).
- `schedule_recurring(kind, payload, interval_s, start_at=None, *,
  priority=0, tenant="default")` creates a recurring definition and returns
  its id. `start_at` defaults to now. It raises `ValueError` for a kind
  without a handler, an `interval_s` that is not a positive `int` or `float`,
  a `start_at` that is not an `int` or `float`, or an invalid priority or
  tenant.
- `RecurringJob` fields: `id`, `kind`, `payload`, `interval_s`,
  `next_run_at` (the next period time; initially `start_at`), `priority`,
  `tenant`, `paused` (bool), and `last_job_id` (the most recently created job,
  or `None`).
- At the start of `run_once`, every definition that is not paused and has
  `next_run_at <= now` is handled, in id order:
  - if its last job is `PENDING` or `RUNNING`, no job is created (a
    definition has at most one unfinished job);
  - otherwise a job is created as if by `submit(kind, payload,
    priority=..., tenant=...)` at now, with `definition_id` set, and becomes
    the definition's `last_job_id`;
  - in both cases `next_run_at` advances to `start_at + k * interval_s` for
    the smallest integer k that makes it greater than now. Missed periods are
    skipped, never made up: at most one job is created per definition and
    call.
- `pause_recurring` and `resume_recurring` set `paused` and return the
  definition (pausing a paused definition, or resuming an active one, changes
  nothing). A paused definition creates no jobs; when it is resumed, the rule
  above applies at the next `run_once`. `delete_recurring` removes the
  definition; the jobs it created stay and keep their `definition_id`.
- `update_recurring(definition_id, *, payload=None, interval_s=None,
  priority=None, tenant=None)` changes the given fields (`None` leaves a field
  as it is), with the checks of `schedule_recurring` (a `payload` must be a
  dict), and returns the definition. Jobs already created keep their values,
  and `next_run_at` does not move.
- `get_recurring` returns one definition; `list_recurring` returns all of
  them ordered by id; `recurring_jobs(definition_id)` returns the jobs the
  definition created, ordered by id. Unknown ids raise `NotFound`.

## Dependencies (new)

- `submit(..., depends_on=[ids])`: every id must be an existing job, so the
  dependency graph can never contain a cycle.
- A job runs only when every job it depends on is `SUCCEEDED` (see Running
  jobs).
- When a job becomes `FAILED` or `CANCELLED` (for any reason, including
  restart recovery), every `PENDING` job that depends on it becomes
  `CANCELLED` with `cancel_reason == "dependency_failed"`, and so on
  downstream, before the call that caused it returns.
- A job submitted with a dependency that is already `FAILED` or `CANCELLED`
  is created and immediately becomes `CANCELLED` with `cancel_reason ==
  "dependency_failed"`; `submit` returns it in that state.
- `dependents(job_id)` returns the jobs that list `job_id` in `depends_on`,
  ordered by id. It raises `JobNotFound` for an unknown id.

## Rate limits (new)

- `set_rate_limit(kind, max_starts, window_s)` limits how often jobs of
  `kind` start: at any time, at most `max_starts` jobs of that kind may have
  started within the last `window_s` seconds, that is with a start time `t`
  such that `now - window_s < t <= now`. Every start counts, retries
  included, and so do starts that happened before the limit was set. It
  returns the limit and replaces an existing limit for the kind. It raises
  `ValueError` for a kind without a handler, a `max_starts` that is not an
  `int` of at least 1, or a `window_s` that is not a positive `int` or
  `float`.
- A job whose kind is at its limit stays `PENDING` and is skipped by
  `run_once`; jobs of other kinds can still run.
- `RateLimit` fields: `kind`, `max_starts`, `window_s`, and `recent_starts`
  (starts of the kind within the window at the time the object was made).
- `clear_rate_limit(kind)` removes the limit (`NotFound` if there is none);
  `list_rate_limits()` returns every limit ordered by kind.
- Limits and start times are stored in SQLite and still apply after a
  restart.

## Webhook notifications (new)

- `subscribe(url, kinds=None, statuses=None)` registers a webhook and returns
  a `Subscription` (`id`, `url`, `kinds`, `statuses`). `kinds` and `statuses`
  are lists of job kinds and status values (`JobStatus` members or their
  strings, stored as strings); `None` means all. It raises `ValueError` for a
  `url` that is not a non-empty string, a `kinds` that is not a list of
  strings, or an unknown status.
- Every state change (see Audit log for the list) is matched against every
  subscription: a subscription matches when the job's kind is in `kinds` and
  the new status is in `statuses` (each check passes when the list is
  `None`). For each match one notification is written to an outbox in SQLite,
  in the same database transaction as the state change itself.
- `Notification` fields: `id` (increasing in event order), `subscription_id`,
  `url`, `job_id`, `payload`, `status` (`"pending"`, `"delivered"`, or
  `"dead"`), `attempts`, `next_attempt_at`, and `last_error`. A new
  notification is `"pending"` with `attempts = 0` and `next_attempt_at` equal
  to the time of the change. `payload` is
  `{"subscription_id", "job_id", "kind", "tenant", "old_status",
  "new_status", "reason", "time"}` with the values of the change
  (`old_status` is `None` for a new job).
- `deliver_notifications(sender)` walks the pending notifications in id order
  and returns how many were delivered during the call. Notifications of the
  same subscription and job are delivered strictly in order: one is attempted
  only when every earlier one of the same subscription and job is
  `"delivered"` or `"dead"`, and only when its `next_attempt_at <= now`.
  An attempt calls `sender(url, payload)` and adds 1 to `attempts`:
  - if it returns, the notification becomes `"delivered"`;
  - if it raises an `Exception`, `last_error` is its message; after the 5th
    failed attempt the notification becomes `"dead"`, otherwise it stays
    `"pending"` with `next_attempt_at = now + 2 ** (attempts - 1)` (1, 2, 4,
    8 seconds) and later notifications of the same subscription and job wait.
    A notification that became `"dead"` no longer holds back the next ones,
    in the same call;
  - if it raises a `BaseException` that is not an `Exception`, it propagates
    and the notification stays as it was before the attempt: delivery is at
    least once, and a restart resumes every pending notification.
- `unsubscribe(subscription_id)` removes the subscription (`NotFound` if
  unknown); its pending notifications become `"dead"` with `last_error`
  `"unsubscribed"`. `list_subscriptions()` returns subscriptions ordered by
  id; `list_notifications(status=None, job_id=None)` returns notifications
  ordered by id, optionally filtered (`ValueError` for an unknown status).
- `retry_notification(notification_id)` gives a `"dead"` notification
  another chance: it becomes `"pending"` with `attempts = 0` and
  `next_attempt_at` = now (`last_error` is kept), and is returned. It raises
  `NotFound` for an unknown id and `InvalidTransition` for a notification that
  is not `"dead"` or whose subscription was removed. A retried notification
  again holds back the later notifications of its subscription and job.

## Audit log and statistics (new)

- Every state change of a job is recorded as an `AuditEntry` with `job_id`,
  `old_status` (`None` for a new job), `new_status`, `time` (now), and
  `reason`:

  | Change | `reason` |
  | --- | --- |
  | job created (`None` → `PENDING`), by `submit` or a recurring definition | `"submitted"` |
  | `PENDING` → `RUNNING` | `"started"` |
  | `RUNNING` → `SUCCEEDED` | `"succeeded"` |
  | `RUNNING` → `PENDING` (retry) or `FAILED` after a handler failure | the error message |
  | `RUNNING` → `PENDING` or `FAILED` during restart recovery | `"interrupted by a process restart"` |
  | → `CANCELLED` because of `cancel` (directly, after the handler, or during recovery) | `"cancel_requested"` |
  | → `CANCELLED` because of a dependency | `"dependency_failed"` |

  Setting `cancel_requested` on a running job, changing a priority, and
  retries waiting for their time are not state changes.
- `history(job_id)` returns the job's entries in the order they happened
  (`JobNotFound` for an unknown id). `audit_log(since=None)` returns the
  entries of all jobs in the order they happened, only those with
  `time > since` when `since` is given (`ValueError` if it is not a number).
- `stats(window_s=None)` returns a dict:

  | Key | Value |
  | --- | --- |
  | `window_s` | the argument |
  | `by_status` | `{status value: number of jobs}` with all five statuses |
  | `by_kind` | `{kind: number of jobs}` for every kind that has jobs |
  | `by_tenant` | `{tenant: number of jobs}` for every tenant that has jobs |
  | `finished` | jobs that became `SUCCEEDED` or `FAILED` within the window |
  | `failed` | of those, the ones that became `FAILED` |
  | `failure_rate` | `failed / finished`, or `0.0` when `finished` is 0 |
  | `average_attempts` | mean `attempts` of the `finished` jobs, or `0.0` |

  `by_status`, `by_kind`, and `by_tenant` count all jobs. The window
  is `now - window_s < time <= now`; with `window_s=None` it covers all time.
  A `window_s` that is not a positive `int` or `float` raises `ValueError`.

## REST API

`handle(runner, method, path, body=None)` returns `(status_code, dict)`.
`path` may carry a query string. Errors are returned as
`{"error": "<message>"}`: `400` for an invalid body or query (whatever
raises `ValueError`), `404` for an unknown id or an unknown method or path,
`409` for `InvalidTransition`. A JSON body that is not an object, or that
lacks a required field (one without `?` below), is a `400`.

| Request | Response |
| --- | --- |
| `POST /jobs` with `{"kind", "payload"?, "priority"?, "tenant"?, "run_at"?, "depends_on"?}` | `201` and the job |
| `GET /jobs/<id>` | `200` and the job |
| `GET /jobs` or `GET /jobs?status=<status>` | `200` and `{"jobs": [...]}` ordered by id |
| `POST /jobs/<id>/cancel` | `200` and the job |
| (new) `POST /jobs/<id>/priority` with `{"priority"}` | `200` and the job |
| (new) `GET /queue` | `200` and `{"jobs": [...]}` in `queue()` order |
| (new) `GET /tenants` | `200` and `{"tenants": [...]}` as `tenants()` returns them |
| (new) `POST /recurring` with `{"kind", "payload"?, "interval_s", "start_at"?, "priority"?, "tenant"?}` | `201` and the definition |
| (new) `GET /recurring` | `200` and `{"recurring": [...]}` |
| (new) `GET /recurring/<id>` | `200` and the definition |
| (new) `PATCH /recurring/<id>` with any of `{"payload", "interval_s", "priority", "tenant"}` | `200` and the definition |
| (new) `GET /recurring/<id>/jobs` | `200` and `{"definition_id", "jobs": [...]}` |
| (new) `POST /recurring/<id>/pause`, `POST /recurring/<id>/resume` | `200` and the definition |
| (new) `DELETE /recurring/<id>` | `200` and `{"deleted": <id>}` |
| (new) `GET /jobs/<id>/dependencies` | `200` and `{"job_id", "depends_on": [jobs], "dependents": [jobs]}` |
| (new) `GET /rate-limits` | `200` and `{"rate_limits": [...]}` |
| (new) `PUT /rate-limits/<kind>` with `{"max_starts", "window_s"}` | `200` and the limit |
| (new) `DELETE /rate-limits/<kind>` | `200` and `{"deleted": "<kind>"}` |
| (new) `POST /subscriptions` with `{"url", "kinds"?, "statuses"?}` | `201` and the subscription |
| (new) `GET /subscriptions` | `200` and `{"subscriptions": [...]}` |
| (new) `DELETE /subscriptions/<id>` | `200` and `{"deleted": <id>}` |
| (new) `GET /notifications`, optionally `?status=<status>&job_id=<id>` | `200` and `{"notifications": [...]}` |
| (new) `POST /notifications/<id>/retry` | `200` and the notification |
| (new) `GET /jobs/<id>/history` | `200` and `{"job_id", "history": [entries]}` |
| (new) `GET /audit`, optionally `?since=<time>` | `200` and `{"entries": [entries]}` |
| (new) `GET /stats`, optionally `?window_s=<seconds>` | `200` and the `stats()` dict |

Fields left out of a body take the defaults of the Python method. In the
response of `GET /jobs/<id>/dependencies`, `depends_on` follows the job's
`depends_on` order and `dependents` is ordered by id. A `job_id`,
`window_s`, or `since` query value that is not a number is a `400`.

The JSON form (`to_dict()`) of each model has exactly its fields: a job has
every field in the Jobs table; a recurring definition, rate limit,
subscription, notification, and audit entry have the fields listed in their
sections (an audit entry: `job_id`, `old_status`, `new_status`, `time`,
`reason`).

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

`render_page(runner, path)` returns `(status_code, html)` for a dashboard
page; `path` may carry a query string. An unknown page or id returns `404`
and HTML containing `Not found`; an invalid query returns `400`.

Pages are HTML fragments with an `<h1>` title and one or more tables of the
form `<table class="NAME"><thead><tr><th>...</th></tr></thead><tbody><tr><td>...</td></tr>...</tbody></table>`,
one `<tr>` per row, exactly the columns listed, in order. Times and
`interval_s`/`window_s` are shown with one decimal. All text taken from jobs,
definitions, and subscriptions is HTML-escaped.

| Page | Title | Tables (`class`: columns) |
| --- | --- | --- |
| `/jobs` | — | `render_jobs(runner.list())` |
| (new) `/queue` | `Queue` | `queue`: `ID`, `Kind`, `Tenant`, `Priority`, `Run at` (`next_run_at`), rows in `queue()` order; then `tenants`: `Tenant`, `Pending`, `Running`, `Finished`, rows as `tenants()` |
| (new) `/recurring` | `Recurring jobs` | `recurring`: `ID`, `Kind`, `Interval`, `Next run`, `State` (`active` or `paused`), `Last job` (id or `-`), `Actions` |
| (new) `/jobs/<id>/dependencies` | `Job <id> dependencies` | `dependencies`: `Relation`, `ID`, `Kind`, `Status`; first one row per `depends_on` job with relation `depends on`, then one row per dependent with relation `dependent` |
| (new) `/rate-limits` | `Rate limits` | `rate-limits`: `Kind`, `Max starts`, `Window`, `Recent starts`, ordered by kind |
| (new) `/notifications` | `Notifications` | `subscriptions`: `ID`, `URL`, `Kinds`, `Statuses` (comma-and-space separated, or `all`); then `outbox`: `ID`, `Subscription`, `Job`, `Event` (the new status), `Status`, `Attempts`, `Next attempt` (for a pending notification, otherwise `-`), `Actions`, every notification by id |
| (new) `/jobs/<id>/history` | `Job <id> history` | `history`: `Time`, `From` (old status, `-` for a new job), `To`, `Reason` |
| (new) `/audit`, optionally `?since=<time>` | `Audit log` | `audit`: `Time`, `Job`, `From`, `To`, `Reason`, rows as `audit_log(since)` |
| (new) `/stats`, optionally `?window_s=<seconds>` | `Statistics` | `stats-status`: `Status`, `Count` (five rows: pending, running, succeeded, failed, cancelled); `stats-kind`: `Kind`, `Count` ordered by kind; `stats-tenant`: `Tenant`, `Count` ordered by tenant; `stats-summary`: `Metric`, `Value` with the rows `Finished`, `Failed`, `Failure rate` (a percentage with one decimal, such as `25.0%`), and `Average attempts` (two decimals, such as `1.50`) |

The `Actions` cell of `/recurring` holds
`<form method="post" action="/recurring/<id>/pause"><button type="submit">Pause</button></form>`
for an active definition and the same form with `resume` and `Resume` for a
paused one. The `Actions` cell of `/notifications` holds
`<form method="post" action="/notifications/<id>/retry"><button type="submit">Retry</button></form>`
for a dead notification whose subscription still exists, and is empty
otherwise.
