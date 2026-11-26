"""Tenant-aware AI access: provider resolution (tenant BYO first, else platform-shared default),
monthly budget enforcement on shared spend, and per-call metering into ai_usage."""
from __future__ import annotations

import time
from datetime import datetime, timezone

import structlog
from sqlalchemy import func, select

from scraper0.core import llm
from scraper0.core.crypto import decrypt, encrypt, hint
from scraper0.db.models import AIProvider, AIUsage, Tenant
from scraper0.db.session import session_scope

log = structlog.get_logger(__name__)

PROVIDER_FIELDS = ("name", "kind", "model", "vision_model", "base_url", "price_in_per_mtok", "price_out_per_mtok",
                   "enabled", "is_default")


class BudgetExceeded(llm.LLMError):
    pass


class NoProvider(llm.LLMError):
    pass


def month_start(now: datetime | None = None) -> datetime:
    n = now or datetime.now(timezone.utc)
    return n.replace(day=1, hour=0, minute=0, second=0, microsecond=0)


def shared_spend_this_month(s, tenant_id: str) -> float:
    return float(s.scalar(select(func.coalesce(func.sum(AIUsage.cost_usd), 0.0)).where(
        AIUsage.tenant_id == tenant_id, AIUsage.byo.is_(False), AIUsage.created_at >= month_start())) or 0.0)


def total_spend_this_month(s, tenant_id: str) -> float:
    return float(s.scalar(select(func.coalesce(func.sum(AIUsage.cost_usd), 0.0)).where(
        AIUsage.tenant_id == tenant_id, AIUsage.created_at >= month_start())) or 0.0)


def _pick(rows: list[AIProvider]) -> AIProvider | None:
    rows = [r for r in rows if r.enabled]
    return next((r for r in rows if r.is_default), rows[0] if rows else None)


def resolve_provider(s, tenant_id: str) -> AIProvider | None:
    own = s.scalars(select(AIProvider).where(AIProvider.tenant_id == tenant_id).order_by(AIProvider.created_at)).all()
    p = _pick(list(own))
    if p:
        return p
    shared = s.scalars(select(AIProvider).where(AIProvider.tenant_id.is_(None)).order_by(AIProvider.created_at)).all()
    return _pick(list(shared))


def to_config(p: AIProvider) -> llm.ProviderConfig:
    return llm.ProviderConfig(kind=p.kind, model=p.model, api_key=decrypt(p.api_key_enc), base_url=p.base_url,
                              vision_model=p.vision_model, price_in_per_mtok=p.price_in_per_mtok,
                              price_out_per_mtok=p.price_out_per_mtok, id=p.id, name=p.name, byo=p.tenant_id is not None)


def public(p: AIProvider) -> dict:
    try:
        key_hint = hint(decrypt(p.api_key_enc))
    except Exception:
        key_hint = "…"
    return {"id": p.id, "name": p.name, "kind": p.kind, "model": p.model, "vision_model": p.vision_model,
            "base_url": p.base_url, "enabled": p.enabled, "is_default": p.is_default,
            "price_in_per_mtok": p.price_in_per_mtok, "price_out_per_mtok": p.price_out_per_mtok,
            "key_hint": key_hint, "scope": "tenant" if p.tenant_id else "shared"}


# ------------------------------------------------------------------ provider CRUD (shared: tenant_id=None)

def validate_provider_input(d: dict, creating: bool) -> dict:
    out = {k: d[k] for k in PROVIDER_FIELDS if k in d}
    if creating:
        for req in ("name", "kind", "model", "api_key"):
            if not d.get(req):
                raise ValueError(f"{req} is required")
    if "kind" in out and out["kind"] not in llm.KINDS:
        raise ValueError(f"kind must be one of {', '.join(llm.KINDS)}")
    if out.get("kind") == "openai_compat" and creating and not d.get("base_url"):
        raise ValueError("base_url is required for openai_compat")
    if d.get("base_url"):
        if not str(d["base_url"]).startswith("https://"):
            raise ValueError("base_url must be https://")
    for k in ("price_in_per_mtok", "price_out_per_mtok"):
        if k in out:
            out[k] = float(out[k] or 0)
            if out[k] < 0:
                raise ValueError(f"{k} must be ≥ 0")
    return out


