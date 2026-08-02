import os
import sys
from pathlib import Path

WORKSPACE = Path(os.environ["EVAL_GRADING_WORKSPACE"]).resolve()
sys.path.insert(0, str(WORKSPACE / "src"))


def mixed_records():
    return [
        {"id": "legacy,1", "customer": "customer \"A\"", "amount_cents": 5,
         "currency": "usd"},
        {"invoice_id": "new-2", "customer_id": "customer-2", "amount_minor": -7,
         "currency": "eur", "future": True},
    ]
