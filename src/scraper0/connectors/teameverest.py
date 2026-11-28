"""TEAMEVEREST — in-person volunteering events from teameverest.ngo (port of legacy portals/events.py).

Flow: events list page → in-person detail URLs → (skip links the backend already has) → detail page
→ optional per-page LLM filter/classifier → structured event.
"""
from __future__ import annotations

import base64
import re
from datetime import datetime, timedelta, timezone

import structlog
from bs4 import BeautifulSoup

from scraper0.connectors import _events_common as common
from scraper0.connectors.base import Connector, ConnectorContext, ConnectorError, register

log = structlog.get_logger(__name__)

PORTAL = "TEAMEVEREST"
BASE_URL = "https://www.teameverest.ngo"
EVENTS_URL = f"{BASE_URL}/events"
TIMEOUT = 15

_IST = timezone(timedelta(hours=5, minutes=30))

# data-inline-binding keys used by the site builder
BINDING_NAME = "dynamic_page_collection.Header Text"
BINDING_DATE = "dynamic_page_collection.Orientation Date Formula"
BINDING_LOCATION = "dynamic_page_collection.IPE Location (Formula)"
BINDING_TIME_DESC = "dynamic_page_collection.Time commitment Description​"


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def get_inperson_event_urls(html: str) -> list[str]:
    """All unique in-person event URLs on the events page, in page order."""
    soup = BeautifulSoup(html, "html.parser")
    seen: set[str] = set()
    urls: list[str] = []
    for a in soup.find_all("a", href=True):
        href = a["href"]
        if "/in-person-volunteering/" not in href:
            continue
        url = href if href.startswith("http") else f"{BASE_URL}{href}"
        if url not in seen:
            seen.add(url)
            urls.append(url)
    return urls


def _decode(b64: str) -> str:
    try:
        return base64.b64decode(b64).decode("utf-8")
    except Exception:  # noqa: BLE001
        return ""


def _binding(soup: BeautifulSoup, key: str) -> str | None:
    """Text of the span bound to `key`. The site builder sometimes leaves data-inline-binding empty
    and only sets the base64 data-inline-binding-encoded attribute, so check both."""
    span = soup.find("span", attrs={"data-inline-binding": key})
    if span is None:
        span = next((s for s in soup.find_all("span", attrs={"data-inline-binding-encoded": True})
                     if _decode(s["data-inline-binding-encoded"]) == key), None)
    if span is None:
        return None
    return span.get_text(strip=True) or None


def _meta(soup: BeautifulSoup, prop: str) -> str | None:
    tag = soup.find("meta", property=prop)
    return tag["content"].strip() if tag and tag.get("content") else None


def parse_ist_to_iso(date_str: str) -> str | None:
    """"March 14, 2026 | 10:00 am IST" → "2026-03-14T04:30:00.000Z"."""
    try:
        cleaned = date_str.replace(" IST", "").strip()
        parts = cleaned.split("|")
        date_part = parts[0].strip()
        time_part = parts[1].strip() if len(parts) > 1 else "12:00 am"
        dt = datetime.strptime(f"{date_part} {time_part}", "%B %d, %Y %I:%M %p").replace(tzinfo=_IST)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    except Exception:  # noqa: BLE001
        return None


def extract_hours(time_desc: str) -> int:
    """"commit 2 hours of your time" → 2 (default 2)."""
    m = re.search(r"(\d+)\s+hours?", time_desc, re.IGNORECASE)
    return int(m.group(1)) if m else 2


def _add_hours(iso: str, hours: int) -> str:
    dt = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%S.000Z").replace(tzinfo=timezone.utc)
    return (dt + timedelta(hours=hours)).strftime("%Y-%m-%dT%H:%M:%S.000Z")


def _event_image_url(soup: BeautifulSoup) -> str | None:
    """Event banner (a fresh signed Airtable URL each scrape), else og:image."""
    for img in soup.find_all("img", attrs={"data-dm-image-path": True}):
        if "airtableusercontent.com" in img["data-dm-image-path"]:
            return img["data-dm-image-path"]
    return _meta(soup, "og:image")


