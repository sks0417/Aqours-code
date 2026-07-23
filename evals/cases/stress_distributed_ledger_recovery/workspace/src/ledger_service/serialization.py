from __future__ import annotations


def serialize_receipt(receipt) -> dict:
    return {
        "batch_id": receipt.batch_id,
        "event_ids": list(receipt.event_ids),
        "balances": dict(receipt.balances),
        "sequences": dict(receipt.sequences),
    }


def serialize_recovery(balances: dict, sequences: dict, event_count: int) -> dict:
    return {
        "balances": dict(sorted(balances.items())),
        "sequences": dict(sorted(sequences.items())),
        "event_count": event_count,
    }
