"""The repository outline the planner's Step 2 plans from."""
from __future__ import annotations

from pathlib import Path

import pytest

from aqours_code.taskgraph import RepoIndex
from aqours_code.taskgraph.outline import (
    OUTLINE_LIMIT,
    REDUCTIONS,
    build_outline,
    is_private,
    is_source_file,
    repo_outline,
)

LONG_DOC = ("Return the area, scaled. This first line is long on purpose, so that the outline "
            "has to cut it off before its end.")

SHAPES = '''\
"""Shapes."""
from __future__ import annotations

import typing as t

if t.TYPE_CHECKING:
    Number = int | float
else:
    Number = object

DEFAULT_SIDES: int = 4
_cache, limit = {}, 10


class Shape(Base, metaclass=Meta):
    """A shape with sides.

    More text that the outline does not show.
    """

    sides: int = 0
    _secret = None
    name = "shape"

    def __init__(self, sides: int = DEFAULT_SIDES, *, name: str | None = None) -> None:
        self.sides = sides

    @t.overload
    def area(self, scale: int) -> int: ...
    @t.overload
    def area(self, scale: float) -> float: ...
    def area(self, scale=1):
        """LONG_DOC"""
        return 0

    def _helper(self):
        return None

    async def draw(self, *points, **options) -> list[str]:
        """
        Draw it.
        """
        class Local:
            pass
        return []


class Empty:
    pass


def make(kind: str, sides: int = 3) -> Shape:
    """Make a shape of ``kind``."""
    def inner():
        return None
    return Shape(sides)


def _private_function():
    pass
'''.replace("LONG_DOC", LONG_DOC)

EXPECTED = '''\
## pkg/shapes.py (60 lines)
Number = ... (L7)
DEFAULT_SIDES = ... (L11)
_cache = ... (L12)
limit = ... (L12)
class Shape(Base, metaclass=Meta) (L15-L45): A shape with sides.
  fields: sides, _secret, name (L21-L23)
  def __init__(self, sides: int=DEFAULT_SIDES, *, name: str | None=None) -> None (L25)
  def area(self, scale=1) (L32): CUT_DOC
  def _helper(self) (L36)
  async def draw(self, *points, **options) -> list[str] (L39): Draw it.
class Empty (L48-L49)
def make(kind: str, sides: int=3) -> Shape (L52): Make a shape of ``kind``.
def _private_function() (L59)
'''.replace("CUT_DOC", LONG_DOC[:97].rstrip() + "...")


def write(root: Path, files: dict[str, str]) -> RepoIndex:
    """Write ``files`` under ``root`` and return an index that lists them."""
    for path, content in files.items():
        target = root / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8", newline="\n")
    return RepoIndex(commit="0" * 40, files=set(files))


def test_outline_lists_classes_functions_methods_and_names(tmp_path):
    index = write(tmp_path, {"pkg/shapes.py": SHAPES})
    outline = build_outline(tmp_path, index)
    assert outline.text == EXPECTED
    assert (outline.reduction, outline.files, outline.omitted_files) == ("none", 1, [])
    assert outline.chars == len(EXPECTED)
    assert repo_outline(tmp_path, index) == EXPECTED
    cut = next(line for line in EXPECTED.splitlines() if "def area" in line)
    assert len(LONG_DOC) > 100
    assert len(cut.split("): ", 1)[1]) == 100  # the docstring's first line, cut to 100
    for absent in ("More text", "class Local", "def inner", "scale: int", "return 0"):
        assert absent not in outline.text  # no bodies, nested definitions or overload stubs


def test_outline_skips_tests_and_conftest_and_other_files(tmp_path):
    code = "def f():\n    pass\n"
    index = write(tmp_path, {
        "app.py": code, "pkg/__init__.py": "", "pkg/mod.py": code,
        "tests/test_app.py": code, "tests/helpers.py": code, "pkg/tests/util.py": code,
        "test_top.py": code, "pkg/test_mod.py": code, "conftest.py": code,
        "pkg/conftest.py": code, "README.md": "# Readme\n", "setup.cfg": "[x]\n",
        "testing.py": code, "pkg/latest_tests.py": code})
    outline = build_outline(tmp_path, index)
    headers = [line for line in outline.text.splitlines() if line.startswith("## ")]
    assert headers == ["## app.py (2 lines)", "## pkg/__init__.py (0 lines)",
                       "## pkg/latest_tests.py (2 lines)", "## pkg/mod.py (2 lines)",
                       "## testing.py (2 lines)"]
    assert outline.files == 5
    assert is_source_file("src/click/types.py") and is_source_file("testing.py")
    for path in ("tests/test_x.py", "tests/helper.py", "a/tests/b.py", "test_x.py",
                 "a/test_x.py", "conftest.py", "a/conftest.py", "README.md", "a/b.txt"):
        assert not is_source_file(path), path


