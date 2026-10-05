import pytest

from conftest import Crash, Script


def status(runner, job):
    return runner.get(job.id).status.value


def test_depends_on_is_stored_without_repeats(make_runner):
    runner = make_runner({"work": Script()})
    a = runner.submit("work", {})
    b = runner.submit("work", {})
    plain = runner.submit("work", {})
    child = runner.submit("work", {}, depends_on=[b.id, a.id, b.id])
    assert plain.depends_on == [] and child.depends_on == [b.id, a.id]
    assert runner.get(child.id).depends_on == [b.id, a.id]
    assert child.to_dict()["depends_on"] == [b.id, a.id]
    assert runner.submit("work", {}, depends_on=(a.id,)).depends_on == [a.id]


def test_invalid_dependencies_are_rejected(make_runner):
    runner = make_runner({"work": Script()})
    a = runner.submit("work", {})
    for depends_on in ([999], [a.id, 999], "1", [str(a.id)], [True], 5):
        with pytest.raises(ValueError):
            runner.submit("work", {}, depends_on=depends_on)
    assert [job.id for job in runner.list()] == [a.id]


def test_job_waits_until_its_dependency_succeeds(make_runner, clock):
    runner = make_runner({"work": Script()})
    parent = runner.submit("work", {}, run_at=clock.now + 5)
    child = runner.submit("work", {}, depends_on=[parent.id])
    assert runner.run_once() is None
    clock.advance(5)
    assert runner.run_once().id == parent.id
    assert runner.run_once().id == child.id


def test_waiting_job_does_not_block_others(make_runner, clock):
    runner = make_runner({"work": Script()})
    parent = runner.submit("work", {}, run_at=clock.now + 5)
    child = runner.submit("work", {}, depends_on=[parent.id], priority=9)
    other = runner.submit("work", {})
    assert runner.run_once().id == other.id
    assert status(runner, child) == "pending"


def test_dependency_waiting_for_a_retry_still_blocks(make_runner, clock, jr):
    runner = make_runner({"flaky": Script(jr.errors.TransientError("busy")),
                          "work": Script()})
    parent = runner.submit("flaky", {})
    child = runner.submit("work", {}, depends_on=[parent.id])
    assert runner.run_once().id == parent.id
    assert runner.run_once() is None
    clock.advance(1)
    assert runner.run_once().id == parent.id
    assert runner.run_once().id == child.id


def test_all_dependencies_must_succeed(make_runner, clock):
    runner = make_runner({"work": Script()})
    a = runner.submit("work", {})
    b = runner.submit("work", {}, run_at=clock.now + 3)
    c = runner.submit("work", {}, depends_on=[a.id, b.id])
    assert runner.run_once().id == a.id
    assert runner.run_once() is None
    clock.advance(3)
    assert [runner.run_once().id, runner.run_once().id] == [b.id, c.id]


def test_failure_cancels_dependents_downstream(make_runner):
    runner = make_runner({"bad": Script(ValueError("broken")), "work": Script()})
    root = runner.submit("bad", {})
    child = runner.submit("work", {}, depends_on=[root.id])
    grandchild = runner.submit("work", {}, depends_on=[child.id])
    other = runner.submit("work", {})
    assert runner.run_once().id == root.id
    for job in (child, grandchild):
        stored = runner.get(job.id)
        assert stored.status.value == "cancelled"
        assert stored.cancel_reason == "dependency_failed"
    assert status(runner, other) == "pending"
    assert runner.run_once().id == other.id and runner.run_once() is None


def test_cancelling_a_job_cancels_its_dependents(make_runner):
    runner = make_runner({"work": Script()})
    root = runner.submit("work", {})
    child = runner.submit("work", {}, depends_on=[root.id])
    cancelled = runner.cancel(root.id)
    assert cancelled.cancel_reason == "cancel_requested"
    assert runner.get(child.id).cancel_reason == "dependency_failed"
    assert runner.run_once() is None


def test_exhausted_retries_cancel_dependents(make_runner, clock, jr):
    busy = jr.errors.TransientError("busy")
    runner = make_runner({"flaky": Script(busy, busy), "work": Script()}, max_attempts=2)
    root = runner.submit("flaky", {})
    child = runner.submit("work", {}, depends_on=[root.id])
    runner.run_once()
    assert status(runner, child) == "pending"
    clock.advance(1)
    runner.run_once()
    assert status(runner, root) == "failed" and status(runner, child) == "cancelled"


def test_submitting_after_a_dependency_failed_cancels_at_once(make_runner):
    runner = make_runner({"bad": Script(ValueError("x")), "work": Script()})
    root = runner.submit("bad", {})
    runner.run_once()
    child = runner.submit("work", {}, depends_on=[root.id])
    assert child.status.value == "cancelled" and child.cancel_reason == "dependency_failed"
    done = runner.submit("work", {})
    runner.run_once()
    assert runner.submit("work", {}, depends_on=[done.id]).status.value == "pending"


def test_dependents_lists_direct_dependents(make_runner, jr):
    runner = make_runner({"work": Script()})
    root = runner.submit("work", {})
    first = runner.submit("work", {}, depends_on=[root.id])
    runner.submit("work", {}, depends_on=[first.id])
    second = runner.submit("work", {}, depends_on=[root.id, first.id])
    assert [job.id for job in runner.dependents(root.id)] == [first.id, second.id]
    assert runner.dependents(second.id) == []
    with pytest.raises(jr.errors.JobNotFound):
        runner.dependents(999)


def test_restart_recovery_failure_cancels_dependents(make_runner):
    runner = make_runner({"work": Script(Crash())}, max_attempts=1)
    root = runner.submit("work", {})
    child = runner.submit("work", {}, depends_on=[root.id])
    with pytest.raises(Crash):
        runner.run_once()
    restarted = make_runner({"work": Script()}, max_attempts=1)
    assert restarted.get(root.id).status.value == "failed"
    assert restarted.get(child.id).cancel_reason == "dependency_failed"
