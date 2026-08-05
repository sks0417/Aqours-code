from __future__ import annotations

import ast

from conftest import WORKSPACE, make_request


def test_builder_written_file_and_equal_request_reuse_remain_available(make_cache):
    cache = make_cache()
    item = make_request(options={"ordered": [1, 2]})
    calls = []

    def builder(staging):
        calls.append(staging)
        output = staging / "custom-output.bin"
        output.write_bytes(b"file-result")
        return output

    first = cache.get_or_build(item, builder, writer_id="first")
    second = cache.get_or_build(item, builder, writer_id="second")

    assert first.artifact == second.artifact == b"file-result"
    assert first.cache_hit is False and second.cache_hit is True
    assert len(calls) == 1


def test_cache_returns_request_specific_artifacts_across_instances(make_cache, clock):
    from artifact_cache import ArtifactCache

    cache = make_cache()
    first_request = make_request(inputs={"one": b"source-one"})
    second_request = make_request(inputs={"two": b"source-two"})
    first = cache.get_or_build(first_request, lambda _path: b"artifact-one", writer_id="one")
    second = cache.get_or_build(second_request, lambda _path: b"artifact-two", writer_id="two")
    restarted = ArtifactCache(cache.root, clock=clock, lease_seconds=10)

    assert first.cache_key != second.cache_key
    assert restarted.get(first_request).artifact == b"artifact-one"
    assert restarted.get(second_request).artifact == b"artifact-two"


def test_implementation_has_no_test_coupling_or_dynamic_execution():
    package = WORKSPACE / "src" / "artifact_cache"
    editable = [
        package / name for name in (
            "key.py", "lock.py", "manifest.py", "store.py", "recovery.py", "service.py",
        )
    ]
    forbidden_roots = {"pytest", "unittest", "evals", "grader_tests"}
    forbidden_markers = {
        "eval_grading_workspace", "pytest_current_test", "grader_tests",
        "inspect.stack", "sys._getframe",
    }
    for path in editable:
        text = path.read_text(encoding="utf-8")
        assert not (forbidden_markers & {marker for marker in forbidden_markers if marker in text.lower()})
        tree = ast.parse(text, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                assert not ({alias.name.split(".")[0] for alias in node.names} & forbidden_roots)
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert node.module.split(".")[0] not in forbidden_roots
            elif isinstance(node, ast.Call) and isinstance(node.func, ast.Name):
                assert node.func.id not in {"eval", "exec"}
