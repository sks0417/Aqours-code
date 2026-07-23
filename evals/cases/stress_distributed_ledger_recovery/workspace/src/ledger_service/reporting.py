"""Optional read-model formatting kept outside the ingestion boundary."""


def balance_rows(snapshot: dict[str, int]) -> list[dict]:
    return [{"account_id": key, "balance": value}
            for key, value in sorted(snapshot.items())]
