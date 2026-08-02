from __future__ import annotations

import re
from collections.abc import Mapping

from .constraints import parse_constraint
from .errors import UnknownPackage, ValidationError
from .semver import Version


_DIGEST = re.compile(r"^[0-9a-f]{64}$")
_PLATFORMS = {"linux", "win32", "darwin"}


def _text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be non-empty string", field=field)
    return value.strip()


def _constraint(value, field: str) -> str:
    text = _text(value, field)
    try:
        parse_constraint(text)
    except (TypeError, ValueError) as exc:
        raise ValidationError("invalid constraint", field=field) from exc
    return text


def _dependencies(value, field: str) -> dict:
    if value is None:
        value = {}
    if not isinstance(value, Mapping):
        raise ValidationError("dependencies must be a mapping", field=field)
    return dict(sorted(
        (_text(name, field), _constraint(constraint, field))
        for name, constraint in value.items()))


def normalize_registry(value) -> dict:
    if not isinstance(value, Mapping) or not value:
        raise ValidationError("registry must be non-empty mapping", field="registry")
    result = {}
    for raw_package, versions in value.items():
        package = _text(raw_package, "package")
        if not isinstance(versions, Mapping) or not versions:
            raise ValidationError("versions must be non-empty mapping", field="versions")
        normalized_versions = {}
        for raw_version, raw_metadata in versions.items():
            version_text = _text(raw_version, "version")
            try:
                normalized_version = str(Version(version_text))
            except ValueError as exc:
                raise ValidationError("invalid version", field="version") from exc
            if normalized_version in normalized_versions:
                raise ValidationError("duplicate normalized version", field="version")
            if not isinstance(raw_metadata, Mapping):
                raise ValidationError("metadata must be a mapping", field="metadata")
            digest = raw_metadata.get("digest")
            if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
                raise ValidationError("invalid digest", field="digest")
            yanked = raw_metadata.get("yanked", False)
            if not isinstance(yanked, bool):
                raise ValidationError("yanked must be boolean", field="yanked")
            optional_raw = raw_metadata.get("optional_dependencies", {})
            if not isinstance(optional_raw, Mapping):
                raise ValidationError(
                    "optional dependencies must be mapping",
                    field="optional_dependencies")
            optional = {
                _text(feature, "feature"): _dependencies(dependencies, "optional_dependencies")
                for feature, dependencies in optional_raw.items()
            }
            platform_raw = raw_metadata.get("platform_dependencies", {})
            if not isinstance(platform_raw, Mapping):
                raise ValidationError(
                    "platform dependencies must be mapping",
                    field="platform_dependencies")
            platform = {}
            for platform_name, dependencies in platform_raw.items():
                if platform_name not in _PLATFORMS:
                    raise ValidationError("invalid platform", field="platform_dependencies")
                platform[platform_name] = _dependencies(
                    dependencies, "platform_dependencies")
            normalized_versions[normalized_version] = {
                "digest": digest, "yanked": yanked,
                "dependencies": _dependencies(
                    raw_metadata.get("dependencies", {}), "dependencies"),
                "optional_dependencies": dict(sorted(optional.items())),
                "platform_dependencies": dict(sorted(platform.items())),
                "conflicts": _dependencies(
                    raw_metadata.get("conflicts", {}), "conflicts"),
            }
        result[package] = normalized_versions
    known = set(result)
    for versions in result.values():
        for metadata in versions.values():
            groups = [
                metadata["dependencies"], metadata["conflicts"],
                *metadata["optional_dependencies"].values(),
                *metadata["platform_dependencies"].values(),
            ]
            for dependencies in groups:
                unknown = set(dependencies) - known
                if unknown:
                    raise ValidationError(
                        "dependency references unknown package", field="dependencies")
    return dict(sorted(result.items()))


def normalize_requirements(value, registry) -> dict:
    if not isinstance(value, Mapping) or not value:
        raise ValidationError(
            "requirements must be non-empty mapping", field="requirements")
    result = {}
    for raw_package, raw_constraint in value.items():
        package = _text(raw_package, "requirements")
        if registry.package(package) is None:
            raise UnknownPackage(package)
        result[package] = _constraint(raw_constraint, "requirements")
    return dict(sorted(result.items()))


def normalize_platform(value) -> str:
    if value not in _PLATFORMS:
        raise ValidationError("invalid platform", field="platform")
    return value


def normalize_features(value, registry) -> dict:
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise ValidationError("features must be a mapping", field="features")
    result = {}
    for raw_package, raw_features in value.items():
        package = _text(raw_package, "features")
        versions = registry.package(package)
        if versions is None:
            raise UnknownPackage(package)
        if not isinstance(raw_features, list):
            raise ValidationError("feature list must be a list", field="features")
        names = [_text(item, "features") for item in raw_features]
        if len(set(names)) != len(names):
            raise ValidationError("features must be unique", field="features")
        available = {
            feature for metadata in versions.values()
            for feature in metadata["optional_dependencies"]
        }
        if not set(names) <= available:
            raise ValidationError("unknown feature", field="features")
        result[package] = sorted(names)
    return dict(sorted(result.items()))


def normalize_lock(value) -> dict | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ValidationError("lock must be a mapping", field="lock")
    result = {}
    for raw_package, raw in value.items():
        package = _text(raw_package, "lock")
        if not isinstance(raw, Mapping):
            raise ValidationError("lock entry must be mapping", field="lock")
        try:
            version = str(Version(_text(raw.get("version"), "lock")))
        except ValueError as exc:
            raise ValidationError("invalid lock version", field="lock") from exc
        digest = raw.get("digest")
        if not isinstance(digest, str) or not _DIGEST.fullmatch(digest):
            raise ValidationError("invalid lock digest", field="lock")
        result[package] = {"version": version, "digest": digest}
    return dict(sorted(result.items()))
