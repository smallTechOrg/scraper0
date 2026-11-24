"""Normalized event model shared by every extractor and portal."""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

EVENT_TYPES = [
    "CLEANLINESS_DRIVE", "BEACH_CLEANUP", "ROAD_CLEANUP", "FOREST_CLEANUP",
    "TREEPLANTATION", "TREKANDPLOG", "VOLUNTEERING", "WORKSHOP", "OTHER",
]


@dataclass
class Event:
    """One event. `to_dict()` is backward compatible with the existing portal contract
    (name, image, portal, start_time, end_time, address, link, organisation, type)
    and adds optional enrichment fields."""
    name: str
    link: str
    portal: str = "WEB"
    start_time: str | None = None      # ISO-8601 UTC, e.g. 2026-03-14T04:30:00.000Z
    end_time: str | None = None
    address: str | None = None
    organisation: str | None = None
    image: str | None = None
    type: str = "OTHER"
    description: str | None = None
    latitude: float | None = None
    longitude: float | None = None
    is_online: bool = False
    price: str | None = None           # "Free" or e.g. "INR 500"
    source_url: str | None = None      # page/feed the event was extracted from
    extraction: str = ""               # jsonld | microdata | ics | rss | opengraph | llm | ...
    sources: list[str] = field(default_factory=list)  # all links merged into this event
    confidence: float = 0.0            # 0..1 completeness/quality score
    warnings: list[str] = field(default_factory=list)  # e.g. no_date, no_address, past (for human review)

    def to_dict(self, extended: bool = True) -> dict[str, Any]:
        d = {
            "name": self.name, "image": self.image, "portal": self.portal,
            "start_time": self.start_time, "end_time": self.end_time,
            "address": self.address, "link": self.link,
            "organisation": self.organisation, "type": self.type,
        }
        if extended:
            d.update({
                "description": self.description, "latitude": self.latitude,
                "longitude": self.longitude, "is_online": self.is_online,
                "price": self.price, "extraction": self.extraction,
                "sources": self.sources or [self.link], "confidence": round(self.confidence, 2),
                "warnings": self.warnings,
            })
        return d


from scraper0.core.text import clean  # noqa: E402,F401  (re-exported)


def score(e: Event) -> float:
    """Completeness score in [0,1]; used to pick the best duplicate and to rank output."""
    weights = [
        (bool(e.name), 0.2), (bool(e.start_time), 0.25), (bool(e.address or e.is_online), 0.2),
        (bool(e.end_time), 0.05), (bool(e.image), 0.1), (bool(e.description), 0.1),
        (e.latitude is not None, 0.05), (bool(e.organisation), 0.05),
    ]
    return sum(w for ok, w in weights if ok)
