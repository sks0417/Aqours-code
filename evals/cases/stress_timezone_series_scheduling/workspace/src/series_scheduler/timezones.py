from __future__ import annotations

from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .errors import ValidationError


def load_timezone(name: str) -> ZoneInfo:
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError) as exc:
        raise ValidationError("unknown IANA timezone", field="timezone") from exc


def resolve_local(value: datetime, zone: ZoneInfo):
    # The prototype stored wall time as if it were already UTC.
    return value.replace(tzinfo=timezone.utc)


def canonical_utc(value: datetime) -> str:
    converted = value.astimezone(timezone.utc).replace(second=0, microsecond=0)
    return converted.strftime("%Y-%m-%dT%H:%MZ")
