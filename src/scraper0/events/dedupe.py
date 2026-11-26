"""Cross-source duplicate detection and merge.

Two events are duplicates when they share a canonical link, or when their dates fall on the
same local day AND names are fuzzy-equal AND (venues are fuzzy-equal OR one venue is missing).
The highest-confidence record wins; missing fields are back-filled from the losers.
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher

from scraper0.events.schema import Event, score

_STOP = re.compile(r"\b(the|a|an|of|at|in|on|by|and|for|2\d{3})\b")
_NON = re.compile(r"[^a-z0-9 ]+")


def norm(text: str | None) -> str:
    t = _NON.sub(" ", (text or "").lower())
    return " ".join(_STOP.sub(" ", t).split())


def _day(e: Event) -> str | None:
    # shift to IST so evening events don't straddle the UTC date boundary
    if not e.start_time:
        return None
    from datetime import datetime, timedelta
    dt = datetime.strptime(e.start_time, "%Y-%m-%dT%H:%M:%S.000Z") + timedelta(hours=5, minutes=30)
    return dt.strftime("%Y-%m-%d")


def _sim(a: str, b: str) -> float:
    if not a or not b:
        return 0.0
    ta, tb = set(a.split()), set(b.split())
    jac = len(ta & tb) / len(ta | tb)
    return max(jac, SequenceMatcher(None, a, b).ratio())


def fingerprint(e: Event) -> str:
    return f"{norm(e.name)}|{_day(e)}|{norm(e.address)[:30]}"


def is_duplicate(a: Event, b: Event) -> bool:
    if a.link and a.link == b.link and (a.start_time == b.start_time):
        return True
    da, db = _day(a), _day(b)
    if da != db:
        return False
    if _sim(norm(a.name), norm(b.name)) < 0.82:
        return False
    va, vb = norm(a.address), norm(b.address)
    return not va or not vb or _sim(va, vb) >= 0.5


def merge(a: Event, b: Event) -> Event:
    best, other = (a, b) if score(a) >= score(b) else (b, a)
    for f in ("start_time", "end_time", "address", "organisation", "image", "description",
              "latitude", "longitude", "price"):
        if getattr(best, f) in (None, "") and getattr(other, f) not in (None, ""):
            setattr(best, f, getattr(other, f))
    if best.type == "OTHER" and other.type != "OTHER":
        best.type = other.type
    best.sources = sorted(set((best.sources or [best.link]) + (other.sources or [other.link])))
    best.confidence = score(best)
    return best


def dedupe(events: list[Event]) -> list[Event]:
    """O(n·k) with bucketing by day so 10k events stay fast."""
    buckets: dict[str | None, list[int]] = {}
    out: list[Event] = []
    for e in events:
        key = _day(e)
        hit = next((i for i in buckets.get(key, []) if is_duplicate(out[i], e)), None)
        if hit is None:
            buckets.setdefault(key, []).append(len(out))
            out.append(e)
        else:
            out[hit] = merge(out[hit], e)
    return out
