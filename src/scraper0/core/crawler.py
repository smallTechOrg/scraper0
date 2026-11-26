"""Generic entity crawler with memory.

Per page, cheapest first:
  cache (unchanged content → previous result) → structured data hook → learned recipe →
  weak heuristic hook → LLM (then LEARN a recipe from the answer so the next page of this
  layout needs no LLM).
Per site, it remembers which link patterns produced entities (crawl hints) and follows those first,
skipping patterns that repeatedly produced nothing.
"""
from __future__ import annotations

import hashlib
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from scraper0.core import discovery
from scraper0.core.entities import EntitySpec
from scraper0.core.llm_extract import llm_extract, page_text
from scraper0.core import json_extract
from scraper0.core.recipes import apply_recipe, domain_of, learn_recipe, merge_rules, path_pattern, pattern_matches
from scraper0.core.render import is_js_shell
import structlog

from scraper0.core.store import Store

log = structlog.get_logger(__name__)

# hook(content, url, content_type) -> list of item dicts (spec fields + any extras)
Hook = Callable[[str, str, str], list[dict]]
LLM_CONFIDENCE_CAP = 0.8
RECIPE_CONFIDENCE_CAP = 0.9
DEAD_PATTERN_MISSES = 3     # pattern skipped once it produced nothing this many times (and never anything)
EXPLORE_BUDGET = 3          # still try a few links from dead patterns per crawl, in case the site changed


@dataclass
class CrawlOptions:
    max_list_pages: int = 5
    max_detail_pages: int = 60
    workers: int = 6
    allow_llm: bool = True
    learn: bool = True
    use_cache: bool = True
    link_hint: re.Pattern | None = None        # regex on URL path: likely detail pages
    skip_links: set[str] = field(default_factory=set)
    progress: Callable[[str, dict], None] | None = None   # (message, data) after each page
    cancelled: Callable[[], bool] | None = None           # cooperative cancellation, checked before each fetch


class Cancelled(Exception):
    pass


@dataclass
class CrawlStats:
    pages: int = 0
    rendered: int = 0
    by_method: dict = field(default_factory=dict)
    recipes_learned: int = 0
    llm_calls: int = 0

    def bump(self, how: str):
        self.by_method[how] = self.by_method.get(how, 0) + 1


