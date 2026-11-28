"""All tables. Learning tables (recipes, crawl_hints, page_cache) are keyed by `entity_key`:
built-in entities ("event") share learned layouts across every tenant — a public page only has to be
learned once for everybody. Custom entities use "<tenant_id>:<name>" so tenants never see each other's.
"""
from __future__ import annotations

import uuid
from datetime import datetime, timezone

from sqlalchemy import JSON, Boolean, DateTime, Float, ForeignKey, Index, Integer, LargeBinary, String, Text, UniqueConstraint
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


def now() -> datetime:
    return datetime.now(timezone.utc)


def new_id() -> str:
    return uuid.uuid4().hex


class Base(DeclarativeBase):
    type_annotation_map = {dict: JSON, list: JSON}


TS = DateTime(timezone=True)


# ------------------------------------------------------------------ tenancy & auth

class Tenant(Base):
    __tablename__ = "tenants"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    slug: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16), default="active")          # active | suspended
    monthly_ai_budget_usd: Mapped[float] = mapped_column(Float, default=20.0)
    created_at: Mapped[datetime] = mapped_column(TS, default=now)


class User(Base):
    __tablename__ = "users"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320), unique=True)
    name: Mapped[str] = mapped_column(String(200), default="")
    password_hash: Mapped[str] = mapped_column(String(255))
    is_platform_admin: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(TS, default=now)
    last_login_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)


class Membership(Base):
    __tablename__ = "memberships"
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), primary_key=True)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), primary_key=True)
    role: Mapped[str] = mapped_column(String(16), default="member")            # admin | member


class Session(Base):
    __tablename__ = "sessions"
    id: Mapped[str] = mapped_column(String(64), primary_key=True)                # sha256 of the cookie token
    user_id: Mapped[str] = mapped_column(ForeignKey("users.id", ondelete="CASCADE"), index=True)
    expires_at: Mapped[datetime] = mapped_column(TS)
    created_at: Mapped[datetime] = mapped_column(TS, default=now)


class Invite(Base):
    __tablename__ = "invites"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    email: Mapped[str] = mapped_column(String(320))
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"))
    role: Mapped[str] = mapped_column(String(16), default="admin")
    token_hash: Mapped[str] = mapped_column(String(64), unique=True)
    expires_at: Mapped[datetime] = mapped_column(TS)
    accepted_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=now)


class AccessRequest(Base):
    __tablename__ = "access_requests"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    name: Mapped[str] = mapped_column(String(200))
    email: Mapped[str] = mapped_column(String(320))
    organisation: Mapped[str] = mapped_column(String(200), default="")
    use_case: Mapped[str] = mapped_column(Text, default="")
    status: Mapped[str] = mapped_column(String(16), default="pending")          # pending | approved | rejected
    tenant_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=now)


class ApiKey(Base):
    __tablename__ = "api_keys"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(100))
    prefix: Mapped[str] = mapped_column(String(16))                              # shown in UI: "s0_ab12…"
    key_hash: Mapped[str] = mapped_column(String(64), unique=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=now)
    last_used_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)


# ------------------------------------------------------------------ AI

class AIProvider(Base):
    """tenant_id NULL = platform-shared provider (managed by the platform admin);
    set = a tenant's own key (BYO), which takes precedence for that tenant."""
    __tablename__ = "ai_providers"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str | None] = mapped_column(ForeignKey("tenants.id", ondelete="CASCADE"), nullable=True, index=True)
    name: Mapped[str] = mapped_column(String(100))
    kind: Mapped[str] = mapped_column(String(32))                                # groq | anthropic | openai_compat
    base_url: Mapped[str | None] = mapped_column(String(500), nullable=True)
    model: Mapped[str] = mapped_column(String(200))
    vision_model: Mapped[str | None] = mapped_column(String(200), nullable=True)
    api_key_enc: Mapped[str] = mapped_column(Text)
    price_in_per_mtok: Mapped[float] = mapped_column(Float, default=0.0)
    price_out_per_mtok: Mapped[float] = mapped_column(Float, default=0.0)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=now)


