import pytest

from conftest import Script

DEFINITION_FIELDS = {"id", "kind", "payload", "interval_s", "next_run_at", "priority",
                     "tenant", "paused", "last_job_id"}


def made_by(runner, definition_id):
    return [job for job in runner.list() if job.definition_id == definition_id]


def test_schedule_returns_an_id_and_stores_the_definition(make_runner, clock):
    runner = make_runner({"tick": Script()})
    definition_id = runner.schedule_recurring("tick", {"n": 1}, 10, start_at=clock.now + 5)
    assert isinstance(definition_id, int)
    definition = runner.get_recurring(definition_id)
    assert (definition.id, definition.kind, definition.payload) == (definition_id, "tick", {"n": 1})
    assert definition.interval_s == 10 and definition.next_run_at == clock.now + 5
    assert definition.paused is False and definition.last_job_id is None
    assert (definition.priority, definition.tenant) == (0, "default")
    assert set(definition.to_dict()) == DEFINITION_FIELDS
    assert [d.id for d in runner.list_recurring()] == [definition_id]


def test_start_defaults_to_now(make_runner, clock):
    runner = make_runner({"tick": Script()})
    definition_id = runner.schedule_recurring("tick", {}, 30)
    assert runner.get_recurring(definition_id).next_run_at == clock.now


def test_delayed_job_does_not_run_before_run_at(make_runner, clock):
    runner = make_runner({"work": Script()})
    job = runner.submit("work", {}, run_at=clock.now + 10)
    assert job.next_run_at == clock.now + 10
    assert runner.run_once() is None
    clock.advance(9.5)
    assert runner.run_once() is None
    clock.advance(0.5)
    assert runner.run_once().id == job.id


def test_run_at_in_the_past_is_due_at_once(make_runner, clock):
    runner = make_runner({"work": Script()})
    job = runner.submit("work", {}, run_at=clock.now - 100)
    assert runner.run_once().id == job.id


def test_each_period_creates_one_job(make_runner, clock):
    runner = make_runner({"tick": Script()})
    definition_id = runner.schedule_recurring("tick", {"n": 1}, 10)
    first = runner.run_once()
    assert first.definition_id == definition_id and first.payload == {"n": 1}
    assert first.status.value == "succeeded"
    assert runner.run_once() is None
    assert runner.get_recurring(definition_id).next_run_at == clock.now + 10
    clock.advance(10)
    second = runner.run_once()
    assert second.id != first.id and second.definition_id == definition_id
    assert runner.get_recurring(definition_id).last_job_id == second.id
    assert len(made_by(runner, definition_id)) == 2


def test_at_most_one_unfinished_job_per_definition(make_runner, clock, jr):
    tick = Script(jr.errors.TransientError("busy"))
    runner = make_runner({"tick": tick}, base_delay=15.0)
    definition_id = runner.schedule_recurring("tick", {}, 10)
    first = runner.run_once()  # fails once, retries at now + 15
    assert first.status.value == "pending"
    clock.advance(10)
    assert runner.run_once() is None  # due period, but the last job is unfinished
    assert len(made_by(runner, definition_id)) == 1
    assert runner.get_recurring(definition_id).next_run_at == clock.now + 10
    clock.advance(5)
    assert runner.run_once().id == first.id  # the retry
    clock.advance(5)
    assert runner.run_once().id != first.id
    assert len(made_by(runner, definition_id)) == 2


def test_missed_periods_are_not_made_up(make_runner, clock):
    runner = make_runner({"tick": Script()})
    definition_id = runner.schedule_recurring("tick", {}, 10)
    clock.advance(35)
    assert runner.run_once().definition_id == definition_id
    assert runner.run_once() is None
    assert len(made_by(runner, definition_id)) == 1
    assert runner.get_recurring(definition_id).next_run_at == 1040.0


def test_paused_definition_creates_nothing_until_resumed(make_runner, clock):
    runner = make_runner({"tick": Script()})
    definition_id = runner.schedule_recurring("tick", {}, 10)
    paused = runner.pause_recurring(definition_id)
    assert paused.paused is True and runner.pause_recurring(definition_id).paused is True
    clock.advance(25)
    assert runner.run_once() is None and made_by(runner, definition_id) == []
    resumed = runner.resume_recurring(definition_id)
    assert resumed.paused is False
    assert runner.run_once().definition_id == definition_id
    assert runner.run_once() is None
    assert runner.get_recurring(definition_id).next_run_at == 1030.0


