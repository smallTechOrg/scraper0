"""What each job kind does. Network/AI work happens here, never while holding a DB session."""
from __future__ import annotations

import re

import structlog
from sqlalchemy import select

from scraper0.config import get_settings
from scraper0.core.crawler import Cancelled, CrawlOptions, Crawler
from scraper0.core.entities import EVENT_SPEC
from scraper0.core.fetch import Fetcher
from scraper0.core.llm import LLMError
from scraper0.core.store import default_store
from scraper0.db.models import JobFile
from scraper0.db.session import session_scope
from scraper0.services import entities as entity_svc
from scraper0.services.ai import AIClient
from scraper0.services.connector_state import DBStateStore
from scraper0.services.items import upsert_items

log = structlog.get_logger(__name__)


class JobFailed(Exception):
    pass


class JobCancelled(Exception):
    pass


def _int(v, default, lo, hi):
    try:
        return max(lo, min(int(v), hi))
    except (TypeError, ValueError):
        return default


def _fetcher() -> Fetcher:
    st = get_settings()
    renderer = None
    if st.render_js == "auto":
        from scraper0.core.render import shared_renderer
        renderer = shared_renderer()
    return Fetcher(min_host_interval=st.min_host_interval_s, respect_robots=st.respect_robots, renderer=renderer)


def run(job_id: str, tenant_id: str, kind: str, inp: dict, source_id: str | None, rep) -> dict:
    ai = AIClient(tenant_id, job_id)
    try:
        if kind == "crawl":
            out = _crawl(tenant_id, inp, source_id, rep, ai)
        elif kind == "connector":
            out = _connector(tenant_id, inp, source_id, rep, ai)
        elif kind == "ingest":
            out = _ingest(job_id, tenant_id, inp, source_id, rep, ai)
        elif kind in ("site_refresh", "site_learn", "site_write"):
            from scraper0.services import site_runs
            out = site_runs.run(job_id, tenant_id, kind, inp, rep, ai)
        else:
            raise JobFailed(f"unknown job kind {kind}")
    except Cancelled as e:
        raise JobCancelled() from e
    if isinstance(out, dict):
        out.setdefault("ai", {"calls": ai.calls, "cost_usd": round(ai.cost_usd, 6), "errors": ai.errors})
    return out


# ------------------------------------------------------------------ crawl

def _crawl(tenant_id, inp, source_id, rep, ai) -> dict:
    try:
        spec = entity_svc.resolve(tenant_id, inp.get("entity", "event"))
    except ValueError as e:
        raise JobFailed(str(e)) from e
    o = inp.get("options") or {}
    urls = inp["urls"]
    common = dict(max_detail_pages=_int(o.get("max_detail_pages"), 60, 1, 500),
                  max_list_pages=_int(o.get("max_list_pages"), 5, 1, 50))
    if spec.key == EVENT_SPEC.key:
        from scraper0.events.engine import Engine, Options
        eng = Engine(_fetcher(), llm=ai.text_fn("extract"), filter_llm=ai.text_fn("filter"), store=default_store())
        opts = Options(portal=str(o.get("portal") or "WEB")[:50], event_filter=o.get("event_filter", ""),
                       category_filter=o.get("category_filter", ""),
                       include_past=bool(o.get("include_past", False)), include_online=bool(o.get("include_online", False)),
                       require_address=bool(o.get("require_address", False)),
                       use_llm_extraction=bool(o.get("allow_llm", True)), learn=bool(o.get("learn", True)),
                       progress=rep, cancelled=rep.cancelled, **common)
        res = eng.run(urls, opts)
        items = res["events"]
        stats = res["stats"]
    else:
        crawler = Crawler(default_store(), _fetcher(), ai.text_fn("extract"))
        copts = CrawlOptions(allow_llm=bool(o.get("allow_llm", True)), learn=bool(o.get("learn", True)),
                             use_cache=bool(o.get("use_cache", True)), progress=rep, cancelled=rep.cancelled,
                             link_hint=re.compile(o["link_pattern"]) if o.get("link_pattern") else None, **common)
        items, stats = [], {"crawl": [], "errors": []}
        for u in urls:
            try:
                got, st = crawler.crawl(u, spec, copts)
            except Cancelled:
                raise
            except Exception as e:
                stats["errors"].append({"source": u, "error": str(e)[:300]})
                continue
            items += got
            stats["crawl"].append({"source": u, "pages": st.pages, "by_method": st.by_method,
                                   "llm_calls": st.llm_calls, "recipes_learned": st.recipes_learned})
        stats["returned"] = len(items)
    rep(f"{len(items)} item(s) extracted", {"items": len(items)}, force=True)
    stored = upsert_items(tenant_id, spec.key, items, source_id)
    return {"entity": spec.name, "items": items, "stats": stats, "stored": stored}


