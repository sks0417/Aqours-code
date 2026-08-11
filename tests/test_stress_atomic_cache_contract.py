from pathlib import Path


def test_after_publish_contract_is_explicit_in_authoritative_case_readme():
    readme = (
        Path(__file__).parents[1]
        / "evals"
        / "cases"
        / "stress_atomic_cache_recovery"
        / "workspace"
        / "README.md"
    ).read_text(encoding="utf-8")

    assert (
        "A fault raised at `after_publish` occurs after durable publication. "
        "The lease must\n"
        "be recorded as terminally `committed` rather than remaining `active` "
        "or becoming\n"
        "`aborted`. Retrying the exact lease and bytes returns the published "
        "entry without\n"
        "rebuilding, while retrying the consumed lease with different bytes is "
        "stale and has\n"
        "no side effects."
    ) in readme
