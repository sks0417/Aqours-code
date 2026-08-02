from __future__ import annotations

import pytest

from conftest import basic_registry, digest
from dependency_resolver import LockError, build_application


def test_valid_strict_lock_is_honored():
    app = build_application(basic_registry())
    lock = {
        "app": {"version": "1.0.0", "digest": digest("a")},
        "core": {"version": "1.2.0", "digest": digest("b")},
    }
    result = app.api.resolve(
        {"app": "1.0.0"}, platform="linux", lock=lock)
    assert result["lock"] == lock


def test_forged_digest_is_rejected():
    app = build_application(basic_registry())
    lock = {
        "app": {"version": "1.0.0", "digest": digest("f")},
        "core": {"version": "1.2.0", "digest": digest("b")},
    }
    with pytest.raises(LockError) as caught:
        app.api.resolve({"app": "1.0.0"}, platform="linux", lock=lock)
    assert caught.value.package == "app"
