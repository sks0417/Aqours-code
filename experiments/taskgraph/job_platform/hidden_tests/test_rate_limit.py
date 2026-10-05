import pytest

from conftest import Script


def test_set_rate_limit_returns_the_limit(make_runner):
    runner = make_runner({"mail": Script()})
    limit = runner.set_rate_limit("mail", 2, 10)
    assert (limit.kind, limit.max_starts, limit.window_s, limit.recent_starts) == ("mail", 2, 10, 0)
    assert limit.to_dict() == {"kind": "mail", "max_starts": 2, "window_s": 10,
                               "recent_starts": 0}


def test_limit_blocks_starts_within_the_window(make_runner, clock):
    runner = make_runner({"mail": Script()})
    runner.set_rate_limit("mail", 2, 10)
    jobs = [runner.submit("mail", {}) for _ in range(3)]
    assert [runner.run_once().id, runner.run_once().id] == [jobs[0].id, jobs[1].id]
    assert runner.run_once() is None
    assert runner.get(jobs[2].id).status.value == "pending"
    clock.advance(9.5)
    assert runner.run_once() is None
    clock.advance(0.5)
    assert runner.run_once().id == jobs[2].id


def test_window_slides(make_runner, clock):
    runner = make_runner({"mail": Script()})
    runner.set_rate_limit("mail", 2, 10)
    for _ in range(4):
        runner.submit("mail", {})
    runner.run_once()           # t = 1000
    clock.advance(5)
    runner.run_once()           # t = 1005
    clock.advance(5)
    assert runner.run_once() is not None  # t = 1010: the 1000 start left the window
    clock.advance(2)
    assert runner.run_once() is None      # 1005 and 1010 are in the window
    clock.advance(3)
    assert runner.run_once() is not None  # t = 1015


def test_other_kinds_still_run(make_runner):
    runner = make_runner({"mail": Script(), "report": Script()})
    runner.set_rate_limit("mail", 1, 60)
    runner.submit("mail", {})
    blocked = runner.submit("mail", {})
    report = runner.submit("report", {})
    runner.run_once()
    assert runner.run_once().id == report.id
    assert runner.run_once() is None
    assert runner.get(blocked.id).status.value == "pending"


def test_retries_count_as_starts(make_runner, clock, jr):
    runner = make_runner({"mail": Script(jr.errors.TransientError("busy"))})
    runner.set_rate_limit("mail", 2, 100)
    first = runner.submit("mail", {})
    runner.run_once()
    clock.advance(1)
    assert runner.run_once().id == first.id
    runner.submit("mail", {})
    assert runner.run_once() is None
    assert runner.list_rate_limits()[0].recent_starts == 2


def test_starts_before_the_limit_was_set_count(make_runner):
    runner = make_runner({"mail": Script()})
    for _ in range(3):
        runner.submit("mail", {})
    runner.run_once()
    runner.run_once()
    limit = runner.set_rate_limit("mail", 2, 60)
    assert limit.recent_starts == 2
    assert runner.run_once() is None


def test_replacing_and_clearing_a_limit(make_runner, jr):
    runner = make_runner({"mail": Script()})
    runner.set_rate_limit("mail", 1, 60)
    runner.set_rate_limit("mail", 3, 30)
    assert [(l.kind, l.max_starts, l.window_s) for l in runner.list_rate_limits()] == [
        ("mail", 3, 30)]
    for _ in range(4):
        runner.submit("mail", {})
    assert [runner.run_once() is not None for _ in range(4)] == [True, True, True, False]
    runner.clear_rate_limit("mail")
    assert runner.list_rate_limits() == []
    assert runner.run_once() is not None
    with pytest.raises(jr.errors.NotFound):
        runner.clear_rate_limit("mail")


def test_limits_are_listed_by_kind(make_runner):
    runner = make_runner({"b": Script(), "a": Script(), "c": Script()})
    runner.set_rate_limit("c", 1, 5)
    runner.set_rate_limit("a", 4, 2.5)
    runner.submit("a", {})
    runner.run_once()
    limits = runner.list_rate_limits()
    assert [(l.kind, l.recent_starts) for l in limits] == [("a", 1), ("c", 0)]
    assert limits[0].window_s == 2.5


def test_invalid_limits_are_rejected(make_runner):
    runner = make_runner({"mail": Script()})
    for args in (("missing", 1, 10), ("mail", 0, 10), ("mail", 1.5, 10), ("mail", True, 10),
                 ("mail", 1, 0), ("mail", 1, -1), ("mail", 1, "10")):
        with pytest.raises(ValueError):
            runner.set_rate_limit(*args)
    assert runner.list_rate_limits() == []


def test_limits_and_starts_survive_a_restart(make_runner, clock):
    runner = make_runner({"mail": Script()})
    runner.set_rate_limit("mail", 1, 60)
    runner.submit("mail", {})
    runner.run_once()
    restarted = make_runner({"mail": Script()})
    assert [(l.kind, l.recent_starts) for l in restarted.list_rate_limits()] == [("mail", 1)]
    waiting = restarted.submit("mail", {})
    assert restarted.run_once() is None
    clock.advance(60)
    assert restarted.run_once().id == waiting.id
