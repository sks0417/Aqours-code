import os
import sys
from pathlib import Path

WORKSPACE = Path(os.environ["EVAL_GRADING_WORKSPACE"]).resolve()
sys.path.insert(0, str(WORKSPACE / "src"))


def records():
    return [
        {"doc_id": "b", "title": "beta", "body": "A long body about Python migration",
         "tags": ["guide", "python", "guide"]},
        {"version": 2, "id": "a", "fields": {"title": "Alpha", "body": "Python reference"},
         "labels": ["python"]},
        {"doc_id": "c", "title": "alpha", "body": "Unrelated", "tags": ["misc"]},
    ]
