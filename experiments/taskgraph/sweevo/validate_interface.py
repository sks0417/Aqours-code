"""Check the interface sections of the SWE-EVO tasks (no model, no Docker).

    python experiments/taskgraph/sweevo/validate_interface.py [<instance_id> ...]
        [--tasks C:\\tg\\sweevo-tasks]

For every instance in use (``instances.txt`` without ``dropped``) it builds the
interface section from the task repository and the gold patch, as
``prepare.py`` does for ``request_iface.md``, and reports:

- its size: files, items (added, changed, removed), characters, and files
  whose interface could not be read;
- the import check: every name the release's test files (those of the test
  patch, with the patch applied) take from the package, by ``from pkg.mod
  import Name`` or as ``pkg.mod.Name`` on an imported module, must exist in
  the base commit or be an item of the section. Each miss is listed with what
  the name is in the gold code;
- body lines: lines of the section that are lines the gold patch adds outside
  ``def``/``class`` headers, decorators and docstrings (there must be none);
- test text: test file paths, test names, and lines added by the test patch
  that appear in the section (there must be none);
- whether ``<task>/request_iface.md`` ends with the section.

It writes ``validation/interface.md`` (the table, the misses) and
``validation/interface.json``.
"""
from __future__ import annotations

import argparse
import ast
import json
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parents[2]))  # the Aqours checkout, for aqours_code.taskgraph

from aqours_code.taskgraph import gitops  # noqa: E402
from aqours_code.taskgraph.sweevo_interface import (  # noqa: E402
    added_body_lines, added_line_numbers, definitions, header_and_docstring_lines,
    imported_names, interface_section, is_public, is_trivial, item_count, module_bindings,
    patched_sources, release_interface, star_imports)
from prepare import selected_ids  # noqa: E402  (this directory is on sys.path)

DEFAULT_TASKS = Path(r"C:\tg\sweevo-tasks")
REPORTS = HERE / "validation"


class Code:
    """Module sources of one side (base, or base plus the gold patch)."""

    def __init__(self, repo: Path, overlay: dict[str, str | None] | None = None):
        self.repo, self.overlay, self.cache = repo, overlay or {}, {}

    def file(self, path: str) -> str | None:
        if path in self.overlay:
            return self.overlay[path]
        if path not in self.cache:
            blob = gitops.blob_at(self.repo, "HEAD", path)
            self.cache[path] = None if blob is None else blob.decode("utf-8", "replace")
        return self.cache[path]

    def module_path(self, module: str) -> str | None:
        base = module.replace(".", "/")
        for path in (f"{base}.py", f"{base}/__init__.py"):
            if self.file(path) is not None:
                return path
        return None

    def bindings(self, module: str, depth: int = 0) -> set[str]:
        """Names bound at module level, following ``from <package module> import *``."""
        path = self.module_path(module)
        source = self.file(path) if path else None
        if source is None:
            return set()
        try:
            names = module_bindings(source)
            stars = star_imports(source)
        except SyntaxError:
            return set()
        if depth < 3:
            for star, level in stars:
                target = star
                if level:
                    parent = module if path.endswith("__init__.py") else module.rsplit(".", 1)[0]
                    for _ in range(level - 1):
                        parent = parent.rsplit(".", 1)[0]
                    target = f"{parent}.{star}" if star else parent
                names |= {name for name in self.bindings(target, depth + 1)
                          if not name.startswith("_")}
        return names

    def has(self, module: str, name: str) -> bool:
        return name in self.bindings(module) or self.module_path(f"{module}.{name}") is not None

    def _imports(self, module: str):
        """The module's import statements at module level: (target module, names)."""
        path = self.module_path(module)
        source = self.file(path) if path else None
        if source is None:
            return []
        try:
            tree = ast.parse(source)
        except SyntaxError:
            return []
        found = []
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and _module_level(tree, node):
                target = node.module or ""
                if node.level:
                    parent = module if path.endswith("__init__.py") else module.rsplit(".", 1)[0]
                    for _ in range(node.level - 1):
                        parent = parent.rsplit(".", 1)[0]
                    target = f"{parent}.{target}" if target else parent
                found.append((target, [(alias.name, alias.asname) for alias in node.names]))
        return found

    def origin(self, module: str, name: str, depth: int = 0) -> tuple[str, str] | None:
        """(file, name) where ``module.name`` is defined, following re-exports in the repository."""
        path = self.module_path(module)
        source = self.file(path) if path else None
        if source is None or depth > 4:
            return None
        try:
            if name in definitions(ast.parse(source)):
                return path, name
        except SyntaxError:
            return None
        for target, aliases in self._imports(module):
            for original, asname in aliases:
                if original == "*" and self.module_path(target) is not None:
                    found = self.origin(target, name, depth + 1)
                    if found:
                        return found
                elif (asname or original) == name and self.module_path(target) is not None:
                    return self.origin(target, original, depth + 1)
        return None

    def external_star(self, module: str) -> str | None:
        """A module outside the repository that ``module`` (or a parent) star-imports."""
        parts = module.split(".")
        for end in range(len(parts), 0, -1):
            for target, aliases in self._imports(".".join(parts[:end])):
                if any(original == "*" for original, _ in aliases) and not self.module_path(target):
                    return target
        return None


