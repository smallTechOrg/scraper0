"""Scratch knowledge for one learn run: page-layout rules, link-pattern hints and a page cache, in memory.

Learning a site (core/crawler.py) works out each page layout once — usually with one AI call — and reuses that
layout's rules for every other page of the same pattern in the same run. When the run ends, what it learned
is turned into a plan (core/plans.py) and frozen into the action's version; nothing here outlives the run.
"""
from __future__ import annotations

import itertools
import threading

STALE_AFTER = 3   # consecutive failed applications → a layout's rules are no longer trusted


class Store:
    def __init__(self):
        self._lock = threading.Lock()
        self._ids = itertools.count(1)
        self._recipes: dict[int, dict] = {}
        self._hints: dict[tuple[str, str], dict] = {}
        self._pages: dict[tuple[str, str], tuple[str, list[dict], str]] = {}

    # ---------------------------------------------------------------- layout rules
    def recipes_for(self, domain: str, entity_key: str) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._recipes.values() if r["domain"] == domain and r["entity"] == entity_key]

    def save_recipe(self, domain: str, path_pattern: str, entity_key: str, strategy: str, rules: dict,
                    sample_url: str, spec_version: int = 1) -> int:
        with self._lock:
            for r in self._recipes.values():
                if (r["domain"], r["path_pattern"], r["entity"]) == (domain, path_pattern, entity_key):
                    r.update(strategy=strategy, rules=rules, sample_url=sample_url, status="active",
                             consecutive_failures=0, spec_version=spec_version)
                    return r["id"]
            rid = next(self._ids)
            self._recipes[rid] = {"id": rid, "domain": domain, "path_pattern": path_pattern, "entity": entity_key,
                                  "strategy": strategy, "rules": rules, "sample_url": sample_url, "successes": 0,
                                  "failures": 0, "consecutive_failures": 0, "status": "active",
                                  "spec_version": spec_version}
            return rid

    def list_recipes(self, domain: str | None = None) -> list[dict]:
        with self._lock:
            return [dict(r) for r in self._recipes.values() if domain in (None, r["domain"])]

    def record_recipe_result(self, recipe_id: int, ok: bool) -> None:
        with self._lock:
            r = self._recipes.get(recipe_id)
            if r is None:
                return
            if ok:
                r["successes"] += 1
                r["consecutive_failures"], r["status"] = 0, "active"
            else:
                r["failures"] += 1
                r["consecutive_failures"] += 1
                if r["consecutive_failures"] >= STALE_AFTER:
                    r["status"] = "stale"

    # ---------------------------------------------------------------- link-pattern hints
    def hints(self, seed: str, entity_key: str) -> dict:
        with self._lock:
            return dict(self._hints.get((seed, entity_key), {}))

    def update_hints(self, seed: str, entity_key: str, outcomes: dict[str, bool]) -> None:
        with self._lock:
            pats = self._hints.setdefault((seed, entity_key), {})
            for p, ok in outcomes.items():
                h = pats.setdefault(p, {"hit": 0, "miss": 0})
                h["hit" if ok else "miss"] += 1

    # ---------------------------------------------------------------- page cache
    def cached_page(self, url: str, key: str, content_hash: str) -> list[dict] | None:
        with self._lock:
            got = self._pages.get((url, key))
            return [dict(i) for i in got[1]] if got and got[0] == content_hash else None

    def cache_page(self, url: str, key: str, content_hash: str, items: list[dict], extraction: str) -> None:
        with self._lock:
            self._pages[(url, key)] = (content_hash, [dict(i) for i in items], extraction)
