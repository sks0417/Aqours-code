from __future__ import annotations


def serialize_result(platform: str, packages) -> dict:
    rows = [
        {"name": item.name, "version": item.version, "digest": item.digest}
        for item in packages
    ]
    lock = {
        item.name: {"version": item.version, "digest": item.digest}
        for item in sorted(packages, key=lambda value: value.name)
    }
    return {"platform": platform, "packages": rows, "lock": lock}
