"""v2: sites and actions (spec/api-v2.md). Sites are shared across tenants; reads and writes run as the caller's tenant."""
from __future__ import annotations

from fastapi import APIRouter, Body, Request, Response
from sqlalchemy import func, or_, select

from scraper0.api.deps import APIError, Principal, require_admin, resolve
from scraper0.db.models import Site
from scraper0.db.session import session_scope
from scraper0.services import site_calls
from scraper0.services import sites as sites_svc
from scraper0.services.sites import SiteError

router = APIRouter(prefix="/v2")


def _call(fn, *a, **kw):
    try:
        return fn(*a, **kw)
    except SiteError as e:
        raise APIError(e.status, e.code, e.message, e.extra) from None


def _viewer(request: Request) -> Principal:
    p = resolve(request, need_tenant=False)
    if p.is_platform_admin:
        return p
    if p.tenant is None:
        raise APIError(400, "no_tenant", "you are not a member of any tenant")
    if p.tenant.status != "active":
        raise APIError(403, "tenant_suspended", "this tenant is suspended")
    return p


def _admin(request: Request) -> Principal:
    p = _viewer(request)
    require_admin(p)            # tenant admin, or platform admin
    return p


def _who(p: Principal) -> str:
    return p.user.email if p.user else f"api_key:{p.tenant.slug if p.tenant else '?'}"


def _base(request: Request) -> str:
    proto = (request.headers.get("x-forwarded-proto") or request.url.scheme).split(",")[0].strip()
    host = (request.headers.get("x-forwarded-host") or request.headers.get("host") or request.url.netloc).split(",")[0]
    return f"{proto}://{host.strip()}"


# ------------------------------------------------------------------ sites

@router.get("/sites")
def list_sites(request: Request, q: str | None = None):
    _viewer(request)
    with session_scope() as s:
        query = select(Site)
        if q and q.strip():
            like = f"%{q.strip().lower()}%"
            query = query.where(or_(func.lower(Site.site_id).like(like), func.lower(Site.base_url).like(like),
                                    func.lower(Site.name).like(like)))
        return {"data": [sites_svc.site_summary(s, site) for site in s.scalars(query.order_by(Site.site_id))]}


@router.post("/sites")
def create_site(request: Request, response: Response, body: dict = Body(...)):
    p = _admin(request)
    data, existing = _call(sites_svc.create_site, body.get("base_url"), p.tenant.id if p.tenant else None)
    response.status_code = 200 if existing else 201
    return {"data": data, "existing": existing}


@router.get("/sites/{site_id}")
def get_site(site_id: str, request: Request):
    _viewer(request)
    with session_scope() as s:
        return {"data": sites_svc.site_public(s, _call(sites_svc.get_site, s, site_id))}


# ------------------------------------------------------------------ actions

@router.post("/sites/{site_id}/actions", status_code=201)
def create_action(site_id: str, request: Request, body: dict = Body(...)):
    p = _admin(request)
    return {"data": _call(sites_svc.create_action, site_id, body, p.tenant.id if p.tenant else None)}


@router.get("/sites/{site_id}/actions/{name}")
def get_action(site_id: str, name: str, request: Request):
    _viewer(request)
    with session_scope() as s:
        return {"data": sites_svc.action_public(_call(sites_svc.get_action, s, site_id, name))}


@router.get("/sites/{site_id}/actions/{name}/items")
def action_items(site_id: str, name: str, request: Request, q: str | None = None, sort: str = "last_seen",
                 limit: int = 50, offset: int = 0):
    """Everything stored for this site + action (shared items), whichever run scraped it."""
    from scraper0.services import items as item_svc
    _viewer(request)
    with session_scope() as s:
        _call(sites_svc.get_action, s, site_id, name)
    if sort not in item_svc.SORTS:
        raise APIError(422, "invalid_input", f"sort must be one of {', '.join(item_svc.SORTS)}")
    rows, total = item_svc.list_items(None, q, max(1, min(limit, 500)), max(0, offset), site_id=site_id,
                                      action=name, sort=sort)
    return {"data": rows, "total": total}


