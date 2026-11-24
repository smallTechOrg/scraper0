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


def classify_type(*texts: str | None) -> str:
    blob = " ".join(t for t in texts if t)[:4000]
    for etype, rx in _RULES:
        if rx.search(blob):
            return etype
    return "OTHER"


_ONLINE = re.compile(r"\b(online|virtual|webinar|zoom|google meet|ms teams|remote)\b", re.I)


def looks_online(*texts: str | None) -> bool:
    return bool(_ONLINE.search(" ".join(t for t in texts if t)[:1500]))