def test_deleting_keeps_the_jobs_already_created(make_runner, clock, jr):
    runner = make_runner({"tick": Script()})
    definition_id = runner.schedule_recurring("tick", {}, 10)
    job = runner.run_once()
    runner.delete_recurring(definition_id)
    with pytest.raises(jr.errors.NotFound):
        runner.get_recurring(definition_id)
    assert runner.list_recurring() == []
    assert runner.get(job.id).definition_id == definition_id
    clock.advance(100)
    assert runner.run_once() is None


def test_unknown_definition_raises_not_found(make_runner, jr):
    runner = make_runner({"tick": Script()})
    calls = (runner.get_recurring, runner.pause_recurring, runner.resume_recurring,
             runner.delete_recurring)
    for call in calls:
        with pytest.raises(jr.errors.NotFound) as caught:
            call(4242)
        assert isinstance(caught.value, jr.errors.JobRunnerError)


def test_invalid_definitions_are_rejected(make_runner):
    runner = make_runner({"tick": Script()})
    bad = [("missing", {}, 10, None), ("tick", {}, 0, None), ("tick", {}, -5, None),
           ("tick", {}, "10", None), ("tick", {}, 10, "soon")]
    for kind, payload, interval, start in bad:
        with pytest.raises(ValueError):
            runner.schedule_recurring(kind, payload, interval, start_at=start)
    with pytest.raises(ValueError):
        runner.schedule_recurring("tick", {}, 10, tenant="")
    assert runner.list_recurring() == []


def test_created_jobs_use_the_definition_priority_and_tenant(make_runner):
    runner = make_runner({"tick": Script()})
    definition_id = runner.schedule_recurring("tick", {}, 10, priority=3, tenant="acme")
    job = runner.run_once()
    assert (job.priority, job.tenant, job.definition_id) == (3, "acme", definition_id)


def test_definitions_survive_a_restart(make_runner, clock):
    runner = make_runner({"tick": Script()})
    definition_id = runner.schedule_recurring("tick", {"n": 2}, 10, start_at=clock.now + 10)
    restarted = make_runner({"tick": Script()})
    assert [d.id for d in restarted.list_recurring()] == [definition_id]
    assert restarted.run_once() is None
    clock.advance(10)
    job = restarted.run_once()
    assert job.definition_id == definition_id and job.payload == {"n": 2}


def test_recurring_jobs_lists_the_jobs_of_a_definition(make_runner, clock, jr):
    runner = make_runner({"tick": Script(), "work": Script()})
    first = runner.schedule_recurring("tick", {}, 10)
    second = runner.schedule_recurring("tick", {"n": 2}, 10)
    runner.submit("work", {})
    for _ in range(2):
        while runner.run_once() is not None:
            pass
        clock.advance(10)
    jobs = runner.recurring_jobs(first)
    assert len(jobs) == 2 and [job.id for job in jobs] == sorted(job.id for job in jobs)
    assert {job.definition_id for job in jobs} == {first}
    assert [job.payload for job in runner.recurring_jobs(second)] == [{"n": 2}, {"n": 2}]
    runner.delete_recurring(second)
    for definition_id in (second, 999):
        with pytest.raises(jr.errors.NotFound):
            runner.recurring_jobs(definition_id)


def test_update_changes_later_jobs_only(make_runner, clock, jr):
    runner = make_runner({"tick": Script()})
    definition_id = runner.schedule_recurring("tick", {"v": 1}, 10)
    first = runner.run_once()
    updated = runner.update_recurring(definition_id, payload={"v": 2}, interval_s=30,
                                      priority=4, tenant="ops")
    assert (updated.payload, updated.interval_s, updated.priority, updated.tenant) == (
        {"v": 2}, 30, 4, "ops")
    assert updated.next_run_at == 1010.0
    assert runner.update_recurring(definition_id).payload == {"v": 2}
    assert runner.get(first.id).payload == {"v": 1}
    clock.advance(10)
    second = runner.run_once()
    assert (second.payload, second.priority, second.tenant) == ({"v": 2}, 4, "ops")
    assert runner.get_recurring(definition_id).next_run_at == 1040.0
    for kwargs in ({"interval_s": 0}, {"payload": [1]}, {"priority": "high"}, {"tenant": ""}):
        with pytest.raises(ValueError):
            runner.update_recurring(definition_id, **kwargs)
    with pytest.raises(jr.errors.NotFound):
        runner.update_recurring(999, priority=1)
