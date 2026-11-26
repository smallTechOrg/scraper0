"""Turn user-supplied material into events: poster images, PDFs, pasted text, ICS, CSV/JSON, URLs.

Nothing is persisted here. Results come back with per-event `warnings` (no_date, no_address,
past, low_confidence) so a human can review before saving.
"""
from __future__ import annotations

import csv
import io
import json
import re
from dataclasses import dataclass
from datetime import datetime
from typing import Callable

from scraper0.events.classify import address_is_online, classify_type, looks_online
from scraper0.core.dates import IST, is_past, parse_datetime
from scraper0.events.extractors import extract_ics
from scraper0.events.schema import EVENT_TYPES, Event, clean, score
from scraper0.core.urls import strip_utm_params

MAX_IMAGE_BYTES = 8_000_000
MAX_TEXT_CHARS = 12_000
MAX_ROWS = 2_000
VisionFn = Callable[[str, bytes, str], str]   # (prompt, image_bytes, mime) -> raw model text
TextFn = Callable[[str], str]

_SIGNATURES = [(b"\x89PNG\r\n\x1a\n", "image/png"), (b"\xff\xd8\xff", "image/jpeg"),
               (b"GIF8", "image/gif"), (b"%PDF-", "application/pdf")]


def sniff(data: bytes) -> str | None:
    """Detect type from magic bytes — never trust the client's filename/Content-Type."""
    for sig, mime in _SIGNATURES:
        if data.startswith(sig):
            return mime
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    head = data[:2000].decode("utf-8", "ignore")
    if "BEGIN:VCALENDAR" in head:
        return "text/calendar"
    return None


_EXTRACT = """You extract events from {what}. Today is {today} (India, IST). Respond ONLY with a JSON array
(empty array if there is no event). Each item:
{{"name": str, "start": "YYYY-MM-DDTHH:MM" (24h, local India time) or null, "end": same or null,
 "address": str|null, "organisation": str|null, "description": str|null (<=300 chars),
 "contact": str|null, "price": "Free"|str|null, "is_online": bool}}
Rules: copy text exactly as written, never invent details. If a date has no year, use the next upcoming
occurrence relative to today. Dates like 04/05/2026 are day/month. If only a date is given, set the time to
00:00. Multiple events (e.g. a schedule or list) become multiple items. Ignore sponsors/advertisements.
{body}"""


def _today() -> str:
    return datetime.now(IST).strftime("%A, %Y-%m-%d")


def _json_array(text: str) -> list:
    t = text.strip().strip("`")
    t = re.sub(r"^json\s*", "", t)
    m = re.search(r"\[.*\]", t, re.S) or re.search(r"\{.*\}", t, re.S)
    data = json.loads(m.group(0) if m else t)
    return data if isinstance(data, list) else [data]


def _from_llm_items(items: list, portal: str, how: str, source: str) -> list[Event]:
    out = []
    for d in items:
        if not isinstance(d, dict) or not clean(d.get("name")):
            continue
        desc = clean(d.get("description"))
        if d.get("contact"):
            desc = clean(f"{desc or ''} Contact: {d['contact']}")
        e = Event(
            name=clean(d["name"]), link=source, portal=portal, start_time=parse_datetime(d.get("start")),
            end_time=parse_datetime(d.get("end")), address=clean(d.get("address")),
            organisation=clean(d.get("organisation")), description=desc, price=clean(d.get("price")),
            is_online=bool(d.get("is_online")), source_url=source, extraction=how, sources=[source],
        )
        e.confidence = min(score(e), 0.8)   # model-read data is capped below structured data
        out.append(e)
    return out


# ------------------------------------------------------------------ images / pdf / text