def content_hash(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8", "ignore")).hexdigest()


_LD = re.compile(r"<script[^>]+ld\+json[^>]*>(.*?)</script>", re.I | re.S)
_META = re.compile(r"<meta[^>]+content=[\"']([^\"']*)[\"']", re.I)
_IMG = re.compile(r"<img[^>]+src=[\"']([^\"']*)[\"']", re.I)


def page_fingerprint(html: str, visible: str) -> str:
    """What the extractors can see: visible text + JSON-LD + meta content + image URLs.
    (Raw HTML would never match twice: nonces, CSRF tokens, build hashes.)"""
    parts = [visible, *_LD.findall(html), *_META.findall(html), *_IMG.findall(html)]
    return content_hash("\x1f".join(parts))


def _best_effort(fn, *a):
    """Cache/hint/stat writes must never fail a crawl."""
    try:
        return fn(*a)
    except Exception as e:
        log.warning("store_write_failed", op=getattr(fn, "__name__", "?"), error=str(e)[:200])
        return None


class Crawler:
    def __init__(self, store: Store, fetcher, llm, strong: Hook | None = None, weak: Hook | None = None):
        self.store, self.fetcher, self.llm = store, fetcher, llm
        self.strong, self.weak = strong, weak

    # ---------------------------------------------------------------- one page
    def extract_page(self, url: str, content: str, ctype: str, spec: EntitySpec, opts: CrawlOptions,
                     allow_llm: bool, stats: CrawlStats) -> list[dict]:
        is_html = "<html" in content[:3000].lower() or "<body" in content.lower() or "html" in (ctype or "")
        key = f"{spec.key}@v{spec.version}"
        h = page_fingerprint(content, page_text(content)[0]) if is_html else content_hash(content)
        if opts.use_cache:
            cached = _best_effort(self.store.cached_page, url, key, h)
            if cached is not None:
                stats.bump("cache")
                return cached

        items, how = self._extract_uncached(url, content, ctype, spec, opts, allow_llm, is_html, stats)
        stats.bump(how)
        # cache positives always; negatives only when the LLM looked (so we never re-ask about this content)
        if opts.use_cache and (items or how == "llm"):
            _best_effort(self.store.cache_page, url, key, h, items, how)
        return items

    def _extract_uncached(self, url, content, ctype, spec, opts, allow_llm, is_html, stats):
        if self.strong:
            items = self.strong(content, url, ctype)
            if items:
                return items, items[0].get("_meta", {}).get("extraction", "structured")
        if not is_html:
            return [], "none"

        domain, pat = domain_of(url), path_pattern(url)
        recipes = [r for r in self.store.recipes_for(domain, spec.key)
                   if r["strategy"] == "selectors" and pattern_matches(r["path_pattern"], url)]
        for r in recipes:
            if r["status"] != "active":
                continue
            item, _ = apply_recipe(r["rules"], content, url, spec)
            ok = not spec.validate(item)
            _best_effort(self.store.record_recipe_result, r["id"], ok)
            if ok:
                item["_meta"] = {"url": url, "extraction": "recipe", "recipe_id": r["id"],
                                 "confidence": min(spec.score(item), RECIPE_CONFIDENCE_CAP)}
                return [item], "recipe"

        if self.weak:
            items = self.weak(content, url, ctype)
            if items:
                return items, "heuristic"

        if not (allow_llm and opts.allow_llm):
            return [], "none"
        stats.llm_calls += 1
        found = llm_extract(content, url, spec, self.llm)
        items = []
        for item, sources in found:
            item["_meta"] = {"url": url, "extraction": "llm", "confidence": min(spec.score(item), LLM_CONFIDENCE_CAP)}
            items.append(item)
        if opts.learn and len(found) == 1:
            self._learn(url, content, spec, pat, domain, recipes, *found[0], stats)
        return items, "llm"

    def _learn(self, url, html, spec, pat, domain, existing, item, sources, stats):
        clean_item = {k: v for k, v in item.items() if not k.startswith("_")}
        rules = learn_recipe(html, url, spec, clean_item, sources)
        if any(r not in rules for r in spec.required):
            log.info("recipe_not_learned", url=url, missing=[r for r in spec.required if r not in rules])
            return
        same = next((r for r in existing if r["path_pattern"] == pat), None)
        if same:
            rules = merge_rules(same["rules"], rules)
        self.store.save_recipe(domain, pat, spec.key, "selectors", rules, url, spec.version)
        stats.recipes_learned += 1
        log.info("recipe_learned", domain=domain, pattern=pat, entity=spec.key, fields=sorted(rules))

    # ---------------------------------------------------------------- one site
    def _fetch_extract(self, url, spec, opts, allow_llm, stats):
        if opts.cancelled and opts.cancelled():
            raise Cancelled()
        got = self._load(url, stats)
        if not got:
            return [], None
        stats.pages += 1
        items = self.extract_page(url, got[0], got[1], spec, opts, allow_llm, stats)
        if opts.progress:
            opts.progress(f"{url} → {len(items)} item(s)", {"pages": stats.pages, "llm_calls": stats.llm_calls,
                                                            "recipes_learned": stats.recipes_learned})
        return items, got[0]

    # ---------------------------------------------------------------- JS pages and their JSON APIs
    def _load(self, url: str, stats: CrawlStats, want_render: bool = False):
        """→ (content, content_type, RenderResult | None). Static fetch first; when the page is a JavaScript
        shell (or its host is known to be), render it in a browser."""
        render = getattr(self.fetcher, "render", None)
        host_known = url.split("://", 1)[-1].split("/", 1)[0] in getattr(self.fetcher, "render_hosts", ())
        got = None if (host_known and render) else self.fetcher.get(url)
        if got is not None:
            content, ctype = got
            is_html = "html" in (ctype or "") or "<html" in content[:3000].lower()
            if not (render and is_html and is_js_shell(content, page_text(content)[0])):
                return content, ctype, None
        if render is None:
            return (got[0], got[1], None) if got else None
        rr = render(url)
        if rr is None:
            return (got[0], got[1], None) if got else None
        stats.rendered += 1
        return rr.html, "text/html", rr

    def _api_key(self, seed: str) -> str:
        from urllib.parse import urlsplit
        return "api:" + (urlsplit(seed).path.rstrip("/") or "/")

    def _api_bodies(self, rules: dict, have: dict[str, str]) -> dict[str, str]:
        bodies = dict(have)
        for lk in (rules.get("lookups") or {}).values():
            if lk["api_url"] not in bodies:
                got = self.fetcher.get(lk["api_url"])
                if got:
                    bodies[lk["api_url"]] = got[0]
        return bodies

    def _api_items(self, spec: EntitySpec, rules: dict, body: str, seed: str, bodies: dict, recipe_id=None) -> list[dict]:
        out = json_extract.items_from_rules(spec, rules, body, seed, bodies)
        for item in out:
            item["_meta"] = {"url": item.get("link") or seed, "extraction": "api", "recipe_id": recipe_id,
                             "confidence": spec.score(item)}
        return out

    def _try_api_recipe(self, seed: str, spec: EntitySpec, stats: CrawlStats) -> list[dict] | None:
        """A stored API recipe for this start URL: call the endpoint directly — no browser, no LLM."""
        key = self._api_key(seed)
        for r in self.store.recipes_for(domain_of(seed), spec.key):
            if r["strategy"] != "json_api" or r["path_pattern"] != key or r["status"] != "active":
                continue
            got = self.fetcher.get(r["rules"]["api_url"])
            items = []
            if got:
                try:
                    items = self._api_items(spec, r["rules"], got[0], seed,
                                            self._api_bodies(r["rules"], {r["rules"]["api_url"]: got[0]}), r["id"])
                except ValueError:
                    items = []
            _best_effort(self.store.record_recipe_result, r["id"], bool(items))
            if items:
                stats.pages += 1
                stats.bump("api_recipe")
                return items
        return None

    def _learn_api(self, seed: str, spec: EntitySpec, rr, opts: CrawlOptions, stats: CrawlStats) -> list[dict] | None:
        """The rendered page fetched JSON: find the array holding this entity, learn the mapping, store it."""
        if not rr.json_responses:
            return None
        from bs4 import BeautifulSoup
        hrefs = [a["href"] for a in BeautifulSoup(rr.html, "html.parser").find_all("a", href=True)]
        def counted_llm(prompt):
            stats.llm_calls += 1
            return self.llm(prompt)
        rules = json_extract.learn_api_rules(spec, rr.json_responses, seed, hrefs,
                                             counted_llm if opts.allow_llm else None)
        if rules is None:
            return None
        bodies = dict(rr.json_responses)
        items = self._api_items(spec, rules, bodies[rules["api_url"]], seed, bodies)
        if not items:
            return None
        if opts.learn:
            rid = _best_effort(self.store.save_recipe, domain_of(seed), self._api_key(seed), spec.key, "json_api",
                               rules, seed, spec.version)
            if rid:
                stats.recipes_learned += 1
                for item in items:
                    item["_meta"]["recipe_id"] = rid
                log.info("api_recipe_learned", seed=seed, api=rules["api_url"], fields=sorted(rules["mapping"]),
                         by=rules.get("learned_by"))
        stats.bump("api")
        return items

    def crawl(self, seed: str, spec: EntitySpec, opts: CrawlOptions | None = None) -> tuple[list[dict], CrawlStats]:
        opts = opts or CrawlOptions()
        stats = CrawlStats()
        api_items = _best_effort(self._try_api_recipe, seed, spec, stats)
        if api_items:
            if opts.progress:
                opts.progress(f"{seed} → {len(api_items)} item(s) from its API", {"pages": stats.pages})
            return api_items, stats
        items: list[dict] = []
        seen, pool = set(), []
        page, n = seed, 0
        while page and n < opts.max_list_pages and page not in seen:
            seen.add(page)
            n += 1
            if opts.cancelled and opts.cancelled():
                raise Cancelled()
            loaded = self._load(page, stats)
            if loaded is None:
                break
            html, ctype, rr = loaded
            stats.pages += 1
            if rr is not None and n == 1:
                learned = self._learn_api(seed, spec, rr, opts, stats)
                if learned:      # the page's own API is authoritative: no need to walk its links
                    if opts.progress:
                        opts.progress(f"{seed} → {len(learned)} item(s) from its API", {"pages": stats.pages})
                    return learned, stats
            got = self.extract_page(page, html, ctype, spec, opts, allow_llm=rr is not None, stats=stats)
            if opts.progress:
                opts.progress(f"{page} → {len(got)} item(s)", {"pages": stats.pages, "llm_calls": stats.llm_calls})
            items += got
            if html is None or not ("<html" in html[:3000].lower() or "<body" in html.lower()):
                break
            pool += discovery.site_links(html, page)
            for feed in discovery.find_feeds(html, page):
                items += self._fetch_extract(feed, spec, opts, False, stats)[0]
            page = discovery.next_page(html, page)

        have = {i.get("_meta", {}).get("url") for i in items} | {i.get("link") for i in items}
        todo = self._rank_links([u for u in dict.fromkeys(pool) if u not in opts.skip_links and u not in have
                                 and u not in seen], seed, spec, opts)
        if not todo and not items:
            todo = self._sitemap(seed, opts)
        todo = todo[:opts.max_detail_pages]

        outcomes: dict[str, bool] = {}
        if todo:
            with ThreadPoolExecutor(max_workers=opts.workers) as ex:
                results = list(ex.map(lambda u: self._fetch_extract(u, spec, opts, True, stats), todo))
            for u, (got, html) in zip(todo, results):
                items += got
                if html is not None:
                    p = path_pattern(u)
                    outcomes[p] = outcomes.get(p, False) or bool(got)
        _best_effort(self.store.update_hints, seed, spec.key, outcomes)
        return items, stats

    def _rank_links(self, links: list[str], seed: str, spec: EntitySpec, opts: CrawlOptions) -> list[str]:
        hints = self.store.hints(seed, spec.key)
        known = {r["path_pattern"] for r in self.store.recipes_for(domain_of(seed), spec.key)
                 if r["strategy"] == "selectors"}
        ranked, explore = [], 0
        for u in links:
            p = path_pattern(u)
            h = hints.get(p, {"hit": 0, "miss": 0})
            path = u.split("://", 1)[-1].split("/", 1)[-1]
            if h["hit"] == 0 and h["miss"] >= DEAD_PATTERN_MISSES:
                if explore >= EXPLORE_BUDGET:
                    continue
                explore += 1
                tier = 4
            elif p in known or h["hit"] > 0:
                tier = 0
            elif opts.link_hint is not None:
                if not opts.link_hint.search("/" + path):
                    continue        # spec-specific crawls only follow plausible detail links
                tier = 1
            else:
                tier = 2
            ranked.append((tier, -h["hit"], u))
        return [u for _, _, u in sorted(ranked, key=lambda t: (t[0], t[1]))]

    def _sitemap(self, seed: str, opts: CrawlOptions) -> list[str]:
        out, queue, visited = [], discovery.sitemap_candidates(seed), set()
        while queue and len(visited) < 6 and len(out) < opts.max_detail_pages:
            u = queue.pop(0)
            if u in visited:
                continue
            visited.add(u)
            got = self.fetcher.get(u)
            if got:
                pages, children = discovery.sitemap_event_urls(got[0], u)
                out += pages
                queue += children
        return list(dict.fromkeys(out))
