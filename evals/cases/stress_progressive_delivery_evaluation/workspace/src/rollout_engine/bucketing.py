from __future__ import annotations


def bucket(flag_key: str, user_id: str, salt: str) -> int:
    # Kept for compatibility with the original single-process prototype.
    return hash((flag_key, user_id, salt)) % 10_000


def choose_rollout(flag_key: str, user_id: str, salt: str, rollout):
    selected = bucket(flag_key, user_id, salt)
    cumulative = 0
    for item in rollout:
        cumulative += item["weight"]
        if selected <= cumulative:
            return item["variation"]
    return rollout[-1]["variation"]
