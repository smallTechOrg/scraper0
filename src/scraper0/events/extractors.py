"""Structured-data extractors. Each takes raw content and returns a list[Event].

Order of trust (best first): JSON-LD schema.org/Event → microdata → ICS → RSS/Atom →
OpenGraph/meta fallback (only when the page looks like a single event page).
"""
from __future__ import annotations

import json
import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from scraper0.core.dates import add_hours, parse_datetime, parse_duration_hours
from scraper0.events.schema import Event, clean, score
from scraper0.core.urls import strip_utm_params

_EVENT_TYPE_RE = re.compile(r"Event$")  # Event, SocialEvent, EducationEvent, ...


# ------------------------------------------------------------------ helpers

def _first(v):
    if isinstance(v, list):
        return _first(v[0]) if v else None
    return v


def _text(v) -> str | None:
    v = _first(v)
    if isinstance(v, dict):
        v = v.get("name") or v.get("@value") or v.get("url")
    return clean(BeautifulSoup(str(v), "html.parser").get_text(" ")) if v is not None else None


def _image(v, base: str) -> str | None:
    v = _first(v)
    if isinstance(v, dict):
        v = v.get("url") or v.get("contentUrl")
    return urljoin(base, v) if isinstance(v, str) and v.strip() else None


def _address(loc) -> tuple[str | None, bool, float | None, float | None]:
    """→ (address, is_online, lat, lng) from a schema.org location (Place/PostalAddress/VirtualLocation)."""
    loc = _first(loc)
    if loc is None:
        return None, False, None, None
    if isinstance(loc, str):
        return clean(loc), False, None, None
    types = loc.get("@type") or ""
    types = types if isinstance(types, list) else [types]
    if "VirtualLocation" in types:
        return None, True, None, None
    lat = lng = None
    geo = _first(loc.get("geo"))
    if isinstance(geo, dict):
        try:
            lat, lng = float(geo.get("latitude")), float(geo.get("longitude"))
        except (TypeError, ValueError):
            pass
    addr = _first(loc.get("address"))
    parts: list[str] = []
    if loc.get("name"):
        parts.append(str(loc["name"]))
    if isinstance(addr, str):
        parts.append(addr)
    elif isinstance(addr, dict):
        for k in ("streetAddress", "addressLocality", "addressRegion", "postalCode", "addressCountry"):
            val = addr.get(k)
            val = val.get("name") if isinstance(val, dict) else val
            if val:
                parts.append(str(val))
    seen, uniq = set(), []
    for p in parts:  # drop repeated fragments ("Cubbon Park, Cubbon Park")
        if p.lower() not in seen:
            seen.add(p.lower())
            uniq.append(p)
    return clean(", ".join(uniq)), False, lat, lng


def _price(offers) -> str | None:
    o = _first(offers)
    if not isinstance(o, dict):
        return None
    p = o.get("price", o.get("lowPrice"))
    if p in (None, ""):
        return None
    try:
        if float(p) == 0:
            return "Free"
    except (TypeError, ValueError):
        pass
    return clean(f"{o.get('priceCurrency', '')} {p}")


def _finish(e: Event) -> Event:
    e.confidence = score(e)
    e.sources = [e.link]
    return e


# ------------------------------------------------------------------ JSON-LD

def _walk(node):
    """Yield every dict in a JSON-LD blob, descending into @graph/lists/itemListElement/etc."""
    if isinstance(node, list):
        for n in node:
            yield from _walk(n)
    elif isinstance(node, dict):
        yield node
        for k, v in node.items():
            if k != "@context" and isinstance(v, (dict, list)):
                yield from _walk(v)


def _is_event(d: dict) -> bool:
    t = d.get("@type")
    ts = t if isinstance(t, list) else [t]
    return any(isinstance(x, str) and _EVENT_TYPE_RE.search(x) for x in ts)


