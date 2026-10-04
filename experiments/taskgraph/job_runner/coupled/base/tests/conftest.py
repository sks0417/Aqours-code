import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from jobrunner.runner import Runner  # noqa: E402
from jobrunner.store import JobStore  # noqa: E402


class FakeClock:
    """A clock that only moves when told to."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def store(tmp_path):
    job_store = JobStore(tmp_path / "jobs.db")
    yield job_store
    job_store.close()


def echo(payload):
    return payload


def boom(payload):
    raise ValueError("bad input")


@pytest.fixture
def runner(store, clock) -> Runner:
    return Runner(store, {"echo": echo, "boom": boom}, clock)
