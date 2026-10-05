import pytest

from conftest import Crash, Recorder, Script

PAYLOAD_KEYS = {"subscription_id", "job_id", "kind", "tenant", "old_status", "new_status",
                "reason", "time"}
NOTIFICATION_FIELDS = {"id", "subscription_id", "url", "job_id", "payload", "status",
                       "attempts", "next_attempt_at", "last_error"}


def test_subscribe_returns_a_subscription(make_runner, jr):
    runner = make_runner({"work": Script()})
    everything = runner.subscribe("https://hooks.example/all")
    some = runner.subscribe("https://hooks.example/some", kinds=["work"],
                            statuses=[jr.models.JobStatus.FAILED, "succeeded"])
    assert (everything.url, everything.kinds, everything.statuses) == (
        "https://hooks.example/all", None, None)
    assert some.kinds == ["work"] and some.statuses == ["failed", "succeeded"]
    assert some.to_dict() == {"id": some.id, "url": "https://hooks.example/some",
                              "kinds": ["work"], "statuses": ["failed", "succeeded"]}
    assert [s.id for s in runner.list_subscriptions()] == [everything.id, some.id]


def test_every_state_change_writes_a_notification(make_runner, clock):
    runner = make_runner({"work": Script()})
    sub = runner.subscribe("https://hooks.example/a")
    job = runner.submit("work", {}, tenant="acme")
    clock.advance(2)
    runner.run_once()
    notes = runner.list_notifications()
    assert [n.payload["new_status"] for n in notes] == ["pending", "running", "succeeded"]
    first = notes[0]
    assert set(first.to_dict()) == NOTIFICATION_FIELDS
    assert (first.subscription_id, first.url, first.job_id) == (sub.id, sub.url, job.id)
    assert (first.status, first.attempts, first.last_error) == ("pending", 0, None)
    assert first.next_attempt_at == 1000.0
    assert set(first.payload) == PAYLOAD_KEYS
    assert first.payload == {"subscription_id": sub.id, "job_id": job.id, "kind": "work",
                             "tenant": "acme", "old_status": None, "new_status": "pending",
                             "reason": "submitted", "time": 1000.0}
    assert notes[2].payload["old_status"] == "running" and notes[2].payload["time"] == 1002.0


def test_subscriptions_filter_by_kind_and_status(make_runner):
    runner = make_runner({"a": Script(), "b": Script(ValueError("no"))})
    by_kind = runner.subscribe("https://k.example", kinds=["a"])
    by_status = runner.subscribe("https://s.example", statuses=["failed"])
    job_a = runner.submit("a", {})
    job_b = runner.submit("b", {})
    runner.run_once()
    runner.run_once()
    kind_notes = runner.list_notifications()
    assert {(n.subscription_id, n.job_id) for n in kind_notes} == {
        (by_kind.id, job_a.id), (by_status.id, job_b.id)}
    status_notes = [n for n in kind_notes if n.subscription_id == by_status.id]
    assert [n.payload["new_status"] for n in status_notes] == ["failed"]
    assert len([n for n in kind_notes if n.subscription_id == by_kind.id]) == 3


def test_only_later_changes_are_notified(make_runner):
    runner = make_runner({"work": Script()})
    runner.submit("work", {})
    runner.subscribe("https://hooks.example/a")
    assert runner.list_notifications() == []
    runner.run_once()
    assert len(runner.list_notifications()) == 2


def test_delivery_calls_the_sender_in_event_order(make_runner):
    runner = make_runner({"work": Script()})
    runner.subscribe("https://hooks.example/a")
    job = runner.submit("work", {})
    runner.run_once()
    sender = Recorder()
    assert runner.deliver_notifications(sender) == 3
    assert sender.events() == [(job.id, "pending"), (job.id, "running"), (job.id, "succeeded")]
    assert all(url == "https://hooks.example/a" for url, _ in sender.calls)
    notes = runner.list_notifications()
    assert {(n.status, n.attempts) for n in notes} == {("delivered", 1)}
    assert sender.calls[0][1] == notes[0].payload
    assert runner.deliver_notifications(sender) == 0 and len(sender.calls) == 3


def test_failed_delivery_is_retried_with_backoff_then_dead(make_runner, clock):
    runner = make_runner({"work": Script()})
    url = "https://down.example"
    runner.subscribe(url)
    runner.submit("work", {})
    sender = Recorder(fail_urls={url: 10})
    expected_waits = [1, 2, 4, 8]
    for wait in expected_waits:
        assert runner.deliver_notifications(sender) == 0
        note = runner.list_notifications()[0]
        assert note.status == "pending" and note.next_attempt_at == clock.now + wait
        assert runner.deliver_notifications(sender) == 0  # not due yet
        clock.advance(wait)
    assert runner.deliver_notifications(sender) == 0
    note = runner.list_notifications()[0]
    assert (note.status, note.attempts) == ("dead", 5)
    assert note.last_error == f"{url} unavailable"
    assert len(sender.calls) == 5


def test_later_events_of_a_job_wait_for_earlier_ones(make_runner, clock):
    runner = make_runner({"work": Script()})
    url = "https://flaky.example"
    runner.subscribe(url)
    job = runner.submit("work", {})
    runner.run_once()
    sender = Recorder(fail_urls={url: 1})
    assert runner.deliver_notifications(sender) == 0
    assert len(sender.calls) == 1
    clock.advance(1)
    assert runner.deliver_notifications(sender) == 3
    assert sender.events() == [(job.id, "pending"), (job.id, "pending"), (job.id, "running"),
                               (job.id, "succeeded")]


