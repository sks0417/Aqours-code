"""Hidden tests: only the public interface listed in SPEC.md is used."""
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))


class FakeClock:
    """A clock that only moves when told to."""

    def __init__(self, start: float = 1000.0):
        self.now = start

    def __call__(self) -> float:
        return self.now

    def advance(self, seconds: float) -> None:
        self.now += seconds


class Crash(BaseException):
    """Escapes run_once like a process crash, leaving the job RUNNING."""


class Script:
    """A handler that follows a script: return a value or raise, one step per call."""

    def __init__(self, *steps):
        self.steps = list(steps)
        self.calls = 0

    def __call__(self, payload):
        self.calls += 1
        step = self.steps.pop(0) if self.steps else "ok"
        if callable(step):
            step = step()
        if isinstance(step, BaseException):
            raise step
        return step


@pytest.fixture
def clock() -> FakeClock:
    return FakeClock()


@pytest.fixture
def make_runner(tmp_path, clock):
    """``make_runner(handlers, **kwargs)``; calling it again simulates a restart."""
    from jobrunner.runner import Runner
    from jobrunner.store import JobStore

    stores = []

    def factory(handlers, **kwargs):
        store = JobStore(tmp_path / "jobs.db")
        stores.append(store)
        return Runner(store, handlers, clock, **kwargs)

    yield factory
    for store in stores:
        close = getattr(store, "close", None)
        if close is not None:
            close()


@pytest.fixture
def jr():
    """The public modules, imported lazily so a missing name fails one test only."""
    from jobrunner import api, dashboard, errors, models

    class Modules:
        pass

    modules = Modules()
    modules.api, modules.dashboard = api, dashboard
    modules.errors, modules.models = errors, models
    return modules
