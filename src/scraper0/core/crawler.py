"""Generic entity crawler with memory.

Per page, cheapest first:
  cache (unchanged content → previous result) → structured data hook → learned recipe →
  weak heuristic hook → LLM (then LEARN a recipe from the answer so the next page of this
  layout needs no LLM).
Per site, it remembers which link patterns produced entities (crawl hints) and follows those first,
skipping patterns that repeatedly produced nothing.

Keeping the AI cost of a learn small:
  - Pages are grouped by *template* (their structure, not their URL): rules learned from one page serve every
    page of that template, whatever path it sits under. The AI is asked at most TEMPLATE_LLM_BUDGET times per
    template and URL pattern, and max_llm_calls times per crawl.
  - A template the AI twice said holds no items is not asked about again.
  - Detail pages are fetched in parallel but read by the AI one at a time, so the second page of a template
    already finds the rules the first one produced.
Finding the right entry: when the seed page itself yields nothing (or only a small unpaged widget feed), links
that name the entity's listing ("Events", "/activities"…, from the spec's name and synonyms) are tried, and the
fullest source wins (CrawlStats.entry).
"""
from __future__ import annotations

import hashlib
import re
import threading
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from typing import Callable

from scraper0.core import discovery
from scraper0.core.entities import EntitySpec
from scraper0.core.llm_extract import llm_extract, page_text
from scraper0.core import json_extract
from scraper0.core.recipes import (apply_recipe, domain_of, learn_recipe, merge_rules, path_pattern, pattern_matches,
                                   template_signature, template_similarity)
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
TEMPLATE_REUSE = 0.8        # similarity at which another pattern's rules are tried on a page (the result is validated)
TEMPLATE_SAME = 0.9         # similarity at which two pages count as one template for the AI budget
TEMPLATE_LLM_BUDGET = 3     # AI calls spent on one template per crawl
EMPTY_PAGES_TO_SKIP = 2     # pages of a template the AI must find empty before its other pages are skipped
MAX_LISTING_CANDIDATES = 3  # listing links tried when the seed page is thin
THIN_ITEMS = 25             # an unpaged feed with fewer items than this may be a home-page widget


@dataclass
class CrawlOptions:
    max_list_pages: int = 5
    max_detail_pages: int = 60
    workers: int = 6
    allow_llm: bool = True
    learn: bool = True
    use_cache: bool = True
    max_llm_calls: int = 24                    # AI calls per crawl, all templates together
    explore_listings: bool = True              # try the site's listing links when the seed page is thin
    link_hint: re.Pattern | None = None        # regex on URL path: likely detail pages
    skip_links: set[str] = field(default_factory=set)
    progress: Callable[[str, dict], None] | None = None   # (message, data) after each page
    cancelled: Callable[[], bool] | None = None           # cooperative cancellation, checked before each fetch


# quoted same-site paths and URLs inside inline scripts: router tables, hydration state, "href":"/x/y"
_SCRIPT_URL = re.compile(r"""["'](https?://[^"'\\\s<>]{4,300}|/[A-Za-z0-9_\-./%]{3,300})["']""")


class Cancelled(Exception):
    pass


