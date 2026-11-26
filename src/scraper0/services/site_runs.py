"""v2 jobs: learn a draft version, refresh a locked list action into the shared item store, run a write.

Network and AI work never happens while a DB session is open (pool cap): every DB step is its own short session.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from scraper0.core.dates import IST
from scraper0.core.entities import EVENT_SPEC
from scraper0.db.models import ActionVersion, SiteAction, SiteItem, WriteRecord, now
from scraper0.db.session import session_scope
from scraper0.services import media
from scraper0.services import sites as sites_svc
from scraper0.services.items import batch_keys

log = structlog.get_logger(__name__)

EVENT_FIELDS = ("name", "start_time", "end_time", "address", "link", "organisation", "image", "description",
                "latitude", "longitude", "is_online", "price", "source_url")
_HASH_EXCLUDE = {"image"}        # CDN/signed image URLs change on every scrape without the event changing
KEEP_PAST_DAYS = 1               # the store keeps items from yesterday (IST) on; older ones are not stored


class ScrapeFailed(Exception):
    pass


# ------------------------------------------------------------------ item shape

def event_data(d: dict) -> dict:
    """Engine/connector event dict → EventData (without `category`, which is per tenant)."""
    out = {k: d.get(k) for k in EVENT_FIELDS}
    out["is_online"] = bool(out["is_online"])
    out["source_url"] = out["source_url"] or out["link"]
    for k in ("latitude", "longitude"):
        try:
            out[k] = float(out[k]) if out[k] not in (None, "") else None
        except (TypeError, ValueError):
            out[k] = None
    return out


def data_hash(data: dict) -> str:
    stable = {k: v for k, v in data.items() if k not in _HASH_EXCLUDE}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()


def normalise(raw: list[dict], portal: str) -> list[dict]:
    """Run raw event dicts through the engine's postprocess (clean, online detection, dedupe) keeping past and
    online events, then drop what ended before yesterday (IST)."""
    from scraper0.events.engine import Engine, Options, item_to_event
    events = []
    for d in raw:
        try:
            events.append(item_to_event(d, portal))
        except TypeError:
            continue
    kept = Engine(fetcher=object()).postprocess(events, Options(portal=portal, include_past=True, include_online=True))
    return trim_old([e.to_dict() for e in kept])


def _parse(iso: str | None) -> datetime | None:
    if not iso:
        return None
    try:
        d = datetime.fromisoformat(str(iso).replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def trim_old(events: list[dict], at: datetime | None = None) -> list[dict]:
    at = at or datetime.now(timezone.utc)
    day = at.astimezone(IST).replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=KEEP_PAST_DAYS)
    out = []
    for e in events:
        last = _parse(e.get("end_time")) or _parse(e.get("start_time"))
        if last is None or last >= day:
            out.append(e)
    return out


# ------------------------------------------------------------------ scraping (shared by learn and refresh)

def _fetcher():
    from scraper0.jobs import execute
    return execute._fetcher()


def _api_key(seed: str) -> str:
    return "api:" + (urlsplit(seed).path.rstrip("/") or "/")


def run_code(connector: str, connector_action: str, tenant_id: str, rep, ai, data: dict | None = None,
             auth: dict | None = None) -> dict:
    from scraper0.connectors.base import ConnectorContext, ConnectorError, get
    from scraper0.core.llm import LLMError
    from scraper0.services.connector_state import DBStateStore
    conn = get(connector)
    if conn is None or connector_action not in conn.actions:
        raise ScrapeFailed(f"connector {connector}.{connector_action} is not available")
    ctx = ConnectorContext(tenant_id=tenant_id, ai=ai, state=DBStateStore(tenant_id, conn.name), auth=auth,
                           progress=rep)
    try:
        return conn.actions[connector_action](dict(data or {}), ctx)
    except ConnectorError as e:
        raise ScrapeFailed(str(e)) from e
    except LLMError as e:
        raise ScrapeFailed(f"AI unavailable: {e}") from e


def crawl_learned(seeds: list[str], portal: str, rep, ai) -> tuple[list[dict], dict]:
    """Engine over the seed URLs, re-learning from the live site (stored recipes and cached pages are ignored,
    then replaced) → (events, merged by_method). Used by learn and repair, so both really look at the site again."""
    from scraper0.core.store import default_store
    from scraper0.events.engine import Engine, Options
    eng = Engine(_fetcher(), llm=ai.text_fn("extract"), filter_llm=ai.text_fn("filter"), store=default_store())
    res = eng.run(seeds, Options(portal=portal, include_past=True, include_online=True, learn=True, relearn=True,
                                 use_llm_extraction=True, progress=rep, cancelled=rep.cancelled))
    errors = res["stats"].get("errors") or []
    if errors and len(errors) == len(seeds):
        raise ScrapeFailed("; ".join(f"{e['source']}: {e['error']}" for e in errors)[:500])
    by_method: dict = {}
    for c in eng.last_crawl_stats:
        for k, n in (c.get("by_method") or {}).items():
            by_method[k] = by_method.get(k, 0) + n
    return trim_old(res["events"]), by_method


def snapshot_for(seeds: list[str], by_method: dict) -> dict:
    """The recipes this learn used, frozen into the version."""
    from scraper0.core.recipes import domain_of
    from scraper0.core.store import default_store
    recipes = []
    for domain in dict.fromkeys(domain_of(u) for u in seeds):
        recipes += [r for r in default_store().recipes_for(domain, EVENT_SPEC.key) if r["status"] == "active"]
    keys = {(domain_of(u), _api_key(u)) for u in seeds}
    api = [r for r in recipes if r["strategy"] == "json_api" and (r["domain"], r["path_pattern"]) in keys]
    sel = [r for r in recipes if r["strategy"] == "selectors"]
    if api and (by_method.get("api") or by_method.get("api_recipe")):
        strategy, used = "json_api", api
    elif sel:
        strategy, used = "selectors", sel
    else:
        strategy, used = "structured", []
    return {"implementation": "learned", "strategy": strategy, "entity": EVENT_SPEC.name, "seed_urls": list(seeds),
            "by_method": by_method,
            "recipes": [{"recipe_id": r["id"], "domain": r["domain"], "path_pattern": r["path_pattern"],
                         "strategy": r["strategy"], "rules": r["rules"], "sample_url": r["sample_url"],
                         "spec_version": r["spec_version"]} for r in used]}


def run_json_api_snapshot(snapshot: dict, seeds: list[str], portal: str, rep) -> list[dict]:
    """A locked json_api version runs from its frozen rules: call the API directly (no browser, no AI)."""
    from scraper0.core import json_extract
    from scraper0.core.recipes import domain_of
    fetcher = _fetcher()
    raw: list[dict] = []
    used = 0
    for seed in seeds:
        r = next((r for r in snapshot.get("recipes") or [] if r["strategy"] == "json_api"
                  and r["domain"] == domain_of(seed) and r["path_pattern"] == _api_key(seed)), None)
        if r is None:
            continue
        used += 1
        rules = r["rules"]
        got = fetcher.get(rules["api_url"])
        if not got:
            raise ScrapeFailed(f"API {rules['api_url']} did not respond")
        bodies = {rules["api_url"]: got[0]}
        for lk in (rules.get("lookups") or {}).values():
            if lk["api_url"] not in bodies:
                g = fetcher.get(lk["api_url"])
                if g:
                    bodies[lk["api_url"]] = g[0]
        try:
            items = json_extract.items_from_rules(EVENT_SPEC, rules, got[0], seed, bodies)
        except (ValueError, KeyError, TypeError) as e:
            raise ScrapeFailed(f"API {rules['api_url']} returned unexpected data: {e}") from e
        for it in items:
            it["_meta"] = {"url": it.get("link") or seed, "extraction": "api", "confidence": EVENT_SPEC.score(it)}
        raw += items
        rep(f"{seed} → {len(items)} item(s) from its API", {"pages": used})
    if not used:
        raise ScrapeFailed("the locked version has no API recipe for its seed URLs")
    return normalise(raw, portal)


def scrape_version(action: dict, snapshot: dict, tenant_id: str, rep, ai) -> list[dict]:
    """Run one version of a list action → normalised event dicts."""
    portal = action["site_id"].upper()
    if action["implementation"] == "code":
        out = run_code(action["connector"], action["connector_action"], tenant_id, rep, ai, data={})
        events = out.get("events") if isinstance(out, dict) else None
        if not isinstance(events, list):
            raise ScrapeFailed("connector returned no events list")
        return normalise(events, events[0].get("portal") or portal if events else portal)
    seeds = snapshot.get("seed_urls") or action["seed_urls"]
    if snapshot.get("strategy") == "json_api":
        return run_json_api_snapshot(snapshot, seeds, portal, rep)
    # selectors / structured data: crawl with the shared recipe store (see NOTES: not frozen to the snapshot)
    return crawl_learned(seeds, portal, rep, ai)[0]


# ------------------------------------------------------------------ job entry

def run(job_id: str, tenant_id: str, kind: str, inp: dict, rep, ai) -> dict:
    if kind == "site_learn":
        return learn(job_id, tenant_id, inp, rep, ai)
    if kind == "site_refresh":
        return refresh(job_id, tenant_id, inp, rep, ai)
    return write(job_id, tenant_id, inp, rep, ai)


def _action_dict(site_id: str, name: str) -> dict:
    with session_scope() as s:
        a = sites_svc.get_action(s, site_id, name)
        return {"id": a.id, "site_id": a.site_id, "name": a.name, "implementation": a.implementation,
                "connector": a.connector, "connector_action": a.connector_action, "seed_urls": list(a.seed_urls or []),
                "mode": a.mode}


def _preview(events: list[dict]) -> dict:
    items = [{**event_data(e), "category": None} for e in events[:sites_svc.PREVIEW_ITEMS]]
    return {"items": items, "count": len(events), "checked_at": now().isoformat()}


def learn(job_id: str, tenant_id: str, inp: dict, rep, ai) -> dict:
    from scraper0.jobs.execute import JobFailed
    a = _action_dict(inp["site_id"], inp["action"])
    version = int(inp["version"])
    try:
        if a["implementation"] == "code":
            snapshot = {"implementation": "code", "connector": a["connector"], "action": a["connector_action"]}
            events = scrape_version(a, snapshot, tenant_id, rep, ai)
        else:
            events, by_method = crawl_learned(a["seed_urls"], a["site_id"].upper(), rep, ai)
            snapshot = snapshot_for(a["seed_urls"], by_method)
        preview = _preview(events)
    except Exception as e:
        msg = str(e)[:500] or type(e).__name__
        with session_scope() as s:
            v = s.scalar(select(ActionVersion).where(ActionVersion.action_id == a["id"], ActionVersion.version == version))
            if v is not None:
                v.status = "broken"
                v.preview = {"items": [], "count": 0, "checked_at": now().isoformat(), "error": msg}
        if isinstance(e, ScrapeFailed):
            raise JobFailed(msg) from e
        raise
    with session_scope() as s:
        v = s.scalar(select(ActionVersion).where(ActionVersion.action_id == a["id"], ActionVersion.version == version))
        v.preview, v.implementation_snapshot = preview, snapshot
    rep(f"version {version}: {preview['count']} item(s)", {"items": preview["count"]}, force=True)
    return {"site_id": a["site_id"], "action": a["name"], "version": version, "preview": preview,
            "implementation_snapshot": snapshot}


# ------------------------------------------------------------------ refresh + shared store

def refresh(job_id: str, tenant_id: str, inp: dict, rep, ai) -> dict:
    from scraper0.jobs.execute import JobFailed
    site_id, name = inp["site_id"], inp["action"]
    try:
        with session_scope() as s:
            act, v = sites_svc.get_locked(s, site_id, name)
            version, snapshot = v.version, dict(v.implementation_snapshot or {})
        a = _action_dict(site_id, name)
        try:
            events = scrape_version(a, snapshot, tenant_id, rep, ai)
        except Exception as e:
            record_failure(site_id, name, str(e)[:500] or type(e).__name__)
            if isinstance(e, ScrapeFailed):
                raise JobFailed(str(e)) from e
            raise
        stored = store_items(site_id, name, version, job_id, events)
        if stored is None:
            raise JobFailed("0 valid items (the last run had some)")
        rep(f"{len(events)} item(s) stored", {"items": len(events)}, force=True)
        return {"site_id": site_id, "action": name, "version": version, "count": len(events), "stored": stored}
    finally:
        with session_scope() as s:
            s.execute(update(SiteAction).where(SiteAction.site_id == site_id, SiteAction.name == name,
                                               SiteAction.refresh_job_id == job_id).values(refresh_job_id=None))


def record_failure(site_id: str, name: str, error: str) -> None:
    with session_scope() as s:
        a = sites_svc.get_action(s, site_id, name)
        a.consecutive_failures = (a.consecutive_failures or 0) + 1
        a.last_failure_at, a.last_error = now(), error
        if a.consecutive_failures >= sites_svc.BROKEN_AFTER and a.status == "locked":
            a.status = "broken"
            log.warning("action_broken", site_id=site_id, action=name, error=error)


def store_items(site_id: str, name: str, version: int, job_id: str, events: list[dict]) -> dict | None:
    """Upsert into the shared store and record the successful scrape. None = a zero-item run that counts as
    a failure (the previous run had items); stored items are then left untouched."""
    with session_scope() as s:
        a = sites_svc.get_action(s, site_id, name)
        prev = a.last_item_count or 0
    if not events and prev > 0:
        record_failure(site_id, name, f"0 valid items (the last run had {prev})")
        return None
    datas = [event_data(e) for e in events]
    copies = media.copy_images([d["image"] for d in datas if d.get("image")])   # no DB session open here
    for d in datas:
        if d.get("image") in copies:
            d["image"] = copies[d["image"]]
    seen, payload = set(), []
    for k, d in zip(batch_keys(datas), datas):
        if k not in seen:
            seen.add(k)
            payload.append((k, sites_svc.item_id(site_id, name, k), data_hash(d), json.loads(json.dumps(d, default=str))))
    try:
        return _store(site_id, name, version, job_id, payload)
    except IntegrityError:
        return _store(site_id, name, version, job_id, payload)


def _store(site_id, name, version, job_id, payload) -> dict:
    counts = {"created": 0, "updated": 0, "unchanged": 0}
    with session_scope() as s:
        a = sites_svc.get_action(s, site_id, name)
        seq = (a.scrape_seq or 0) + 1
        ids = [i for _, i, _, _ in payload]
        existing = {}
        for chunk in range(0, len(ids), 500):
            existing.update({r.id: r for r in s.scalars(select(SiteItem).where(SiteItem.id.in_(ids[chunk:chunk + 500])))})
        t = now()
        for key, iid, h, data in payload:
            it = existing.get(iid)
            if it is None:
                s.add(SiteItem(id=iid, site_id=site_id, action=name, item_key=key, data=data, content_hash=h,
                               first_seen_at=t, last_seen_at=t, updated_at=t, seen_seq=seq, version=version,
                               job_id=job_id))
                counts["created"] += 1
            else:
                if it.content_hash != h:
                    it.content_hash, it.updated_at = h, t
                    counts["updated"] += 1
                else:
                    counts["unchanged"] += 1
                if it.data != data:
                    it.data = data
                it.last_seen_at, it.seen_seq, it.version, it.job_id = t, seq, version, job_id
        a.scrape_seq, a.last_scraped_at, a.last_success_at = seq, t, t
        a.last_item_count, a.consecutive_failures, a.last_error = len(payload), 0, None
        if a.status == "broken":
            a.status = "locked"
    return counts


# ------------------------------------------------------------------ writes

def finalize_write(record_id: int, job_id: str, status: str, output=None, error: str | None = None) -> None:
    with session_scope() as s:
        s.execute(update(WriteRecord).where(WriteRecord.id == record_id, WriteRecord.job_id == job_id).values(
            status=status, output=output, error=error, updated_at=now()))


def write(job_id: str, tenant_id: str, inp: dict, rep, ai) -> dict:
    from scraper0.jobs.execute import JobFailed
    from scraper0.jobs.runner import decrypt_auth
    record_id = int(inp["write_record_id"])
    started = {"v": False}

    def progress(message: str, data: dict | None = None, **kw):
        if not started["v"] and ((data or {}).get("submit_started") or str(message).lower().startswith("submitting")):
            started["v"] = True       # persisted before the submit click: a crash after this is outcome_unknown
            with session_scope() as s:
                s.execute(update(WriteRecord).where(WriteRecord.id == record_id, WriteRecord.job_id == job_id)
                          .values(submit_started=True, updated_at=now()))
        rep(message, data, **kw)

    try:
        out = run_code(inp["connector"], inp["connector_action"], tenant_id, progress, ai, data=inp.get("data") or {},
                       auth=decrypt_auth(inp))
    except Exception as e:
        msg = str(e)[:500] or type(e).__name__
        finalize_write(record_id, job_id, "outcome_unknown" if started["v"] else "failed", error=msg)
        raise JobFailed(msg) from e
    finalize_write(record_id, job_id, "succeeded", output=out)
    return {"data": out, "write": {"status": "succeeded", "submit_started": started["v"]}}
