"""POST /api/v1/scrape — the old web-scraper contract, kept for hashtaglocal-backend.

Runs the connector as a job inside this request (so it shows in the dashboard and events land in
stored results) and returns the connector payload as {"data": ...}.
"""
from __future__ import annotations

from fastapi import APIRouter, Body, Request
from fastapi.responses import JSONResponse
from sqlalchemy import select

from scraper0.api.deps import APIError, resolve
from scraper0.config import get_settings
from scraper0.db.models import Job, Tenant
from scraper0.db.session import session_scope
from scraper0.jobs import runner

router = APIRouter()
SOURCES = {"GOV_ISSUE_PORTAL", "EVENT_PORTAL"}


def legacy_error(message: str, status: int = 422) -> JSONResponse:
    return JSONResponse({"code": status, "status": "Unprocessable Entity" if status == 422 else "Error",
                         "message": message, "errors": {"json": [message]}}, status_code=status)


def _tenant_id(request: Request) -> str:
    st = get_settings()
    if request.headers.get("x-api-key") or request.cookies.get("s0_session"):
        return resolve(request).tenant_id
    if not st.legacy_keyless:
        raise APIError(401, "unauthenticated", "send X-API-Key")
    with session_scope() as s:
        t = s.scalar(select(Tenant).where(Tenant.slug == st.legacy_tenant_slug))
        if t is None or t.status != "active":
            raise APIError(503, "legacy_tenant_missing", f"legacy tenant {st.legacy_tenant_slug!r} not found")
        return t.id


@router.post("/api/v1/scrape")
def scrape(request: Request, body: dict = Body(...)):
    from scraper0.connectors.base import get
    try:
        tenant_id = _tenant_id(request)
    except APIError as e:
        return legacy_error(e.message, e.status)
    source = body.get("source")
    ctx = body.get("context") or {}
    portal = ctx.get("portal")
    action = (ctx.get("action") or {})
    action_type, data = action.get("type"), action.get("data")
    if source not in SOURCES:
        return legacy_error("Unsupported source")
    if not isinstance(data, dict) or not portal or not action_type:
        return legacy_error("context.portal, context.action.type and context.action.data are required")
    conn = get(portal)
    if conn is None or conn.source != source:
        return legacy_error("Unsupported portal")
    if action_type not in conn.actions:
        return legacy_error(f"Unsupported action type '{action_type}' for source '{source}' and portal '{portal}'")

    backend = (request.headers.get("x-backend-url") or "").strip().rstrip("/")
    allowed = get_settings().backend_url_allowlist
    backend_url = backend if backend and backend in allowed else None
    if action_type == "FETCH_EVENTS" and backend_url is None:
        return legacy_error("Missing or unrecognized X-Backend-Url header")

    inp = {"connector": portal, "action": action_type, "data": data, "backend_url": backend_url}
    if ctx.get("auth"):
        inp["auth"] = ctx["auth"]
    job_id = runner.submit(tenant_id, "connector", inp)
    runner.run_job(job_id)
    with session_scope() as s:
        j = s.get(Job, job_id)
        if j.status != "succeeded":
            return legacy_error(j.error or "Unknown error")
        return {"data": (j.result or {}).get("data")}
