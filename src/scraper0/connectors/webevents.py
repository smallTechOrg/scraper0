"""WEBEVENTS: the generic learning event engine exposed as a connector (legacy FETCH_EVENTS contract)."""
from __future__ import annotations

from scraper0.connectors.base import Connector, ConnectorContext, ConnectorError, register


def fetch_events(data: dict, ctx: ConnectorContext) -> dict:
    from scraper0.connectors._events_common import fetch_existing_links
    from scraper0.core.store import default_store
    from scraper0.events.engine import Engine, Options
    from scraper0.jobs.execute import _fetcher

    urls = data.get("urls") or []
    if not isinstance(urls, list) or not urls or not all(isinstance(u, str) for u in urls):
        raise ConnectorError("action.data.urls must be a non-empty list of URLs")
    try:
        max_pages = max(1, min(int(data.get("max_detail_pages", 60)), 200))
    except (TypeError, ValueError):
        raise ConnectorError("max_detail_pages must be an integer")
    portal = str(data.get("portal") or "WEB")[:50]
    text_fn = getattr(ctx.ai, "text_fn", None)
    eng = Engine(_fetcher(), llm=text_fn("extract") if text_fn else ctx.ai.text,
                 filter_llm=text_fn("filter") if text_fn else ctx.ai.text, store=default_store())
    opts = Options(
        portal=portal, event_filter=data.get("event_filter", ""), category_filter=data.get("category_filter", ""),
        include_past=bool(data.get("include_past", False)), include_online=bool(data.get("include_online", False)),
        require_address=bool(data.get("require_address", False)), max_detail_pages=max_pages,
        existing_links=fetch_existing_links(portal, backend_url=ctx.backend_url),
        progress=ctx.progress,
    )
    return eng.run(urls[:20], opts)


register(Connector(
    name="WEBEVENTS", source="EVENT_PORTAL",
    description="Events from any website(s) by URL: structured data, learned layouts, AI fallback",
    actions={"FETCH_EVENTS": fetch_events},
))