@dataclass
class CrawlStats:
    pages: int = 0
    rendered: int = 0
    by_method: dict = field(default_factory=dict)
    recipes_learned: int = 0
    llm_calls: int = 0
    plan: dict | None = None                   # set when the site's API was learned as a plan
    entry: str | None = None                   # the listing page that was used instead of the seed, if any
    plan_url: str | None = None                # the page whose API the plan was learned from
    notes: list = field(default_factory=list)  # why something wasn't learned

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
        self._plan_lock = threading.Lock()      # one plan learned per crawl, even with parallel page workers
        self._tpl_lock = threading.Lock()
        self._templates: list[dict] = []        # {"sig", "calls", "negative", "positive"} per page template seen
        self._layout_sig: dict[int, frozenset] = {}     # recipe id → signature of the page it was learned from
        self._api_asked: set = set()            # API calls the AI was already asked to map (once per crawl)

    def _template(self, sig: frozenset, pattern: str) -> dict:
        """The AI budget is kept per template *and* URL pattern: pages that look alike under another path can
        still bind different fields (a site builder's "virtual" and "in person" event pages), so each pattern
        gets its own few tries — while rules, once learned, are shared across patterns."""
        with self._tpl_lock:
            best, score = None, 0.0
            for t in self._templates:
                sim = template_similarity(sig, t["sig"]) if t["pattern"] == pattern else 0.0
                if sim > score:
                    best, score = t, sim
            if best is not None and score >= TEMPLATE_SAME:
                return best
            t = {"sig": sig, "pattern": pattern, "calls": 0, "empties": 0, "negative": False, "positive": False}
            self._templates.append(t)
            return t

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
        layouts = [r for r in self.store.recipes_for(domain, spec.key) if r["strategy"] == "selectors"]
        recipes = [r for r in layouts if pattern_matches(r["path_pattern"], url)]
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

        # The same template under another URL pattern (/events/in-person/* and /events/virtual/*): its rules
        # apply here too. Whether they really do is decided by the result validating, not by the URL.
        sig = None
        others = [r for r in layouts if r not in recipes and r["status"] == "active" and r["id"] in self._layout_sig]
        if others:
            sig = template_signature(content)
            for r in others:
                if template_similarity(sig, self._layout_sig[r["id"]]) < TEMPLATE_REUSE:
                    continue
                item, _ = apply_recipe(r["rules"], content, url, spec)
                if not spec.validate(item):
                    rid = _best_effort(self.store.save_recipe, domain, pat, spec.key, "selectors", r["rules"], url,
                                       spec.version)
                    if rid:
                        self._layout_sig[rid] = sig
                    item["_meta"] = {"url": url, "extraction": "recipe", "recipe_id": rid or r["id"],
                                     "confidence": min(spec.score(item), RECIPE_CONFIDENCE_CAP)}
                    return [item], "recipe"

        if self.weak:
            items = self.weak(content, url, ctype)
            if items:
                return items, "heuristic"

        if not (allow_llm and opts.allow_llm):
            return [], "none"
        sig = sig if sig is not None else template_signature(content)
        tpl = self._template(sig, pat)
        if tpl["negative"] or tpl["calls"] >= TEMPLATE_LLM_BUDGET or stats.llm_calls >= opts.max_llm_calls:
            return [], "skipped"            # the AI already looked at this template, or the budget is spent
        tpl["calls"] += 1
        stats.llm_calls += 1
        info: dict = {}
        found = llm_extract(content, url, spec, self.llm, info=info)
        if found:
            tpl["positive"] = True
        elif info.get("found") is False and not tpl["positive"]:
            tpl["empties"] += 1             # one page without the item (an expired event) is not the template
            tpl["negative"] = tpl["empties"] >= EMPTY_PAGES_TO_SKIP
        log.info("page_read_by_ai", url=url, items=len(found), said_empty=info.get("found") is False,
                 template_calls=tpl["calls"])
        items = []
        for item, sources in found:
            item["_meta"] = {"url": url, "extraction": "llm", "confidence": min(spec.score(item), LLM_CONFIDENCE_CAP)}
            items.append(item)
        if opts.learn and len(found) == 1:
            self._learn(url, content, spec, pat, domain, recipes, *found[0], stats, sig=sig)
        return items, "llm"

    def _learn(self, url, html, spec, pat, domain, existing, item, sources, stats, sig=None):
        clean_item = {k: v for k, v in item.items() if not k.startswith("_")}
        rules = learn_recipe(html, url, spec, clean_item, sources)
        if any(r not in rules for r in spec.required):
            log.info("recipe_not_learned", url=url, missing=[r for r in spec.required if r not in rules])
            return
        same = next((r for r in existing if r["path_pattern"] == pat), None)
        if same:
            rules = merge_rules(same["rules"], rules)
        rid = self.store.save_recipe(domain, pat, spec.key, "selectors", rules, url, spec.version)
        if rid and sig is not None:
            self._layout_sig[rid] = sig
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
        rr = got[2]
        if rr is not None and stats.plan is None and getattr(rr, "requests", None):
            with self._plan_lock:               # a rendered page deeper in the site can be the one calling the API
                if stats.plan is None:
                    learned = self._learn_api(url, spec, rr, opts, stats)
                    if learned:
                        return learned, got[0]
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
        hosts = getattr(self.fetcher, "render_hosts", None)
        if hosts is not None:          # this host serves JavaScript shells: render its other pages straight away
            hosts.add(url.split("://", 1)[-1].split("/", 1)[0])
        return rr.html, "text/html", rr

    def _learn_api(self, seed: str, spec: EntitySpec, rr, opts: CrawlOptions, stats: CrawlStats) -> list[dict] | None:
        """The rendered page called an API (any method, any host): learn a plan for it. The plan, not the
        browser, is what later runs use."""
        calls = getattr(rr, "requests", None) or []
        if not calls:
            return None
        from bs4 import BeautifulSoup

        from scraper0.core import plan_learn, plans
        soup = BeautifulSoup(rr.html, "html.parser")
        base = rr.final_url or seed
        from urllib.parse import urljoin
        hrefs = [urljoin(base, a["href"]) for a in soup.find_all("a", href=True)]
        imgs = [urljoin(base, i["src"]) for i in soup.find_all("img", src=True)]

        def counted_llm(prompt):
            stats.llm_calls += 1
            return self.llm(prompt)
        try:
            plan, items, raw = plan_learn.learn_plan(spec, calls, seed, self.fetcher, hrefs, imgs,
                                                     counted_llm if opts.allow_llm else None, asked=self._api_asked)
        except plan_learn.LearnFailed as e:
            log.info("plan_not_learned", seed=seed, reason=str(e)[:300])
            stats.notes.append(str(e)[:300])
            return None
        if self._better_link(seed, plan, raw, rr, hrefs) or \
                self._find_link(seed, plan, spec, raw, items, rr, hrefs, stats):
            items, _ = plans.run_plan(plan, self.fetcher, spec, seed)
        for item in items:
            item["_meta"] = {"url": item.get("link") or seed, "extraction": "api", "confidence": spec.score(item)}
        stats.plan, stats.plan_url = plan, seed
        stats.recipes_learned += 1
        stats.bump("api")
        return items

    def _find_link(self, seed: str, plan: dict, spec: EntitySpec, raw: list[dict], items: list[dict], rr,
                   hrefs: list[str], stats: CrawlStats) -> bool:
        """The items' cards are not anchors and the items carry no URL. Look where the site keeps its routes:
        URLs inside the page's scripts, then (in the browser, learning only) where clicking two items leads —
        the address bar, or the opened page's canonical URL. A template is kept only if it holds for two items
        and the pages it gives resolve."""
        if plan.get("link_template") or len(raw) < 2:
            return False
        base = rr.final_url or seed
        from urllib.parse import urljoin
        scripted = [urljoin(base, m) for m in dict.fromkeys(_SCRIPT_URL.findall(rr.html or ""))][:4000]
        template = json_extract.infer_link_template(raw, hrefs + scripted)
        if template and not self._links_resolve(template, plan, spec, raw, base):
            template = None
        opener = getattr(self.fetcher, "open_items", None)
        if template is None and opener is not None:
            names = [str(i.get("name") or i.get("title") or "") for i in items[:60]]
            shown = [n for n in names if n and " ".join(n.split())[:28] in (rr.html or "")]     # only what the page shows
            try:
                opened = opener(base, (shown or [n for n in names if n])[:3]) or []
            except Exception as e:  # noqa: BLE001 - best effort
                log.info("open_items_failed", seed=seed, error=str(e)[:200])
                opened = []
            if opened:
                stats.rendered += 1
            urls = [o["url"] for o in opened] + [o["canonical"] for o in opened if o.get("canonical")]
            for cand in (json_extract.infer_link_template(raw, [o["canonical"] for o in opened if o.get("canonical")]),
                         json_extract.infer_link_template(raw, urls)):
                if cand and self._links_resolve(cand, plan, spec, raw, base):
                    template = cand
                    break
        if template is None:
            return False
        log.info("link_template_found", seed=seed, template=template)
        plan["link_template"] = template
        return True

    def _links_resolve(self, template: str, plan: dict, spec: EntitySpec, raw: list[dict], base: str) -> bool:
        """Fetch the pages the template gives for two items: both must answer 200 and show the item's name
        (a JavaScript shell can't show it without a browser, so a 200 shell is accepted)."""
        request = getattr(self.fetcher, "request", None)
        if request is None:
            return True
        from scraper0.core.plans import Record, Runner
        checked = 0
        for r in raw[:6]:
            url = json_extract._link(template, r, base)
            if not url:
                continue
            res = request("GET", url)
            if res is None or res.status != 200:
                return False
            ents = Runner(self.fetcher)._entities(plan, spec, [Record(r, base=base)], {}, base, validate_items=False)
            name = str((ents[0].get("name") or ents[0].get("title")) if ents else "")[:24].lower()
            text = res.text or ""
            if name and name not in text.lower() and not is_js_shell(text, page_text(text)[0]):
                return False
            checked += 1
            if checked >= 2:
                return True
        return checked > 0

    def _better_link(self, seed: str, plan: dict, raw: list[dict], rr, hrefs: list[str]) -> bool:
        """A home page often links to items as '/list?scrollTo={id}' while the list page itself links to
        real detail pages ('/list/{slug}'). When the learned link has the item only in its query, render
        that list page once (learning time only) and prefer a detail-page link found there."""
        template = plan.get("link_template")
        render = getattr(self.fetcher, "render", None)
        if render is None or not json_extract.link_in_query_only(template):
            return False
        from urllib.parse import urljoin, urlsplit

        from bs4 import BeautifulSoup
        listing = urljoin(rr.final_url or seed, urlsplit(template).path)
        try:
            more = render(listing)
        except Exception as e:  # noqa: BLE001 - best effort; the query link still works
            log.info("relink_render_failed", url=listing, error=str(e))
            return False
        if more is None:
            return False
        extra = [urljoin(more.final_url or listing, a["href"])
                 for a in BeautifulSoup(more.html, "html.parser").find_all("a", href=True)]
        better = json_extract.infer_link_template(raw, hrefs + extra)
        if better and json_extract._template_rank(better) < json_extract._template_rank(template):
            log.info("link_template_improved", seed=seed, old=template, new=better)
            plan["link_template"] = better
            return True
        return False

    def crawl(self, seed: str, spec: EntitySpec, opts: CrawlOptions | None = None) -> tuple[list[dict], CrawlStats]:
        opts = opts or CrawlOptions()
        stats = CrawlStats()
        first = self._list_stage(seed, spec, opts, stats)
        best_items, best_plan, best_entry = first["items"], stats.plan, seed
        if opts.learn and opts.explore_listings and _thin(best_items, best_plan) and not _names_listing(seed, spec):
            # The seed (often the home page) has nothing, or only a small widget feed. The site's own listing
            # page usually has the full set: try the links that name it and keep the fullest source.
            seed_plan_url = stats.plan_url
            for cand in listing_candidates(first["html"] or "", seed, spec)[:MAX_LISTING_CANDIDATES]:
                stats.plan, stats.plan_url = None, None
                c = self._list_stage(cand, spec, opts, stats)
                c_items = c["items"] if stats.plan is not None else c["items"] + self._detail_stage(cand, c, spec, opts, stats)
                log.info("listing_candidate", seed=seed, candidate=cand, items=len(c_items), plan=stats.plan is not None)
                if _better(c_items, stats.plan, best_items, best_plan):
                    # an API plan may have been found one link deeper than the candidate: that page is the entry
                    best_items, best_plan, best_entry = c_items, stats.plan, (stats.plan_url or cand)
                if not _thin(best_items, best_plan):
                    break
            stats.plan = best_plan
            stats.plan_url = best_entry if best_entry != seed else seed_plan_url
        if best_entry == seed:
            if stats.plan is None:
                best_items = first["items"] + self._detail_stage(seed, first, spec, opts, stats)
                if stats.plan is not None and stats.plan_url and stats.plan_url != seed:
                    stats.entry = stats.plan_url           # a page one link down turned out to call the API
        else:
            stats.entry = best_entry
            log.info("entry_chosen", seed=seed, entry=best_entry, items=len(best_items))
        return best_items, stats

    def _list_stage(self, seed: str, spec: EntitySpec, opts: CrawlOptions, stats: CrawlStats) -> dict:
        """The seed page and its next pages: an API plan if the page loads its data from one, else the items on
        the pages themselves plus the links worth following. → {"items", "pool", "seen", "html"}."""
        items: list[dict] = []
        seen, pool = set(), []
        first_html = None
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
            first_html = first_html if first_html is not None else html
            stats.pages += 1
            if rr is not None and stats.plan is None:
                learned = self._learn_api(page, spec, rr, opts, stats)
                if learned:      # the page's own API is authoritative: no need to walk its links
                    if opts.progress:
                        opts.progress(f"{page} → {len(learned)} item(s) from its API", {"pages": stats.pages})
                    return {"items": learned, "pool": [], "seen": seen, "html": first_html}
            got = self.extract_page(page, html, ctype, spec, opts, allow_llm=rr is not None, stats=stats)
            if not got and rr is None and n == 1 and opts.learn and getattr(self.fetcher, "render", None):
                # Static HTML with no items can still load them by script (a search page calling a POST API):
                # render it once while learning, to see what it fetches.
                rr2 = self.fetcher.render(page)
                if rr2 is not None:
                    stats.rendered += 1
                    learned = self._learn_api(page, spec, rr2, opts, stats)
                    if learned:
                        if opts.progress:
                            opts.progress(f"{page} → {len(learned)} item(s) from its API", {"pages": stats.pages})
                        return {"items": learned, "pool": [], "seen": seen, "html": first_html}
            if opts.progress:
                opts.progress(f"{page} → {len(got)} item(s)", {"pages": stats.pages, "llm_calls": stats.llm_calls})
            items += got
            if html is None or not ("<html" in html[:3000].lower() or "<body" in html.lower()):
                break
            pool += discovery.site_links(html, page)
            for feed in discovery.find_feeds(html, page):
                items += self._fetch_extract(feed, spec, opts, False, stats)[0]
            page = discovery.next_page(html, page)
        return {"items": items, "pool": pool, "seen": seen, "html": first_html}

    def _detail_stage(self, seed: str, listed: dict, spec: EntitySpec, opts: CrawlOptions, stats: CrawlStats) -> list[dict]:
        """Follow the list pages' links. Pages are fetched in parallel and read without the AI first; what is
        still unread then goes to the AI one page at a time, so each template is learned once."""
        items: list[dict] = []
        have = {i.get("_meta", {}).get("url") for i in listed["items"]} | {i.get("link") for i in listed["items"]}
        todo = self._rank_links([u for u in dict.fromkeys(listed["pool"]) if u not in opts.skip_links and u not in have
                                 and u not in listed["seen"]], seed, spec, opts)
        if not todo and not listed["items"]:
            todo = self._sitemap(seed, opts)
        todo = todo[:opts.max_detail_pages]

        outcomes: dict[str, bool] = {}
        if todo:
            with ThreadPoolExecutor(max_workers=opts.workers) as ex:
                results = list(ex.map(lambda u: self._fetch_extract(u, spec, opts, False, stats), todo))
            for u, (got, html) in zip(todo, results):
                if stats.plan is not None and any(i.get("_meta", {}).get("extraction") == "api" for i in got):
                    return got                      # a deeper page turned out to call the site's API
                if not got and html is not None and opts.allow_llm and \
                        ("<html" in html[:3000].lower() or "<body" in html.lower()):
                    if opts.cancelled and opts.cancelled():
                        raise Cancelled()
                    stats.by_method["none"] = max(0, stats.by_method.get("none", 1) - 1)
                    got = self.extract_page(u, html, "text/html", spec, opts, True, stats)
                    if opts.progress:
                        opts.progress(f"{u} → {len(got)} item(s)", {"pages": stats.pages, "llm_calls": stats.llm_calls,
                                                                    "recipes_learned": stats.recipes_learned})
                items += got
                if html is not None:
                    p = path_pattern(u)
                    outcomes[p] = outcomes.get(p, False) or bool(got)
        _best_effort(self.store.update_hints, seed, spec.key, outcomes)
        return items

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