def prepare_image(data: bytes) -> tuple[bytes, str]:
    """Normalise to a ≤1600px JPEG: fewer tokens, EXIF-rotation fixed, any format → one format."""
    from PIL import Image, ImageOps
    img = ImageOps.exif_transpose(Image.open(io.BytesIO(data)))
    if img.mode in ("RGBA", "LA", "P"):
        bg = Image.new("RGB", img.size, "white")
        bg.paste(img.convert("RGBA"), mask=img.convert("RGBA").split()[-1])
        img = bg
    img = img.convert("RGB")
    img.thumbnail((1600, 1600))
    buf = io.BytesIO()
    img.save(buf, "JPEG", quality=88)
    return buf.getvalue(), "image/jpeg"


def events_from_image(data: bytes, vision: VisionFn, portal: str, source: str) -> list[Event]:
    jpg, mime = prepare_image(data)
    prompt = _EXTRACT.format(what="this event poster / flyer / screenshot", today=_today(), body="")
    return _from_llm_items(_json_array(vision(prompt, jpg, mime)), portal, "image", source)


def events_from_text(text: str, llm: TextFn, portal: str, source: str, how: str = "text") -> list[Event]:
    body = "Text:\n" + text[:MAX_TEXT_CHARS]
    prompt = _EXTRACT.format(what="the text below", today=_today(), body=body)
    return _from_llm_items(_json_array(llm(prompt)), portal, how, source)


def pdf_text(data: bytes) -> str:
    from pypdf import PdfReader
    r = PdfReader(io.BytesIO(data))
    return "\n".join((p.extract_text() or "") for p in r.pages[:15])


# ------------------------------------------------------------------ CSV / JSON

_ALIASES = {
    "name": {"name", "title", "event", "event name", "event_name"},
    "start": {"start", "start_time", "start time", "start_date", "start date", "date", "datetime", "when", "starts"},
    "end": {"end", "end_time", "end time", "end_date", "end date", "ends"},
    "time": {"time"},
    "address": {"address", "location", "venue", "where", "place"},
    "link": {"link", "url", "website", "event_url", "event link"},
    "organisation": {"organisation", "organization", "organiser", "organizer", "org", "host", "ngo"},
    "type": {"type", "category", "event_type"},
    "image": {"image", "image_url", "poster", "banner"},
    "description": {"description", "details", "about", "summary"},
    "latitude": {"latitude", "lat"},
    "longitude": {"longitude", "lng", "lon", "long"},
}


def _canon(row: dict) -> dict:
    out: dict = {}
    for k, v in row.items():
        key = re.sub(r"\s+", " ", str(k or "").strip().lower())
        for canon, names in _ALIASES.items():
            if key in names and canon not in out and v not in (None, ""):
                out[canon] = v
    return out


def events_from_rows(rows: list[dict], portal: str, source: str, how: str) -> list[Event]:
    out = []
    for i, raw in enumerate(rows[:MAX_ROWS], 1):
        r = _canon(raw)
        name = clean(r.get("name"))
        if not name:
            continue
        start_txt = r.get("start")
        if start_txt and r.get("time") and not re.search(r"\d:\d", str(start_txt)):
            start_txt = f"{start_txt} {r['time']}"
        link = strip_utm_params(str(r["link"]).strip()) if r.get("link") else f"{source}#row{i}"
        etype = str(r.get("type") or "").strip().upper().replace(" ", "_")
        try:
            lat, lng = float(r["latitude"]), float(r["longitude"])
        except (KeyError, TypeError, ValueError):
            lat = lng = None
        e = Event(
            name=name, link=link, portal=portal, start_time=parse_datetime(start_txt),
            end_time=parse_datetime(r.get("end")), address=clean(r.get("address")),
            organisation=clean(r.get("organisation")), image=clean(r.get("image")),
            description=clean(r.get("description")), latitude=lat, longitude=lng,
            type=etype if etype in EVENT_TYPES else "OTHER", source_url=source, extraction=how, sources=[link],
        )
        e.confidence = score(e)
        if start_txt and not e.start_time:
            e.warnings.append(f"unparsable_date:{str(start_txt)[:40]}")
        out.append(e)
    return out


