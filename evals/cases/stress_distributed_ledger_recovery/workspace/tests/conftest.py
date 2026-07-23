from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))


def event(event_id, account_id, sequence, delta, *, partition="east",
          transaction_id="txn-1", currency="USD"):
    return {
        "event_id": event_id,
        "transaction_id": transaction_id,
        "account_id": account_id,
        "partition": partition,
        "sequence": sequence,
        "delta": delta,
        "currency": currency,
    }
