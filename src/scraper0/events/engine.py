"""Event pipeline on top of the generic learning crawler (scraper_core).

Per page (cheapest first): page cache → JSON-LD/microdata/ICS/RSS/OpenGraph → learned recipe →
labelled-text heuristic → LLM (+ learn a recipe so the next page of that layout skips the LLM).
Per site: crawl hints remember which link patterns yield events.
Then: normalise → classify → filter → dedupe (incl. cross-source) → rank.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field, fields as dc_fields
from typing import Callable

from scraper0.core import discovery
from scraper0.events.classify import address_is_online, classify_type, looks_online
from scraper0.core.dates import is_past
from scraper0.events.dedupe import dedupe
from scraper0.events.extractors import extract_all, extract_heuristic
from scraper0.core.fetch import Fetcher
from scraper0.events.schema import EVENT_TYPES, Event, clean, score
from scraper0.core.crawler import Cancelled, CrawlOptions, Crawler
from scraper0.core.entities import EVENT_SPEC
from scraper0.core.store import Store
from scraper0.core.urls import strip_utm_params

import structlog

log = structlog.get_logger(__name__)

# detail-ish paths for events (used to decide which listing links are worth fetching)
EVENT_LINK_HINT = re.compile(discovery._DETAIL_HINT.pattern + "|" + discovery._EVENT_PATH.pattern, re.I)


@dataclass
class Options:
    portal: str = "WEB"
    include_past: bool = False
    include_online: bool = False
    require_start: bool = True
    require_address: bool = False
    max_detail_pages: int = 60
    max_list_pages: int = 5
    workers: int = 6
    event_filter: str = ""
    category_filter: str = ""
    use_llm_extraction: bool = True
    learn: bool = True
    progress: Callable[[str, dict], None] | None = None
    cancelled: Callable[[], bool] | None = None
    existing_links: set[str] = field(default_factory=set)
    now: object = None  # injectable clock for tests


LLMFn = Callable[[str], str]

_FILTER_PROMPT = """You filter events for a civic volunteering platform.
Event filter: %s
Category filter: %s
For each event decide select (true/false) and pick one type from: %s
Respond ONLY with a JSON array: [{"index":0,"select":true,"type":"..."}]
Events:
%s"""

_EVENT_FIELDS = {f.name for f in dc_fields(Event)}


def no_llm(prompt: str) -> str:
    from scraper0.core.llm import LLMError
    raise LLMError("no AI client configured")


def _json(text: str):
    t = text.strip().strip("`")
    t = re.sub(r"^json\s*", "", t)
    m = re.search(r"[\[{].*[\]}]", t, re.S)
    return json.loads(m.group(0) if m else t)


def event_to_item(e: Event) -> dict:
    d = {k: getattr(e, k) for k in _EVENT_FIELDS if k not in ("confidence", "warnings")}
    d["_meta"] = {"url": e.source_url or e.link, "extraction": e.extraction, "confidence": e.confidence}
    return d


def item_to_event(item: dict, portal: str) -> Event:
    meta = item.get("_meta", {})
    page = meta.get("url") or item.get("link") or ""
    kw = {k: v for k, v in item.items() if k in _EVENT_FIELDS}
    kw.setdefault("link", page)
    kw["link"] = strip_utm_params(kw["link"])
    kw["portal"] = portal
    kw.setdefault("source_url", page)
    kw["extraction"] = kw.get("extraction") or meta.get("extraction", "")
    kw["sources"] = kw.get("sources") or [kw["link"]]
    e = Event(**{"name": "", **kw})
    e.confidence = min(score(e), meta.get("confidence", 1.0) or 1.0)
    if meta.get("extraction") in ("recipe", "api"):
        e.extraction = meta["extraction"]
    return e


class Engine:
    def __init__(self, fetcher: Fetcher | None = None, llm: LLMFn | None = None, store: Store | None = None,
                 filter_llm: LLMFn | None = None):
        if fetcher is None:
            from scraper0.config import get_settings
            st = get_settings()
            fetcher = Fetcher(min_host_interval=st.min_host_interval_s, respect_robots=st.respect_robots)
        self.fetcher = fetcher
        self.llm = llm or no_llm
        self.filter_llm = filter_llm or self.llm
        self.store = store or Store()          # scratch knowledge for this engine's runs only
        self.last_crawl_stats: list[dict] = []

    def _crawler(self, portal: str) -> Crawler:
        def strong(c, u, t):
            return [event_to_item(e) for e in extract_all(c, u, portal, t, heuristic=False)]
        def weak(c, u, t):
            return [event_to_item(e) for e in extract_heuristic(c, u, portal)]
        return Crawler(self.store, self.fetcher, self.llm, strong=strong, weak=weak)

    def scrape_source(self, seed: str, o: Options) -> list[Event]:
        copts = CrawlOptions(
            max_list_pages=o.max_list_pages, max_detail_pages=o.max_detail_pages, workers=o.workers,
            allow_llm=o.use_llm_extraction, learn=o.learn, link_hint=EVENT_LINK_HINT,
            skip_links=set(o.existing_links), progress=o.progress, cancelled=o.cancelled,
        )
        items, stats = self._crawler(o.portal).crawl(seed, EVENT_SPEC, copts)
        self.last_crawl_stats.append({"source": seed, "pages": stats.pages, "by_method": stats.by_method,
                                      "llm_calls": stats.llm_calls, "recipes_learned": stats.recipes_learned,
                                      "rendered": stats.rendered, "plan": stats.plan, "notes": stats.notes,
                                      "entry": stats.entry})
        return [item_to_event(i, o.portal) for i in items]

    # ---------------------------------------------------------------- pipeline
    def run(self, seeds: list[str], o: Options) -> dict:
        raw: list[Event] = []
        stats = {"sources": len(seeds), "raw": 0, "errors": []}
        for seed in seeds:
            try:
                raw += self.scrape_source(seed, o)
            except Cancelled:
                raise
            except Exception as e:  # one broken source must never kill the run
                stats["errors"].append({"source": seed, "error": str(e)})
        stats["raw"] = len(raw)
        stats["crawl"] = self.last_crawl_stats[-len(seeds):] if seeds else []
        events = self.postprocess(raw, o, stats)
        return {"events": [e.to_dict() for e in events], "stats": stats}

    def postprocess(self, raw: list[Event], o: Options, stats: dict | None = None) -> list[Event]:
        stats = stats if stats is not None else {}
        kept: list[Event] = []
        drop = {"no_name": 0, "no_start": 0, "past": 0, "online": 0, "no_address": 0,
                "cancelled": 0, "known": 0, "filtered": 0}
        for e in raw:
            e.name = clean(e.name) or ""
            e.link = strip_utm_params(e.link)
            if not e.name:
                drop["no_name"] += 1
                continue
            if "cancelled" in e.extraction:
                drop["cancelled"] += 1
                continue
            if e.link in o.existing_links:
                drop["known"] += 1
                continue
            if o.require_start and not e.start_time:
                drop["no_start"] += 1
                continue
            if not o.include_past and is_past(e.start_time if not e.end_time else e.end_time, o.now):
                drop["past"] += 1
                continue
            e.is_online = (e.is_online or address_is_online(e.address)
                           or (not e.address and looks_online(e.name, e.description)))
            if e.is_online and not o.include_online:
                drop["online"] += 1
                continue
            if o.require_address and not e.address:
                drop["no_address"] += 1
                continue
            e.type = e.type if e.type in EVENT_TYPES and e.type != "OTHER" else classify_type(e.name, e.description)
            e.confidence = score(e)
            kept.append(e)

        kept = dedupe(kept)
        if o.event_filter or o.category_filter:
            kept, d = self._llm_filter(kept, o)
            drop["filtered"] += d
        kept.sort(key=lambda e: (e.start_time or "9999", -e.confidence))
        stats["dropped"] = drop
        stats["returned"] = len(kept)
        return kept

    def _llm_filter(self, events: list[Event], o: Options, batch: int = 8) -> tuple[list[Event], int]:
        out: list[Event] = []
        for i in range(0, len(events), batch):
            chunk = events[i:i + batch]
            listing = "\n".join(
                f"[{j}] {e.name} | {e.organisation or ''} | {e.address or ''} | {(e.description or '')[:300]}"
                for j, e in enumerate(chunk))
            try:
                res = _json(self.filter_llm(_FILTER_PROMPT % (o.event_filter, o.category_filter, ", ".join(EVENT_TYPES), listing)))
                verdict = {r["index"]: r for r in res if isinstance(r, dict) and "index" in r}
            except Exception as ex:
                log.info(f"LLM filter failed ({ex}); falling back to rule-based classification")
                verdict = {}
            for j, e in enumerate(chunk):
                v = verdict.get(j)
                if v is None:  # LLM unavailable: keep, don't silently lose events
                    out.append(e)
                    continue
                if v.get("select"):
                    if v.get("type") in EVENT_TYPES:
                        e.type = v["type"]
                    out.append(e)
        return out, len(events) - len(out)
