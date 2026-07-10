import os
import tempfile
from pathlib import Path
import sys

workspace = Path(os.environ["EVAL_GRADING_WORKSPACE"])
sys.path.insert(0, str(workspace / "src"))

from uploader import upload_file


def test_absolute_filename_cannot_escape_user_directory():
    with tempfile.TemporaryDirectory() as tmp:
        outside = Path(tmp) / "outside.txt"
        try:
            upload_file(tmp, "alice", str(outside), b"bad")
        except ValueError:
            pass
        else:
            raise AssertionError("absolute filename should raise ValueError")
        assert not outside.exists()


def test_normal_nested_upload_stays_inside_user_directory():
    with tempfile.TemporaryDirectory() as tmp:
        path = upload_file(tmp, "alice", "photos/avatar.png", b"image")
        assert path == Path(tmp) / "alice" / "photos" / "avatar.png"
        assert path.read_bytes() == b"image"


def test_dotted_filename_and_multi_level_escape_behavior():
    with tempfile.TemporaryDirectory() as tmp:
        dotted = upload_file(tmp, "alice", "archive.v2.tar", b"ok")
        assert dotted == Path(tmp) / "alice" / "archive.v2.tar"
        try:
            upload_file(tmp, "alice", "../../secret.txt", b"bad")
        except ValueError:
            pass
        else:
            raise AssertionError("multi-level traversal should raise ValueError")
        assert not (Path(tmp) / "secret.txt").exists()
