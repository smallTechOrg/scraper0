"""IVOLUNTEERIN — volunteering opportunities from ivolunteer.in (port of legacy portals/ivolunteer_events.py).

Flow: paginate the search API → dedupe by SID → when a filter is given: skip links the backend
already has, then LLM batch filter/classify → enrich missing addresses from the detail page →
drop events that still have no address.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode

import structlog
from bs4 import BeautifulSoup

from scraper0.connectors import _events_common as common
from scraper0.connectors.base import Connector, ConnectorContext, ConnectorError, register

log = structlog.get_logger(__name__)

PORTAL = "IVOLUNTEERIN"
BASE_URL = "https://www.ivolunteer.in"
SEARCH_API = f"{BASE_URL}/search/GetOpportunitiesSearchResultBlockListing"
PLACEHOLDER_IMAGE = ("https://cdn0.handsonconnect.org/00006c/images/__thumbs/"
                     "Square%20logo_green.png/Square%20logo_green__300x300.png")
TIMEOUT = 15
PAGE_SIZE = 48   # the site's block size
MAX_PAGES = 10   # safety cap

_IST = timezone(timedelta(hours=5, minutes=30))

# Default Solr exclusion query (filters out sub-site partner listings)
SOLR_EXCLUDE = (
    '-wsTag:"Subsite RBL" AND -wsTag:"Brillio-BringingSmiles" '
    'AND -wsTag:"Subsite-Welspun" AND -wsTag:"DAV-Sushrusha" '
    'AND -wsTag:"VFS Global" AND -wsTag:"ACTcorp"'
)


# ---------------------------------------------------------------------------
# Search API
# ---------------------------------------------------------------------------

def build_form_data(current_rows: int = 0) -> dict:
    return {
        "parameters[solrQuery]": SOLR_EXCLUDE,
        "parameters[sort_by]": "Distance",
        "parameters[searchvo_date_from]": "",
        "parameters[searchvo_date_to]": "",
        "parameters[age_volunteer_specific]": "",
        "parameters[my_saved_searches]": "",
        "parameters[share_search_result]": "",
        "parameters[save_current_search_as]": "",
        "parameters[keyword]": "",
        "parameters[location-type]": "Address",
        "parameters[temporal_auxiliar]": "",
        "parameters[location]": "",
        "parameters[distance]": "Any",
        "parameters[address-suggestion-bias]": "in",
        "parameters[countRegular]": "0",
        "parameters[countTraining]": "0",
        "parameters[countFilled]": "0",
        "parameters[countEvents]": "0",
        "parameters[countOpp55]": "0",
        "parameters[language]": "en-IN",
        "currentRows": str(current_rows),
        "isSearch": "true",
        "blockId": "486",
    }


def _post_search(session, bind: dict, current_rows: int) -> dict:
    resp = session.post(SEARCH_API, data=urlencode(build_form_data(current_rows)), timeout=TIMEOUT, headers={
        "Content-Type": "application/x-www-form-urlencoded; charset=UTF-8",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/search",
        "X-Requested-With": "XMLHttpRequest",
    }, **bind)
    resp.raise_for_status()
    return resp.json()


def fetch_all_opportunities(session, bind: dict, progress=None) -> list[dict]:
    opps: list[dict] = []
    page = 0
    while page < MAX_PAGES:
        data = _post_search(session, bind, page * PAGE_SIZE)
        got = data.get("opportunities", [])
        opps.extend(got)
        total = data.get("total", 0)
        log.info("ivolunteer_page", page=page + 1, got=len(got), total=total)
        if progress:
            progress(f"iVolunteer: page {page + 1} fetched", {"page": page + 1, "records": len(opps), "total": total})
        if not got or len(opps) >= total:
            break
        page += 1
    return opps


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def parse_datetime(iso_str: str | None) -> str | None:
    """"2026-04-04T11:00:00Z" → "2026-04-04T11:00:00.000Z"."""
    if not iso_str:
        return None
    try:
        return datetime.fromisoformat(iso_str.replace("Z", "+00:00")).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    except Exception:  # noqa: BLE001
        return None


def parse_end_date(end_date_str: str | None, start_iso: str | None) -> str | None:
    """endDate 'dd-mm-yy' → end of that day IST in UTC; falls back to start."""
    if not end_date_str:
        return start_iso
    try:
        dt = datetime.strptime(end_date_str, "%d-%m-%y").replace(hour=23, minute=59, second=59, tzinfo=_IST)
        return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    except Exception:  # noqa: BLE001
        return start_iso


def _strip_html(html: str) -> str:
    return BeautifulSoup(html, "html.parser").get_text(separator=" ", strip=True)


def build_event_text(opp: dict) -> str:
    parts = [
        f"Event Name: {opp.get('Title', '')}",
        f"Organisation: {opp.get('OrganizationName', '')}",
        f"Location: {opp.get('Location', '')}",
        f"Type: {opp.get('TypeIndicator', '')}",
        f"Duration: {opp.get('Duration', '')}",
        f"Date: {opp.get('StartDateTimeValue', '')} to {opp.get('endDate', '')}",
        f"Ongoing: {'yes' if opp.get('isOngoing') else 'no'}",
        f"Spots Available: {opp.get('SpotsAvailable', '')}",
    ]
    if opp.get("Description"):
        parts.append(f"Description: {_strip_html(opp['Description'])}")
    if opp.get("TimeColumnDataAriaLabel"):
        parts.append(f"Schedule: {_strip_html(opp['TimeColumnDataAriaLabel'])}")
    return "\n".join(parts)


def opp_link(opp: dict) -> str:
    link = opp.get("OccurrenceUrl") or ""
    if link and not link.startswith("http"):
        link = f"{BASE_URL}{link}"
    return common.strip_utm_params(link)


def parse_opportunity(opp: dict) -> dict:
    start_time = parse_datetime(opp.get("StartDateTimeValue"))
    return {
        "name": opp.get("Title"),
        "image": opp.get("ImageURL") or PLACEHOLDER_IMAGE,
        "portal": PORTAL,
        "start_time": start_time,
        "end_time": parse_end_date(opp.get("endDate"), start_time),
        "address": (opp.get("Location") or "").strip() or None,
        "link": opp_link(opp),
        "organisation": opp.get("OrganizationName") or "iVolunteer",
        "type": common.OTHER,
    }


def dedupe_by_sid(opps: list[dict]) -> list[dict]:
    seen: set = set()
    out = []
    for o in opps:
        sid = o.get("SID")
        if sid and sid in seen:
            continue
        seen.add(sid)
        out.append(o)
    return out


# ---------------------------------------------------------------------------
# Address enrichment
# ---------------------------------------------------------------------------

LOCATION_PATTERNS = [
    re.compile(r"📍\s*Location\s*[:\-–]\s*(.+)", re.IGNORECASE),
    re.compile(r"(?:^|\n)\s*Location\s*[:\-–]\s*(.+)", re.IGNORECASE | re.MULTILINE),
    re.compile(r"(?:^|\n)\s*Where\s*[:\-–]\s*(.+)", re.IGNORECASE | re.MULTILINE),
    re.compile(r"(?:^|\n)\s*Venue\s*[:\-–]\s*(.+)", re.IGNORECASE | re.MULTILINE),
    re.compile(r"(?:^|\n)\s*Address\s*[:\-–]\s*(.+)", re.IGNORECASE | re.MULTILINE),
    # "📍 Mode: In-person (locations across Bengaluru)" → city from the parenthetical
    re.compile(r"📍[^\n]*\(locations?\s+(?:across|in|around)\s+([^)]+)\)", re.IGNORECASE),
    re.compile(r"locations?\s+(?:across|in|around)\s+(\w[\w\s,]+)", re.IGNORECASE),
]
_ADDRESS_REJECT = {"mode", "in-person", "online", "virtual", "remote", "flexible", "tbd", "tba"}


def extract_address_from_text(text: str) -> str | None:
    for pattern in LOCATION_PATTERNS:
        m = pattern.search(text)
        if not m:
            continue
        addr = m.group(1).strip().rstrip(".•")
        if not addr or len(addr) <= 3:
            continue
        first_word = addr.split(":")[0].split(",")[0].split("(")[0].strip().lower()
        if first_word in _ADDRESS_REJECT:
            continue
        if "to be shared" in addr.lower() or "to be confirmed" in addr.lower():
            continue
        return addr
    return None


def address_from_detail_html(html: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    loc_input = soup.find("input", class_="occurrence-location-name")
    if loc_input:
        val = (loc_input.get("value") or "").strip()
        if val:
            return val
    body = soup.find("div", id="content-page")
    if body:
        return extract_address_from_text(body.get_text("\n", strip=True))
    return None


def scrape_address_from_detail(session, bind: dict, url: str) -> str | None:
    try:
        resp = session.get(url, timeout=TIMEOUT, **bind)
        if resp.status_code != 200:
            return None
        return address_from_detail_html(resp.text)
    except Exception as e:  # noqa: BLE001
        log.warning("ivolunteer_address_failed", url=url, error=str(e))
        return None


def enrich_missing_addresses(session, bind: dict, events: list[dict]) -> None:
    for ev in events:
        if ev.get("address") or not ev.get("link"):
            continue
        address = scrape_address_from_detail(session, bind, ev["link"])
        if address:
            log.info("ivolunteer_address_enriched", link=ev["link"], address=address)
            ev["address"] = address


# ---------------------------------------------------------------------------
# Action
# ---------------------------------------------------------------------------

def select_events(opps: list[dict], ctx: ConnectorContext, event_filter: str, category_filter: str) -> list[dict]:
    if not (event_filter or category_filter):
        return [parse_opportunity(o) for o in opps]
    new = opps
    events = []
    for opp, typ in common.filter_in_batches(ctx.ai, new, build_event_text, event_filter, category_filter,
                                             progress=ctx.progress, label="opportunities"):
        ev = parse_opportunity(opp)
        ev["type"] = typ
        events.append(ev)
    log.info("llm_matched", matched=len(events), total=len(new))
    return events


def fetch_events(data: dict, ctx: ConnectorContext) -> dict:
    event_filter = data.get("event_filter", "") or ""
    category_filter = data.get("category_filter", "") or ""
    try:
        bind = common.bind_kwargs(common.local_ipv4())
        session = common.make_session()
        raw = fetch_all_opportunities(session, bind, progress=ctx.progress)
        if not raw:
            return {"events": []}
        events = select_events(dedupe_by_sid(raw), ctx, event_filter, category_filter)
        enrich_missing_addresses(session, bind, events)
        before = len(events)
        events = [e for e in events if e.get("address")]
        if before != len(events):
            log.info("ivolunteer_dropped_no_address", count=before - len(events))
        ctx.progress(f"{len(events)} events matched", {"events": len(events), "dropped_no_address": before - len(events)})
        return {"events": events}
    except ConnectorError:
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("ivolunteer_failed")
        raise ConnectorError(f"Unexpected error: {e}") from e


register(Connector(
    name=PORTAL,
    source="EVENT_PORTAL",
    description="Volunteering opportunities from iVolunteer.in",
    actions={"FETCH_EVENTS": fetch_events},
))
