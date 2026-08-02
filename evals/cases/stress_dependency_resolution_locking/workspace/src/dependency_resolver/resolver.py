from __future__ import annotations

from collections import defaultdict

from .constraints import matches, parse_constraint
from .errors import (
    LockError, PackageConflict, UnknownPackage, UnsatisfiedConstraints,
)
from .graph import installation_order
from .models import SelectedPackage
from .serialization import serialize_result
from .semver import Version


class DependencySolver:
    def __init__(self, registry):
        self._registry = registry

    @staticmethod
    def _active_dependencies(metadata, platform, enabled_features):
        dependencies = dict(metadata["dependencies"])
        # The original client installed every optional and platform adapter.
        for values in metadata["optional_dependencies"].values():
            dependencies.update(values)
        for values in metadata["platform_dependencies"].values():
            dependencies.update(values)
        return dependencies

    def resolve(self, requirements, *, platform, features, lock):
        constraints = defaultdict(list)
        for package, constraint in requirements.items():
            constraints[package].append(constraint)
        selected = {}
        edges = defaultdict(set)
        pending = list(sorted(requirements))
        while pending:
            package = pending.pop(0)
            if package in selected:
                version = selected[package][0]
                parsed = [
                    parse_constraint(value) for value in constraints[package]]
                if not any(matches(version, item) for item in parsed):
                    raise UnsatisfiedConstraints(package, constraints[package])
                continue
            versions = self._registry.package(package)
            if versions is None:
                raise UnknownPackage(package)
            parsed = [parse_constraint(value) for value in constraints[package]]
            candidates = [
                (Version(version), metadata)
                for version, metadata in versions.items()
                if not metadata["yanked"]
                and any(matches(Version(version), item) for item in parsed)
            ]
            if lock and package in lock:
                candidates = [
                    item for item in candidates
                    if str(item[0]) == lock[package]["version"]]
            candidates.sort(key=lambda item: item[0], reverse=True)
            if not candidates:
                if lock and package in lock:
                    raise LockError(package, "pin is unavailable or incompatible")
                raise UnsatisfiedConstraints(package, constraints[package])
            version, metadata = candidates[0]
            selected[package] = (version, metadata)
            dependencies = self._active_dependencies(
                metadata, platform, features.get(package, []))
            for dependency, constraint in dependencies.items():
                edges[package].add(dependency)
                constraints[dependency].append(constraint)
                pending.append(dependency)
            for other, constraint in metadata["conflicts"].items():
                if other in selected and matches(
                        selected[other][0], parse_constraint(constraint)):
                    raise PackageConflict(package, other)

        order = installation_order(selected, edges)
        packages = [
            SelectedPackage(name, str(selected[name][0]), selected[name][1]["digest"])
            for name in order
        ]
        return serialize_result(platform, packages)