class AIUsage(Base):
    __tablename__ = "ai_usage"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    provider_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    model: Mapped[str] = mapped_column(String(200))
    purpose: Mapped[str] = mapped_column(String(32))                             # extract | vision | filter | captcha
    input_tokens: Mapped[int] = mapped_column(Integer, default=0)
    output_tokens: Mapped[int] = mapped_column(Integer, default=0)
    cost_usd: Mapped[float] = mapped_column(Float, default=0.0)
    byo: Mapped[bool] = mapped_column(Boolean, default=False)
    job_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=now, index=True)


# ------------------------------------------------------------------ connectors

class ConnectorState(Base):
    """Encrypted per-tenant state a connector must keep between runs (e.g. BBMP login cookies) —
    Cloud Run's filesystem is ephemeral, so nothing lives on disk."""
    __tablename__ = "connector_state"
    tenant_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    connector: Mapped[str] = mapped_column(String(64), primary_key=True)
    key: Mapped[str] = mapped_column(String(200), primary_key=True)
    value_enc: Mapped[str] = mapped_column(Text)
    updated_at: Mapped[datetime] = mapped_column(TS, default=now)


# ------------------------------------------------------------------ learning

class EntitySpecRow(Base):
    __tablename__ = "entity_specs"
    tenant_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    name: Mapped[str] = mapped_column(String(64), primary_key=True)
    spec: Mapped[dict] = mapped_column(JSON)
    version: Mapped[int] = mapped_column(Integer, default=1)
    updated_at: Mapped[datetime] = mapped_column(TS, default=now)


