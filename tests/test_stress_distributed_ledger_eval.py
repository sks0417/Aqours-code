from __future__ import annotations

import json
from pathlib import Path

import pytest

from evals import run_eval


ROOT = Path(__file__).resolve().parents[1]
CASE = ROOT / "evals" / "cases" / "stress_distributed_ledger_recovery"


def copy_workspace(target: Path) -> Path:
    run_eval.copy_case_workspace(CASE, target)
    return target


def replace_exact(path: Path, old: str, new: str) -> None:
    source = path.read_text(encoding="utf-8")
    assert old in source, f"reference patch anchor missing in {path}"
    path.write_text(source.replace(old, new, 1), encoding="utf-8")


def apply_controlled_solution(workspace: Path) -> None:
    root = workspace / "src" / "ledger_service"
    replace_exact(
        root / "fingerprint.py",
        '''        "sequence": event.sequence,
        # The legacy producer predates signed deltas.
        "currency": event.currency,
''',
        '''        "sequence": event.sequence,
        "delta": event.delta,
        "currency": event.currency,
''')
    replace_exact(
        root / "repositories" / "balances.py",
        '''    def apply_many(self, events):
        for event in events:
            if event.account_id not in self._balances:
                raise UnknownAccount(event.account_id)
            expected = self._currencies[event.account_id]
            if event.currency != expected:
                raise CurrencyMismatch(event.account_id, expected, event.currency)
            before = self._balances[event.account_id]
            after = before + event.delta
            if after < 0:
                raise InsufficientFunds(event.account_id, event.delta, before)
            self._balances[event.account_id] = after
''',
        '''    def apply_many(self, events):
        staged = dict(self._balances)
        for event in events:
            if event.account_id not in staged:
                raise UnknownAccount(event.account_id)
            expected = self._currencies[event.account_id]
            if event.currency != expected:
                raise CurrencyMismatch(event.account_id, expected, event.currency)
            before = staged[event.account_id]
            after = before + event.delta
            if after < 0:
                raise InsufficientFunds(event.account_id, event.delta, before)
            staged[event.account_id] = after
        self._balances = staged
''')
    replace_exact(
        root / "repositories" / "sequences.py",
        '''        for partition, sequences in grouped.items():
            expected = self._values.get(partition, 0) + 1
            if sequences[0] != expected:
                raise SequenceConflict(partition, expected, sequences[0])
''',
        '''        for partition, sequences in grouped.items():
            expected = self._values.get(partition, 0) + 1
            for actual in sequences:
                if actual != expected:
                    raise SequenceConflict(partition, expected, actual)
                expected += 1
''')
    replace_exact(
        root / "recovery" / "checksum.py",
        '''        "balances": document["balances"],
        "event_ids": document["event_ids"],
''',
        '''        "balances": document["balances"],
        "sequences": document["sequences"],
        "event_ids": document["event_ids"],
''')
    replace_exact(
        root / "service.py",
        '''        event_snapshot = self._events.snapshot()
        sequence_snapshot = self._sequences.snapshot()
        receipt_snapshot = self._receipts.snapshot()
        batch_id = self._receipt_ids.allocate()
        self._idempotency.bind(key, fingerprint, None)
        try:
            self._sequences.validate(events)
            self._events.append_many(events)
            self._balances.apply_many(events)
            self._sequences.advance(events)
            receipt = Receipt(
                batch_id=batch_id,
                event_ids=tuple(event.event_id for event in events),
                balances=tuple(sorted(self._balances.snapshot().items())),
                sequences=tuple(sorted(self._sequences.snapshot().items())),
            )
            self._receipts.add(receipt)
            self._idempotency.bind(key, fingerprint, receipt)
            return serialize_receipt(receipt)
        except Exception:
            self._events.restore(event_snapshot)
            self._sequences.restore(sequence_snapshot)
            self._receipts.restore(receipt_snapshot)
            raise
''',
        '''        snapshots = {
            "events": self._events.snapshot(),
            "balances": self._balances.snapshot(),
            "sequences": self._sequences.snapshot(),
            "idempotency": self._idempotency.snapshot(),
            "receipts": self._receipts.snapshot(),
            "receipt_ids": self._receipt_ids.snapshot(),
        }
        try:
            self._sequences.validate(events)
            self._events.append_many(events)
            self._balances.apply_many(events)
            self._sequences.advance(events)
            receipt = Receipt(
                batch_id=self._receipt_ids.allocate(),
                event_ids=tuple(event.event_id for event in events),
                balances=tuple(sorted(self._balances.snapshot().items())),
                sequences=tuple(sorted(self._sequences.snapshot().items())),
            )
            self._receipts.add(receipt)
            self._idempotency.bind(key, fingerprint, receipt)
            return serialize_receipt(receipt)
        except Exception:
            self._events.restore(snapshots["events"])
            self._balances.restore(snapshots["balances"])
            self._sequences.restore(snapshots["sequences"])
            self._idempotency.restore(snapshots["idempotency"])
            self._receipts.restore(snapshots["receipts"])
            self._receipt_ids.restore(snapshots["receipt_ids"])
            raise
''')
    recovery = root / "recovery" / "replayer.py"
    source = recovery.read_text(encoding="utf-8")
    method_start = source.index("    def recover(self) -> dict:\n")
    recovery.write_text(source[:method_start] + '''    def recover(self) -> dict:
        checkpoint = self._checkpoints.latest()
        events = self._events.all()
        live_balances = self._balances.snapshot()
        live_sequences = self._sequences.snapshot()
        try:
            if checkpoint is None:
                base_balances = dict(self._initial_balances)
                base_sequences = {}
                tail = events
            else:
                if not isinstance(checkpoint, dict):
                    raise CheckpointCorrupt("malformed checkpoint")
                try:
                    if checkpoint_digest(checkpoint) != checkpoint.get("digest"):
                        raise CheckpointCorrupt("checkpoint digest mismatch")
                    event_count = checkpoint["event_count"]
                    if isinstance(event_count, bool) or not isinstance(event_count, int):
                        raise CheckpointCorrupt("invalid event count")
                    if event_count < 0 or event_count > len(events):
                        raise CheckpointCorrupt("invalid event count")
                    expected_ids = [event.event_id for event in events[:event_count]]
                    if checkpoint["event_ids"] != expected_ids:
                        raise CheckpointCorrupt("event prefix mismatch")
                    base_balances = checkpoint["balances"]
                    base_sequences = checkpoint["sequences"]
                    if (not isinstance(base_balances, dict)
                            or set(base_balances) != set(self._initial_balances)
                            or any(isinstance(value, bool) or not isinstance(value, int)
                                   or value < 0 for value in base_balances.values())):
                        raise CheckpointCorrupt("invalid balance snapshot")
                    if (not isinstance(base_sequences, dict)
                            or any(not isinstance(key, str) or not key
                                   or isinstance(value, bool) or not isinstance(value, int)
                                   or value <= 0
                                   for key, value in base_sequences.items())):
                        raise CheckpointCorrupt("invalid sequence snapshot")
                except (KeyError, TypeError, ValueError) as exc:
                    raise CheckpointCorrupt(f"malformed checkpoint: {exc}") from exc
                tail = events[event_count:]

            self._balances.restore(base_balances)
            self._sequences.restore(base_sequences)
            self._sequences.validate(tail)
            self._balances.apply_many(tail)
            self._sequences.advance(tail)
            return serialize_recovery(
                self._balances.snapshot(), self._sequences.snapshot(), len(events))
        except Exception:
            self._balances.restore(live_balances)
            self._sequences.restore(live_sequences)
            raise
''', encoding="utf-8")


