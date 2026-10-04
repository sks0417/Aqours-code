from jobrunner.models import JobStatus, Outcome


def test_progress_columns_round_trip(store):
    job = store.add("echo", {"x": 1}, created_at=5.0, max_attempts=4)
    assert store.progress(job.id) == {"attempts": 0, "max_attempts": 4, "next_run_at": 5.0,
                                      "last_error": None, "cancel_requested": 0}
    store.update_progress(job.id, attempts=2, last_error="oops", cancel_requested=True)
    progress = store.progress(job.id)
    assert progress["attempts"] == 2 and progress["last_error"] == "oops"
    assert progress["cancel_requested"] == 1


def test_finish_stores_outcome(store):
    job = store.add("echo", {}, created_at=1.0)
    store.claim(job.id)
    assert store.get(job.id).status is JobStatus.RUNNING
    done = store.finish(job.id, Outcome(status=JobStatus.SUCCEEDED, result=[1, 2]))
    assert done.status is JobStatus.SUCCEEDED and done.result == [1, 2]
    failed = store.add("echo", {}, created_at=2.0)
    store.finish(failed.id, Outcome(status=JobStatus.PENDING, error="later", next_run_at=9.0))
    assert store.progress(failed.id)["next_run_at"] == 9.0
    assert store.progress(failed.id)["last_error"] == "later"
