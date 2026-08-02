from __future__ import annotations


def serialize_evaluation(evaluation) -> dict:
    return {
        "flag_key": evaluation.flag_key,
        "variation": evaluation.variation,
        "value": evaluation.value,
        "reason": evaluation.reason,
    }


def serialize_exposure(exposure) -> dict:
    return {
        "request_id": exposure.request_id,
        "flag_key": exposure.flag_key,
        "variation": exposure.variation,
        "reason": exposure.reason,
    }
