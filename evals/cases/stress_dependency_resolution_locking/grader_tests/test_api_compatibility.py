from __future__ import annotations

import inspect


def test_exports_and_signatures_are_stable(make_application):
    import dependency_resolver

    expected = {
        "ResolverApplication", "build_api", "build_application",
        "DependencyResolverError", "ValidationError", "UnknownPackage",
        "UnsatisfiedConstraints", "DependencyCycle", "PackageConflict",
        "LockError",
    }
    assert expected <= set(dependency_resolver.__all__)
    app = make_application()
    assert str(inspect.signature(app.api.resolve)) == (
        "(requirements, *, platform, features=None, lock=None)")
    assert str(inspect.signature(app.api.replace_registry)) == "(registry)"
    assert str(inspect.signature(app.api.cache_size)) == "()"


def test_application_exposes_expected_diagnostics(make_application):
    app = make_application()
    registry, revision = app.registry.snapshot()
    assert set(registry) == {"app", "core"}
    assert revision == 1
    assert app.cache.snapshot() == {}
    assert app.api.cache_size() == 0
