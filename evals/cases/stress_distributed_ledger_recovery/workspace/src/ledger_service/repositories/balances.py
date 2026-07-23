from __future__ import annotations

from ..errors import CurrencyMismatch, InsufficientFunds, UnknownAccount


class BalanceProjection:
    def __init__(self, accounts: dict[str, dict]):
        self._currencies = {key: value["currency"] for key, value in accounts.items()}
        self._balances = {key: value["balance"] for key, value in accounts.items()}

    def apply_many(self, events):
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

    def balance(self, account_id: str) -> dict:
        if account_id not in self._balances:
            raise UnknownAccount(account_id)
        return {
            "account_id": account_id,
            "currency": self._currencies[account_id],
            "balance": self._balances[account_id],
        }

    def snapshot(self):
        return dict(self._balances)

    def restore(self, snapshot):
        self._balances = dict(snapshot)

    @property
    def currencies(self):
        return dict(self._currencies)
