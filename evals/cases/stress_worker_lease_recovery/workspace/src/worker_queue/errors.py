from __future__ import annotations


class QueueError(Exception):
    """Base class for public queue errors."""


class ValidationError(QueueError):
    pass


class IdempotencyConflict(QueueError):
    pass


class JobNotFound(QueueError):
    pass


class StaleLease(QueueError):
    pass


class InvalidStateTransition(QueueError):
    pass


class OperationConflict(QueueError):
    pass
