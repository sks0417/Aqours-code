# Interface sections

Made by `validate_interface.py` (no model, no Docker). For each instance in use: the size of the section `prepare.py` appends to `request_iface.md`; the import check (every name the release's test files take from the package, by `from pkg.mod import Name` or as `pkg.mod.Name`, exists in the base commit or is an item of the section); lines of the section that are body lines the gold patch adds (outside headers, decorators, docstrings); and test file paths, test names or lines of the test patch found in the section.

Columns: *Test imports*: names checked; *In base*: they exist in the base commit; *In section*: items of the section (in brackets, through a re-export: a module that imports the name from the module that defines it); *Unchecked*: names that come from a star import of a package outside the repository (`from distributed import *` in `dask/distributed.py`), at the base too; *Misses*: the rest, listed below. A section line counts as a body line or test text only if it is not itself a line of a gold header or docstring; those coincidences are listed at the end.

| Instance | Files | Items (added / changed / removed) | Characters | Not read | Test imports | In base | In section | Unchecked | Misses | Body lines in section | Test text in section | request_iface.md |
| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |
| iterative__dvc_1.1.7_1.1.8 | 2 | 3 (0 / 3 / 0) | 775 | 0 | 35 | 35 | 0 (0) | 0 | 0 | 0 | 0 | matches |
| dask__dask_2023.6.1_2023.7.0 | 2 | 2 (0 / 2 / 0) | 1855 | 0 | 101 | 101 | 0 (0) | 0 | 0 | 0 | 0 | matches |
| iterative__dvc_1.6.3_1.6.4 | 5 | 6 (4 / 2 / 0) | 1385 | 0 | 8 | 8 | 0 (0) | 0 | 0 | 0 | 0 | matches |
| dask__dask_2023.8.0_2023.8.1 | 1 | 1 (0 / 1 / 0) | 455 | 0 | 126 | 126 | 0 (0) | 0 | 0 | 0 | 0 | matches |
| iterative__dvc_0.89.0_0.90.0 | 5 | 16 (11 / 5 / 0) | 2143 | 0 | 160 | 159 | 1 (0) | 0 | 0 | 0 | 0 | matches |
| iterative__dvc_2.19.0_2.20.0 | 9 | 13 (7 / 6 / 0) | 3480 | 0 | 28 | 28 | 0 (0) | 0 | 0 | 0 | 0 | matches |
| pydantic__pydantic_v2.7.0_v2.7.1 | 3 | 5 (4 / 1 / 0) | 2652 | 0 | 208 | 206 | 2 (2) | 0 | 0 | 0 | 0 | matches |
| dask__dask_2023.6.0_2023.6.1 | 7 | 22 (12 / 3 / 7) | 5217 | 0 | 301 | 293 | 4 (1) | 4 | 0 | 0 | 0 | matches |

## Import-check misses

None.

## Coincidences (not leaks)

Section lines equal to a line the gold patch adds in a body or to a line of the test patch, which are themselves lines of a gold signature or docstring:

- iterative__dvc_2.19.0_2.20.0: `jobs=None,`, `no_download=False,`, `no_exec=False,`, `self,`
- dask__dask_2023.6.0_2023.6.1: `See Also`
