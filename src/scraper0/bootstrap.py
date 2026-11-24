"""Startup: migrate, then seed what an empty install needs (platform admin, shared Groq provider,
legacy tenant). Every step is idempotent."""
from __future__ import annotations

import os

import structlog
from sqlalchemy import select

from scraper0.config import REPO_ROOT, get_settings
from scraper0.db.models import AIProvider, Tenant, User
from scraper0.db.session import get_engine, session_scope

log = structlog.get_logger(__name__)

GROQ_PRICES = (0.11, 0.34)   # llama-4-scout USD / 1M tokens (editable in the dashboard)


def migrate() -> None:
    from alembic import command
    from alembic.config import Config
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["connection_engine"] = get_engine()
    if get_engine().dialect.name == "postgresql":
        with get_engine().connect() as c:   # one migrator at a time across instances
            c.exec_driver_sql("SELECT pg_advisory_lock(735001)")
            try:
                command.upgrade(cfg, "head")
            finally:
                c.exec_driver_sql("SELECT pg_advisory_unlock(735001)")
                c.commit()
    else:
        command.upgrade(cfg, "head")


def seed() -> None:
    from scraper0.services import ai as ai_svc
    from scraper0.services import auth as auth_svc
    st = get_settings()
    with session_scope() as s:
        if st.bootstrap_admin_email and st.bootstrap_admin_password:
            email = st.bootstrap_admin_email.strip().lower()
            if s.scalar(select(User).where(User.email == email)) is None:
                auth_svc.create_user(s, email, st.bootstrap_admin_password.get_secret_value(), "Admin", True)
                log.info("bootstrap_admin_created", email=email)
        key = st.bootstrap_groq_api_key.get_secret_value() if st.bootstrap_groq_api_key else os.getenv("GROQ_API_KEY")
        if key and s.scalar(select(AIProvider).where(AIProvider.tenant_id.is_(None))) is None:
            ai_svc.create_provider(s, None, {"name": "Groq (shared)", "kind": "groq", "model": st.bootstrap_groq_model,
                                             "vision_model": st.bootstrap_groq_model, "api_key": key,
                                             "price_in_per_mtok": GROQ_PRICES[0], "price_out_per_mtok": GROQ_PRICES[1],
                                             "is_default": True})
            log.info("bootstrap_groq_provider_created")
        slug = (st.bootstrap_legacy_tenant or "").strip()
        if slug and s.scalar(select(Tenant).where(Tenant.slug == slug)) is None:
            s.add(Tenant(slug=slug, name=slug, monthly_ai_budget_usd=50.0))
            log.info("bootstrap_tenant_created", slug=slug)


def startup() -> None:
    if get_settings().auto_migrate:
        migrate()
    seed()
