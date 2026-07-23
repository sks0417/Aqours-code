from __future__ import annotations

from ..errors import CheckpointCorrupt
from ..serialization import serialize_recovery
from .checksum import checkpoint_digest


class RecoveryService:
    def __init__(self, initial_balances, events, balances, sequences, checkpoints):
        self._initial_balances = dict(initial_balances)
        self._events = events
        self._balances = balances
        self._sequences = sequences
        self._checkpoints = checkpoints

    def create_checkpoint(self) -> dict:
        events = self._events.all()
        document = {
            "balances": self._balances.snapshot(),
            "sequences": self._sequences.snapshot(),
            "event_ids": [event.event_id for event in events],
            "event_count": len(events),
        }
        document["digest"] = checkpoint_digest(document)
        self._checkpoints.save(document)
        return self._checkpoints.latest()

    def recover(self) -> dict:
        checkpoint = self._checkpoints.latest()
        events = self._events.all()
        if checkpoint is None:
            self._balances.restore(self._initial_balances)
            self._sequences.restore({})
            self._balances.apply_many(events)
            self._sequences.advance(events)
            return serialize_recovery(
                self._balances.snapshot(), self._sequences.snapshot(), len(events))

        self._balances.restore(checkpoint.get("balances", {}))
        self._sequences.restore(checkpoint.get("sequences", {}))
        try:
            if checkpoint_digest(checkpoint) != checkpoint.get("digest"):
                raise CheckpointCorrupt("checkpoint digest mismatch")
            event_count = checkpoint["event_count"]
            if event_count < 0 or event_count > len(events):
                raise CheckpointCorrupt("invalid event count")
            if checkpoint["event_ids"] != [event.event_id for event in events[:event_count]]:
                raise CheckpointCorrupt("event prefix mismatch")
        except (KeyError, TypeError, ValueError) as exc:
            raise CheckpointCorrupt(f"malformed checkpoint: {exc}") from exc
        return serialize_recovery(
            self._balances.snapshot(), self._sequences.snapshot(), len(events))