def create_provider(s, tenant_id: str | None, d: dict) -> AIProvider:
    fields = validate_provider_input(d, creating=True)
    p = AIProvider(tenant_id=tenant_id, api_key_enc=encrypt(d["api_key"]), **fields)
    existing = s.scalars(select(AIProvider).where(AIProvider.tenant_id.is_(None) if tenant_id is None
                                                  else AIProvider.tenant_id == tenant_id)).all()
    if not existing:
        p.is_default = True
    if p.is_default:
        for e in existing:
            e.is_default = False
    s.add(p)
    s.flush()
    return p


def update_provider(s, p: AIProvider, d: dict) -> AIProvider:
    fields = validate_provider_input(d, creating=False)
    for k, v in fields.items():
        setattr(p, k, v)
    if d.get("api_key"):
        p.api_key_enc = encrypt(d["api_key"])
    if fields.get("is_default"):
        q = select(AIProvider).where(AIProvider.id != p.id)
        q = q.where(AIProvider.tenant_id.is_(None)) if p.tenant_id is None else q.where(AIProvider.tenant_id == p.tenant_id)
        for e in s.scalars(q):
            e.is_default = False
    return p


def test_provider(p: AIProvider) -> dict:
    t = time.monotonic()
    try:
        r = llm.complete(to_config(p), "Reply with the single word: ok", max_tokens=5, retries=0, timeout=30)
        return {"ok": bool(r.text.strip()), "latency_ms": int((time.monotonic() - t) * 1000)}
    except Exception as e:
        return {"ok": False, "latency_ms": int((time.monotonic() - t) * 1000), "error": str(e)[:300]}


# ------------------------------------------------------------------ the client handed to crawlers/connectors

class AIClient:
    """Callable surface used by extraction code. Never holds a DB session during the HTTP call."""

    def __init__(self, tenant_id: str, job_id: str | None = None, complete=None):
        self.tenant_id, self.job_id, self._complete_override = tenant_id, job_id, complete
        self.calls = 0
        self.cost_usd = 0.0
        self.errors: list[str] = []     # budget / provider problems, reported in the job result

    def _provider(self) -> llm.ProviderConfig:
        with session_scope() as s:
            row = resolve_provider(s, self.tenant_id)
            if row is None:
                raise NoProvider("no AI provider configured — the platform admin must add one, or add your own key")
            cfg = to_config(row)
            if not cfg.byo:
                tenant = s.get(Tenant, self.tenant_id)
                budget = tenant.monthly_ai_budget_usd if tenant else 0.0
                if shared_spend_this_month(s, self.tenant_id) >= budget:
                    raise BudgetExceeded(f"monthly AI budget of ${budget:.2f} reached — raise it or add your own key")
            return cfg

    def _call(self, purpose: str, prompt: str, image: bytes | None = None, mime: str = "image/jpeg") -> str:
        try:
            cfg = self._provider()
        except llm.LLMError as e:
            if str(e) not in self.errors:
                self.errors.append(str(e))
            raise
        res = (self._complete_override or llm.complete)(cfg, prompt, image=image, mime=mime)
        cost = cfg.cost(res.input_tokens, res.output_tokens)
        self.calls += 1
        self.cost_usd += cost
        with session_scope() as s:
            s.add(AIUsage(tenant_id=self.tenant_id, provider_id=cfg.id, model=res.model, purpose=purpose,
                          input_tokens=res.input_tokens, output_tokens=res.output_tokens, cost_usd=cost,
                          byo=cfg.byo, job_id=self.job_id))
        log.info("ai_call", tenant_id=self.tenant_id, purpose=purpose, model=res.model, kind=cfg.kind,
                 input_tokens=res.input_tokens, output_tokens=res.output_tokens, cost_usd=round(cost, 6), byo=cfg.byo)
        return res.text

    # connector protocol
    def text(self, prompt: str, purpose: str = "extract") -> str:
        return self._call(purpose, prompt)

    def vision(self, prompt: str, image: bytes, mime: str = "image/jpeg", purpose: str = "vision") -> str:
        return self._call(purpose, prompt, image=image, mime=mime)

    def text_fn(self, purpose: str):
        return lambda prompt: self.text(prompt, purpose)

    def vision_fn(self, purpose: str = "vision"):
        return lambda prompt, image, mime: self.vision(prompt, image, mime, purpose)
