"""Tenant-scoped management: tenant info, members, API keys, AI providers/usage, entities."""
from __future__ import annotations

from datetime import timedelta

from fastapi import APIRouter, Body, Request, Response
from sqlalchemy import func, select

from scraper0.api.deps import APIError, require_admin, resolve
from scraper0.config import get_settings
from scraper0.db.models import AIProvider, AIUsage, ApiKey, Membership, Tenant, User, now
from scraper0.db.session import session_scope
from scraper0.services import ai as ai_svc
from scraper0.services import auth as auth_svc
from scraper0.services import entities as entity_svc

router = APIRouter(prefix="/v1")


@router.get("/tenant")
def tenant(request: Request):
    p = resolve(request)
    with session_scope() as s:
        t = s.get(Tenant, p.tenant_id)
        return {"data": {"id": t.id, "slug": t.slug, "name": t.name, "status": t.status,
                         "monthly_ai_budget_usd": t.monthly_ai_budget_usd,
                         "ai_spend_this_month_usd": round(ai_svc.total_spend_this_month(s, t.id), 4),
                         "ai_shared_spend_this_month_usd": round(ai_svc.shared_spend_this_month(s, t.id), 4)}}


# ------------------------------------------------------------------ members

@router.get("/members")
def members(request: Request):
    p = resolve(request)
    with session_scope() as s:
        rows = s.execute(select(User, Membership.role).join(Membership, Membership.user_id == User.id)
                         .where(Membership.tenant_id == p.tenant_id).order_by(User.email)).all()
        return {"data": [{"user_id": u.id, "email": u.email, "name": u.name, "role": r} for u, r in rows]}


@router.post("/members/invite")
def invite(request: Request, body: dict = Body(...)):
    p = resolve(request)
    require_admin(p)
    email = str(body.get("email") or "").strip().lower()
    if "@" not in email:
        raise APIError(422, "invalid_input", "valid email required")
    with session_scope() as s:
        try:
            raw = auth_svc.create_invite(s, p.tenant_id, email, str(body.get("role") or "member"))
        except ValueError as e:
            raise APIError(422, "invalid_input", str(e))
        return {"data": {"invite_url": f"{get_settings().frontend_url.rstrip('/')}/invite/{raw}"}}


@router.delete("/members/{user_id}", status_code=204)
def remove_member(user_id: str, request: Request):
    p = resolve(request)
    require_admin(p)
    with session_scope() as s:
        m = s.get(Membership, (user_id, p.tenant_id))
        if m is None:
            raise APIError(404, "not_found", "member not found")
        if m.role == "admin":
            admins = s.scalar(select(func.count()).select_from(Membership).where(
                Membership.tenant_id == p.tenant_id, Membership.role == "admin"))
            if admins <= 1:
                raise APIError(409, "last_admin", "cannot remove the last admin")
        s.delete(m)
    return Response(status_code=204)


# ------------------------------------------------------------------ API keys

def _key(k: ApiKey) -> dict:
    return {"id": k.id, "name": k.name, "prefix": k.prefix, "created_at": k.created_at.isoformat(),
            "last_used_at": k.last_used_at.isoformat() if k.last_used_at else None,
            "revoked_at": k.revoked_at.isoformat() if k.revoked_at else None}


@router.get("/api-keys")
def list_keys(request: Request):
    p = resolve(request)
    with session_scope() as s:
        return {"data": [_key(k) for k in s.scalars(select(ApiKey).where(ApiKey.tenant_id == p.tenant_id)
                                                    .order_by(ApiKey.created_at.desc()))]}


@router.post("/api-keys", status_code=201)
def create_key(request: Request, body: dict = Body(default={})):
    p = resolve(request)
    require_admin(p)
    with session_scope() as s:
        k, raw = auth_svc.create_api_key(s, p.tenant_id, str(body.get("name") or "key"))
        return {"data": {**_key(k), "key": raw}}


@router.delete("/api-keys/{key_id}", status_code=204)
def revoke_key(key_id: str, request: Request):
    p = resolve(request)
    require_admin(p)
    with session_scope() as s:
        k = s.get(ApiKey, key_id)
        if k is None or k.tenant_id != p.tenant_id:
            raise APIError(404, "not_found", "key not found")
        k.revoked_at = k.revoked_at or now()
    return Response(status_code=204)


# ------------------------------------------------------------------ AI providers (tenant BYO) + usage