def test_a_dead_notification_releases_the_next_one(make_runner, clock):
    runner = make_runner({"work": Script()})
    url = "https://down.example"
    runner.subscribe(url)
    job = runner.submit("work", {})
    runner.run_once()
    sender = Recorder(fail_urls={url: 5})
    for wait in (1, 2, 4, 8):
        runner.deliver_notifications(sender)
        clock.advance(wait)
    assert runner.deliver_notifications(sender) == 2
    assert [n.status for n in runner.list_notifications()] == ["dead", "delivered", "delivered"]
    assert sender.events()[-2:] == [(job.id, "running"), (job.id, "succeeded")]


def test_order_is_kept_per_subscription_and_job(make_runner):
    runner = make_runner({"work": Script()})
    down = runner.subscribe("https://down.example")
    up = runner.subscribe("https://up.example")
    first = runner.submit("work", {})
    second = runner.submit("work", {})
    failing = {first.id}

    calls = []

    def sender(url, payload):
        calls.append((url, payload["job_id"]))
        if url == down.url and payload["job_id"] in failing:
            raise ConnectionError("nope")

    assert runner.deliver_notifications(sender) == 3
    assert calls == [(down.url, first.id), (up.url, first.id), (down.url, second.id),
                     (up.url, second.id)]
    statuses = {(n.subscription_id, n.job_id): n.status for n in runner.list_notifications()}
    assert statuses[(down.id, first.id)] == "pending"
    assert statuses[(up.id, first.id)] == statuses[(down.id, second.id)] == "delivered"


def test_crash_during_delivery_is_redelivered_after_a_restart(make_runner):
    runner = make_runner({"work": Script()})
    runner.subscribe("https://hooks.example/a")
    job = runner.submit("work", {})
    with pytest.raises(Crash):
        runner.deliver_notifications(Recorder(crash=True))
    note = runner.list_notifications()[0]
    assert (note.status, note.attempts) == ("pending", 0)
    restarted = make_runner({"work": Script()})
    sender = Recorder()
    assert restarted.deliver_notifications(sender) == 1
    assert sender.events() == [(job.id, "pending")]


def test_outbox_and_backoff_survive_a_restart(make_runner, clock):
    runner = make_runner({"work": Script()})
    url = "https://down.example"
    runner.subscribe(url)
    runner.submit("work", {})
    runner.deliver_notifications(Recorder(fail_urls={url: 1}))
    restarted = make_runner({"work": Script()})
    note = restarted.list_notifications()[0]
    assert (note.status, note.attempts, note.next_attempt_at) == ("pending", 1, clock.now + 1)
    sender = Recorder()
    assert restarted.deliver_notifications(sender) == 0
    clock.advance(1)
    assert restarted.deliver_notifications(sender) == 1


def test_unsubscribe_kills_pending_notifications(make_runner, jr):
    runner = make_runner({"work": Script()})
    sub = runner.subscribe("https://hooks.example/a")
    runner.submit("work", {})
    runner.unsubscribe(sub.id)
    assert runner.list_subscriptions() == []
    note = runner.list_notifications()[0]
    assert (note.status, note.last_error) == ("dead", "unsubscribed")
    runner.run_once()
    assert len(runner.list_notifications()) == 1
    assert runner.deliver_notifications(Recorder()) == 0
    with pytest.raises(jr.errors.NotFound):
        runner.unsubscribe(sub.id)
    with pytest.raises(jr.errors.InvalidTransition):
        runner.retry_notification(note.id)


def test_list_notifications_filters(make_runner):
    runner = make_runner({"work": Script()})
    runner.subscribe("https://hooks.example/a")
    first = runner.submit("work", {})
    second = runner.submit("work", {})
    runner.deliver_notifications(Recorder())
    runner.run_once()
    assert [n.job_id for n in runner.list_notifications(job_id=second.id)] == [second.id]
    assert len(runner.list_notifications(status="pending")) == 2
    assert {n.job_id for n in runner.list_notifications(status="delivered")} == {
        first.id, second.id}
    assert runner.list_notifications(status="dead", job_id=first.id) == []
    with pytest.raises(ValueError):
        runner.list_notifications(status="lost")


def test_invalid_subscriptions_are_rejected(make_runner):
    runner = make_runner({"work": Script()})
    for args, kwargs in ((("",), {}), ((5,), {}), (("https://x",), {"kinds": "work"}),
                         (("https://x",), {"kinds": [1]}),
                         (("https://x",), {"statuses": ["lost"]})):
        with pytest.raises(ValueError):
            runner.subscribe(*args, **kwargs)
    assert runner.list_subscriptions() == []


def test_a_dead_notification_can_be_retried(make_runner, clock, jr):
    runner = make_runner({"work": Script()})
    url = "https://down.example"
    runner.subscribe(url)
    job = runner.submit("work", {})
    sender = Recorder(fail_urls={url: 5})
    for wait in (1, 2, 4, 8):
        runner.deliver_notifications(sender)
        clock.advance(wait)
    runner.deliver_notifications(sender)
    dead = runner.list_notifications()[0]
    assert dead.status == "dead"
    runner.run_once()
    retried = runner.retry_notification(dead.id)
    assert (retried.id, retried.status, retried.attempts) == (dead.id, "pending", 0)
    assert retried.next_attempt_at == clock.now and retried.last_error == f"{url} unavailable"
    later = runner.list_notifications()[1]
    with pytest.raises(jr.errors.InvalidTransition):
        runner.retry_notification(later.id)
    with pytest.raises(jr.errors.NotFound):
        runner.retry_notification(999)
    good = Recorder()
    assert runner.deliver_notifications(good) == 3
    assert good.events() == [(job.id, "pending"), (job.id, "running"), (job.id, "succeeded")]
