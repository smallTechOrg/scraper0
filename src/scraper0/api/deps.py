"""Errors, authentication and authorisation for the HTTP layer."""
from __future__ import annotations

import hmac
from dataclasses import dataclass

from fastapi import Request
from fastapi.responses import JSONResponse

from scraper0.config import get_settings
from scraper0.db.models import Membership, Tenant, User
from scraper0.db.session import session_scope
from scraper0.services import auth as auth_svc

SESSION_COOKIE = "s0_session"
CSRF_HEADER = "x-requested-with"
CSRF_VALUE = "scraper0"


class APIError(Exception):
    def __init__(self, status: int, code: str, message: str):
        self.status, self.code, self.message = status, code, message


def error_response(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


@dataclass
class TenantRef:
    id: str
    slug: str
    name: str
    status: str


@dataclass
class UserRef:
    id: str
    email: str
    name: str
    is_platform_admin: bool


@dataclass
class Principal:
    user: UserRef | None
    tenant: TenantRef | None
    role: str            # admin | member | service (API key) | none
    via: str             # session | api_key

    @property
    def is_platform_admin(self) -> bool:
        return bool(self.user and self.user.is_platform_admin)

    @property
    def tenant_id(self) -> str:
        if self.tenant is None:
            raise APIError(400, "no_tenant", "you are not a member of any tenant")
        return self.tenant.id


def _tref(t: Tenant) -> TenantRef:
    return TenantRef(t.id, t.slug, t.name, t.status)


def _uref(u: User) -> UserRef:
    return UserRef(u.id, u.email, u.name, u.is_platform_admin)


def resolve(request: Request, *, need_tenant: bool = True) -> Principal:
    key = request.headers.get("x-api-key")
    if key:
        with session_scope() as s:
            t = auth_svc.api_key_tenant(s, key)
            if t is None:
                raise APIError(401, "invalid_api_key", "invalid or revoked API key")
            p = Principal(None, _tref(t), "service", "api_key")
    else:
        raw = request.cookies.get(SESSION_COOKIE)
        with session_scope() as s:
            u = auth_svc.session_user(s, raw)
            if u is None:
                raise APIError(401, "unauthenticated", "log in or send X-API-Key")
            if request.method not in ("GET", "HEAD", "OPTIONS") and request.headers.get(CSRF_HEADER) != CSRF_VALUE:
                raise APIError(403, "csrf", f"send header X-Requested-With: {CSRF_VALUE}")
            tenants = auth_svc.memberships(s, u)
            # EventSource can't send headers, so GETs may pass ?tenant= (same membership check)
            wanted = request.headers.get("x-tenant") or (
                request.query_params.get("tenant") if request.method == "GET" else None)
            chosen = None
            if wanted:
                chosen = next(((t, r) for t, r in tenants if t.id == wanted or t.slug == wanted), None)
                if chosen is None and u.is_platform_admin:
                    t = s.get(Tenant, wanted)
                    chosen = (t, "admin") if t else None
                if chosen is None:
                    raise APIError(403, "not_a_member", "you are not a member of that tenant")
            elif tenants:
                chosen = tenants[0]
            p = Principal(_uref(u), _tref(chosen[0]) if chosen else None, chosen[1] if chosen else "none", "session")
    if need_tenant:
        if p.tenant is None:
            raise APIError(400, "no_tenant", "you are not a member of any tenant")
        if p.tenant.status != "active" and not p.is_platform_admin:
            raise APIError(403, "tenant_suspended", "this tenant is suspended")
    return p


def require_admin(p: Principal, allow_service: bool = False) -> None:
    if p.role == "admin" or p.is_platform_admin or (allow_service and p.role == "service"):
        return
    raise APIError(403, "forbidden", "tenant admin role required")


def require_platform_admin(request: Request) -> Principal:
    p = resolve(request, need_tenant=False)
    if not p.is_platform_admin:
        raise APIError(403, "forbidden", "platform admin only")
    return p


def require_internal(request: Request) -> None:
    tok = get_settings().internal_token
    got = request.headers.get("authorization", "").removeprefix("Bearer ").strip()
    if tok is None or not hmac.compare_digest(got, tok.get_secret_value()):
        raise APIError(401, "unauthorized", "internal endpoint")


def membership_role(s, user_id: str, tenant_id: str) -> str | None:
    m = s.get(Membership, (user_id, tenant_id))
    return m.role if m else None
