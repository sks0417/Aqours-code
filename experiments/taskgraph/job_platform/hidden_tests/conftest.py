"""Hidden tests: only the public interface listed in SPEC.md is used.

``regression/`` holds the job runner's hidden tests (renamed ``test_jr_*``);
they share this conftest.
"""
import re
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
    """Escapes run_once (or a sender) like a process crash."""


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


class Recorder:
    """A webhook sender that records calls; ``fail_urls`` maps a url to failures left."""

    def __init__(self, fail_urls=None, crash=False):
        self.calls = []
        self.fail_urls = dict(fail_urls or {})
        self.crash = crash

    def __call__(self, url, payload):
        self.calls.append((url, dict(payload)))
        if self.crash:
            raise Crash()
        left = self.fail_urls.get(url, 0)
        if left:
            self.fail_urls[url] = left - 1
            raise ConnectionError(f"{url} unavailable")

    def events(self, url=None):
        """(job_id, new_status) of every call, optionally only for ``url``."""
        return [(payload["job_id"], payload["new_status"])
                for called, payload in self.calls if url is None or called == url]


def table_rows(html, css_class):
    """Cell texts of each body row of the table with ``css_class``."""
    match = re.search(rf'<table class="{re.escape(css_class)}">(.*?)</table>', html, flags=re.S)
    assert match, f"no table {css_class!r} in {html!r}"
    body = re.search(r"<tbody>(.*?)</tbody>", match.group(1), flags=re.S)
    rows = re.findall(r"<tr>(.*?)</tr>", body.group(1) if body else "", flags=re.S)
    return [re.findall(r"<td>(.*?)</td>", row, flags=re.S) for row in rows]


def table_headers(html, css_class):
    """Header texts of the table with ``css_class``."""
    match = re.search(rf'<table class="{re.escape(css_class)}">(.*?)</table>', html, flags=re.S)
    assert match, f"no table {css_class!r} in {html!r}"
    return re.findall(r"<th>(.*?)</th>", match.group(1))


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