# ------------------------------------------------------------------ connector

def _connector(tenant_id, inp, source_id, rep, ai) -> dict:
    from scraper0.connectors.base import ConnectorContext, ConnectorError, get
    from scraper0.jobs.runner import decrypt_auth
    conn = get(inp["connector"])
    if conn is None:
        raise JobFailed(f"unknown connector {inp['connector']!r}")
    action = conn.actions.get(inp["action"])
    if action is None:
        raise JobFailed(f"connector {conn.name} has no action {inp['action']!r}")
    ctx = ConnectorContext(tenant_id=tenant_id, ai=ai, state=DBStateStore(tenant_id, conn.name),
                           backend_url=inp.get("backend_url"), auth=decrypt_auth(inp), progress=rep)
    try:
        data = action(dict(inp.get("data") or {}), ctx)
    except ConnectorError as e:
        raise JobFailed(str(e)) from e
    except LLMError as e:
        raise JobFailed(f"AI unavailable: {e}") from e
    if isinstance(data, dict) and isinstance(data.get("events"), list):
        stored = upsert_items(tenant_id, EVENT_SPEC.key, data["events"], source_id)
        rep(f"{len(data['events'])} event(s)", {"items": len(data["events"])}, force=True)
        return {"data": data, "stored": stored}
    return {"data": data}


# ------------------------------------------------------------------ ingest (uploads)

def _ingest(job_id, tenant_id, inp, source_id, rep, ai) -> dict:
    from scraper0.events.dedupe import dedupe
    from scraper0.events.engine import Engine, Options
    from scraper0.events.ingest import IngestInput, IngestOptions, ingest, review_flags
    with session_scope() as s:
        files = [IngestInput(f.filename, f.data) for f in s.scalars(select(JobFile).where(JobFile.job_id == job_id))]
    portal = str(inp.get("portal") or "ADMIN")[:50]
    rep(f"reading {len(files)} file(s)", force=True)
    res = ingest(files, inp.get("text"), ai.text_fn("extract"), ai.vision_fn("vision"), IngestOptions(portal=portal))
    events, report = res["events"], res["report"]
    urls = [u for u in inp.get("urls") or [] if isinstance(u, str)][:10]
    if urls:
        eng = Engine(_fetcher(), llm=ai.text_fn("extract"), store=default_store())
        opts = Options(portal=portal, include_past=True, include_online=True, require_start=False,
                       progress=rep, cancelled=rep.cancelled)
        for u in urls:
            try:
                got = eng.scrape_source(u, opts)
                events += got
                report.append({"input": u, "events": len(got), "error": None})
            except Cancelled:
                raise
            except Exception as e:
                report.append({"input": u, "events": 0, "error": str(e)[:300]})
        review_flags(events)
    events = dedupe(events)
    events.sort(key=lambda e: (e.start_time or "9999", e.name))
    items = [e.to_dict() for e in events]
    stored = upsert_items(tenant_id, EVENT_SPEC.key, items, source_id)
    return {"entity": "event", "items": items, "report": report, "stored": stored,
            "needs_review": sum(1 for e in events if e.warnings)}