def _learn(site_id: str, name: str, request: Request, response: Response):
    p = _admin(request)
    if p.tenant is None:
        raise APIError(400, "no_tenant", "pick a tenant (X-Tenant) to run the learn job as")
    response.status_code = 202
    return {"data": _call(sites_svc.start_learn, site_id, name, p.tenant.id)}


@router.post("/sites/{site_id}/actions/{name}/learn")
def learn(site_id: str, name: str, request: Request, response: Response):
    return _learn(site_id, name, request, response)


@router.post("/sites/{site_id}/actions/{name}/repair")
def repair(site_id: str, name: str, request: Request, response: Response):
    return _learn(site_id, name, request, response)


@router.post("/sites/{site_id}/actions/{name}/refresh")
def refresh(site_id: str, name: str, request: Request, response: Response):
    """Refresh the locked version now, ignoring freshness (replaces "run connector" from the dashboard).
    Joins a refresh that is already running for this site + action."""
    p = _admin(request)
    if p.tenant is None:
        raise APIError(400, "no_tenant", "pick a tenant (X-Tenant) to run the refresh as")
    from scraper0.db.models import Job, now
    t0 = now()
    job_id = _call(site_calls.refresh_now, p.tenant.id, site_id, name)
    if job_id is None:
        raise APIError(503, "dispatch_failed", "the refresh could not be started; try again")
    with session_scope() as s:
        j = s.get(Job, job_id)
        created = site_calls._aware(j.created_at) if j is not None else None
        joined = created is not None and created < site_calls._aware(t0)
        visible = j is not None and j.tenant_id == p.tenant.id     # GET /v1/jobs/{id} works for this tenant
    response.status_code = 202
    return {"data": {"job_id": job_id, "joined": joined, "visible": visible}}


@router.get("/sites/{site_id}/actions/{name}/versions")
def versions(site_id: str, name: str, request: Request):
    _viewer(request)
    return {"data": _call(sites_svc.list_versions, site_id, name)}


@router.post("/sites/{site_id}/actions/{name}/versions", status_code=201)
def add_version(site_id: str, name: str, request: Request, body: dict = Body(...)):
    """A new draft version of a plan action: {"plan": <action plan>}."""
    _admin(request)
    return {"data": _call(sites_svc.add_plan_version, site_id, name, body.get("plan"))}


@router.post("/sites/{site_id}/actions/{name}/versions/{n}/lock")
def lock(site_id: str, name: str, n: int, request: Request):
    p = _admin(request)
    _call(sites_svc.lock_version, site_id, name, n, _who(p))
    return {"data": _call(sites_svc.contract, site_id, name, _base(request))}


@router.get("/sites/{site_id}/actions/{name}/contract")
def contract(site_id: str, name: str, request: Request):
    _viewer(request)
    return {"data": _call(sites_svc.contract, site_id, name, _base(request))}


# ------------------------------------------------------------------ calls

@router.post("/sites/{site_id}/actions/{name}/read")
def read(site_id: str, name: str, request: Request, response: Response, body: dict | None = Body(None)):
    p = resolve(request)
    body = body or {}
    with session_scope() as s:
        mode = _call(sites_svc.get_action, s, site_id, name).mode
    if mode == "lookup":
        status, data = _call(site_calls.read_lookup, p.tenant_id, site_id, name, body)
        response.status_code = status
        return {"data": data}
    return {"data": _call(site_calls.read_list, p.tenant_id, site_id, name, body)}


@router.post("/sites/{site_id}/actions/{name}/write")
def write(site_id: str, name: str, request: Request, response: Response, body: dict | None = Body(None)):
    p = resolve(request)
    status, data = _call(site_calls.write, p.tenant_id, site_id, name, body or {},
                         request.headers.get("idempotency-key"))
    response.status_code = status
    return {"data": data}
