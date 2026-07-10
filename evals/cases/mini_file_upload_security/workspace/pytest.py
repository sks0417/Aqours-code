from __future__ import annotations

import importlib.util
import inspect
import traceback
from pathlib import Path


def main() -> int:
    failures = []
    tests = 0
    for test_file in sorted((Path.cwd() / "tests").glob("test_*.py")):
        spec = importlib.util.spec_from_file_location(test_file.stem, test_file)
        module = importlib.util.module_from_spec(spec)
        try:
            assert spec.loader is not None
            spec.loader.exec_module(module)
            for name, value in vars(module).items():
                if name.startswith("test_") and inspect.isfunction(value):
                    tests += 1
                    try:
                        value()
                    except Exception:
                        failures.append((f"{test_file.name}::{name}", traceback.format_exc()))
        except Exception:
            failures.append((test_file.name, traceback.format_exc()))
    for label, detail in failures:
        print(f"FAILED {label}")
        print(detail)
    if failures:
        print(f"{len(failures)} failed, {tests} collected")
        return 1
    print(f"{tests} passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
