from __future__ import annotations

import math

import pytest

from artifact_cache import BuildRequest, InvalidRequest
from conftest import make_request


def test_input_mapping_order_and_content_are_canonical(make_cache):
    cache = make_cache()
    first = make_request(inputs={"b": b"two", "a": "one"})
    reordered = make_request(inputs={"a": "one", "b": b"two"})
    changed_name = make_request(inputs={"a": "one", "c": b"two"})
    changed_content = make_request(inputs={"a": "one", "b": b"changed"})

    assert cache.key_for(first) == cache.key_for(reordered)
    assert cache.key_for(first) != cache.key_for(changed_name)
    assert cache.key_for(first) != cache.key_for(changed_content)


def test_nested_mapping_and_unordered_collections_are_canonical(make_cache):
    cache = make_cache()
    first = make_request(options={
        "targets": {"linux", "windows"},
        "nested": {"z": frozenset({3, 1}), "a": {"enabled": True}},
    })
    equivalent = make_request(options={
        "nested": {"a": {"enabled": True}, "z": frozenset({1, 3})},
        "targets": {"windows", "linux"},
    })

    assert cache.key_for(first) == cache.key_for(equivalent)


def test_ordered_values_and_container_types_remain_distinct(make_cache):
    cache = make_cache()
    values = [
        make_request(options={"steps": ["compile", "link"]}),
        make_request(options={"steps": ["link", "compile"]}),
        make_request(options={"steps": ("compile", "link")}),
        make_request(options={"steps": {"compile", "link"}}),
    ]

    assert len({cache.key_for(value) for value in values}) == len(values)


def test_all_effective_versions_and_format_are_included(make_cache):
    cache = make_cache()
    base = make_request(options={"optimize": 2})
    variants = [
        make_request(options={"optimize": 3}),
        make_request(options={"optimize": 2}, tool_version="tool-2"),
        make_request(options={"optimize": 2}, namespace_version="namespace-2"),
        make_request(options={"optimize": 2}, artifact_format="tar"),
    ]

    assert all(cache.key_for(base) != cache.key_for(value) for value in variants)


def test_scratch_path_is_excluded_and_bytes_do_not_collide_with_text(make_cache):
    cache = make_cache()
    first = make_request(inputs={"input": b"same"}, scratch_dir="one")
    moved = make_request(inputs={"input": b"same"}, scratch_dir="elsewhere/two")
    text = make_request(inputs={"input": "same"}, scratch_dir="one")

    assert cache.key_for(first) == cache.key_for(moved)
    assert cache.key_for(first) != cache.key_for(text)


def test_invalid_requests_are_rejected_deterministically(make_cache):
    invalid = [
        BuildRequest(inputs={}),
        BuildRequest(inputs={"": b"x"}),
        BuildRequest(inputs={"x": object()}),
        BuildRequest(inputs={"x": b"x"}, options={"bad": math.nan}),
        BuildRequest(inputs={"x": b"x"}, options={1: "non-string key"}),
        BuildRequest(inputs={"x": b"x"}, tool_version=""),
        BuildRequest(inputs={"x": b"x"}, namespace_version=""),
        BuildRequest(inputs={"x": b"x"}, artifact_format=""),
    ]
    for request in invalid:
        with pytest.raises(InvalidRequest):
            make_cache().key_for(request)
