from pathlib import Path


def upload_file(base_dir, username, filename, content):
    user_dir = Path(base_dir) / username
    user_dir.mkdir(parents=True, exist_ok=True)
    target = user_dir / filename
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(content)
    return target
