from __future__ import annotations

import threading

from conftest import request


def test_basic_build_then_hit_runs_builder_once(cache):
    calls = []

    def builder(staging):
        calls.append(staging)
        return b"artifact-v1"

    first = cache.get_or_build(request(), builder, writer_id="worker-a")
    second = cache.get_or_build(request(), builder, writer_id="worker-b")

    assert first.artifact == b"artifact-v1"
    assert first.cache_hit is False
    assert second.artifact == b"artifact-v1"
    assert second.cache_hit is True
    assert len(calls) == 1


def test_concurrent_equal_requests_execute_one_builder(cache):
    entered = threading.Event()
    release = threading.Event()
    calls = []
    results = []
    errors = []

    def builder(staging):
        calls.append(staging)
        entered.set()
        assert release.wait(2)
        return b"shared"

    def run(worker):
        try:
            results.append(cache.get_or_build(request(), builder, writer_id=worker))
        except BaseException as exc:  # surfaced after joining for a useful assertion
            errors.append(exc)

    first = threading.Thread(target=run, args=("one",))
    second = threading.Thread(target=run, args=("two",))
    first.start()
    assert entered.wait(2)
    second.start()
    release.set()
    first.join(3)
    second.join(3)

    assert not first.is_alive() and not second.is_alive()
    assert errors == []
    assert len(calls) == 1
    assert [result.artifact for result in results] == [b"shared", b"shared"]


def test_different_keys_build_concurrently(cache):
    rendezvous = threading.Barrier(2)
    errors = []

    def run(source):
        try:
            cache.get_or_build(
                request(source=source),
                lambda _staging: (rendezvous.wait(timeout=2), source)[1],
                writer_id=source.decode(),
            )
        except BaseException as exc:
            errors.append(exc)

    threads = [
        threading.Thread(target=run, args=(b"alpha",)),
        threading.Thread(target=run, args=(b"bravo",)),
    ]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(3)

    assert not any(thread.is_alive() for thread in threads)
    assert errors == []

