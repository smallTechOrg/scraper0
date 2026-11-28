"""MYBHARATGOVIN — volunteering events from mybharat.gov.in (port of legacy portals/mybharat_events.py).

Flow: paginate the site's search API (Upcoming + Ongoing, type=vo) → dedupe by id → when a filter is
given: skip links the backend already has, then LLM batch filter/classify → structured events.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import structlog
from bs4 import BeautifulSoup

from scraper0.connectors import _events_common as common
from scraper0.connectors.base import Connector, ConnectorContext, ConnectorError, register

log = structlog.get_logger(__name__)

PORTAL = "MYBHARATGOVIN"
SEARCH_API = "https://search-api-prod.mybharats.in/events"
CDN_URL = "https://cdn-prod.mybharats.in/"
BASE_URL = "https://mybharat.gov.in"
TIMEOUT = 15
PAGE_LIMIT = 12   # the site's page size
MAX_PAGES = 20    # per tab, safety cap

_IST = timezone(timedelta(hours=5, minutes=30))


# ---------------------------------------------------------------------------
# Search API
# ---------------------------------------------------------------------------

def build_query(tab: str = "Upcoming", page: int = 1, today: str | None = None) -> dict:
    today = today or datetime.now(_IST).strftime("%Y-%m-%d")
    filters: list[dict] = [{"term": {"type": "vo"}}]
    if tab == "Upcoming":
        filters.append({"range": {"event_from_date": {"gt": today}}})
    elif tab == "Ongoing":
        filters.append({"range": {"event_to_date": {"gte": today}}})
        filters.append({"range": {"event_from_date": {"lte": today}}})
    elif tab == "Past":
        filters.append({"range": {"event_to_date": {"lt": today}}})
    return {
        "query": {"bool": {"must": filters, "should": []}},
        "sort": [{"event_from_date": {"order": "asc"}}],
        "page": str(page),
        "limit": PAGE_LIMIT,
    }


def _post_search(session, bind: dict, body: dict) -> dict:
    resp = session.post(SEARCH_API, json=body, timeout=TIMEOUT, headers={
        "Content-Type": "application/json",
        "Origin": BASE_URL,
        "Referer": f"{BASE_URL}/",
    }, **bind)
    resp.raise_for_status()
    return resp.json()


def fetch_all_records(session, bind: dict, tab: str = "Upcoming", progress=None) -> list[dict]:
    records: list[dict] = []
    page = 1
    while page <= MAX_PAGES:
        data = _post_search(session, bind, build_query(tab, page=page))
        sources = [r["_source"] for r in data.get("records", []) if "_source" in r]
        records.extend(sources)
        total = data.get("total", 0)
        log.info("mybharat_page", tab=tab, page=page, got=len(sources), total=total)
        if progress:
            progress(f"MY Bharat {tab}: page {page} fetched", {"tab": tab, "page": page, "records": len(records),
                                                               "total": total})
        if not sources or len(records) >= total:
            break
        page += 1
    return records


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------

def _image_url(image_path: str | None) -> str | None:
    return f"{CDN_URL}events/{image_path}" if image_path else None


def parse_datetime_ist(date_str: str, time_str: str | None) -> str | None:
    """('2026-03-28', '10:00:00') in IST → '2026-03-28T04:30:00.000Z'."""
    try:
        if time_str:
            dt = datetime.strptime(f"{date_str} {time_str}", "%Y-%m-%d %H:%M:%S")
        else:
            dt = datetime.strptime(date_str, "%Y-%m-%d")
        return dt.replace(tzinfo=_IST).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.000Z")
    except Exception:  # noqa: BLE001
        return None


def _strip_html(html: str) -> str:
    return BeautifulSoup(html, "html.parser").get_text(separator=" ", strip=True)


def build_event_text(record: dict) -> str:
    parts = [
        f"Event Name: {record.get('event_name', '')}",
        f"Organisation: {record.get('org_name', '')}",
        f"Partner: {record.get('partner_name', '')}",
        f"Location: {record.get('event_address', '')} {record.get('district', '')} {record.get('state', '')}",
        f"Date: {record.get('event_from_date', '')} to {record.get('event_to_date', '')}",
    ]
    desc = record.get("description", "")
    if desc:
        parts.append(f"Description: {_strip_html(desc)}")
    names = [a.get("name", "") for a in record.get("geteventactivity", []) or [] if a.get("name")]
    if names:
        parts.append(f"Activities: {', '.join(names)}")
    return "\n".join(parts)


def build_address(record: dict) -> str | None:
    parts = []
    addr = record.get("event_address") or record.get("event_address_1")
    if addr:
        parts.append(addr.strip())
    for k in ("district", "state"):
        if record.get(k):
            parts.append(record[k])
    if record.get("nyf_event_pincode"):
        parts.append(str(record["nyf_event_pincode"]))
    return ", ".join(parts) if parts else None


def record_link(record: dict) -> str:
    link = record.get("event_link") or ""
    if not link:
        slug = (record.get("event_name") or "").replace(" ", "-")
        link = f"{BASE_URL}/pages/event_detail?event_name={slug}&key={record.get('id', '')}"
    return common.strip_utm_params(link)


def parse_record(record: dict) -> dict:
    return {
        "name": record.get("event_name"),
        "image": _image_url(record.get("image_path")),
        "portal": PORTAL,
        "start_time": parse_datetime_ist(record.get("event_from_date", ""), record.get("event_start_time")),
        "end_time": parse_datetime_ist(record.get("event_to_date", ""), record.get("event_end_time")),
        "address": build_address(record),
        "link": record_link(record),
        "organisation": record.get("org_name") or record.get("partner_name") or "MY Bharat",
        "type": common.OTHER,
    }


def dedupe_by_id(records: list[dict]) -> list[dict]:
    seen: set = set()
    out = []
    for r in records:
        eid = r.get("id")
        if eid and eid in seen:
            continue
        seen.add(eid)
        out.append(r)
    return out


# ---------------------------------------------------------------------------
# Action
# ---------------------------------------------------------------------------

def select_events(records: list[dict], ctx: ConnectorContext, event_filter: str, category_filter: str) -> list[dict]:
    """Without a filter every record is returned; with one, the AI filters and classifies them."""
    if not (event_filter or category_filter):
        return [parse_record(r) for r in records]
    new = records
    events = []
    for record, typ in common.filter_in_batches(ctx.ai, new, build_event_text, event_filter, category_filter,
                                                progress=ctx.progress):
        ev = parse_record(record)
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
        raw: list[dict] = []
        for tab in ("Upcoming", "Ongoing"):
            raw.extend(fetch_all_records(session, bind, tab, progress=ctx.progress))
        if not raw:
            return {"events": []}
        events = select_events(dedupe_by_id(raw), ctx, event_filter, category_filter)
        ctx.progress(f"{len(events)} events matched", {"events": len(events)})
        return {"events": events}
    except ConnectorError:
        raise
    except Exception as e:  # noqa: BLE001
        log.exception("mybharat_failed")
        raise ConnectorError(f"Unexpected error: {e}") from e


register(Connector(
    name=PORTAL,
    source="EVENT_PORTAL",
    description="Volunteering events from the MY Bharat portal (mybharat.gov.in)",
    actions={"FETCH_EVENTS": fetch_events},
))
