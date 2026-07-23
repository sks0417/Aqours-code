class LedgerServiceError(Exception):
    pass


class ValidationError(LedgerServiceError):
    def __init__(self, message: str, *, field: str):
        super().__init__(message)
        self.field = field


class UnknownAccount(LedgerServiceError):
    def __init__(self, account_id: str):
        super().__init__(f"unknown account: {account_id}")
        self.account_id = account_id


class CurrencyMismatch(LedgerServiceError):
    def __init__(self, account_id: str, expected: str, actual: str):
        super().__init__(f"currency mismatch for {account_id}: {expected} != {actual}")
        self.account_id, self.expected, self.actual = account_id, expected, actual


class InsufficientFunds(LedgerServiceError):
    def __init__(self, account_id: str, attempted: int, available: int):
        super().__init__(f"insufficient funds for {account_id}")
        self.account_id, self.attempted, self.available = account_id, attempted, available


class SequenceConflict(LedgerServiceError):
    def __init__(self, partition: str, expected: int, actual: int):
        super().__init__(f"sequence conflict for {partition}: expected {expected}, got {actual}")
        self.partition, self.expected, self.actual = partition, expected, actual


class DuplicateEvent(LedgerServiceError):
    def __init__(self, event_id: str):
        super().__init__(f"duplicate event: {event_id}")
        self.event_id = event_id


class IdempotencyConflict(LedgerServiceError):
    def __init__(self, key: str):
        super().__init__(f"idempotency conflict: {key}")
        self.key = key


class CheckpointCorrupt(LedgerServiceError):
    pass
