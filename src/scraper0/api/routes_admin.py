"""Platform admin: tenants, access requests, shared AI providers, usage across tenants."""
from __future__ import annotations

import re

from fastapi import APIRouter, Body, Request, Response
from sqlalchemy import func, select

from scraper0.api.deps import APIError, require_platform_admin
from scraper0.api.routes_tenant import usage_summary
from scraper0.config import get_settings
from scraper0.db.models import AccessRequest, AIProvider, Membership, Tenant
from scraper0.db.session import session_scope
from scraper0.services import ai as ai_svc
from scraper0.services import auth as auth_svc

router = APIRouter(prefix="/v1/admin")
_SLUG = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")


def _tenant(s, t: Tenant) -> dict:
    members = s.scalar(select(func.count()).select_from(Membership).where(Membership.tenant_id == t.id))
    return {"id": t.id, "slug": t.slug, "name": t.name, "status": t.status,
            "monthly_ai_budget_usd": t.monthly_ai_budget_usd,
            "ai_spend_this_month_usd": round(ai_svc.total_spend_this_month(s, t.id), 4),
            "members": members, "created_at": t.created_at.isoformat()}


def create_tenant(s, slug: str, name: str, budget, admin_email: str | None) -> tuple[Tenant, str | None]:
    slug = (slug or "").strip().lower()
    if not _SLUG.match(slug):
        raise APIError(422, "invalid_input", "slug: 2–63 chars, lowercase letters, digits, dashes")
    if s.scalar(select(Tenant).where(Tenant.slug == slug)):
        raise APIError(409, "slug_taken", "a tenant with that slug exists")
    try:
        budget = float(budget if budget is not None else 20.0)
    except (TypeError, ValueError):
        raise APIError(422, "invalid_input", "monthly_ai_budget_usd must be a number")
    t = Tenant(slug=slug, name=(name or slug).strip()[:200], monthly_ai_budget_usd=max(0.0, budget))
    s.add(t)
    s.flush()
    url = None
    if admin_email:
        raw = auth_svc.create_invite(s, t.id, admin_email, "admin")
        url = f"{get_settings().frontend_url.rstrip('/')}/invite/{raw}"
    return t, url


@router.get("/tenants")
def tenants(request: Request):
    require_platform_admin(request)
    with session_scope() as s:
        return {"data": [_tenant(s, t) for t in s.scalars(select(Tenant).order_by(Tenant.created_at))]}


@router.post("/tenants", status_code=201)
def new_tenant(request: Request, body: dict = Body(...)):
    require_platform_admin(request)
    with session_scope() as s:
        t, url = create_tenant(s, body.get("slug"), body.get("name"), body.get("monthly_ai_budget_usd"),
                               body.get("admin_email"))
        return {"data": {"tenant": _tenant(s, t), "invite_url": url}}


@router.patch("/tenants/{tid}")
def patch_tenant(tid: str, request: Request, body: dict = Body(...)):
    require_platform_admin(request)
    with session_scope() as s:
        t = s.get(Tenant, tid)
        if t is None:
            raise APIError(404, "not_found", "tenant not found")
        if "name" in body:
            t.name = str(body["name"]).strip()[:200] or t.name
        if "status" in body:
            if body["status"] not in ("active", "suspended"):
                raise APIError(422, "invalid_input", "status must be active or suspended")
            t.status = body["status"]
        if "monthly_ai_budget_usd" in body:
            try:
                t.monthly_ai_budget_usd = max(0.0, float(body["monthly_ai_budget_usd"]))
            except (TypeError, ValueError):
                raise APIError(422, "invalid_input", "monthly_ai_budget_usd must be a number")
        return {"data": _tenant(s, t)}


@router.post("/tenants/{tid}/invite")
def invite_to_tenant(tid: str, request: Request, body: dict = Body(...)):
    require_platform_admin(request)
    with session_scope() as s:
        if s.get(Tenant, tid) is None:
            raise APIError(404, "not_found", "tenant not found")
        try:
            raw = auth_svc.create_invite(s, tid, str(body.get("email") or ""), str(body.get("role") or "admin"))
        except ValueError as e:
            raise APIError(422, "invalid_input", str(e))
        return {"data": {"invite_url": f"{get_settings().frontend_url.rstrip('/')}/invite/{raw}"}}


