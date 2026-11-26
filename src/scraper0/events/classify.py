"""Free, instant rule-based event-type classification (runs before/instead of the LLM)."""
from __future__ import annotations

import re

# Ordered: first matching rule wins, so specific types come before generic ones.
_RULES: list[tuple[str, re.Pattern]] = [
    ("BEACH_CLEANUP", re.compile(r"\b(beach|shore|coast|seashore)\b.*\b(clean|plog|drive|cleanup)|\bbeach clean", re.I)),
    ("TREKANDPLOG", re.compile(r"\b(trek|hike|plog|plogging|trail)\b", re.I)),
    ("FOREST_CLEANUP", re.compile(r"\b(forest|wildlife|national park|sanctuary)\b.*\b(clean|drive|restor)", re.I)),
    ("ROAD_CLEANUP", re.compile(r"\b(road|highway|street|footpath)\b.*\b(clean|sweep|drive)", re.I)),
    ("TREEPLANTATION", re.compile(r"\b(tree|sapling|plantation|plant(ing)? (a )?tree|afforest|miyawaki|reforest)", re.I)),
    ("CLEANLINESS_DRIVE", re.compile(r"\b(clean(liness|up|-up)?|swachh|waste|garbage|litter|lake clean|sanitation)\b", re.I)),
    ("WORKSHOP", re.compile(r"\b(workshop|training|masterclass|seminar|webinar|bootcamp|orientation)\b", re.I)),
    ("VOLUNTEERING", re.compile(r"\b(volunteer|volunteering|community service|donation drive|food distribution|blood donation|NGO)\b", re.I)),
]


def _match(blob: str) -> str | None:
    for etype, rx in _RULES:
        if rx.search(blob):
            return etype
    return None


def classify_type(name: str | None, description: str | None = None) -> str:
    """The title decides. Only when it says nothing do we look at the opening of the description —
    a word like "waste" deep in a long description says little about what the event is."""
    return _match(name or "") or _match((description or "")[:300]) or "OTHER"


_ONLINE = re.compile(r"\b(online|virtual|webinar|zoom|google meet|ms teams|remote)\b", re.I)


_ONLINE_ADDRESS = re.compile(r"\s*(online|virtual|zoom|google meet|ms teams|microsoft teams|webinar|remote)"
                             r"(\s*(event|meeting|session|only))?\s*", re.I)


def address_is_online(address: str | None) -> bool:
    """The venue field itself says the event is online ("Online", "Zoom", "Virtual event")."""
    return bool(address and _ONLINE_ADDRESS.fullmatch(address))


def looks_online(*texts: str | None) -> bool:
    return bool(_ONLINE.search(" ".join(t for t in texts if t)[:1500]))