def _module_level(tree: ast.Module, target) -> bool:
    from aqours_code.taskgraph.sweevo_interface import _at_module_level  # noqa: PLC0415

    return _at_module_level(tree, target)


def what_it_is(gold: Code, module: str, name: str) -> str:
    """What ``module.name`` is in the gold code, for a miss."""
    path = gold.module_path(module)
    if gold.module_path(f"{module}.{name}") is not None:
        return "a module the release adds (modules are not interface items)"
    source = gold.file(path) if path else None
    if source is None:
        return "not found: the module does not exist in the gold code"
    try:
        tree = ast.parse(source)
    except SyntaxError:
        return "the gold module does not parse"
    found = definitions(tree)
    if name in found:
        kind = found[name].kind
        if not is_public(name, kind, set()):
            return f"a private {kind} of the gold code (the rule lists public names only)"
        return f"a public {kind} of the gold code that the section does not list"
    if name in module_bindings(source):
        return "imported into the module by the gold code (a re-export, not a definition)"
    if name in gold.bindings(module):
        return "re-exported by a star import in the gold code"
    return "not found in the gold code either"


def import_check(task_dir: Path, package: str, gold_after: dict, files) -> dict:
    repo = task_dir / "repo"
    test_patch = (task_dir / "grading" / "test.patch").read_text(encoding="utf-8")
    _, test_after = patched_sources(repo, test_patch)
    base, gold = Code(repo), Code(repo, gold_after)
    listed = {(interface.path, item.qualname) for interface in files
              for item in interface.items if item.change != "removed"}

    def is_module(dotted: str) -> bool:
        return base.module_path(dotted) is not None or gold.module_path(dotted) is not None

    checked, in_base, in_interface, reexported, misses, unchecked = 0, 0, 0, 0, [], []
    for test_file, source in sorted(test_after.items()):
        if source is None or not test_file.endswith(".py"):
            continue
        try:
            names = imported_names(source, package, is_module)
        except SyntaxError as exc:
            misses.append({"test_file": test_file, "module": "", "name": "", "how": "",
                           "is": f"the test file does not parse: {exc}"})
            continue
        for module, name, how in names:
            checked += 1
            if base.has(module, name):
                in_base += 1
                continue
            path = gold.module_path(module)
            if path is not None and (path, name) in listed:
                in_interface += 1
                continue
            origin = gold.origin(module, name)
            if origin is not None and origin in listed:
                in_interface += 1
                reexported += 1
                continue
            outside = base.external_star(module) or gold.external_star(module)
            if outside:
                unchecked.append({"test_file": test_file, "module": module, "name": name,
                                  "how": how, "star_import_from": outside})
                continue
            misses.append({"test_file": test_file, "module": module, "name": name, "how": how,
                           "is": what_it_is(gold, module, name)})
    return {"checked": checked, "in_base": in_base, "in_interface": in_interface,
            "in_interface_by_reexport": reexported, "unchecked": unchecked, "misses": misses}