# ------------------------------------------------------------------ finding the listing page

def _stem(word: str) -> str:
    w = word.lower()
    if w.endswith("ies") and len(w) > 4:
        return w[:-3] + "y"
    return w[:-1] if w.endswith("s") and not w.endswith("ss") and len(w) > 3 else w


# what sites call a page that lists things, whatever the things are
LISTING_WORDS = ("search", "browse", "find", "explore", "discover", "directory", "listing", "catalogue", "catalog")


def listing_vocabulary(spec: EntitySpec) -> list[str]:
    """Words a site uses for the page that lists this entity: the spec's name first, then the generic words for
    a listing ("search", "browse"), then the entity's synonyms."""
    words = [spec.name.replace("_", " ")] + list(LISTING_WORDS) + list(getattr(spec, "synonyms", None) or [])
    return list(dict.fromkeys(_stem(w) for w in words if w))


def entity_vocabulary(spec: EntitySpec) -> set[str]:
    return {_stem(w) for w in [spec.name.replace("_", " ")] + list(getattr(spec, "synonyms", None) or []) if w}


def _words(text: str) -> set[str]:
    return {_stem(t) for t in re.split(r"[^a-z0-9]+", text.lower()) if t}


def _names_listing(url: str, spec: EntitySpec) -> bool:
    """The URL's path already names the entity ("/pages/events"): it is the listing, not a home page."""
    from urllib.parse import urlsplit
    return bool(_words(urlsplit(url).path) & (entity_vocabulary(spec) | set(_stem(w) for w in LISTING_WORDS)))


