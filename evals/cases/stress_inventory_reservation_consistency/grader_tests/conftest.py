from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest


WORKSPACE = Path(os.environ["EVAL_GRADING_WORKSPACE"]).resolve()
SRC = WORKSPACE / "src"
sys.path.insert(0, str(SRC))


@pytest.fixture
def make_application():
    from inventory_service import build_application

    def factory(stock=None):
        return build_application(stock or {"A": 10, "B": 8, "C": 6})

    return factory


def request(order_id: str, *lines: tuple[str, int]) -> dict[str, object]:
    return {
        "order_id": order_id,
        "items": [
            {"sku": sku, "quantity": quantity}
            for sku, quantity in lines
        ],
    }
