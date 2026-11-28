"""Jobs (history + live progress), scheduled sources (schedules on site actions), stored items + change feed."""
from __future__ import annotations

import json
import time
from datetime import datetime

from fastapi import APIRouter, Body, Request, Response
from fastapi.responses import StreamingResponse
from sqlalchemy import select

from scraper0.api.deps import APIError, require_admin, resolve
from scraper0.db.models import Job, JobEvent, Source, now
from scraper0.db.session import session_scope
from scraper0.jobs import runner, scheduler
from scraper0.services import entities as entity_svc
from scraper0.services import items as item_svc
from scraper0.services.sites import SiteError

router = APIRouter(prefix="/v1")
SSE_MAX_S = 55 * 60


def _own_job(s, tenant_id: str, job_id: str) -> Job:
    j = s.get(Job, job_id)
    if j is None or j.tenant_id != tenant_id:
        raise APIError(404, "not_found", "job not found")
    return j


@router.post("/jobs")
def create_job(request: Request, body: dict | None = Body(None)):
    """Ad-hoc jobs (crawl a URL, run a connector, upload a file) are gone: every scrape runs through an
    onboarded site and its locked action."""
    resolve(request)
    raise APIError(422, "invalid_input",
                   "ad-hoc jobs are not supported: onboard the site (POST /v2/sites), lock an action, then read it "
                   "(POST /v2/sites/{site_id}/actions/{name}/read) or refresh it "
                   "(POST /v2/sites/{site_id}/actions/{name}/refresh)")


@router.get("/jobs")
def list_jobs(request: Request, status: str | None = None, kind: str | None = None, limit: int = 50,
              before: str | None = None, source_id: str | None = None):
    p = resolve(request)
    with session_scope() as s:
        q = select(Job).where(Job.tenant_id == p.tenant_id)
        if status:
            q = q.where(Job.status == status)
        if kind:
            q = q.where(Job.kind == kind)
        if source_id:
            q = q.where(Job.source_id == source_id)
        if before:
            try:
                q = q.where(Job.created_at < datetime.fromisoformat(before))
            except ValueError:
                raise APIError(422, "invalid_input", "before must be an ISO timestamp")
        rows = s.scalars(q.order_by(Job.created_at.desc()).limit(max(1, min(limit, 200)))).all()
        return {"data": [runner.job_public(j, include_result=False) for j in rows]}


@router.get("/jobs/{job_id}")
def get_job(job_id: str, request: Request):
    p = resolve(request)
    with session_scope() as s:
        return {"data": runner.job_public(_own_job(s, p.tenant_id, job_id))}


@router.post("/jobs/{job_id}/cancel")
def cancel_job(job_id: str, request: Request):
    p = resolve(request)
    with session_scope() as s:
        _own_job(s, p.tenant_id, job_id)
    runner.request_cancel(job_id)
    with session_scope() as s:
        return {"data": runner.job_public(s.get(Job, job_id))}