def listing_candidates(html: str, page_url: str, spec: EntitySpec) -> list[str]:
    """Same-site links on `page_url` that name the entity's listing by link text or path, best first: the entity's
    own name before synonyms, a text match before a path-only match, shallow paths before deep ones."""
    from urllib.parse import urljoin, urlsplit

    from bs4 import BeautifulSoup
    vocab = listing_vocabulary(spec)
    host = urlsplit(page_url).netloc.removeprefix("www.")
    here = page_url.split("#")[0].rstrip("/")
    ranked: dict[str, tuple] = {}
    for a in BeautifulSoup(html, "html.parser").find_all("a", href=True):
        url = urljoin(page_url, a["href"]).split("#")[0]
        p = urlsplit(url)
        if p.scheme not in ("http", "https") or p.netloc.removeprefix("www.") != host or url.rstrip("/") == here:
            continue
        if discovery._SKIP.search(url) or p.query:
            continue
        text = " ".join(a.get_text(" ").split())
        segs = [x for x in p.path.split("/") if x]
        if not segs or len(segs) > 3 or len(text) > 40:
            continue
        tw, pw = _words(text), _words(segs[-1])
        hit = [i for i, w in enumerate(vocab) if w in tw or w in pw]
        if not hit:
            continue
        w = vocab[min(hit)]
        exact = 0 if tw == {w} or pw == {w} else 1          # "Events" / "/events" before "Mega Events"
        key = (min(hit), exact, 0 if (tw & set(vocab)) else 1, len(segs), len(url))
        if url not in ranked or key < ranked[url]:
            ranked[url] = key
    return [u for u, _ in sorted(ranked.items(), key=lambda kv: kv[1])]


def _paged(plan: dict | None) -> bool:
    return bool(plan) and any(st.get("paginate") for st in plan.get("steps") or [])


def _thin(items: list, plan: dict | None) -> bool:
    """Nothing found, or an unpaged feed small enough to be a home-page widget rather than the full list."""
    return not items or (plan is not None and not _paged(plan) and len(items) < THIN_ITEMS)


def _better(items: list, plan: dict | None, best_items: list, best_plan: dict | None) -> bool:
    n, b = len(items), len(best_items)
    if n == 0:
        return False
    return n > max(b * 1.2, b + 1) or (n >= b and _paged(plan) and not _paged(best_plan))
