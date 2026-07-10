import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from auth_service import authenticate, role_for


def test_valid_password_authenticates_user():
    assert authenticate("alice", "wonderland") is True


def test_wrong_password_is_rejected():
    assert authenticate("alice", "wrong") is False


def test_empty_password_is_rejected_for_existing_user():
    assert authenticate("alice", "") is False


def test_unknown_user_is_rejected():
    assert authenticate("mallory", "wonderland") is False


def test_role_lookup_still_works():
    assert role_for("bob") == "user"
