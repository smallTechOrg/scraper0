"""Robust date/time parsing → ISO-8601 UTC strings."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone

from dateutil import parser as dparser

IST = timezone(timedelta(hours=5, minutes=30))
_TZINFOS = {"IST": IST, "UTC": timezone.utc, "GMT": timezone.utc, "Z": timezone.utc}
_FMT = "%Y-%m-%dT%H:%M:%S.000Z"


def to_iso(dt: datetime, default_tz=IST) -> str:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=default_tz)
    return dt.astimezone(timezone.utc).strftime(_FMT)


def parse_datetime(value, default_tz=IST) -> str | None:
    """Parse almost any date string. Naive times are assumed to be in `default_tz` (IST).
    Date-only values become midnight local time. Returns ISO-8601 UTC or None."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            ts = value / 1000 if value > 1e11 else value
            return datetime.fromtimestamp(ts, timezone.utc).strftime(_FMT)
        except (OverflowError, OSError, ValueError):
            return None
    s = str(value).strip()
    if not s:
        return None
    s = s.replace("|", " ").replace("–", "-").replace(" at ", " ")
    s = re.sub(r"\s+", " ", s)
    try:
        dt = dparser.parse(s, tzinfos=_TZINFOS, fuzzy=True, dayfirst=_looks_dayfirst(s))
    except (ValueError, OverflowError, TypeError):
        return None
    if not 2000 <= dt.year <= 2100:
        return None
    return to_iso(dt, default_tz)


def _looks_dayfirst(s: str) -> bool:
    # Indian sites: 04-04-26 / 04/04/2026 are dd-mm. ISO (yyyy-mm-dd) is unaffected by dayfirst.
    return bool(re.match(r"^\d{1,2}[-/.]\d{1,2}[-/.]\d{2,4}", s))


def parse_duration_hours(value: str | None) -> float | None:
    """'PT2H30M' (ISO-8601) or 'commit 2 hours' → hours."""
    if not value:
        return None
    m = re.fullmatch(r"P(?:T)?(?:(\d+)D)?(?:T?(\d+)H)?(?:(\d+)M)?", value.strip(), re.I)
    if m and any(m.groups()):
        d, h, mi = (int(x or 0) for x in m.groups())
        return d * 24 + h + mi / 60
    m = re.search(r"(\d+(?:\.\d+)?)\s*(?:hours?|hrs?)", value, re.I)
    return float(m.group(1)) if m else None


def add_hours(iso: str, hours: float) -> str:
    dt = datetime.strptime(iso, _FMT).replace(tzinfo=timezone.utc)
    return (dt + timedelta(hours=hours)).strftime(_FMT)


def is_past(iso: str | None, now: datetime | None = None, grace_hours: float = 0) -> bool:
    if not iso:
        return False
    now = now or datetime.now(timezone.utc)
    dt = datetime.strptime(iso, _FMT).replace(tzinfo=timezone.utc)
    return dt + timedelta(hours=grace_hours) < now
