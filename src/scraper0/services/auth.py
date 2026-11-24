"""Passwords (argon2), dashboard sessions, tenant API keys, invites."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError
from sqlalchemy import select

from scraper0.core.crypto import sha256, token
from scraper0.db.models import ApiKey, Invite, Membership, Session, Tenant, User, now

_ph = PasswordHasher()
_DUMMY = _ph.hash("timing-equaliser")
API_KEY_PREFIX = "s0_"
MIN_PASSWORD = 10


def _aware(dt: datetime) -> datetime:
    # SQLite returns naive datetimes; everything we store is UTC
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def hash_password(pw: str) -> str:
    if len(pw) < MIN_PASSWORD:
        raise ValueError(f"password must be at least {MIN_PASSWORD} characters")
    return _ph.hash(pw)


def authenticate(s, email: str, password: str) -> User | None:
    u = s.scalar(select(User).where(User.email == email.strip().lower()))
    try:
        _ph.verify(u.password_hash if u else _DUMMY, password)
    except (VerificationError, InvalidHashError):
        return None
    if u is None:
        return None
    u.last_login_at = now()
    return u


def create_user(s, email: str, password: str, name: str = "", platform_admin: bool = False) -> User:
    email = email.strip().lower()
    if "@" not in email:
        raise ValueError("invalid email")
    if s.scalar(select(User).where(User.email == email)):
        raise ValueError("a user with this email already exists")
    u = User(email=email, name=name, password_hash=hash_password(password), is_platform_admin=platform_admin)
    s.add(u)
    s.flush()
    return u


# ------------------------------------------------------------------ sessions

def create_session(s, user: User, days: int) -> str:
    raw = token()
    s.add(Session(id=sha256(raw), user_id=user.id, expires_at=now() + timedelta(days=days)))
    return raw


def session_user(s, raw: str | None) -> User | None:
    if not raw:
        return None
    sess = s.get(Session, sha256(raw))
    if sess is None or _aware(sess.expires_at) < now():
        return None
    return s.get(User, sess.user_id)


def end_session(s, raw: str | None) -> None:
    if raw:
        sess = s.get(Session, sha256(raw))
        if sess:
            s.delete(sess)


# ------------------------------------------------------------------ API keys

def create_api_key(s, tenant_id: str, name: str) -> tuple[ApiKey, str]:
    raw = API_KEY_PREFIX + token(30)
    k = ApiKey(tenant_id=tenant_id, name=name[:100] or "key", prefix=raw[:10], key_hash=sha256(raw))
    s.add(k)
    s.flush()
    return k, raw


def api_key_tenant(s, raw: str | None) -> Tenant | None:
    if not raw or not raw.startswith(API_KEY_PREFIX):
        return None
    k = s.scalar(select(ApiKey).where(ApiKey.key_hash == sha256(raw)))
    if k is None or k.revoked_at is not None:
        return None
    if k.last_used_at is None or _aware(k.last_used_at) < now() - timedelta(minutes=5):
        k.last_used_at = now()      # throttled write
    return s.get(Tenant, k.tenant_id)


# ------------------------------------------------------------------ invites

def create_invite(s, tenant_id: str, email: str, role: str = "admin", days: int = 7) -> str:
    if role not in ("admin", "member"):
        raise ValueError("role must be admin or member")
    raw = token(24)
    s.add(Invite(email=email.strip().lower(), tenant_id=tenant_id, role=role, token_hash=sha256(raw),
                 expires_at=now() + timedelta(days=days)))
    return raw


def accept_invite(s, raw: str, name: str, password: str) -> User:
    inv = s.scalar(select(Invite).where(Invite.token_hash == sha256(raw or "")))
    if inv is None or inv.accepted_at is not None or _aware(inv.expires_at) < now():
        raise ValueError("this invite is invalid or has expired")
    user = s.scalar(select(User).where(User.email == inv.email))
    if user is None:
        user = create_user(s, inv.email, password, name)
    if not s.get(Membership, (user.id, inv.tenant_id)):
        s.add(Membership(user_id=user.id, tenant_id=inv.tenant_id, role=inv.role))
    inv.accepted_at = now()
    s.flush()
    return user


def memberships(s, user: User) -> list[tuple[Tenant, str]]:
    rows = s.execute(select(Tenant, Membership.role).join(Membership, Membership.tenant_id == Tenant.id)
                     .where(Membership.user_id == user.id).order_by(Tenant.name)).all()
    return [(t, r) for t, r in rows]
