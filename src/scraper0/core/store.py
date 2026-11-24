"""Learned-knowledge repository (recipes, crawl hints, page cache) over the shared engine.

Every method opens and closes its own short session: the crawler calls these between network
requests and must never hold one of the (max three) DB connections while fetching.
`entity_key` is EntitySpec.key: "event" (shared by all tenants) or "<tenant_id>:<name>".
"""
from __future__ import annotations

from sqlalchemy import delete, select

from scraper0.db.models import CrawlHint, PageCache, Recipe, now
from scraper0.db.session import session_scope

STALE_AFTER = 3   # consecutive failed applications → recipe goes stale and is relearned


def _recipe_dict(r: Recipe) -> dict:
    return {
        "id": r.id, "domain": r.domain, "path_pattern": r.path_pattern, "entity": r.entity_key,
        "strategy": r.strategy, "rules": r.rules, "sample_url": r.sample_url, "successes": r.successes,
        "failures": r.failures, "consecutive_failures": r.consecutive_failures, "status": r.status,
        "spec_version": r.spec_version,
        "created_at": r.created_at.isoformat() if r.created_at else None,
        "updated_at": r.updated_at.isoformat() if r.updated_at else None,
    }


class Store:
    # ---------------------------------------------------------------- recipes
    def recipes_for(self, domain: str, entity_key: str) -> list[dict]:
        with session_scope() as s:
            rows = s.scalars(select(Recipe).where(Recipe.domain == domain, Recipe.entity_key == entity_key)).all()
            return [_recipe_dict(r) for r in rows]

    def save_recipe(self, domain: str, path_pattern: str, entity_key: str, strategy: str, rules: dict,
                    sample_url: str, spec_version: int = 1) -> int:
        with session_scope() as s:
            r = s.scalar(select(Recipe).where(Recipe.domain == domain, Recipe.path_pattern == path_pattern,
                                              Recipe.entity_key == entity_key))
            if r is None:
                r = Recipe(domain=domain, path_pattern=path_pattern, entity_key=entity_key, successes=0, failures=0)
                s.add(r)
            r.strategy, r.rules, r.sample_url = strategy, rules, sample_url
            r.status, r.consecutive_failures, r.spec_version, r.updated_at = "active", 0, spec_version, now()
            s.flush()
            return r.id

    def record_recipe_result(self, recipe_id: int, ok: bool) -> None:
        with session_scope() as s:
            r = s.get(Recipe, recipe_id)
            if r is None:
                return
            if ok:
                r.successes += 1
                r.consecutive_failures = 0
                r.status = "active"
            else:
                r.failures += 1
                r.consecutive_failures += 1
                if r.consecutive_failures >= STALE_AFTER:
                    r.status = "stale"
            r.updated_at = now()

    def list_recipes(self, domain: str | None = None, entity_keys: list[str] | None = None) -> list[dict]:
        with session_scope() as s:
            q = select(Recipe)
            if domain:
                q = q.where(Recipe.domain == domain)
            if entity_keys is not None:
                q = q.where(Recipe.entity_key.in_(entity_keys))
            return [_recipe_dict(r) for r in s.scalars(q.order_by(Recipe.domain, Recipe.path_pattern))]

    def get_recipe(self, recipe_id: int) -> dict | None:
        with session_scope() as s:
            r = s.get(Recipe, recipe_id)
            return _recipe_dict(r) if r else None

    def delete_recipe(self, recipe_id: int) -> bool:
        with session_scope() as s:
            return s.execute(delete(Recipe).where(Recipe.id == recipe_id)).rowcount > 0

    # ---------------------------------------------------------------- crawl hints
    def hints(self, seed: str, entity_key: str) -> dict:
        with session_scope() as s:
            h = s.scalar(select(CrawlHint).where(CrawlHint.seed == seed, CrawlHint.entity_key == entity_key))
            return dict(h.patterns) if h else {}

    def update_hints(self, seed: str, entity_key: str, outcomes: dict[str, bool]) -> None:
        if not outcomes:
            return
        with session_scope() as s:
            h = s.scalar(select(CrawlHint).where(CrawlHint.seed == seed, CrawlHint.entity_key == entity_key))
            pats = {k: dict(v) for k, v in (h.patterns if h else {}).items()}
            for pat, ok in outcomes.items():
                p = pats.setdefault(pat, {"hit": 0, "miss": 0})
                p["hit" if ok else "miss"] += 1
            if h is None:
                s.add(CrawlHint(seed=seed, entity_key=entity_key, patterns=pats))
            else:
                h.patterns, h.updated_at = pats, now()

    # ---------------------------------------------------------------- page cache
    def cached_page(self, url: str, key: str, content_hash: str) -> list[dict] | None:
        with session_scope() as s:
            p = s.scalar(select(PageCache).where(PageCache.url == url, PageCache.entity_key == key))
            return list(p.items) if p and p.content_hash == content_hash else None

    def cache_page(self, url: str, key: str, content_hash: str, items: list[dict], extraction: str) -> None:
        import json
        safe = json.loads(json.dumps(items, default=str))
        with session_scope() as s:
            p = s.scalar(select(PageCache).where(PageCache.url == url, PageCache.entity_key == key))
            if p is None:
                s.add(PageCache(url=url, entity_key=key, content_hash=content_hash, items=safe, extraction=extraction))
            else:
                p.content_hash, p.items, p.extraction, p.fetched_at = content_hash, safe, extraction, now()


_default = Store()


def default_store() -> Store:
    return _default
