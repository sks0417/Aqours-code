from __future__ import annotations

from artifact_cache import BuildRequest

from conftest import request


def test_mapping_and_set_order_are_canonical_but_sequence_order_is_semantic(cache):
    first = BuildRequest(
        inputs={"b": b"two", "a": "one"},
        options={"flags": {"debug", "strict"}, "nested": {"z": 1, "a": 2}},
        tool_version="tool-7",
    )
    equivalent = BuildRequest(
        inputs={"a": "one", "b": b"two"},
        options={"nested": {"a": 2, "z": 1}, "flags": {"strict", "debug"}},
        tool_version="tool-7",
    )
    reordered_list = BuildRequest(
        inputs={"a": "one", "b": b"two"},
        options={"nested": {"a": 2, "z": 1}, "flags": ["strict", "debug"]},
        tool_version="tool-7",
    )

    assert cache.key_for(first) == cache.key_for(equivalent)
    assert cache.key_for(first) != cache.key_for(reordered_list)


def test_every_effective_field_changes_key_but_scratch_directory_does_not(cache):
    base = request(options={"mode": "release"}, scratch_dir="tmp/one")
    same = request(options={"mode": "release"}, scratch_dir="tmp/two")
    variants = [
        request(source=b"changed", options={"mode": "release"}),
        request(options={"mode": "debug"}),
        request(options={"mode": "release"}, tool_version="py-2"),
        request(options={"mode": "release"}, namespace_version="cache-v3"),
        request(options={"mode": "release"}, artifact_format="zip"),
    ]

    assert cache.key_for(base) == cache.key_for(same)
    assert all(cache.key_for(base) != cache.key_for(item) for item in variants)

