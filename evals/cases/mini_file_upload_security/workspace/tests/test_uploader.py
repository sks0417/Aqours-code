import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from uploader import upload_file


def test_normal_upload_writes_inside_user_directory():
    with tempfile.TemporaryDirectory() as tmp:
        path = upload_file(tmp, "alice", "avatar.png", b"image")
        assert path == Path(tmp) / "alice" / "avatar.png"
        assert path.read_bytes() == b"image"


def test_dotted_filename_is_allowed():
    with tempfile.TemporaryDirectory() as tmp:
        path = upload_file(tmp, "alice", "report.v1.txt", b"ok")
        assert path == Path(tmp) / "alice" / "report.v1.txt"
        assert path.read_bytes() == b"ok"


def test_path_traversal_is_rejected_and_does_not_write_outside_user_directory():
    with tempfile.TemporaryDirectory() as outer:
        base = Path(outer) / "uploads"
        base.mkdir()
        escape_target = Path(outer) / "secret.txt"
        try:
            upload_file(base, "alice", "../secret.txt", b"stolen")
        except ValueError:
            pass
        else:
            raise AssertionError("path traversal should raise ValueError")
        assert not escape_target.exists()
        assert not (base / "secret.txt").exists()


def test_nested_escape_is_rejected():
    with tempfile.TemporaryDirectory() as tmp:
        try:
            upload_file(tmp, "alice", "safe/../../../escape.txt", b"bad")
        except ValueError:
            pass
        else:
            raise AssertionError("nested traversal should raise ValueError")
