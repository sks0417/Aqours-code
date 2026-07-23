from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class LedgerEvent:
    event_id: str
    transaction_id: str
    account_id: str
    partition: str
    sequence: int
    delta: int
    currency: str


@dataclass(frozen=True)
class Receipt:
    batch_id: str
    event_ids: tuple[str, ...]
    balances: tuple[tuple[str, int], ...]
    sequences: tuple[tuple[str, int], ...]
