import pytest

from conftest import Script


def run_all(runner):
    order = []
    while (job := runner.run_once()) is not None:
        order.append(job.id)
    return order


def test_new_job_has_default_priority_and_tenant(make_runner):
    runner = make_runner({"work": Script()})
    job = runner.submit("work", {})
    assert job.priority == 0 and job.tenant == "default"
    body = job.to_dict()
    assert body["priority"] == 0 and body["tenant"] == "default"


def test_higher_priority_runs_first(make_runner):
    runner = make_runner({"work": Script()})
    low = runner.submit("work", {}, priority=0)
    high = runner.submit("work", {}, priority=5)
    mid = runner.submit("work", {}, priority=2)
    below = runner.submit("work", {}, priority=-1)
    assert run_all(runner) == [high.id, mid.id, low.id, below.id]


def test_equal_priority_runs_earliest_due_then_oldest(make_runner, clock):
    runner = make_runner({"work": Script()})
    first = runner.submit("work", {})
    second = runner.submit("work", {})
    earlier = runner.submit("work", {}, run_at=clock.now - 5)
    assert run_all(runner) == [earlier.id, first.id, second.id]


def test_tenants_take_turns_within_a_priority(make_runner):
    runner = make_runner({"work": Script()})
    a1, a2, a3 = (runner.submit("work", {}, tenant="a") for _ in range(3))
    b1, b2 = (runner.submit("work", {}, tenant="b") for _ in range(2))
    assert run_all(runner) == [a1.id, b1.id, a2.id, b2.id, a3.id]


def test_tenant_that_never_ran_goes_first(make_runner):
    runner = make_runner({"work": Script()})
    a1 = runner.submit("work", {}, tenant="a")
    a2 = runner.submit("work", {}, tenant="a")
    assert runner.run_once().id == a1.id
    c1 = runner.submit("work", {}, tenant="c")
    assert run_all(runner) == [c1.id, a2.id]


def test_priority_wins_over_tenant_rotation(make_runner):
    runner = make_runner({"work": Script()})
    runner.submit("work", {}, tenant="a")
    runner.run_once()
    b_low = runner.submit("work", {}, tenant="b")
    a_high = runner.submit("work", {}, tenant="a", priority=1)
    assert run_all(runner) == [a_high.id, b_low.id]


def test_rotation_survives_a_restart(make_runner):
    runner = make_runner({"work": Script()})
    a1 = runner.submit("work", {}, tenant="a")
    a2 = runner.submit("work", {}, tenant="a")
    b1 = runner.submit("work", {}, tenant="b")
    assert runner.run_once().id == a1.id
    restarted = make_runner({"work": Script()})
    assert run_all(restarted) == [b1.id, a2.id]


def test_priority_and_tenant_survive_a_restart(make_runner):
    runner = make_runner({"work": Script()})
    job = runner.submit("work", {}, priority=7, tenant="acme")
    restarted = make_runner({"work": Script()})
    stored = restarted.get(job.id)
    assert (stored.priority, stored.tenant) == (7, "acme")


def test_set_priority_reorders_pending_jobs(make_runner):
    runner = make_runner({"work": Script()})
    first = runner.submit("work", {})
    second = runner.submit("work", {})
    changed = runner.set_priority(second.id, 3)
    assert changed.id == second.id and changed.priority == 3
    assert runner.get(second.id).priority == 3
    assert run_all(runner) == [second.id, first.id]


def test_set_priority_errors(make_runner, jr):
    runner = make_runner({"work": Script()})
    job = runner.submit("work", {})
    for bad in ("1", 1.5, True, None):
        with pytest.raises(ValueError):
            runner.set_priority(job.id, bad)
    with pytest.raises(jr.errors.JobNotFound):
        runner.set_priority(999, 1)
    runner.run_once()
    with pytest.raises(jr.errors.InvalidTransition):
        runner.set_priority(job.id, 1)


def test_submit_rejects_invalid_priority_and_tenant(make_runner):
    runner = make_runner({"work": Script()})
    for kwargs in ({"priority": "1"}, {"priority": 1.0}, {"priority": True},
                   {"tenant": ""}, {"tenant": 5}, {"tenant": None}):
        with pytest.raises(ValueError):
            runner.submit("work", {}, **kwargs)
    assert runner.list() == []


def test_queue_lists_pending_jobs_by_priority(make_runner, clock):
    runner = make_runner({"work": Script()})
    low = runner.submit("work", {})
    later = runner.submit("work", {}, priority=2, run_at=clock.now + 10)
    sooner = runner.submit("work", {}, priority=2, run_at=clock.now + 5)
    mid = runner.submit("work", {}, priority=1)
    assert [job.id for job in runner.queue()] == [sooner.id, later.id, mid.id, low.id]
    assert runner.run_once().id == mid.id
    assert [job.id for job in runner.queue()] == [sooner.id, later.id, low.id]


def test_tenants_summarize_jobs_per_tenant(make_runner):
    box = {}

    def look():
        box["during"] = box["runner"].tenants()
        return "ok"

    runner = make_runner({"work": Script(look), "bad": Script(ValueError("x"))})
    box["runner"] = runner
    assert runner.tenants() == []
    runner.submit("bad", {}, tenant="b")
    runner.submit("work", {}, tenant="a")
    runner.submit("work", {}, tenant="b")
    runner.run_once()
    runner.run_once()
    assert box["during"] == [{"tenant": "a", "pending": 0, "running": 1, "finished": 0},
                             {"tenant": "b", "pending": 1, "running": 0, "finished": 1}]
    assert runner.tenants() == [{"tenant": "a", "pending": 0, "running": 0, "finished": 1},
                                {"tenant": "b", "pending": 1, "running": 0, "finished": 1}]