@router.get("/ai/providers")
def providers(request: Request):
    p = resolve(request)
    with session_scope() as s:
        shared = s.scalars(select(AIProvider).where(AIProvider.tenant_id.is_(None)).order_by(AIProvider.created_at)).all()
        own = s.scalars(select(AIProvider).where(AIProvider.tenant_id == p.tenant_id).order_by(AIProvider.created_at)).all()
        active = ai_svc.resolve_provider(s, p.tenant_id)
        return {"data": {"shared": [ai_svc.public(x) for x in shared], "own": [ai_svc.public(x) for x in own],
                         "active": ai_svc.public(active) if active else None}}


def _own_provider(s, p, pid) -> AIProvider:
    row = s.get(AIProvider, pid)
    if row is None or row.tenant_id != p.tenant_id:
        raise APIError(404, "not_found", "provider not found")
    return row


@router.post("/ai/providers", status_code=201)
def add_provider(request: Request, body: dict = Body(...)):
    p = resolve(request)
    require_admin(p)
    with session_scope() as s:
        try:
            return {"data": ai_svc.public(ai_svc.create_provider(s, p.tenant_id, body))}
        except ValueError as e:
            raise APIError(422, "invalid_input", str(e))


@router.patch("/ai/providers/{pid}")
def patch_provider(pid: str, request: Request, body: dict = Body(...)):
    p = resolve(request)
    require_admin(p)
    with session_scope() as s:
        try:
            return {"data": ai_svc.public(ai_svc.update_provider(s, _own_provider(s, p, pid), body))}
        except ValueError as e:
            raise APIError(422, "invalid_input", str(e))


@router.delete("/ai/providers/{pid}", status_code=204)
def delete_provider(pid: str, request: Request):
    p = resolve(request)
    require_admin(p)
    with session_scope() as s:
        s.delete(_own_provider(s, p, pid))
    return Response(status_code=204)


@router.post("/ai/providers/{pid}/test")
def test_provider(pid: str, request: Request):
    p = resolve(request)
    require_admin(p)
    with session_scope() as s:
        row = _own_provider(s, p, pid)
        s.expunge(row)
    return {"data": ai_svc.test_provider(row)}


def usage_summary(s, tenant_id: str | None, days: int) -> dict:
    since = now() - timedelta(days=days)
    q = select(AIUsage).where(AIUsage.created_at >= since)
    if tenant_id:
        q = q.where(AIUsage.tenant_id == tenant_id)
    rows = s.scalars(q).all()
    by_day: dict[str, dict] = {}
    by_purpose: dict[str, dict] = {}
    by_tenant: dict[str, dict] = {}
    for r in rows:
        day = r.created_at.date().isoformat()
        for bucket, key in ((by_day, day), (by_purpose, r.purpose), (by_tenant, r.tenant_id)):
            b = bucket.setdefault(key, {"usd": 0.0, "calls": 0})
            b["usd"] += r.cost_usd
            b["calls"] += 1
    def rnd(d, name):
        return [{name: k, "usd": round(v["usd"], 6), "calls": v["calls"]} for k, v in sorted(d.items())]
    return {"total_usd": round(sum(r.cost_usd for r in rows), 6), "by_day": rnd(by_day, "day"),
            "by_purpose": rnd(by_purpose, "purpose"), "by_tenant": rnd(by_tenant, "tenant_id")}


@router.get("/ai/usage")
def usage(request: Request, days: int = 30):
    p = resolve(request)
    with session_scope() as s:
        out = usage_summary(s, p.tenant_id, max(1, min(days, 366)))
        out.pop("by_tenant")
        out["budget_usd"] = s.get(Tenant, p.tenant_id).monthly_ai_budget_usd
        return {"data": out}


# ------------------------------------------------------------------ entities

@router.get("/entities")
def entities(request: Request):
    p = resolve(request)
    return {"data": entity_svc.list_specs(p.tenant_id)}


@router.put("/entities/{name}")
def put_entity(name: str, request: Request, body: dict = Body(...)):
    p = resolve(request)
    require_admin(p, allow_service=True)
    try:
        return {"data": entity_svc.put_spec(p.tenant_id, name, body)}
    except (ValueError, TypeError, KeyError) as e:
        raise APIError(422, "invalid_spec", str(e))


@router.delete("/entities/{name}", status_code=204)
def delete_entity(name: str, request: Request):
    p = resolve(request)
    require_admin(p, allow_service=True)
    if not entity_svc.delete_spec(p.tenant_id, name):
        raise APIError(404, "not_found", "entity not found")
    return Response(status_code=204)