@router.get("/jobs/{job_id}/events")
def job_events(job_id: str, request: Request):
    p = resolve(request)
    with session_scope() as s:
        _own_job(s, p.tenant_id, job_id)
    try:
        last = int(request.headers.get("last-event-id") or request.query_params.get("after") or 0)
    except ValueError:
        last = 0

    def stream():
        nonlocal last
        started = time.monotonic()
        yield "retry: 2000\n\n"
        while time.monotonic() - started < SSE_MAX_S:
            with session_scope() as s:
                evs = s.scalars(select(JobEvent).where(JobEvent.job_id == job_id, JobEvent.id > last)
                                .order_by(JobEvent.id).limit(200)).all()
                rows = [(e.id, e.type, e.data) for e in evs]
                status = s.scalar(select(Job.status).where(Job.id == job_id))
            for eid, etype, data in rows:
                last = eid
                yield f"id: {eid}\nevent: {etype}\ndata: {json.dumps(data, default=str)}\n\n"
                if etype == "done":
                    return
            if status in runner.TERMINAL and not rows:
                yield f"event: done\ndata: {json.dumps({'status': status})}\n\n"
                return
            yield ": keepalive\n\n"
            time.sleep(1.0)

    return StreamingResponse(stream(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache, no-transform", "X-Accel-Buffering": "no"})


# ------------------------------------------------------------------ sources

def _source(s, src: Source) -> dict:
    last = s.get(Job, src.last_job_id) if src.last_job_id else None
    if last is not None and last.tenant_id != src.tenant_id:     # joined another tenant's refresh: status only
        last_view = {"id": None, "kind": last.kind, "status": last.status,
                     "finished_at": last.finished_at.isoformat() if last.finished_at else None}
    else:
        last_view = runner.job_public(last, include_result=False) if last else None
    return {"id": src.id, "name": src.name, "site_id": src.site_id, "action": src.action,
            "interval_minutes": src.interval_minutes, "enabled": src.enabled,
            "next_run_at": src.next_run_at.isoformat() if src.next_run_at else None,
            "last_job": last_view, "created_at": src.created_at.isoformat()}


def _check_source(p, body: dict, partial: bool) -> dict:
    from scraper0.services import sites as sites_svc
    out = {}
    if not partial or "name" in body:
        name = str(body.get("name") or "").strip()
        if not name:
            raise APIError(422, "invalid_input", "name is required")
        out["name"] = name[:200]
    if not partial or "site_id" in body or "action" in body:
        site_id, action = str(body.get("site_id") or "").strip(), str(body.get("action") or "fetch_events").strip()
        if not site_id:
            raise APIError(422, "invalid_input", "site_id is required (an onboarded site)")
        with session_scope() as s:
            try:
                a, _v = sites_svc.get_locked(s, site_id, action)
            except sites_svc.SiteError as e:
                raise APIError(e.status, e.code, e.message, e.extra) from None
            if a.mode != "list":
                raise APIError(422, "invalid_input", f"{action} is a {a.mode} action; schedules refresh list actions")
        out["site_id"], out["action"] = site_id, action
    if "interval_minutes" in body or not partial:
        try:
            iv = int(body.get("interval_minutes", 1440))
        except (TypeError, ValueError):
            raise APIError(422, "invalid_input", "interval_minutes must be an integer")
        if iv < scheduler.MIN_INTERVAL:
            raise APIError(422, "invalid_input", f"interval_minutes must be ≥ {scheduler.MIN_INTERVAL}")
        out["interval_minutes"] = iv
    if "enabled" in body:
        out["enabled"] = bool(body["enabled"])
    return out


@router.get("/sources")
def list_sources(request: Request):
    p = resolve(request)
    with session_scope() as s:
        rows = s.scalars(select(Source).where(Source.tenant_id == p.tenant_id).order_by(Source.created_at)).all()
        return {"data": [_source(s, x) for x in rows]}


@router.post("/sources", status_code=201)
def create_source(request: Request, body: dict = Body(...)):
    p = resolve(request)
    require_admin(p, allow_service=True)
    fields = _check_source(p, body, partial=False)
    with session_scope() as s:
        src = Source(tenant_id=p.tenant_id, next_run_at=now(), **fields)
        s.add(src)
        s.flush()
        return {"data": _source(s, src)}


def _own_source(s, p, sid) -> Source:
    src = s.get(Source, sid)
    if src is None or src.tenant_id != p.tenant_id:
        raise APIError(404, "not_found", "source not found")
    return src


@router.patch("/sources/{sid}")
def patch_source(sid: str, request: Request, body: dict = Body(...)):
    p = resolve(request)
    require_admin(p, allow_service=True)
    fields = _check_source(p, body, partial=True)
    with session_scope() as s:
        src = _own_source(s, p, sid)
        for k, v in fields.items():
            setattr(src, k, v)
        return {"data": _source(s, src)}


@router.delete("/sources/{sid}", status_code=204)
def delete_source(sid: str, request: Request):
    p = resolve(request)
    require_admin(p, allow_service=True)
    with session_scope() as s:
        s.delete(_own_source(s, p, sid))
    return Response(status_code=204)


@router.post("/sources/{sid}/run", status_code=202)
def run_source(sid: str, request: Request):
    p = resolve(request)
    with session_scope() as s:
        _own_source(s, p, sid)
    require_admin(p, allow_service=True)
    try:
        job_id = scheduler.run_source(sid, force=True)
    except SiteError as e:
        raise APIError(e.status, e.code, e.message, e.extra) from None
    if job_id is None:
        raise APIError(503, "dispatch_failed", "the refresh could not be started; try again")
    with session_scope() as s:
        j = s.get(Job, job_id)
        if j is None or j.tenant_id != p.tenant_id:
            return {"data": {"id": None, "kind": "site_refresh", "status": j.status if j else "queued"}}
        return {"data": runner.job_public(j)}


# ------------------------------------------------------------------ items + change feed

def _entity_key(tenant_id: str, entity: str | None) -> str | None:
    if not entity:
        return None
    try:
        return entity_svc.resolve(tenant_id, entity).key
    except ValueError as e:
        raise APIError(422, "invalid_entity", str(e))


@router.get("/items")
def items(request: Request, entity: str | None = None, q: str | None = None, site: str | None = None,
          action: str | None = None, sort: str = "last_seen", limit: int = 50, offset: int = 0):
    """Shared site items (whoever scraped them) plus this tenant's private items."""
    p = resolve(request)
    if sort not in item_svc.SORTS:
        raise APIError(422, "invalid_input", f"sort must be one of {', '.join(item_svc.SORTS)}")
    rows, total = item_svc.list_items(p.tenant_id, _entity_key(p.tenant_id, entity), q,
                                      max(1, min(limit, 500)), max(0, offset), site_id=site or None,
                                      action=action or None, sort=sort)
    return {"data": rows, "total": total}


@router.get("/items/sites")
def item_sites(request: Request):
    """Sites holding items this tenant can see, with counts (for filters)."""
    p = resolve(request)
    return {"data": item_svc.site_filter_options(p.tenant_id)}


@router.get("/items/changes")
def item_changes(request: Request, entity: str | None = None, cursor: int = 0, limit: int = 500):
    p = resolve(request)
    rows, nxt, more = item_svc.changes(p.tenant_id, _entity_key(p.tenant_id, entity), max(0, cursor),
                                       max(1, min(limit, 2000)))
    return {"data": rows, "next_cursor": nxt, "has_more": more}