# ------------------------------------------------------------------ access requests

def _ar(a: AccessRequest) -> dict:
    return {"id": a.id, "name": a.name, "email": a.email, "organisation": a.organisation, "use_case": a.use_case,
            "status": a.status, "tenant_id": a.tenant_id, "created_at": a.created_at.isoformat()}


@router.get("/access-requests")
def access_requests(request: Request, status: str | None = "pending"):
    require_platform_admin(request)
    with session_scope() as s:
        q = select(AccessRequest).order_by(AccessRequest.created_at.desc())
        if status:
            q = q.where(AccessRequest.status == status)
        return {"data": [_ar(a) for a in s.scalars(q)]}


@router.post("/access-requests/{aid}/approve")
def approve(aid: str, request: Request, body: dict = Body(...)):
    require_platform_admin(request)
    with session_scope() as s:
        a = s.get(AccessRequest, aid)
        if a is None or a.status != "pending":
            raise APIError(404, "not_found", "no pending request with that id")
        t, url = create_tenant(s, body.get("slug"), body.get("name") or a.organisation or a.name,
                               body.get("monthly_ai_budget_usd"), a.email)
        a.status, a.tenant_id = "approved", t.id
        return {"data": {"tenant": _tenant(s, t), "invite_url": url}}


@router.post("/access-requests/{aid}/reject")
def reject(aid: str, request: Request):
    require_platform_admin(request)
    with session_scope() as s:
        a = s.get(AccessRequest, aid)
        if a is None or a.status != "pending":
            raise APIError(404, "not_found", "no pending request with that id")
        a.status = "rejected"
        return {"data": _ar(a)}


# ------------------------------------------------------------------ shared AI providers

def _shared(s, pid) -> AIProvider:
    p = s.get(AIProvider, pid)
    if p is None or p.tenant_id is not None:
        raise APIError(404, "not_found", "provider not found")
    return p


@router.get("/ai/providers")
def shared_providers(request: Request):
    require_platform_admin(request)
    with session_scope() as s:
        rows = s.scalars(select(AIProvider).where(AIProvider.tenant_id.is_(None)).order_by(AIProvider.created_at))
        return {"data": [ai_svc.public(p) for p in rows]}


@router.post("/ai/providers", status_code=201)
def add_shared(request: Request, body: dict = Body(...)):
    require_platform_admin(request)
    with session_scope() as s:
        try:
            return {"data": ai_svc.public(ai_svc.create_provider(s, None, body))}
        except ValueError as e:
            raise APIError(422, "invalid_input", str(e))


@router.patch("/ai/providers/{pid}")
def patch_shared(pid: str, request: Request, body: dict = Body(...)):
    require_platform_admin(request)
    with session_scope() as s:
        try:
            return {"data": ai_svc.public(ai_svc.update_provider(s, _shared(s, pid), body))}
        except ValueError as e:
            raise APIError(422, "invalid_input", str(e))


@router.delete("/ai/providers/{pid}", status_code=204)
def delete_shared(pid: str, request: Request):
    require_platform_admin(request)
    with session_scope() as s:
        s.delete(_shared(s, pid))
    return Response(status_code=204)


@router.post("/ai/providers/{pid}/test")
def test_shared(pid: str, request: Request):
    require_platform_admin(request)
    with session_scope() as s:
        p = _shared(s, pid)
        s.expunge(p)
    return {"data": ai_svc.test_provider(p)}


@router.get("/usage")
def usage(request: Request, days: int = 30):
    require_platform_admin(request)
    with session_scope() as s:
        out = usage_summary(s, None, max(1, min(days, 366)))
        slugs = {t.id: t.slug for t in s.scalars(select(Tenant))}
        for row in out["by_tenant"]:
            row["slug"] = slugs.get(row["tenant_id"])
        out.pop("by_purpose")
        return {"data": out}
