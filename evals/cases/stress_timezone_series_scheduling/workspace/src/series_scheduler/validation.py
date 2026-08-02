from __future__ import annotations

import re
from collections.abc import Mapping
from datetime import datetime, timezone

from .errors import UnknownResource, ValidationError
from .models import Series
from .recurrence import WEEKDAYS
from .timezones import canonical_utc, load_timezone


_KEY = re.compile(r"^[A-Za-z0-9._:-]+$")
_LOCAL = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}$")


def _text(value, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise ValidationError(f"{field} must be non-empty string", field=field)
    return value.strip()


def normalize_key(value, field: str) -> str:
    result = _text(value, field)
    if len(result) > 128 or not _KEY.fullmatch(result):
        raise ValidationError("invalid operation key", field=field)
    return result


def parse_local(value, field: str) -> datetime:
    text = _text(value, field)
    if not _LOCAL.fullmatch(text):
        raise ValidationError("local timestamp must use minute precision", field=field)
    try:
        return datetime.fromisoformat(text)
    except ValueError as exc:
        raise ValidationError("invalid local timestamp", field=field) from exc


def parse_instant(value, field: str):
    text = _text(value, field)
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ValidationError("invalid instant", field=field) from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        raise ValidationError("instant must include offset", field=field)
    if parsed.second or parsed.microsecond:
        raise ValidationError("instant must use minute precision", field=field)
    return parsed.astimezone(timezone.utc)


def normalize_resources(value) -> dict:
    if not isinstance(value, Mapping) or not value:
        raise ValidationError("resources must be non-empty mapping", field="resources")
    result = {}
    for raw_id, raw in value.items():
        resource_id = _text(raw_id, "resource_id")
        if not isinstance(raw, Mapping):
            raise ValidationError("resource must be mapping", field="resources")
        capacity = raw.get("capacity")
        if isinstance(capacity, bool) or not isinstance(capacity, int) or capacity <= 0:
            raise ValidationError("capacity must be positive integer", field="capacity")
        timezone_name = _text(raw.get("timezone"), "timezone")
        result[resource_id] = {
            "timezone_name": timezone_name,
            "timezone": load_timezone(timezone_name),
            "capacity": capacity,
        }
    return dict(sorted(result.items()))


def normalize_series(value, resources) -> Series:
    if not isinstance(value, Mapping):
        raise ValidationError("series must be a mapping", field="payload")
    series_id = _text(value.get("series_id"), "series_id")
    resource_id = _text(value.get("resource_id"), "resource_id")
    if resources.get(resource_id) is None:
        raise UnknownResource(resource_id)
    duration = value.get("duration_minutes")
    if isinstance(duration, bool) or not isinstance(duration, int) or duration <= 0:
        raise ValidationError("duration must be positive integer", field="duration_minutes")
    recurrence = value.get("recurrence")
    if not isinstance(recurrence, Mapping):
        raise ValidationError("recurrence must be a mapping", field="recurrence")
    frequency = recurrence.get("frequency")
    if frequency not in {"DAILY", "WEEKLY"}:
        raise ValidationError("invalid frequency", field="frequency")
    interval, count = recurrence.get("interval"), recurrence.get("count")
    for field, item in (("interval", interval), ("count", count)):
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            raise ValidationError(f"{field} must be positive integer", field=field)
    if count > 100:
        raise ValidationError("count must be at most 100", field="count")
    weekdays = recurrence.get("weekdays", [])
    if frequency == "WEEKLY":
        if (not isinstance(weekdays, list) or not weekdays
                or any(item not in WEEKDAYS for item in weekdays)
                or len(set(weekdays)) != len(weekdays)):
            raise ValidationError("invalid weekdays", field="weekdays")
        weekdays = sorted(weekdays, key=WEEKDAYS.get)
    else:
        weekdays = []
    exdates_raw = value.get("exdates", [])
    if not isinstance(exdates_raw, list):
        raise ValidationError("exdates must be a list", field="exdates")
    exdates = tuple(parse_local(item, "exdates") for item in exdates_raw)
    if len(set(exdates)) != len(exdates):
        raise ValidationError("exdates must be unique", field="exdates")
    return Series(
        series_id=series_id, resource_id=resource_id,
        local_start=parse_local(value.get("start"), "start"),
        duration_minutes=duration,
        recurrence={
            "frequency": frequency, "interval": interval,
            "count": count, "weekdays": weekdays,
        },
        exdates=exdates,
    )


def normalize_window(start, end):
    left, right = parse_instant(start, "window_start"), parse_instant(end, "window_end")
    if left >= right:
        raise ValidationError("window must be non-empty", field="window")
    return canonical_utc(left), canonical_utc(right)


def normalize_seats(value) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value <= 0:
        raise ValidationError("seats must be positive integer", field="seats")
    return value