def test_text(test_patch: str) -> tuple[set[str], set[str], set[str]]:
    """Test file paths, test names, and non-trivial lines the test patch adds."""
    paths = set(added_line_numbers(test_patch))
    names = set(re.findall(r"^\+\s*(?:async\s+)?def\s+(test\w*)", test_patch, re.M))
    names |= set(re.findall(r"^\+\s*class\s+(Test\w*)", test_patch, re.M))
    lines = {line[1:].strip() for line in test_patch.splitlines()
             if line.startswith("+") and not line.startswith("+++")
             and not is_trivial(line[1:])}
    return paths, names, lines


def check(instance_id: str, tasks: Path) -> dict:
    task_dir = tasks / instance_id
    instance = json.loads((task_dir / "grading" / "instance.json").read_text(encoding="utf-8"))
    gold_patch = (task_dir / "grading" / "gold.patch").read_text(encoding="utf-8")
    test_patch = (task_dir / "grading" / "test.patch").read_text(encoding="utf-8")
    files = release_interface(task_dir / "repo", gold_patch)
    section = interface_section(files)
    _, gold_after = patched_sources(task_dir / "repo", gold_patch)
    body = added_body_lines(gold_patch, gold_after)
    signature_lines = header_and_docstring_lines(gold_after)
    section_lines = [line.strip() for line in section.splitlines()]
    # A section line equal to a body line or a test-patch line that is itself a
    # line of a gold header or docstring (``self,``, ``jobs=None,``) is a
    # coincidence, listed apart; any other such line is a leak.
    body_same = {line for line in section_lines if line in body}
    paths, names, lines = test_text(test_patch)
    test_same = {line for line in section_lines if line in lines}
    body_hits = sorted(body_same - signature_lines)
    test_hits = sorted({path for path in paths if path in section}
                       | {name for name in names if re.search(rf"\b{re.escape(name)}\b", section)}
                       | (test_same - signature_lines))
    request_iface = task_dir / "request_iface.md"
    counts = {change: sum(1 for interface in files for item in interface.items
                          if item.change == change) for change in ("added", "changed", "removed")}
    return {
        "instance_id": instance_id, "package": instance["package"],
        "files": len(files), "items": item_count(files), **counts,
        "chars": len(section), "not_read": [interface.path for interface in files
                                            if interface.error],
        "imports": import_check(task_dir, instance["package"], gold_after, files),
        "body_lines_in_section": body_hits, "gold_body_lines": len(body),
        "test_text_in_section": test_hits,
        "coincidences": sorted((body_same | test_same) & signature_lines),
        "request_iface_matches": request_iface.is_file()
        and request_iface.read_text(encoding="utf-8").endswith(section),
    }