def events_from_csv(text: str, portal: str, source: str) -> list[Event]:
    text = text.lstrip("\ufeff")
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t|")
    except csv.Error:
        dialect = csv.excel
    return events_from_rows(list(csv.DictReader(io.StringIO(text), dialect=dialect)), portal, source, "csv")


def events_from_json(text: str, portal: str, source: str) -> list[Event]:
    data = json.loads(text)
    if isinstance(data, dict):
        data = data.get("events") or data.get("data") or [data]
    return events_from_rows([d for d in data if isinstance(d, dict)], portal, source, "json")


# ------------------------------------------------------------------ orchestrator

@dataclass
class IngestInput:
    filename: str
    data: bytes


@dataclass
class IngestOptions:
    portal: str = "ADMIN"
    default_year_fix: bool = True
    now: datetime | None = None


def review_flags(events: list[Event], now: datetime | None = None) -> None:
    for e in events:
        w = e.warnings
        if not e.start_time and "no_date" not in w:
            w.append("no_date")
        if address_is_online(e.address):
            e.is_online = True
        if not e.address and not (e.is_online or looks_online(e.name, e.description)) and "no_address" not in w:
            w.append("no_address")
        if is_past(e.end_time or e.start_time, now) and "past" not in w:
            w.append("past")
        if e.confidence < 0.5 and "low_confidence" not in w:
            w.append("low_confidence")
        if e.type == "OTHER":
            e.type = classify_type(e.name, e.description)


def ingest(inputs: list[IngestInput], text: str | None, llm: TextFn, vision: VisionFn,
           opts: IngestOptions | None = None) -> dict:
    """Process everything the user gave us. Per-input failures are reported, never fatal."""
    o = opts or IngestOptions()
    events: list[Event] = []
    report: list[dict] = []

    def run(label: str, fn):
        try:
            got = fn()
            events.extend(got)
            report.append({"input": label, "events": len(got), "error": None})
        except Exception as ex:
            report.append({"input": label, "events": 0, "error": f"{type(ex).__name__}: {ex}"[:300]})

    for n, inp in enumerate(inputs):
        src = f"upload:{inp.filename}"
        mime = sniff(inp.data)
        name = inp.filename.lower()
        if mime and mime.startswith("image/"):
            if len(inp.data) > MAX_IMAGE_BYTES:
                report.append({"input": inp.filename, "events": 0, "error": "image too large (max 8MB)"})
                continue
            run(inp.filename, lambda d=inp.data, s=src: events_from_image(d, vision, o.portal, s))
        elif mime == "application/pdf":
            def pdf(d=inp.data, s=src):
                t = pdf_text(d)
                if len(t.strip()) < 40:
                    raise ValueError("PDF has no extractable text (scanned?) — upload it as an image instead")
                return events_from_text(t, llm, o.portal, s, "pdf")
            run(inp.filename, pdf)
        elif mime == "text/calendar":
            run(inp.filename, lambda d=inp.data, s=src: extract_ics(d.decode("utf-8", "ignore"), s, o.portal))
        elif name.endswith((".csv", ".tsv")):
            run(inp.filename, lambda d=inp.data, s=src: events_from_csv(d.decode("utf-8-sig", "ignore"), o.portal, s))
        elif name.endswith(".json"):
            run(inp.filename, lambda d=inp.data, s=src: events_from_json(d.decode("utf-8-sig", "ignore"), o.portal, s))
        elif name.endswith(".txt") or name.endswith(".md"):
            run(inp.filename, lambda d=inp.data, s=src: events_from_text(d.decode("utf-8", "ignore"), llm, o.portal, s, "text"))
        else:
            report.append({"input": inp.filename, "events": 0, "error": "unsupported file type"})
    if text and text.strip():
        run("pasted text", lambda: events_from_text(text, llm, o.portal, "upload:text", "text"))

    for e in events:
        e.link = strip_utm_params(e.link)
    review_flags(events, o.now)
    return {"events": events, "report": report}
