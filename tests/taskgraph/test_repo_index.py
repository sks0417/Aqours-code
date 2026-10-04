from __future__ import annotations

import pytest

from aqours_code.taskgraph import build_index, parse_symbol
from aqours_code.taskgraph.repo_index import extract_symbols
from taskgraph_support import commit_files


def test_lists_every_file_at_the_commit(toy_index):
    assert toy_index.files == {
        "models.py", "store.py", "runner.py", "tests/test_basic.py",
        "README.md", "broken.py",
    }
    assert toy_index.has_file("README.md")
    assert not toy_index.has_file("missing.py")


@pytest.mark.parametrize("symbol", [
    "runner.py::run_loop",                       # module-level function
    "models.py::JobStatus",                      # class
    "runner.py::MAX_ATTEMPTS",                   # module-level variable
    "store.py::JobStore.add",                    # method
    "store.py::JobStore.list_unfinished",
    "store.py::JobStore.__init__",
    "models.py::JobStatus.PENDING",              # Enum member
    "models.py::JobStatus.DONE",
    "models.py::Job.id",                         # dataclass field
    "models.py::Job.attempts",
    "store.py::JobStore.jobs",                   # self.xxx in __init__ (annotated)
    "store.py::JobStore.version",                # self.xxx in __init__
    "models.py::Config.Limits",                  # nested class
    "models.py::Config.Limits.max_jobs",
    "tests/test_basic.py::test_run_loop_counts_unfinished_jobs",
])
def test_extracts_symbol(toy_index, symbol):
    assert toy_index.has_symbol(symbol)


@pytest.mark.parametrize("symbol", [
    "runner.py::count",                 # local variable
    "runner.py::JobStore",              # imported name
    "store.py::JobStore.job",           # comprehension variable
    "README.md::anything",
])
def test_does_not_index_non_definitions(toy_index, symbol):
    assert not toy_index.has_symbol(symbol)


def test_every_symbol_matches_the_naming_rule(toy_index):
    for symbol in toy_index.symbols:
        ref = parse_symbol(symbol)
        assert ref.path in toy_index.files


def test_syntax_error_file_is_skipped_with_warning(toy_index):
    assert not any(symbol.startswith("broken.py::") for symbol in toy_index.symbols)
    assert len(toy_index.warnings) == 1
    assert "broken.py" in toy_index.warnings[0]


def test_reads_the_requested_commit_without_checkout(toy_repo):
    newer = commit_files(toy_repo.path, {
        "runner.py": "MAX_ATTEMPTS = 5\n\n\ndef run_loop(store):\n    return 0\n\n\n"
                     "def drain(store):\n    return 0\n",
        "extra.py": "VALUE = 1\n",
    }, "second commit")
    old = build_index(toy_repo.path, toy_repo.commit)
    new = build_index(toy_repo.path, newer)
    assert not old.has_symbol("runner.py::drain")
    assert not old.has_file("extra.py")
    assert new.has_symbol("runner.py::drain")
    assert new.has_symbol("extra.py::VALUE")
    assert old.commit == toy_repo.commit and new.commit == newer


def test_unknown_commit_raises(toy_repo):
    with pytest.raises(RuntimeError):
        build_index(toy_repo.path, "0" * 40)


def test_scope_preserving_blocks_and_self_attributes():
    source = '''
try:
    import fast as impl
except ImportError:
    FALLBACK = True

if True:
    A, (B, *C) = 1, (2, 3)

class Outer:
    x: int
    y = 1

    def __init__(this, value):
        this.value = value
        this.a, this.b = 1, 2
        if value:
            this.flag: bool = True

        def helper():
            this.hidden = 1

    async def run(self):
        self.not_init = 1

    class Inner:
        def method(self):
            pass
'''
    symbols = extract_symbols(source, "pkg/mod.py")
    expected = {
        "FALLBACK", "A", "B", "C", "Outer", "Outer.x", "Outer.y", "Outer.__init__",
        "Outer.value", "Outer.a", "Outer.b", "Outer.flag", "Outer.run",
        "Outer.Inner", "Outer.Inner.method",
    }
    assert symbols == {f"pkg/mod.py::{name}" for name in expected}
