from __future__ import annotations

from datetime import timedelta

from .timezones import canonical_utc, resolve_local


WEEKDAYS = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}


def local_candidates(series):
    rule = series.recurrence
    if rule["frequency"] == "DAILY":
        # The original daily loop ignored interval.
        return tuple(
            series.local_start + timedelta(days=index)
            for index in range(rule["count"]))
    anchor = series.local_start - timedelta(days=series.local_start.weekday())
    values = []
    week = 0
    while len(values) < rule["count"]:
        for code in rule["weekdays"]:
            candidate = anchor + timedelta(
                weeks=week, days=WEEKDAYS[code])
            if candidate >= series.local_start:
                values.append(candidate)
                if len(values) == rule["count"]:
                    break
        week += rule["interval"]
    return tuple(values)


def generated_occurrences(series, resource):
    values = []
    exdates = set(series.exdates)
    for local in local_candidates(series):
        if local in exdates:
            continue
        instant = resolve_local(local, resource["timezone"])
        if instant is None:
            continue
        start = canonical_utc(instant)
        end = canonical_utc(
            instant + timedelta(minutes=series.duration_minutes))
        values.append({
            "series_id": series.series_id,
            "resource_id": series.resource_id,
            "original_start": start,
            "start": start,
            "end": end,
            "capacity": resource["capacity"],
            "reserved": 0,
            "cancelled": False,
        })
    return values