def extract_jsonld(html: str, page_url: str, portal: str = "WEB") -> list[Event]:
    soup = BeautifulSoup(html, "html.parser")
    events, seen = [], set()
    for tag in soup.find_all("script", type=re.compile(r"ld\+json", re.I)):
        raw = (tag.string or tag.get_text() or "").strip()
        if not raw:
            continue
        try:
            data = json.loads(raw)
        except json.JSONDecodeError:
            try:  # some sites emit trailing commas / control chars
                data = json.loads(re.sub(r",\s*([}\]])", r"\1", raw), strict=False)
            except json.JSONDecodeError:
                continue
        for d in _walk(data):
            if not _is_event(d) or not (d.get("name") and (d.get("startDate") or d.get("url"))):
                continue
            ev = _event_from_schema(d, page_url, portal, "jsonld")
            key = (ev.name, ev.start_time, ev.link)
            if ev and key not in seen:
                seen.add(key)
                events.append(ev)
    return events


def _event_from_schema(d: dict, page_url: str, portal: str, how: str) -> Event:
    link = urljoin(page_url, _text(d.get("url")) or page_url)
    start = parse_datetime(_first(d.get("startDate")))
    end = parse_datetime(_first(d.get("endDate")))
    if start and not end:
        h = parse_duration_hours(_first(d.get("duration")) if d.get("duration") else None)
        end = add_hours(start, h) if h else None
    address, online, lat, lng = _address(d.get("location"))
    mode = str(d.get("eventAttendanceMode") or "")
    online = online or mode.endswith("OnlineEventAttendanceMode")
    org = _first(d.get("organizer")) or _first(d.get("performer"))
    ev = Event(
        name=_text(d.get("name")) or "", link=strip_utm_params(link), portal=portal,
        start_time=start, end_time=end, address=address, is_online=online,
        organisation=_text(org), image=_image(d.get("image"), page_url),
        description=_text(d.get("description")), latitude=lat, longitude=lng,
        price=_price(d.get("offers")), source_url=page_url, extraction=how,
    )
    if str(d.get("eventStatus") or "").endswith("EventCancelled"):
        ev.extraction += "+cancelled"
    return _finish(ev)


# ------------------------------------------------------------------ microdata

def extract_microdata(html: str, page_url: str, portal: str = "WEB") -> list[Event]:
    soup = BeautifulSoup(html, "html.parser")
    out = []
    for scope in soup.find_all(attrs={"itemtype": re.compile(r"schema\.org/\w*Event$")}):
        def prop(name):
            el = scope.find(attrs={"itemprop": name})
            if not el:
                return None
            return (el.get("content") or el.get("datetime") or el.get("href") or el.get("src")
                    or el.get_text(" ", strip=True))
        name = clean(prop("name"))
        if not name:
            continue
        loc = scope.find(attrs={"itemprop": "location"})
        ev = Event(
            name=name, link=strip_utm_params(urljoin(page_url, prop("url") or page_url)),
            portal=portal, start_time=parse_datetime(prop("startDate")),
            end_time=parse_datetime(prop("endDate")),
            address=clean(loc.get_text(" ", strip=True)) if loc else None,
            image=urljoin(page_url, prop("image")) if prop("image") else None,
            description=clean(prop("description")), source_url=page_url, extraction="microdata",
        )
        out.append(_finish(ev))
    return out


# ------------------------------------------------------------------ ICS

def _unfold_ics(text: str) -> list[str]:
    return re.sub(r"\r?\n[ \t]", "", text).splitlines()


def _ics_unescape(v: str) -> str:
    return v.replace("\\n", "\n").replace("\\,", ",").replace("\\;", ";").replace("\\\\", "\\")


def extract_ics(text: str, page_url: str, portal: str = "WEB") -> list[Event]:
    out, cur = [], None
    for line in _unfold_ics(text):
        if line == "BEGIN:VEVENT":
            cur = {}
        elif line == "END:VEVENT" and cur is not None:
            name = clean(_ics_unescape(cur.get("SUMMARY", "")))
            if name and cur.get("DTSTART"):
                geo = cur.get("GEO", "")
                lat = lng = None
                if ";" in geo:
                    try:
                        lat, lng = (float(x) for x in geo.split(";"))
                    except ValueError:
                        pass
                tz = cur.get("DTSTART@TZID")
                start = _ics_dt(cur["DTSTART"], tz)
                end = _ics_dt(cur["DTEND"], cur.get("DTEND@TZID")) if cur.get("DTEND") else None
                link = cur.get("URL") or page_url
                if not cur.get("URL"):  # stable per-event fragment so events in one file stay distinct
                    import hashlib
                    ident = cur.get("UID") or hashlib.sha1(f"{name}|{cur['DTSTART']}".encode()).hexdigest()[:10]
                    link = f"{page_url}#{ident}"
                out.append(_finish(Event(
                    name=name, link=strip_utm_params(link), portal=portal, start_time=start,
                    end_time=end, address=clean(_ics_unescape(cur.get("LOCATION", ""))),
                    description=clean(_ics_unescape(cur.get("DESCRIPTION", ""))),
                    organisation=clean(cur.get("ORGANIZER@CN")), latitude=lat, longitude=lng,
                    source_url=page_url, extraction="ics",
                )))
            cur = None
        elif cur is not None and ":" in line:
            head, _, val = line.partition(":")
            name, *params = head.split(";")
            cur[name.upper()] = val
            for p in params:
                k, _, pv = p.partition("=")
                cur[f"{name.upper()}@{k.upper()}"] = pv.strip('"')
    return out


