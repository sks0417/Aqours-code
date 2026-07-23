from __future__ import annotations

from dataclasses import dataclass

from .api import LedgerAPI
from .recovery import RecoveryService
from .repositories import (
    BalanceProjection, CheckpointRepository, EventStore,
    IdempotencyRepository, ReceiptIdSequence, ReceiptRepository,
    SequenceRepository,
)
from .service import LedgerService
from .validation import normalize_accounts


@dataclass
class LedgerApplication:
    api: LedgerAPI
    event_store: EventStore
    balances: BalanceProjection
    sequences: SequenceRepository
    idempotency: IdempotencyRepository
    receipts: ReceiptRepository
    receipt_ids: ReceiptIdSequence
    checkpoints: CheckpointRepository


def build_application(initial_accounts) -> LedgerApplication:
    accounts = normalize_accounts(initial_accounts)
    event_store = EventStore()
    balances = BalanceProjection(accounts)
    sequences = SequenceRepository()
    idempotency = IdempotencyRepository()
    receipts = ReceiptRepository()
    receipt_ids = ReceiptIdSequence()
    checkpoints = CheckpointRepository()
    service = LedgerService(
        event_store, balances, sequences, idempotency, receipts, receipt_ids)
    recovery = RecoveryService(
        {key: value["balance"] for key, value in accounts.items()},
        event_store, balances, sequences, checkpoints)
    api = LedgerAPI(service, recovery)
    return LedgerApplication(
        api, event_store, balances, sequences, idempotency,
        receipts, receipt_ids, checkpoints)


def build_api(initial_accounts) -> LedgerAPI:
    return build_application(initial_accounts).api