def test_a_file_that_cannot_be_parsed_is_named(tmp_path):
    index = write(tmp_path, {"broken.py": "def broken(:\n    pass\n", "ok.py": "X = 1\n",
                             "gone.py": ""})
    (tmp_path / "gone.py").unlink()
    assert build_outline(tmp_path, index).text == (
        "## broken.py (2 lines) (could not be parsed)\n\n"
        "## gone.py (0 lines) (could not be parsed)\n\n"
        "## ok.py (1 lines)\nX = ... (L1)\n")


def test_a_repository_without_source_files(tmp_path):
    index = write(tmp_path, {"README.md": "# Readme\n", "tests/test_a.py": "x = 1\n"})
    outline = build_outline(tmp_path, index)
    assert (outline.text, outline.reduction, outline.files) == (
        "(no Python source files)\n", "none", 0)


def test_private_names():
    assert is_private("_helper") and is_private("__mangled")
    assert not is_private("__init__") and not is_private("public") and not is_private("__all__")


def test_outline_is_shortened_step_by_step_to_fit_the_limit(tmp_path):
    files = {"pkg/shapes.py": SHAPES}
    files.update({f"pkg/m{number}.py": f'"""Module {number}."""\n\n\nclass C{number}:\n'
                  f'    """Class {number}."""\n\n    def run(self):\n'
                  f'        """Run {number}."""\n\n\ndef f{number}():\n'
                  f'    """Function {number}."""\n\n\n_T{number} = 1\nV{number} = 2\n'
                  for number in range(1, 6)})
    index = write(tmp_path, files)
    assert REDUCTIONS == ("none", "no_docstrings", "no_private", "no_methods", "truncated")
    assert OUTLINE_LIMIT == 60_000

    full = build_outline(tmp_path, index)
    assert full.reduction == "none" and ": Run 3." in full.text and "_T3 = ..." in full.text
    assert build_outline(tmp_path, index, limit=full.chars).reduction == "none"

    no_docs = build_outline(tmp_path, index, limit=full.chars - 1)
    assert no_docs.reduction == "no_docstrings" and no_docs.chars < full.chars
    assert no_docs.text.startswith("(Shortened to fit: docstrings are left out.)\n\n## pkg/m1.py")
    assert "Run 3." not in no_docs.text and "A shape with sides" not in no_docs.text
    assert "  def run(self) (L7)\n" in no_docs.text and "_T3 = ... (L15)" in no_docs.text
    assert "  def _helper(self) (L36)" in no_docs.text

    no_private = build_outline(tmp_path, index, limit=no_docs.chars - 1)
    assert no_private.reduction == "no_private" and no_private.chars < no_docs.chars
    assert "names starting with `_` are left out" in no_private.text.splitlines()[0]
    for private in ("_T3", "_helper", "_private_function", "_cache", "_secret"):
        assert private not in no_private.text
    assert "  def __init__(self, sides: int=DEFAULT_SIDES" in no_private.text  # not private
    assert "  fields: sides, name (L21-L23)" in no_private.text
    assert "V3 = ... (L16)" in no_private.text and "  def run(self) (L7)" in no_private.text

    no_methods = build_outline(tmp_path, index, limit=no_private.chars - 1)
    assert no_methods.reduction == "no_methods" and no_methods.chars < no_private.chars
    assert no_methods.text.splitlines()[0] == (
        "(Shortened to fit: only classes and top-level functions are listed.)")
    section = no_methods.text[no_methods.text.index("## pkg/shapes.py"):]
    assert section == ("## pkg/shapes.py (60 lines)\n"
                       "class Shape(Base, metaclass=Meta) (L15-L45)\n"
                       "class Empty (L48-L49)\n"
                       "def make(kind: str, sides: int=3) -> Shape (L52)\n")
    assert no_methods.omitted_files == []

    cut = build_outline(tmp_path, index, limit=no_methods.chars - 1)
    assert cut.reduction == "truncated" and cut.chars <= no_methods.chars - 1
    assert cut.omitted_files == ["pkg/shapes.py"] and cut.files == 6
    assert "## pkg/m5.py" in cut.text and "## pkg/shapes.py" not in cut.text
    assert cut.text.endswith("(Outline cut off here: 1 more source file(s) are not listed: "
                             "pkg/shapes.py.)\n")

    for limit in (400, 250, 60):
        small = build_outline(tmp_path, index, limit=limit)
        assert small.reduction == "truncated" and small.chars <= limit
        listed = [line[3:].split(" ")[0] for line in small.text.splitlines()
                  if line.startswith("## ")]
        assert listed + small.omitted_files == sorted(files)  # every file is listed or named


@pytest.mark.parametrize("limit", [10_000, 500])
def test_outline_never_exceeds_its_limit(tmp_path, limit):
    index = write(tmp_path, {f"m{number:02}.py": SHAPES for number in range(30)})
    outline = build_outline(tmp_path, index, limit=limit)
    assert outline.chars <= limit
    assert outline.reduction == ("no_methods" if limit == 10_000 else "truncated")