def _ics_dt(val: str, tzid: str | None) -> str | None:
    from zoneinfo import ZoneInfo
    tz = None
    if tzid:
        try:
            tz = ZoneInfo(tzid)
        except Exception:
            tz = None
    m = re.fullmatch(r"(\d{4})(\d{2})(\d{2})(?:T(\d{2})(\d{2})(\d{2})?)?(Z)?", val.strip())
    if not m:
        return parse_datetime(val)
    y, mo, d, hh, mm, ss, z = m.groups()
    iso = f"{y}-{mo}-{d}T{hh or '00'}:{mm or '00'}:{ss or '00'}" + ("Z" if z else "")
    from scraper0.core.dates import IST
    return parse_datetime(iso, default_tz=tz or IST)


# ------------------------------------------------------------------ RSS / Atom

def extract_feed(xml_text: str, page_url: str, portal: str = "WEB") -> list[Event]:
    """RSS/Atom. Only items carrying a start date (ev:startdate, xCal, or pubDate as a weak
    signal when the feed is clearly an events feed) become events."""
    try:
        root = ET.fromstring(xml_text.encode("utf-8") if isinstance(xml_text, str) else xml_text)
    except ET.ParseError:
        return []
    out = []
    for item in [e for e in root.iter() if e.tag.split("}")[-1] in ("item", "entry")]:
        f: dict[str, str] = {}
        for ch in item:
            tag = ch.tag.split("}")[-1].lower()
            f.setdefault(tag, (ch.text or ch.attrib.get("href") or "").strip())
        start = f.get("startdate") or f.get("dtstart") or f.get("start") or f.get("event_start")
        if not start:
            continue
        link = f.get("link") or f.get("guid") or page_url
        out.append(_finish(Event(
            name=clean(f.get("title")) or "", link=strip_utm_params(link), portal=portal,
            start_time=parse_datetime(start), end_time=parse_datetime(f.get("enddate") or f.get("dtend")),
            address=clean(f.get("location") or f.get("venue")),
            description=_text(f.get("description") or f.get("summary") or f.get("content")),
            source_url=page_url, extraction="feed",
        )))
    return [e for e in out if e.name]


# ------------------------------------------------------------------ OpenGraph fallback

def extract_opengraph(html: str, page_url: str, portal: str = "WEB") -> list[Event]:
    """Single-event-page fallback from og:/event: meta tags + <time datetime>."""
    soup = BeautifulSoup(html, "html.parser")

    def meta(*names):
        for n in names:
            t = soup.find("meta", attrs={"property": n}) or soup.find("meta", attrs={"name": n})
            if t and t.get("content"):
                return t["content"].strip()

    title = meta("og:title", "twitter:title") or (soup.title.get_text(strip=True) if soup.title else None)
    start = meta("event:start_time", "article:published_time_event", "startDate")
    if not start:
        times = [t.get("datetime") for t in soup.find_all("time", datetime=True)]
        start = next((t for t in times if parse_datetime(t)), None)
    if not (title and start):
        return []
    return [_finish(Event(
        name=clean(title.split("|")[0]) or "", link=strip_utm_params(meta("og:url") or page_url),
        portal=portal, start_time=parse_datetime(start),
        end_time=parse_datetime(meta("event:end_time")), address=clean(meta("event:location", "place:location")),
        image=meta("og:image"), description=clean(meta("og:description", "description")),
        organisation=clean(meta("og:site_name")), source_url=page_url, extraction="opengraph",
    ))]


