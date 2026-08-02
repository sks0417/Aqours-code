from __future__ import annotations

from dependency_resolver.constraints import matches, parse_constraint
from dependency_resolver.semver import Version


def allowed(version, constraint):
    return matches(Version(version), parse_constraint(constraint))


def test_semver_numeric_prerelease_and_build_precedence():
    assert Version("10.0.0") > Version("2.99.99")
    assert Version("1.0.0") > Version("1.0.0-rc.9")
    assert Version("1.0.0-beta.11") > Version("1.0.0-beta.2")
    assert Version("1.0.0-alpha") < Version("1.0.0-alpha.x")
    assert Version("1.2.3+one") == Version("1.2.3+two")


def test_comma_constraints_are_and_not_or():
    constraint = ">=1.2.0, <2.0.0"
    assert allowed("1.5.0", constraint)
    assert not allowed("1.0.0", constraint)
    assert not allowed("2.1.0", constraint)


def test_zero_major_caret_and_tilde_bounds():
    assert allowed("0.2.9", "^0.2.3")
    assert not allowed("0.3.0", "^0.2.3")
    assert allowed("0.0.3", "^0.0.3")
    assert not allowed("0.0.4", "^0.0.3")
    assert allowed("1.2.9", "~1.2.3")
    assert not allowed("1.3.0", "~1.2.3")


def test_prerelease_requires_explicit_active_constraint():
    assert not allowed("2.0.0-rc.1", ">=1.0.0")
    assert allowed("2.0.0-rc.2", ">=2.0.0-rc.1,<2.0.0")
