"""The interface of a SWE-EVO release: extractor, section, and the checks of the validation."""
from __future__ import annotations

import textwrap

from aqours_code.taskgraph.sweevo_interface import (
    DOCSTRING_LINES,
    SECTION_INTRO,
    SECTION_TITLE,
    added_body_lines,
    added_line_numbers,
    file_interface,
    header_and_docstring_lines,
    imported_names,
    interface_section,
    is_test_path,
    item_count,
    module_bindings,
    release_interface,
)
from taskgraph_support import commit_files, git

OLD = textwrap.dedent('''\
    """A module."""
    import os

    LIMIT = 3
    _hidden = 1


    def kept(a, b=2):
        return a + b


    def gone(x):
        return x


    def reshaped(a, b):
        return a


    def _private_helper():
        return 0


    class Store(object):
        def get(self, key):
            return key

        def __init__(self, root):
            self.root = root

        def _cache(self):
            return {}
    ''')

NEW = textwrap.dedent('''\
    """A module."""
    import os

    LIMIT = 3
    _hidden = 1
    TIMEOUT, RETRIES = 30, 2
    _secret = "x"


    def kept(a, b=2):
        return a + b


    def reshaped(a, b, *, mode: str = "fast") -> int:
        """Reshape.

        More words.
        """
        return a


    def added(path: "os.PathLike", *args, **kwargs):
        def nested_helper():
            return 1
        return nested_helper()


    def _private_helper(extra):
        return 0


    def _new_private():
        return 0


    class Store(object):
        def get(self, key, default=None):
            return key

        def __init__(self, root, mode):
            self.root = root

        def _cache(self, size):
            return {}

        @property
        def size(self):
            return len(self.root)


    class HTTPStore(Store, metaclass=type):
        """A store over HTTP."""

        timeout = 10

        def __init__(self, url):
            self.url = url

        @classmethod
        def from_parts(
            cls,
            host=None,
            port=80,
        ):
            return cls(f"{host}:{port}")

        def _connect(self):
            return None

        def __eq__(self, other):
            return self.url == other.url


    class _Internal:
        def run(self):
            return 1
    ''')


def items_by_name(interface):
    return {item.qualname: item for item in interface.items}


def test_added_class_with_bases_methods_and_its_dunders():
    items = items_by_name(file_interface("pkg/store.py", OLD, NEW))
    cls = items["HTTPStore"]
    assert (cls.change, cls.kind) == ("added", "class")
    assert cls.signatures == ["class HTTPStore(Store, metaclass=type):"]
    assert cls.docstring == "A store over HTTP."
    for name in ("HTTPStore.__init__", "HTTPStore.from_parts", "HTTPStore.__eq__"):
        assert items[name].change == "added" and items[name].kind == "method"
    assert items["HTTPStore.from_parts"].signatures == [
        "@classmethod\ndef from_parts(\n    cls,\n    host=None,\n    port=80,\n):"]
    assert "HTTPStore._connect" not in items  # private, even in a new class
    assert "HTTPStore.timeout" not in items  # class attributes are not listed


def test_added_function_and_module_level_names():
    items = items_by_name(file_interface("pkg/store.py", OLD, NEW))
    assert items["added"].signatures == ['def added(path: "os.PathLike", *args, **kwargs):']
    assert items["added"].kind == "function" and items["added"].change == "added"
    assert items["TIMEOUT"].kind == "name" and items["RETRIES"].kind == "name"
    assert items["TIMEOUT"].signatures == []  # the name only, no value
    assert "LIMIT" not in items  # unchanged


def test_changed_signatures_show_the_new_one():
    items = items_by_name(file_interface("pkg/store.py", OLD, NEW))
    assert items["reshaped"].change == "changed"
    assert items["reshaped"].signatures == [
        'def reshaped(a, b, *, mode: str = "fast") -> int:']
    assert items["Store.get"].change == "changed"
    assert items["Store.get"].signatures == ["def get(self, key, default=None):"]
    assert items["Store.size"].change == "added"
    assert items["Store.size"].signatures == ["@property\ndef size(self):"]
    assert "kept" not in items  # same signature