class Recipe(Base):
    __tablename__ = "recipes"
    __table_args__ = (UniqueConstraint("domain", "path_pattern", "entity_key", name="uq_recipe"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    domain: Mapped[str] = mapped_column(String(255), index=True)
    path_pattern: Mapped[str] = mapped_column(String(500))
    entity_key: Mapped[str] = mapped_column(String(120))
    strategy: Mapped[str] = mapped_column(String(32), default="selectors")
    rules: Mapped[dict] = mapped_column(JSON)
    sample_url: Mapped[str | None] = mapped_column(Text, nullable=True)
    successes: Mapped[int] = mapped_column(Integer, default=0)
    failures: Mapped[int] = mapped_column(Integer, default=0)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    status: Mapped[str] = mapped_column(String(16), default="active")            # active | stale
    spec_version: Mapped[int] = mapped_column(Integer, default=1)
    created_at: Mapped[datetime] = mapped_column(TS, default=now)
    updated_at: Mapped[datetime] = mapped_column(TS, default=now)


class CrawlHint(Base):
    __tablename__ = "crawl_hints"
    __table_args__ = (UniqueConstraint("seed", "entity_key", name="uq_hint"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    seed: Mapped[str] = mapped_column(String(2000))
    entity_key: Mapped[str] = mapped_column(String(120))
    patterns: Mapped[dict] = mapped_column(JSON)
    updated_at: Mapped[datetime] = mapped_column(TS, default=now)


class PageCache(Base):
    __tablename__ = "page_cache"
    __table_args__ = (UniqueConstraint("url", "entity_key", name="uq_page"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    url: Mapped[str] = mapped_column(String(2000))
    entity_key: Mapped[str] = mapped_column(String(120))
    content_hash: Mapped[str] = mapped_column(String(64))
    items: Mapped[list] = mapped_column(JSON)
    extraction: Mapped[str | None] = mapped_column(String(32), nullable=True)
    fetched_at: Mapped[datetime] = mapped_column(TS, default=now)


# ------------------------------------------------------------------ jobs, sources, results

class Job(Base):
    __tablename__ = "jobs"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    kind: Mapped[str] = mapped_column(String(32))                                # crawl | connector | ingest
    status: Mapped[str] = mapped_column(String(16), default="queued", index=True)  # queued|running|succeeded|failed|cancelled
    input: Mapped[dict] = mapped_column(JSON)
    result: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    progress: Mapped[dict] = mapped_column(JSON, default=dict)
    source_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    attempts: Mapped[int] = mapped_column(Integer, default=0)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(TS, default=now, index=True)
    started_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    heartbeat_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)


class JobFile(Base):
    """Uploaded bytes for an ingest job; deleted when the job finishes (any worker can run the job)."""
    __tablename__ = "job_files"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(32), index=True)
    filename: Mapped[str] = mapped_column(String(300))
    data: Mapped[bytes] = mapped_column(LargeBinary)


class JobEvent(Base):
    __tablename__ = "job_events"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    job_id: Mapped[str] = mapped_column(String(32), index=True)
    type: Mapped[str] = mapped_column(String(32))                                # log | progress | item | done
    data: Mapped[dict] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(TS, default=now)


class Source(Base):
    __tablename__ = "sources"
    id: Mapped[str] = mapped_column(String(32), primary_key=True, default=new_id)
    tenant_id: Mapped[str] = mapped_column(String(32), index=True)
    name: Mapped[str] = mapped_column(String(200))
    kind: Mapped[str] = mapped_column(String(32))                                # crawl | connector
    config: Mapped[dict] = mapped_column(JSON)
    interval_minutes: Mapped[int] = mapped_column(Integer, default=1440)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    next_run_at: Mapped[datetime | None] = mapped_column(TS, nullable=True, index=True)
    last_job_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=now)


class Item(Base):
    """Every stored item, in one table. A site item (site_id + action set) is shared by every tenant: whichever run
    scrapes that site (a v2 refresh, a v1 crawl or connector job, any tenant) updates the same row, and its id
    is itm_ + sha256(site_id|action|item_key)[:24]. A private item (tenant_id set, action null: uploads, and
    tenant-defined entities) is visible to that tenant only; its id hashes "tenant:" + tenant_id instead."""
    __tablename__ = "items"
    __table_args__ = (UniqueConstraint("site_id", "action", "item_key", name="uq_item_site"),
                      UniqueConstraint("tenant_id", "entity_key", "item_key", name="uq_item_tenant"),
                      Index("ix_items_action", "site_id", "action", "seen_seq"),
                      Index("ix_items_last_seen", "last_seen_at"))
    id: Mapped[str] = mapped_column(String(32), primary_key=True)
    site_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    action: Mapped[str | None] = mapped_column(String(64), nullable=True)
    entity_key: Mapped[str] = mapped_column(String(120))
    tenant_id: Mapped[str | None] = mapped_column(String(32), nullable=True)   # set only on private items
    item_key: Mapped[str] = mapped_column(String(500))                           # canonical link or fingerprint
    data: Mapped[dict] = mapped_column(JSON)
    content_hash: Mapped[str] = mapped_column(String(64))
    first_seen_at: Mapped[datetime] = mapped_column(TS, default=now)
    last_seen_at: Mapped[datetime] = mapped_column(TS, default=now)              # any run that saw it
    updated_at: Mapped[datetime] = mapped_column(TS, default=now)                # its content last changed
    seen_seq: Mapped[int] = mapped_column(Integer, default=0)                    # SiteAction.scrape_seq of the last v2 refresh that saw it
    job_id: Mapped[str | None] = mapped_column(String(32), nullable=True)        # last job that saw it


class ItemChange(Base):
    """Append-only change log for all items; its id is the change-feed cursor. Each tenant's feed shows the
    changes of shared site items plus its own private items."""
    __tablename__ = "item_changes"
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    item_id: Mapped[str] = mapped_column(String(32), index=True)
    change: Mapped[str] = mapped_column(String(16))                              # created | updated
    created_at: Mapped[datetime] = mapped_column(TS, default=now)


# ------------------------------------------------------------------ v2: sites and actions (shared across tenants)

class Site(Base):
    """A website, shared by every tenant. site_id = host without www., dots → dashes."""
    __tablename__ = "sites"
    site_id: Mapped[str] = mapped_column(String(255), primary_key=True)
    base_url: Mapped[str] = mapped_column(String(500))
    name: Mapped[str] = mapped_column(String(255))
    created_by_tenant: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=now)


class SiteAction(Base):
    """Something scraper0 can do on a site. Refresh bookkeeping (one in flight, freshness, health) lives here."""
    __tablename__ = "site_actions"
    __table_args__ = (UniqueConstraint("site_id", "name", name="uq_site_action"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    site_id: Mapped[str] = mapped_column(ForeignKey("sites.site_id", ondelete="CASCADE"), index=True)
    name: Mapped[str] = mapped_column(String(64))
    kind: Mapped[str] = mapped_column(String(16))                                # read | write
    mode: Mapped[str] = mapped_column(String(16), default="list")               # list | lookup | write
    entity: Mapped[str] = mapped_column(String(64), default="event")
    implementation: Mapped[str] = mapped_column(String(16), default="learned")  # learned | code
    connector: Mapped[str | None] = mapped_column(String(64), nullable=True)
    connector_action: Mapped[str | None] = mapped_column(String(64), nullable=True)
    seed_urls: Mapped[list] = mapped_column(JSON, default=list)
    status: Mapped[str] = mapped_column(String(16), default="draft")            # draft | locked | broken
    locked_version: Mapped[int | None] = mapped_column(Integer, nullable=True)
    input_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    output_schema: Mapped[dict] = mapped_column(JSON, default=dict)
    last_success_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    last_failure_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    consecutive_failures: Mapped[int] = mapped_column(Integer, default=0)
    last_item_count: Mapped[int | None] = mapped_column(Integer, nullable=True)
    last_scraped_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)  # last successful scrape (freshness)
    scrape_seq: Mapped[int] = mapped_column(Integer, default=0)                 # bumps on each successful scrape
    refresh_job_id: Mapped[str | None] = mapped_column(String(32), nullable=True)  # the one refresh in flight
    created_by_tenant: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=now)


class ActionVersion(Base):
    __tablename__ = "action_versions"
    __table_args__ = (UniqueConstraint("action_id", "version", name="uq_action_version"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    action_id: Mapped[int] = mapped_column(ForeignKey("site_actions.id", ondelete="CASCADE"), index=True)
    version: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(16), default="draft")            # draft | locked | retired | broken
    preview: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    implementation_snapshot: Mapped[dict] = mapped_column(JSON, default=dict)
    job_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=now)
    locked_at: Mapped[datetime | None] = mapped_column(TS, nullable=True)
    locked_by: Mapped[str | None] = mapped_column(String(320), nullable=True)


class AIVerdict(Base):
    """Per-tenant AI filter/category verdict for one item content under one option set."""
    __tablename__ = "ai_verdicts"
    tenant_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    item_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    content_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    options_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    selected: Mapped[bool] = mapped_column(Boolean, default=True)
    category: Mapped[str | None] = mapped_column(String(100), nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=now)


class WriteRecord(Base):
    """Idempotency record of a write: one per (tenant, site, action, key)."""
    __tablename__ = "write_records"
    __table_args__ = (UniqueConstraint("tenant_id", "site_id", "action", "idem_key", name="uq_write_record"),)
    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    tenant_id: Mapped[str] = mapped_column(String(32))
    site_id: Mapped[str] = mapped_column(String(255))
    action: Mapped[str] = mapped_column(String(64))
    idem_key: Mapped[str] = mapped_column(String(200))
    status: Mapped[str] = mapped_column(String(16))           # running | succeeded | outcome_unknown | failed
    job_id: Mapped[str | None] = mapped_column(String(32), nullable=True)
    submit_started: Mapped[bool] = mapped_column(Boolean, default=False)
    output: Mapped[dict | None] = mapped_column(JSON, nullable=True)
    error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(TS, default=now)
    updated_at: Mapped[datetime] = mapped_column(TS, default=now)
