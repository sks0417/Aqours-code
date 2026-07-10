import os
import sys
from pathlib import Path

workspace = Path(os.environ["EVAL_GRADING_WORKSPACE"])
sys.path.insert(0, str(workspace / "src"))

from auth_service import authenticate, role_for


def test_all_empty_passwords_are_rejected():
    assert authenticate("alice", "") is False
    assert authenticate("bob", "") is False


def test_known_users_with_valid_passwords_still_authenticate():
    assert authenticate("alice", "wonderland") is True
    assert authenticate("bob", "builder") is True


def test_unknown_users_and_roles_still_fail_cleanly():
    assert authenticate("mallory", "builder") is False
    assert role_for("mallory") is None
    assert role_for("alice") == "admin"
    assert role_for("bob") == "user"