def run_case_grader(workspace: Path, artifacts: Path) -> dict:
    artifacts.mkdir(parents=True, exist_ok=True)
    trace = artifacts / "trace.jsonl"
    trace.write_text("\n".join(json.dumps(event) for event in [
        {"type": "tool_use", "tool": "glob", "input": {"pattern": "src/**/*.py"}},
        {"type": "tool_use", "tool": "read_file", "input": {"path": "README.md"}},
        {"type": "tool_use", "tool": "bash", "input": {"command": "python -m pytest -q"}},
        {"type": "final_answer", "content": "implemented and verified"},
    ]) + "\n", encoding="utf-8")
    paths = {name: artifacts / name for name in ("final.md", "stdout.txt", "stderr.txt")}
    for path in paths.values():
        path.write_text("", encoding="utf-8")
    result, _ = run_eval.run_grader(
        CASE, workspace, trace, paths["final.md"],
        paths["stdout.txt"], paths["stderr.txt"])
    return result


@pytest.fixture(scope="module")
def baseline(tmp_path_factory):
    root = tmp_path_factory.mktemp("ledger-baseline")
    return run_case_grader(copy_workspace(root / "workspace"), root / "artifacts")


@pytest.fixture(scope="module")
def corrected(tmp_path_factory):
    root = tmp_path_factory.mktemp("ledger-correct")
    workspace = copy_workspace(root / "workspace")
    apply_controlled_solution(workspace)
    return run_case_grader(workspace, root / "artifacts")


def test_case_is_larger_and_has_isolated_outcome_groups():
    metadata = run_eval.load_metadata(CASE)
    files = [path for path in (CASE / "workspace").rglob("*") if path.is_file()]
    assert metadata["difficulty"] == 6
    assert metadata["max_model_calls"] == 50
    assert 25 <= len(files) <= 35
    assert set(metadata["forbidden_paths"]) == {"README.md", "pyproject.toml", "tests/**"}
    assert len(list((CASE / "grader_tests").glob("test_*.py"))) == 6


def test_faulty_workspace_gets_deterministic_partial_credit(baseline):
    assert baseline["passed"] is False
    assert baseline["score"] == 25
    assert baseline["breakdown"] == {
        "functional_correctness": 5,
        "code_quality": 20,
        "runtime_efficiency": 0,
        "token_cost": 0,
    }
    assert set(baseline["metrics"]["failed_outcome_groups"]) == {
        "atomic_ingestion", "exactly_once", "partition_ordering",
        "checkpoint_recovery", "regression",
    }


def test_controlled_solution_passes_all_hidden_groups(corrected):
    assert corrected["passed"] is True
    assert corrected["score"] == 70
    assert corrected["breakdown"] == {
        "functional_correctness": 50,
        "code_quality": 20,
        "runtime_efficiency": 0,
        "token_cost": 0,
    }
    assert corrected["metrics"]["failed_outcome_groups"] == []
