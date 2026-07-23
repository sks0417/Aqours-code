from __future__ import annotations

import json

from codepilot_s20.model_broker import BrokerModelClient, PROTOCOL_VERSION
from codepilot_s20.model_budget import (
    can_spend_optional_calls,
    finalization_reserve_active,
    model_budget_snapshot,
)


class BudgetClient:
    def __init__(self, maximum: int, used: int, retries: int = 0):
        self.maximum = maximum
        self.used = used
        self.retries = retries

    def budget_snapshot(self):
        return {
            "max_calls": self.maximum,
            "call_count": self.used,
            "max_provider_retries": self.retries,
        }


def test_budget_reserve_is_dynamic_and_protects_optional_work():
    client = BudgetClient(40, 28, retries=1)

    snapshot = model_budget_snapshot(client)
    reviewer_allowed, _ = can_spend_optional_calls(client, 3)
    large_role_allowed, _ = can_spend_optional_calls(client, 5)

    assert snapshot["reserve_calls"] == 8
    assert snapshot["remaining_calls"] == 12
    assert reviewer_allowed is True
    assert large_role_allowed is False

    client.used = 32
    active, snapshot = finalization_reserve_active(client)
    assert active is True
    assert snapshot["remaining_calls"] == 8


def test_non_budgeted_clients_remain_unrestricted():
    allowed, snapshot = can_spend_optional_calls(object(), 100)

    assert allowed is True
    assert snapshot == {"available": False}


def test_broker_client_reads_only_its_live_budget_snapshot(tmp_path):
    nonce = "nonce123456789012"
    stats_dir = tmp_path / "stats"
    stats_dir.mkdir()
    (stats_dir / "broker_stats.json").write_text(json.dumps({
        "version": PROTOCOL_VERSION,
        "nonce": nonce,
        "call_count": 11,
        "request_count": 10,
        "max_calls": 40,
        "max_provider_retries": 1,
        "last_error": "must not be exposed",
    }), encoding="utf-8")
    client = BrokerModelClient(tmp_path, nonce)

    snapshot = client.budget_snapshot()

    assert snapshot["call_count"] == 11
    assert snapshot["max_calls"] == 40
    assert "last_error" not in snapshot

    payload = json.loads(
        (stats_dir / "broker_stats.json").read_text(encoding="utf-8"))
    payload["nonce"] = "another"
    (stats_dir / "broker_stats.json").write_text(
        json.dumps(payload), encoding="utf-8")
    assert client.budget_snapshot() == {}


def test_broker_client_uses_conservative_config_fallback_without_stats(tmp_path):
    client = BrokerModelClient(
        tmp_path, "nonce123456789012", max_calls=40,
        max_provider_retries=1,
    )

    snapshot = client.budget_snapshot()

    assert snapshot == {
        "source": "configured_fallback",
        "request_count": 0,
        "call_count": 0,
        "max_calls": 40,
        "max_provider_retries": 1,
    }
