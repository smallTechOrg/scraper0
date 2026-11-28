"""What each job kind does. Network/AI work happens here, never while holding a DB session.

Only onboarded sites are scraped: site_* jobs (services/site_runs.py) and code connectors run for a v2
lookup or the legacy /api/v1/scrape. There is no ad-hoc URL crawl and no upload any more."""
from __future__ import annotations

import structlog

from scraper0.config import get_settings
from scraper0.core.crawler import Cancelled
from scraper0.core.entities import EVENT_SPEC
from scraper0.core.fetch import Fetcher
from scraper0.core.llm import LLMError
from scraper0.services.ai import AIClient
from scraper0.services.connector_state import DBStateStore
from scraper0.services.items import store_run_items

log = structlog.get_logger(__name__)


class JobFailed(Exception):
    pass


class JobCancelled(Exception):
    pass


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
        if kind == "connector":
            out = _connector(job_id, tenant_id, inp, source_id, rep, ai)
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


# ------------------------------------------------------------------ connector

def _connector_site(connector: str) -> tuple[str, str] | None:
    """The site + list action a connector's events belong to (from the seeded code actions)."""
    from scraper0.services.sites import CODE_ACTIONS, site_id_of
    for base, name, _kind, mode, _entity, conn, _conn_action in CODE_ACTIONS:
        if conn == connector and mode == "list":
            return site_id_of(base), name
    return None


def _connector(job_id, tenant_id, inp, source_id, rep, ai) -> dict:
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
        stored = store_run_items(tenant_id, EVENT_SPEC.key, data["events"], job_id,
                                 site=_connector_site(conn.name))
        rep(f"{len(data['events'])} event(s)", {"items": len(data["events"])}, force=True)
        return {"data": data, "stored": stored}
    return {"data": data}
