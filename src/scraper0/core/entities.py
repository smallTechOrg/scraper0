"""Entity specs: *what* to scrape, as data. Event is one built-in spec; any other entity
(jobs, NGOs, products, volunteer roles…) is just another EntitySpec — no new code needed.
"""
from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any
from urllib.parse import urljoin

FIELD_TYPES = {"text", "datetime", "url", "image", "number", "bool", "enum", "address"}


@dataclass
class FieldSpec:
    name: str
    type: str = "text"
    required: bool = False
    description: str = ""
    choices: list[str] = field(default_factory=list)   # for enum
    weight: float = 1.0                                # contribution to the confidence score

    def __post_init__(self):
        if self.type not in FIELD_TYPES:
            raise ValueError(f"field {self.name}: unknown type {self.type!r} (use one of {sorted(FIELD_TYPES)})")
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", self.name):
            raise ValueError(f"field name {self.name!r} must be snake_case")


@dataclass
class EntitySpec:
    name: str
    description: str
    fields: list[FieldSpec]
    version: int = 1
    tenant_id: str | None = None      # None = built-in (learned layouts shared by all tenants)
    # optional enrichment fields: filled when a structured source (JSON API) has them, never asked of the LLM
    extra_fields: list[FieldSpec] = field(default_factory=list)
    # other words sites use for this entity's listing ("activities", "opportunities"): used while learning to
    # find the listing page from a home page, and to recognise a URL that already is one
    synonyms: list[str] = field(default_factory=list)

    def __post_init__(self):
        self.fields = [f if isinstance(f, FieldSpec) else FieldSpec(**f) for f in self.fields]
        self.extra_fields = [f if isinstance(f, FieldSpec) else FieldSpec(**f) for f in self.extra_fields]
        if not re.fullmatch(r"[a-z][a-z0-9_]{0,40}", self.name):
            raise ValueError("entity name must be snake_case")
        if not any(f.required for f in self.fields):
            raise ValueError("an entity needs at least one required field")
        names = [f.name for f in self.fields]
        if len(names) != len(set(names)):
            raise ValueError("duplicate field names")

    @property
    def key(self) -> str:
        """Storage key for recipes/hints/cache: built-ins are shared, custom entities are per tenant."""
        return self.name if self.tenant_id is None else f"{self.tenant_id}:{self.name}"

    @property
    def required(self) -> list[str]:
        return [f.name for f in self.fields if f.required]

    def field(self, name: str) -> FieldSpec | None:
        return next((f for f in self.fields if f.name == name), None)

    @property
    def all_fields(self) -> list[FieldSpec]:
        return self.fields + self.extra_fields

    def any_field(self, name: str) -> FieldSpec | None:
        return next((f for f in self.all_fields if f.name == name), None)

    def to_dict(self) -> dict:
        d = asdict(self)
        d.pop("tenant_id", None)
        d.pop("extra_fields", None)
        d.pop("synonyms", None)
        d["builtin"] = self.tenant_id is None
        return d

    @classmethod
    def from_dict(cls, d: dict, tenant_id: str | None = None) -> "EntitySpec":
        return cls(name=d["name"], description=d.get("description", ""), fields=d["fields"],
                   version=int(d.get("version", 1)), tenant_id=tenant_id)

    # -- values -------------------------------------------------------------
    def coerce(self, fname: str, value: Any, page_url: str = "") -> Any:
        """Normalise one raw value to the field's type. Returns None when it can't."""
        from scraper0.core.dates import parse_datetime
        from scraper0.core.text import clean
        f = self.any_field(fname)
        if f is None or value is None:
            return None
        if isinstance(value, (list, tuple)):
            value = " ".join(str(v) for v in value if v is not None)
        if f.type in ("text", "address"):
            return clean(str(value))
        if f.type == "datetime":
            return parse_datetime(value)
        if f.type in ("url", "image"):
            v = str(value).strip()
            if not v or v.startswith(("javascript:", "data:", "mailto:")):
                return None
            return urljoin(page_url, v)
        if f.type == "number":
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                return float(value)
            m = re.search(r"-?\d+(?:[.,]\d+)?", str(value).replace(",", ""))
            return float(m.group(0)) if m else None
        if f.type == "bool":
            if isinstance(value, bool):
                return value
            return str(value).strip().lower() in {"true", "yes", "1", "y"}
        if f.type == "enum":
            v = str(value).strip().upper().replace(" ", "_")
            return v if v in f.choices else None
        return value

    def validate(self, item: dict) -> list[str]:
        """→ names of required fields that are missing/invalid."""
        return [n for n in self.required if item.get(n) in (None, "")]

    def score(self, item: dict) -> float:
        total = sum(f.weight for f in self.fields) or 1
        return round(sum(f.weight for f in self.fields if item.get(f.name) not in (None, "")) / total, 3)


EVENT_SPEC = EntitySpec(
    name="event",
    description="a single upcoming real-world or online event (drive, cleanup, workshop, meetup, volunteering session)",
    fields=[
        FieldSpec("name", "text", True, "event title", weight=2),
        FieldSpec("start_time", "datetime", True, "when it starts (date and time)", weight=2.5),
        FieldSpec("end_time", "datetime", False, "when it ends", weight=0.5),
        FieldSpec("address", "address", False, "venue / full address", weight=2),
        FieldSpec("organisation", "text", False, "organiser / host", weight=0.5),
        FieldSpec("image", "image", False, "banner/poster image url", weight=1),
        FieldSpec("description", "text", False, "short summary", weight=1),
        FieldSpec("price", "text", False, "ticket price or Free", weight=0.25),
    ],
    extra_fields=[
        FieldSpec("latitude", "number"), FieldSpec("longitude", "number"), FieldSpec("is_online", "bool"),
    ],
    synonyms=["activity", "opportunity", "program", "programme", "workshop", "drive", "volunteer", "volunteering",
              "meetup", "calendar", "happening", "whats on"],
)

BUILTIN_SPECS = {EVENT_SPEC.name: EVENT_SPEC}