# ------------------------------------------------------------------ heuristic (labelled text)

_LABELS = {
    "when": re.compile(r"^(?:date(?:\s*(?:&|and)\s*time)?|when|starts?|start date|date/time|schedule)\s*[:\-–]\s*(.+)$", re.I),
    "where": re.compile(r"^(?:location|venue|where|address|place|meeting point)\s*[:\-–]\s*(.+)$", re.I),
    "org": re.compile(r"^(?:organi[sz]ed by|organi[sz]er|hosted by|presented by)\s*[:\-–]\s*(.+)$", re.I),
}
_RANGE = re.compile(r"(.+?\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?)\s*(?:-|–|to)\s*(\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?)", re.I)
_REJECT_LOC = re.compile(r"^(online|virtual|tbd|tba|to be (shared|confirmed|announced)|n/?a)\b", re.I)


def _split_range(text: str) -> tuple[str, str | None]:
    """'March 14, 2026 10:00 am - 12:00 pm' → ('March 14, 2026 10:00 am', 'March 14, 2026 12:00 pm')."""
    m = _RANGE.fullmatch(text.strip())
    if not m:
        return text, None
    start, end_t = m.group(1), m.group(2)
    date_part = re.sub(r"\s*\d{1,2}(?::\d{2})?\s*[ap]\.?m\.?\s*$", "", start, flags=re.I)
    return start, f"{date_part} {end_t}"


def extract_heuristic(html: str, page_url: str, portal: str = "WEB") -> list[Event]:
    """Last no-LLM layer for single event pages with 'Date: … / Venue: …' style labels or <time>.
    Requires a title AND a parsable start date, so ordinary pages yield nothing."""
    soup = BeautifulSoup(html, "html.parser")
    h1 = soup.find("h1")
    og = soup.find("meta", property="og:title")
    name = clean(h1.get_text(" ") if h1 else (og.get("content") if og else None))
    if not name:
        return []
    for t in soup(["script", "style", "noscript"]):
        t.decompose()
    found: dict[str, str] = {}
    for line in soup.get_text("\n").splitlines():
        line = clean(line.replace("📍", "").replace("📅", "")) or ""
        for key, rx in _LABELS.items():
            m = rx.match(line)
            if m and key not in found:
                found[key] = m.group(1).strip()
    start_txt, end_txt = _split_range(found["when"]) if "when" in found else (None, None)
    start = parse_datetime(start_txt)
    if not start:
        tm = soup.find("time", datetime=True)
        start = parse_datetime(tm["datetime"]) if tm else None
    if not start:
        return []
    where = found.get("where")
    if where and _REJECT_LOC.match(where):
        where = None
    desc = soup.find("meta", attrs={"name": "description"})
    img = soup.find("meta", property="og:image")
    canon = soup.find("link", rel="canonical")
    return [_finish(Event(
        name=name, link=strip_utm_params(urljoin(page_url, canon["href"]) if canon and canon.get("href") else page_url),
        portal=portal, start_time=start, end_time=parse_datetime(end_txt), address=clean(where),
        organisation=clean(found.get("org")), image=urljoin(page_url, img["content"]) if img and img.get("content") else None,
        description=clean(desc.get("content")) if desc else None, source_url=page_url, extraction="heuristic",
    ))]


# ------------------------------------------------------------------ dispatcher

def extract_all(content: str, page_url: str, portal: str = "WEB", content_type: str = "",
                heuristic: bool = True) -> list[Event]:
    """Run the extractor cascade appropriate for `content`; first layer that yields events wins."""
    head = content[:500].lstrip()
    if "BEGIN:VCALENDAR" in content[:2000] or "calendar" in content_type:
        return extract_ics(content, page_url, portal)
    if head.startswith("<?xml") or head.startswith("<rss") or head.startswith("<feed") or "xml" in content_type:
        return extract_feed(content, page_url, portal)
    evs = extract_jsonld(content, page_url, portal) or extract_microdata(content, page_url, portal)
    evs = evs or extract_opengraph(content, page_url, portal)
    return evs or (extract_heuristic(content, page_url, portal) if heuristic else [])
