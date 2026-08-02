import os
import sys
from pathlib import Path

import pytest

WORKSPACE = Path(os.environ["EVAL_GRADING_WORKSPACE"]).resolve()
sys.path.insert(0, str(WORKSPACE / "src"))


def rows(*identifiers):
    return [{"external_id": value, "sku": f"SKU-{value}", "quantity": 1}
            for value in identifiers]