def test_removed_function_and_methods_of_a_removed_class():
    items = items_by_name(file_interface("pkg/store.py", OLD, NEW))
    assert items["gone"].change == "removed" and items["gone"].signatures == []
    gone_class = file_interface("pkg/a.py", "class Old:\n    def m(self):\n        pass\n", "")
    assert [(item.change, item.qualname) for item in gone_class.items] == [("removed", "Old")]


def test_private_names_are_left_out():
    names = set(items_by_name(file_interface("pkg/store.py", OLD, NEW)))
    for private in ("_secret", "_hidden", "_private_helper", "_new_private", "Store._cache",
                    "Store.__init__", "_Internal", "_Internal.run"):
        assert private not in names  # a dunder counts only in a new class


def test_nested_functions_are_left_out():
    names = set(items_by_name(file_interface("pkg/store.py", OLD, NEW)))
    assert not any("nested_helper" in name for name in names)


def test_docstrings_are_included_and_capped():
    items = items_by_name(file_interface("pkg/store.py", OLD, NEW))
    assert items["reshaped"].docstring == "Reshape.\n\nMore words."
    long_doc = "\n".join(f"line {n}" for n in range(30))
    source = f'def documented():\n    """{long_doc}\n    """\n    return 1\n'
    item = file_interface("pkg/d.py", None, source).items[0]
    assert item.docstring.splitlines() == [f"line {n}" for n in range(DOCSTRING_LINES)] + ["..."]


def test_a_file_that_does_not_parse_is_reported():
    broken = file_interface("pkg/bad.py", "def ok():\n    pass\n", "def broken(:\n    pass\n")
    assert broken.items == [] and broken.error.startswith("SyntaxError: gold version")
    text = interface_section([broken])
    assert "### `pkg/bad.py`" in text and "could not be read" in text


def test_a_new_file_and_conditional_definitions():
    source = textwrap.dedent('''\
        import sys
        if sys.version_info >= (3, 8):
            def compat(x):
                return x
        else:
            def compat(x, y=None):
                return x
        try:
            FAST = True
        except ImportError:
            FAST = False
        ''')
    items = items_by_name(file_interface("pkg/new.py", None, source))
    assert items["compat"].signatures == ["def compat(x):", "def compat(x, y=None):"]
    assert items["FAST"].kind == "name"


def test_the_section_never_shows_a_body():
    interface = file_interface("pkg/store.py", OLD, NEW)
    text = interface_section([interface])
    assert text.startswith(f"{SECTION_TITLE}\n\n{SECTION_INTRO}\n\n### `pkg/store.py`\n\n```python\n")
    for body in ("return a + b", "self.url = url", "nested_helper()", "return len(self.root)",
                 'cls(f"{host}:{port}")', "timeout = 10", "= 30"):
        assert body not in text
    assert "# added: class HTTPStore\nclass HTTPStore(Store, metaclass=type):" in text
    assert "# existing class Store: its added or changed methods\nclass Store(object):" in text
    assert "    # changed: get (the new signature)\n    def get(self, key, default=None):" in text
    assert "# removed: function gone" in text
    assert item_count([interface]) == len(interface.items)
    compile(text.split("```python\n")[1].split("```")[0], "stub", "exec")  # a valid stub


