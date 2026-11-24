from __future__ import annotations

import threading
import time
from collections import defaultdict, deque

from fastapi import APIRouter, Body, Request, Response
from fastapi.responses import JSONResponse
from sqlalchemy import select, text

from scraper0 import __version__
from scraper0.api.deps import SESSION_COOKIE, APIError, resolve
from scraper0.config import get_settings
from scraper0.core.entities import BUILTIN_SPECS
from scraper0.db.models import AccessRequest, User
from scraper0.db.session import session_scope
from scraper0.services import auth as auth_svc

router = APIRouter()

_rl: dict[str, deque] = defaultdict(deque)
_rl_lock = threading.Lock()


def _rate_limit(key: str, limit: int, window_s: int) -> None:
    nowt = time.monotonic()
    with _rl_lock:
        q = _rl[key]
        while q and nowt - q[0] > window_s:
            q.popleft()
        if len(q) >= limit:
            raise APIError(429, "rate_limited", "too many requests, try again later")
        q.append(nowt)


def client_ip(request: Request) -> str:
    fwd = request.headers.get("x-forwarded-for", "")
    return fwd.split(",")[0].strip() if fwd else (request.client.host if request.client else "?")


@router.get("/api/health")
def health():
    db = "ok"
    try:
        with session_scope() as s:
            s.execute(text("SELECT 1"))
    except Exception:
        db = "error"
    body = {"status": "ok" if db == "ok" else "degraded", "message": "Service is running", "db": db,
            "version": __version__}
    return JSONResponse(body, status_code=200 if db == "ok" else 503)


@router.get("/v1/meta")
def meta():
    from scraper0.connectors.base import all_connectors
    return {"data": {"version": __version__, "entities": list(BUILTIN_SPECS),
                     "connectors": [{"name": c.name, "source": c.source, "description": c.description,
                                     "actions": sorted(c.actions)} for c in all_connectors()]}}


@router.post("/v1/access-requests", status_code=201)
def access_request(request: Request, body: dict = Body(...)):
    _rate_limit(f"ar:{client_ip(request)}", 5, 3600)
    name, email = str(body.get("name") or "").strip(), str(body.get("email") or "").strip().lower()
    if not name or "@" not in email or len(email) > 320 or len(name) > 200:
        raise APIError(422, "invalid_input", "name and a valid email are required")
    with session_scope() as s:
        ar = AccessRequest(name=name, email=email, organisation=str(body.get("organisation") or "")[:200],
                           use_case=str(body.get("use_case") or "")[:4000])
        s.add(ar)
        s.flush()
        return {"data": {"id": ar.id, "status": "pending"}}


# ------------------------------------------------------------------ auth

def _set_cookie(resp: Response, raw: str):
    st = get_settings()
    resp.set_cookie(SESSION_COOKIE, raw, max_age=st.session_days * 86400, httponly=True, samesite="lax",
                    secure=st.cookie_secure, path="/")


def _user(u: User) -> dict:
    return {"id": u.id, "email": u.email, "name": u.name, "is_platform_admin": u.is_platform_admin}


@router.post("/v1/auth/login")
def login(request: Request, response: Response, body: dict = Body(...)):
    _rate_limit(f"login:{client_ip(request)}", 20, 900)
    with session_scope() as s:
        u = auth_svc.authenticate(s, str(body.get("email") or ""), str(body.get("password") or ""))
        if u is None:
            raise APIError(401, "invalid_credentials", "wrong email or password")
        raw = auth_svc.create_session(s, u, get_settings().session_days)
        _set_cookie(response, raw)
        return {"data": {"user": _user(u)}}


@router.post("/v1/auth/logout", status_code=204)
def logout(request: Request):
    with session_scope() as s:
        auth_svc.end_session(s, request.cookies.get(SESSION_COOKIE))
    resp = Response(status_code=204)
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@router.get("/v1/auth/me")
def me(request: Request):
    p = resolve(request, need_tenant=False)
    if p.user is None:
        raise APIError(400, "not_a_user", "API keys have no user; use GET /v1/tenant")
    with session_scope() as s:
        u = s.get(User, p.user.id)
        tenants = [{"id": t.id, "slug": t.slug, "name": t.name, "role": r, "status": t.status}
                   for t, r in auth_svc.memberships(s, u)]
        return {"data": {"user": _user(u), "tenants": tenants}}


@router.post("/v1/auth/accept-invite")
def accept_invite(request: Request, response: Response, body: dict = Body(...)):
    _rate_limit(f"invite:{client_ip(request)}", 20, 900)
    with session_scope() as s:
        try:
            u = auth_svc.accept_invite(s, str(body.get("token") or ""), str(body.get("name") or ""),
                                       str(body.get("password") or ""))
        except ValueError as e:
            raise APIError(400, "invalid_invite", str(e))
        _set_cookie(response, auth_svc.create_session(s, u, get_settings().session_days))
        return {"data": {"user": _user(u)}}


@router.post("/v1/auth/password")
def change_password(request: Request, body: dict = Body(...)):
    p = resolve(request, need_tenant=False)
    if p.user is None:
        raise APIError(400, "not_a_user", "API keys have no password")
    _rate_limit(f"pw:{p.user.id}", 10, 900)
    with session_scope() as s:
        u = auth_svc.authenticate(s, p.user.email, str(body.get("current_password") or ""))
        if u is None:
            raise APIError(401, "invalid_credentials", "current password is wrong")
        try:
            u.password_hash = auth_svc.hash_password(str(body.get("new_password") or ""))
        except ValueError as e:
            raise APIError(422, "invalid_input", str(e))
        from sqlalchemy import delete

        from scraper0.db.models import Session as DbSession
        s.execute(delete(DbSession).where(DbSession.user_id == u.id))   # log out everywhere
        raw = auth_svc.create_session(s, u, get_settings().session_days)
    resp = JSONResponse({"data": {"ok": True}})
    _set_cookie(resp, raw)
    return resp


def users_by_ids(s, ids):
    return {u.id: u for u in s.scalars(select(User).where(User.id.in_(ids)))}