def write_report(results: list[dict]) -> None:
    rows = ["# Interface sections", "",
            "Made by `validate_interface.py` (no model, no Docker). For each instance in use: "
            "the size of the section `prepare.py` appends to `request_iface.md`; the import "
            "check (every name the release's test files take from the package, by `from "
            "pkg.mod import Name` or as `pkg.mod.Name`, exists in the base commit or is an "
            "item of the section); lines of the section that are body lines the gold patch "
            "adds (outside headers, decorators, docstrings); and test file paths, test names "
            "or lines of the test patch found in the section.", "",
            "Columns: *Test imports*: names checked; *In base*: they exist in the base "
            "commit; *In section*: items of the section (in brackets, through a re-export: a "
            "module that imports the name from the module that defines it); *Unchecked*: "
            "names that come from a star import of a package outside the repository "
            "(`from distributed import *` in `dask/distributed.py`), at the base too; "
            "*Misses*: the rest, listed below. A section line counts as a body line or test "
            "text only if it is not itself a line of a gold header or docstring; those "
            "coincidences are listed at the end.", "",
            "| Instance | Files | Items (added / changed / removed) | Characters | Not read | "
            "Test imports | In base | In section | Unchecked | Misses | Body lines in section | "
            "Test text in section | request_iface.md |", "|" + " --- |" * 13]
    for r in results:
        imports = r["imports"]
        rows.append("| " + " | ".join([
            r["instance_id"], str(r["files"]),
            f"{r['items']} ({r['added']} / {r['changed']} / {r['removed']})", str(r["chars"]),
            ", ".join(r["not_read"]) or "0", str(imports["checked"]), str(imports["in_base"]),
            f"{imports['in_interface']} ({imports['in_interface_by_reexport']})",
            str(len(imports["unchecked"])), str(len(imports["misses"])),
            str(len(r["body_lines_in_section"])), str(len(r["test_text_in_section"])),
            "matches" if r["request_iface_matches"] else "DIFFERS"]) + " |")
    rows += ["", "## Import-check misses", ""]
    if not any(r["imports"]["misses"] for r in results):
        rows.append("None.")
    for r in results:
        misses = r["imports"]["misses"]
        if not misses:
            continue
        rows += [f"### {r['instance_id']} ({len(misses)})", ""]
        for miss in misses:
            rows.append(f"- `{miss['module']}.{miss['name']}` ({miss['how']}, in "
                        f"`{miss['test_file']}`): {miss['is']}")
        rows.append("")
    if any(r["coincidences"] for r in results):
        rows += ["", "## Coincidences (not leaks)", "",
                 "Section lines equal to a line the gold patch adds in a body or to a line of "
                 "the test patch, which are themselves lines of a gold signature or docstring:",
                 ""]
        for r in results:
            if r["coincidences"]:
                rows.append(f"- {r['instance_id']}: "
                            + ", ".join(f"`{line}`" for line in r["coincidences"]))
    leaks = [r for r in results if r["body_lines_in_section"] or r["test_text_in_section"]]
    if leaks:
        rows += ["", "## Body lines or test text in a section", ""]
        for r in leaks:
            for line in r["body_lines_in_section"]:
                rows.append(f"- {r['instance_id']}: body line `{line}`")
            for text in r["test_text_in_section"]:
                rows.append(f"- {r['instance_id']}: test text `{text}`")
    REPORTS.mkdir(exist_ok=True)
    (REPORTS / "interface.md").write_text("\n".join(rows).rstrip() + "\n", encoding="utf-8",
                                          newline="\n")
    (REPORTS / "interface.json").write_text(json.dumps(results, indent=1) + "\n",
                                            encoding="utf-8", newline="\n")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("ids", nargs="*")
    parser.add_argument("--tasks", type=Path, default=DEFAULT_TASKS)
    args = parser.parse_args(argv)
    used, _, _ = selected_ids()
    results = [check(instance_id, args.tasks) for instance_id in (args.ids or used)]
    for r in results:
        imports = r["imports"]
        print(f"{r['instance_id']}: {r['items']} items, {r['chars']} chars; imports "
              f"{imports['checked']} (base {imports['in_base']}, section "
              f"{imports['in_interface']}, unchecked {len(imports['unchecked'])}, misses "
              f"{len(imports['misses'])}); body lines "
              f"{len(r['body_lines_in_section'])}; test text {len(r['test_text_in_section'])}")
    write_report(results)
    bad = [r for r in results if r["body_lines_in_section"] or r["test_text_in_section"]
           or r["not_read"] or not r["request_iface_matches"]]
    return 1 if bad else 0


if __name__ == "__main__":
    raise SystemExit(main())
