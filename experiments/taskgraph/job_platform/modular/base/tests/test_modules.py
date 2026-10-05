"""Tests of the extension points: store tables, transitions, scheduler rules, web registries."""
import pytest

from jobrunner import scheduler, store as store_module, transitions, web
from jobrunner.dashboard import render_page
from jobrunner.errors import InvalidTransition
from jobrunner.models import JobStatus


@pytest.fixture
def events():
    seen = []

    def listener(store, event):
        seen.append(event)

    transitions.subscribe(listener)
    yield seen
    transitions._listeners.remove(listener)


def test_store_add_and_update_fields(store):
    job = store.add("echo", {"x": 1}, created_at=5.0, max_attempts=4, next_run_at=9.0)
    assert (job.max_attempts, job.next_run_at, job.attempts) == (4, 9.0, 0)
    store.update(job.id, attempts=2, last_error="oops", cancel_requested=True)
    job = store.get(job.id)
    assert job.attempts == 2 and job.last_error == "oops" and job.cancel_requested
    with pytest.raises(ValueError):
        store.update(job.id, bogus=1)


def test_registered_tables_are_created(store):
    store_module.register_schema("CREATE TABLE IF NOT EXISTS extra (id INTEGER PRIMARY KEY)")
    try:
        store.create_tables()
        store.execute("INSERT INTO extra (id) VALUES (7)")
        assert [row["id"] for row in store.query("SELECT id FROM extra")] == [7]
    finally:
        store_module._schemas.remove(
            "CREATE TABLE IF NOT EXISTS extra (id INTEGER PRIMARY KEY)")


def test_transaction_rolls_back_every_write(store):
    job = store.add("echo", {}, created_at=1.0)
    with pytest.raises(RuntimeError):
        with store.transaction():
            store.update(job.id, attempts=5)
            raise RuntimeError("stop")
    assert store.get(job.id).attempts == 0


def test_transitions_publish_events_in_order(runner, clock, events):
    job = runner.submit("echo", {})
    runner.run_once()
    changes = [(e.job.id, e.old_status, e.new_status, e.reason) for e in events]
    assert changes == [
        (job.id, None, JobStatus.PENDING, "submitted"),
        (job.id, JobStatus.PENDING, JobStatus.RUNNING, "started"),
        (job.id, JobStatus.RUNNING, JobStatus.SUCCEEDED, "succeeded"),
    ]
    assert all(e.time == clock.now for e in events)


def test_events_caused_by_a_subscriber_come_after_the_current_one(runner, store, clock):
    order = []

    def first(store_, event):
        order.append(("first", event.new_status))
        if event.new_status is JobStatus.PENDING and event.old_status is None:
            transitions.change(store_, event.job.id, JobStatus.CANCELLED,
                               reason="test", now=clock())

    def second(store_, event):
        order.append(("second", event.new_status))

    transitions.subscribe(first)
    transitions.subscribe(second)
    try:
        job = runner.submit("echo", {})
    finally:
        transitions._listeners.remove(first)
        transitions._listeners.remove(second)
    assert job.status is JobStatus.CANCELLED
    assert order == [("first", JobStatus.PENDING), ("second", JobStatus.PENDING),
                     ("first", JobStatus.CANCELLED), ("second", JobStatus.CANCELLED)]


def test_invalid_transition_is_rejected(runner, store, clock):
    job = runner.submit("echo", {})
    with pytest.raises(InvalidTransition):
        transitions.change(store, job.id, JobStatus.SUCCEEDED, reason="x", now=clock())


def test_scheduler_filters_and_orderings(runner, store, clock):
    low = runner.submit("echo", {"rank": 1})
    high = runner.submit("echo", {"rank": 2})
    blocked = runner.submit("echo", {"rank": 3, "blocked": True})
    scheduler.register_filter("test-block", lambda s, job, now: not job.payload.get("blocked"))
    scheduler.register_ordering("test-rank", 5, lambda s, job: -job.payload["rank"])
    try:
        assert [job.id for job in scheduler.can_run(store, clock())] == [low.id, high.id]
        assert scheduler.pick_next(store, clock()).id == high.id
    finally:
        del scheduler._filters["test-block"]
        del scheduler._orderings["test-rank"]
    assert scheduler.pick_next(store, clock()).id == low.id
    assert blocked.id > high.id


def test_web_registries_dispatch_routes_and_pages(runner):
    @web.route("GET", r"/test/(?P<n>\d+)")
    def test_route(runner_, request, n):
        if n == "0":
            raise ValueError("zero")
        return 200, {"n": int(n), "q": request.query.get("q")}

    @web.page(r"/test-page")
    def test_page(runner_, request):
        return 200, web.table("t", ["A"], [[web.Markup("<b>ok</b>")], ["<i>"]])

    try:
        assert web.dispatch(runner, "get", "/test/3?q=x") == (200, {"n": 3, "q": "x"})
        assert web.dispatch(runner, "GET", "/test/0") == (400, {"error": "zero"})
        assert web.dispatch(runner, "POST", "/test/3")[0] == 404
        status, html = render_page(runner, "/test-page")
        assert status == 200 and "<td><b>ok</b></td>" in html and "&lt;i&gt;" in html
    finally:
        del web._routes[("GET", r"/test/(?P<n>\d+)")]
        del web._pages[r"/test-page"]