def test_release_interface_reads_only_non_test_python_files(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    git(repo, "init", "-q")
    commit_files(repo, {"pkg/store.py": OLD, "tests/test_store.py": "def test_a():\n    pass\n",
                        "README.md": "x\n"}, "base")
    for relative, content in {"pkg/store.py": NEW, "README.md": "y\n",
                              "tests/test_store.py": "def test_a():\n    pass\n\n\n"
                                                     "def test_new_feature():\n    pass\n"}.items():
        (repo / relative).write_text(content, encoding="utf-8", newline="\n")
    patch = git(repo, "diff") + "\n"
    git(repo, "checkout", "--", ".")
    files = release_interface(repo, patch)
    assert [interface.path for interface in files] == ["pkg/store.py"]
    text = interface_section(files)
    assert "test_new_feature" not in text and "tests/test_store.py" not in text
    assert git(repo, "status", "--porcelain") == ""  # the repository is not touched


def test_is_test_path():
    for path in ("tests/test_x.py", "dask/array/tests/helpers.py", "pkg/test_util.py",
                 "conftest.py", "pkg/util_test.py", "testing/tools.py"):
        assert is_test_path(path)
    for path in ("pkg/testing.py", "dvc/remote/local.py", "pydantic/_internal/x.py"):
        assert not is_test_path(path)


# ── the checks of the validation ──

def test_imported_names_from_imports_and_module_attributes():
    source = textwrap.dedent('''\
        import os
        import pkg.remote.local
        import pkg.path_info as pi
        from pkg.path_info import HTTPURLInfo, URLInfo as U
        from pkg.remote import local
        from other import thing

        def test_x():
            pkg.remote.local.RemoteLOCAL.is_protected
            pi.PathInfo("x")
            local.CopyMode
            HTTPURLInfo.from_parts
            os.path.join
        ''')
    modules = {"pkg", "pkg.remote", "pkg.remote.local", "pkg.path_info"}
    names = imported_names(source, "pkg", lambda dotted: dotted in modules)
    assert set(names) == {
        ("pkg.path_info", "HTTPURLInfo", "from"), ("pkg.path_info", "URLInfo", "from"),
        ("pkg.remote", "local", "from"),
        ("pkg.remote.local", "RemoteLOCAL", "attribute"), ("pkg.path_info", "PathInfo", "attribute"),
        ("pkg.remote.local", "CopyMode", "attribute"),
    }
    assert len(names) == len(set(names))  # each once; no class attribute, nothing from os


def test_a_module_alias_rebound_in_the_file_is_not_followed():
    source = ("import pkg.array as pa\n\ndef test_x(ddf):\n    pa = ddf.a\n"
              "    return pa.corr(ddf.b)\n")
    assert imported_names(source, "pkg", lambda dotted: dotted in {"pkg", "pkg.array"}) == []


def test_header_and_docstring_lines():
    source = ('"""Module doc."""\n@decorate(\n    1)\ndef f(\n    self,\n    x=1,\n):\n'
              '    """Doc of f."""\n    return x\n')
    assert header_and_docstring_lines({"pkg/m.py": source, "tests/test_m.py": source}) == {
        '"""Module doc."""', "@decorate(", "1)", "def f(", "self,", "x=1,", "):",
        '"""Doc of f."""'}


def test_module_bindings():
    source = ("import a.b\nfrom c import d as e\nX = 1\ndef f():\n    import g\n"
              "class K:\n    pass\ntry:\n    from h import i\nexcept ImportError:\n    i = None\n")
    assert module_bindings(source) == {"a", "e", "X", "f", "K", "i"}


def test_added_lines_outside_headers_decorators_and_docstrings():
    patch = textwrap.dedent('''\
        diff --git a/pkg/m.py b/pkg/m.py
        --- a/pkg/m.py
        +++ b/pkg/m.py
        @@ -1,2 +1,9 @@
         import os
        +
        +@decorated
        +def new(a,
        +        b=1):
        +    """Docs of new."""
        +    value = compute_something(a, b)
        +    return value
         X = 1
        ''')
    assert added_line_numbers(patch) == {"pkg/m.py": [2, 3, 4, 5, 6, 7, 8]}
    after = {"pkg/m.py": 'import os\n\n@decorated\ndef new(a,\n        b=1):\n'
                         '    """Docs of new."""\n    value = compute_something(a, b)\n'
                         '    return value\nX = 1\n'}
    assert added_body_lines(patch, after) == {"value = compute_something(a, b)",
                                              "return value"}