def parse_event_detail(html: str, url: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")

    name = _binding(soup, BINDING_NAME)
    if not name:
        og_title = _meta(soup, "og:title")
        if og_title and "|" in og_title:
            candidate = og_title.split("|")[0].strip()
            generic = {"volunteering opportunities", "in person volunteering", "events"}
            name = candidate if candidate.lower() not in generic else None
        if not name:
            name = url.rstrip("/").split("/")[-1].replace("-", " ").title()

    link = common.strip_utm_params(_meta(soup, "og:url") or url)

    title_tag = soup.find("title")
    title_text = title_tag.get_text(strip=True) if title_tag else ""
    organisation = title_text.split("|")[-1].strip() if "|" in title_text else "Team Everest"

    start_time = end_time = None
    date_text = _binding(soup, BINDING_DATE)
    if date_text:
        start_time = parse_ist_to_iso(date_text)
        if start_time:
            end_time = _add_hours(start_time, extract_hours(_binding(soup, BINDING_TIME_DESC) or ""))

    return {
        "name": name,
        "image": _event_image_url(soup),
        "portal": PORTAL,
        "start_time": start_time,
        "end_time": end_time,
        "address": _binding(soup, BINDING_LOCATION),
        "link": link,
        "organisation": organisation,
        "type": "In Person Volunteering",
    }


def _link_variants(url: str) -> set[str]:
    """The list page links https://, while og:url (what we store) is http:// — dedupe on both."""
    u = common.strip_utm_params(url)
    return {u, re.sub(r"^https://", "http://", u), re.sub(r"^http://", "https://", u)}


# ---------------------------------------------------------------------------
# Action
# ---------------------------------------------------------------------------

def fetch_events(data: dict, ctx: ConnectorContext) -> dict:
    event_filter = data.get("event_filter", "") or ""
    category_filter = data.get("category_filter", "") or ""
    try:
        bind = common.bind_kwargs(common.local_ipv4())
        session = common.make_session()

        list_resp = session.get(EVENTS_URL, timeout=TIMEOUT, **bind)
        list_resp.raise_for_status()
        event_urls = get_inperson_event_urls(list_resp.text)
        ctx.progress(f"Found {len(event_urls)} in-person event pages", {"pages": len(event_urls)})
        if not event_urls:
            return {"events": []}

        use_llm = bool(event_filter or category_filter)
        events: list[dict] = []
        for n, url in enumerate(event_urls, 1):
            resp = session.get(url, timeout=TIMEOUT, **bind)
            if resp.status_code != 200:
                log.info("detail_not_ok", url=url, status=resp.status_code)
                continue
            html = resp.text
            event = parse_event_detail(html, url)
            if use_llm:
                page_text = BeautifulSoup(html, "html.parser").get_text(separator=" ", strip=True)
                verdict = common.classify_single(ctx.ai, page_text, event_filter, category_filter)
                if not common.is_selected(verdict):
                    log.info("llm_rejected", url=url)
                    continue
                event["type"] = verdict.get("type", common.OTHER)
            events.append(event)
            ctx.progress(f"Processed {n}/{len(event_urls)} event pages", {"done": n, "total": len(event_urls),
                                                                         "matched": len(events)})
        ctx.progress(f"{len(events)} events matched", {"events": len(events)})
        return {"events": events}
    except ConnectorError:
        raise
    except Exception as e:  # noqa: BLE001 — legacy: any failure → "Unexpected error: ..."
        log.exception("teameverest_failed")
        raise ConnectorError(f"Unexpected error: {e}") from e


register(Connector(
    name=PORTAL,
    source="EVENT_PORTAL",
    description="In-person volunteering events from teameverest.ngo",
    actions={"FETCH_EVENTS": fetch_events},
))
