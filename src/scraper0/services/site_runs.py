"""v2 jobs: learn a draft version, refresh a locked list action into the shared item store, run a write.

Network and AI work never happens while a DB session is open (pool cap): every DB step is its own short session.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timedelta, timezone

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from scraper0.core.dates import IST
from scraper0.core.signed_urls import expires_at_iso
from scraper0.core.entities import EVENT_SPEC
from scraper0.db.models import ActionVersion, SiteAction, WriteRecord, now
from scraper0.db.session import session_scope
from scraper0.services import sites as sites_svc
from scraper0.services import items as items_svc
from scraper0.services.items import batch_keys

log = structlog.get_logger(__name__)

EVENT_FIELDS = ("name", "start_time", "end_time", "address", "link", "organisation", "image", "description",
                "latitude", "longitude", "is_online", "price", "source_url")
# CDN/signed image URLs change on every scrape without the event changing. scraper0 stores image *links* only;
# copying the image is the caller's job, and image_expires_at says how soon (core/signed_urls.py).
_HASH_EXCLUDE = {"image", "image_expires_at"}
KEEP_PAST_DAYS = 1               # the store keeps items from yesterday (IST) on; older ones are not stored


class ScrapeFailed(Exception):
    pass


# ------------------------------------------------------------------ item shape

def event_data(d: dict) -> dict:
    """Engine/connector event dict → EventData (without `category`, which is per tenant)."""
    out = {k: d.get(k) for k in EVENT_FIELDS}
    out["image_expires_at"] = expires_at_iso(out["image"])
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


def _structured(portal: str):
    """JSON-LD / microdata reader handed to plan runs (no AI)."""
    from scraper0.events.engine import event_to_item
    from scraper0.events.extractors import extract_all

    def read(html: str, url: str) -> list[dict]:
        return [event_to_item(e) for e in extract_all(html, url, portal, "text/html", heuristic=False)]
    return read


def learn_plans(seeds: list[str], portal: str, rep, ai) -> dict:
    """Learn the site from its seed URLs (AI allowed) → the version's implementation snapshot:
    {"implementation": "learned", "plans": [{"seed", "plan"}], "learn": {...}}. Each seed yields one plan:
    the API plan when the page loads its data from one, else list page → detail pages with the layout rules
    worked out during this learn. Every plan is then run cold (no browser, no AI) and must produce items."""
    from scraper0.core import plans
    from scraper0.core.recipes import domain_of, path_pattern
    from scraper0.events.engine import Engine, Options
    fetcher = _fetcher()
    eng = Engine(fetcher, llm=ai.text_fn("extract"), filter_llm=ai.text_fn("filter"))
    out, notes, by_method = [], [], {}
    for seed in seeds:
        eng.last_crawl_stats = []
        events = eng.run([seed], Options(portal=portal, include_past=True, include_online=True, learn=True,
                                         use_llm_extraction=True, progress=rep, cancelled=rep.cancelled))["events"]
        stats = eng.last_crawl_stats[0] if eng.last_crawl_stats else {}
        for k, n in (stats.get("by_method") or {}).items():
            by_method[k] = by_method.get(k, 0) + n
        notes += stats.get("notes") or []
        plan = stats.get("plan")
        if plan is None:
            pats = sorted({path_pattern(e.get("source_url") or e.get("link")) for e in events
                           if (e.get("source_url") or e.get("link")) and
                           domain_of(e.get("source_url") or e.get("link")) == domain_of(seed)} - {path_pattern(seed)})
            if not pats:
                notes.append(f"{seed}: no detail pages with items were found")
                continue
            layouts = [r for r in eng.store.recipes_for(domain_of(seed), EVENT_SPEC.key) if r["strategy"] == "selectors"]
            plan = plans.from_selectors(layouts, seed, patterns=pats,
                                        navigate=plans.learn_navigate(fetcher, seed))
        out.append({"seed": seed, "plan": plan})
    if not out:
        raise ScrapeFailed("nothing could be learned: " + ("; ".join(notes)[:400] or "no items found"))
    return {"implementation": "learned", "entity": EVENT_SPEC.name, "seed_urls": list(seeds), "plans": out,
            "learn": {"by_method": by_method, "ai_calls": ai.calls, "notes": notes}}


def run_plans(snapshot: dict, portal: str, rep, ai) -> tuple[list[dict], list[dict]]:
    """Run a learned version's plans → (normalised events, per-plan run stats). No browser; AI only in plan steps
    that declare it."""
    from scraper0.core import plans
    fetcher = _fetcher()
    raw, runs = [], []
    for entry in snapshot.get("plans") or []:
        try:
            items, st = plans.run_plan(entry["plan"], fetcher, EVENT_SPEC, entry["seed"], structured=_structured(portal),
                                       progress=rep, ai=ai)
        except plans.PlanError as e:
            raise ScrapeFailed(f"{entry['seed']}: {e}") from e
        for it in items:
            it.setdefault("_meta", {"url": it.get("link") or entry["seed"], "extraction": "plan"})
        raw += items
        runs.append({"seed": entry["seed"], **st.as_dict()})
        rep(f"{entry['seed']} → {len(items)} item(s)", {"pages": st.pages})
    if not snapshot.get("plans"):
        raise ScrapeFailed("this version has no plan: run repair to learn one")
    return normalise(raw, portal), runs


def scrape_version(action: dict, snapshot: dict, tenant_id: str, rep, ai) -> tuple[list[dict], list[dict]]:
    """Run one version of a list action → (normalised event dicts, run stats)."""
    portal = action["site_id"].upper()
    if action["implementation"] == "code":
        out = run_code(action["connector"], action["connector_action"], tenant_id, rep, ai, data={})
        events = out.get("events") if isinstance(out, dict) else None
        if not isinstance(events, list):
            raise ScrapeFailed("connector returned no events list")
        return normalise(events, events[0].get("portal") or portal if events else portal), []
    return run_plans(snapshot, portal, rep, ai)


# ------------------------------------------------------------------ job entry

def run(job_id: str, tenant_id: str, kind: str, inp: dict, rep, ai) -> dict:
    if kind == "site_learn":
        return learn(job_id, tenant_id, inp, rep, ai)
    if kind == "site_refresh":
        return refresh(job_id, tenant_id, inp, rep, ai)
    if kind == "site_lookup":
        return lookup(job_id, tenant_id, inp, rep, ai)
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
        else:
            snapshot = learn_plans(a["seed_urls"], a["site_id"].upper(), rep, ai)
        events, runs = scrape_version(a, snapshot, tenant_id, rep, ai)     # the preview is what refreshes will do
        if a["implementation"] != "code" and not events:
            raise ScrapeFailed("the learned plan produced no items when run without AI")
        if runs:
            snapshot["check_run"] = runs
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
            events, runs = scrape_version(a, snapshot, tenant_id, rep, ai)
        except Exception as e:
            record_failure(site_id, name, str(e)[:500] or type(e).__name__)
            if isinstance(e, ScrapeFailed):
                raise JobFailed(str(e)) from e
            raise
        stored = store_items(site_id, name, version, job_id, events)
        if stored is None:
            raise JobFailed("0 valid items (the last run had some)")
        rep(f"{len(events)} item(s) stored", {"items": len(events)}, force=True)
        out = {"site_id": site_id, "action": name, "version": version, "count": len(events), "stored": stored}
        if runs:
            out["plan_runs"] = runs
        return out
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
    with session_scope() as s:
        a = sites_svc.get_action(s, site_id, name)
        seq = (a.scrape_seq or 0) + 1
        counts = items_svc.upsert_rows(s, payload, entity_key=EVENT_SPEC.key, site_id=site_id, action=name,
                                       job_id=job_id, seq=seq)
        t = now()
        a.scrape_seq, a.last_scraped_at, a.last_success_at = seq, t, t
        a.last_item_count, a.consecutive_failures, a.last_error = len(payload), 0, None
        if a.status == "broken":
            a.status = "locked"
    return counts


# ------------------------------------------------------------------ action plans (lookups and writes as data)

def _action_renderer():
    """The browser action plans run in (tests swap this for one that may reach their local fake site)."""
    from scraper0.core.render import shared_renderer
    return shared_renderer()


def _version_snapshot(site_id: str, name: str, version: int | None) -> dict:
    with session_scope() as s:
        a = sites_svc.get_action(s, site_id, name)
        v = s.scalar(select(ActionVersion).where(ActionVersion.action_id == a.id,
                                                 ActionVersion.version == (version or a.locked_version)))
        if v is None:
            raise ScrapeFailed(f"action {name!r} has no version {version}")
        return dict(v.implementation_snapshot or {})


def run_action(site_id: str, plan: dict, tenant_id: str, data: dict, auth: dict | None, rep, ai, on_commit=None,
               timeout_s: float = 420) -> tuple[dict, dict]:
    """Run one action plan as this tenant → (output, run stats). The tenant's session for the site is reused and
    saved through the encrypted per-tenant state store. Raises action_plans.ActionError."""
    from scraper0.core import action_plans
    from scraper0.services.connector_state import DBStateStore
    state = DBStateStore(tenant_id, f"site:{site_id}"[:64])
    return action_plans.run_action_plan(plan, _action_renderer(), data, auth, ai=ai, state=state, progress=rep,
                                        on_commit=on_commit, timeout_s=timeout_s)


def lookup(job_id: str, tenant_id: str, inp: dict, rep, ai) -> dict:
    """A plan lookup (e.g. complaint status): input → output."""
    from scraper0.core.plans import PlanError
    from scraper0.jobs.execute import JobFailed
    from scraper0.jobs.runner import decrypt_auth
    snapshot = _version_snapshot(inp["site_id"], inp["action"], inp.get("version"))
    try:
        out, stats = run_action(inp["site_id"], snapshot["plan"], tenant_id, inp.get("data") or {}, decrypt_auth(inp),
                                rep, ai, timeout_s=150)
    except PlanError as e:
        raise JobFailed(str(e)[:500]) from e
    return {"data": out, "plan_run": stats}


def verify_write(site_id: str, spec: dict, output: dict, tenant_id: str, rep, ai) -> dict:
    """After a write succeeded: confirm it with another action of the same site (a lookup of what was just
    created). → {"verified": bool, "action", "output" | "error"}; never raises — the write already happened."""
    from scraper0.core.plans import render
    try:
        data = render(spec.get("input") or {}, {"output": output})
        with session_scope() as s:
            a, v = sites_svc.get_locked(s, site_id, spec["action"])
            if a.mode != "lookup":
                raise ScrapeFailed(f"{spec['action']} is not a lookup action")
            impl, conn, conn_action = a.implementation, a.connector, a.connector_action
            snapshot = dict(v.implementation_snapshot or {})
        if impl == "plan":
            got, _ = run_action(site_id, snapshot["plan"], tenant_id, data, None, rep, ai, timeout_s=150)
        else:
            got = run_code(conn, conn_action, tenant_id, rep, ai, data=data)
        return {"verified": True, "action": spec["action"], "output": got}
    except Exception as e:  # noqa: BLE001
        return {"verified": False, "action": spec["action"], "error": str(e)[:300] or type(e).__name__}


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

    def on_commit():
        """A plan's commit step is about to run: persist it first, so a crash from here on is outcome_unknown."""
        started["v"] = True
        with session_scope() as s:
            s.execute(update(WriteRecord).where(WriteRecord.id == record_id, WriteRecord.job_id == job_id)
                      .values(submit_started=True, updated_at=now()))

    stats = verify = None
    try:
        if inp.get("connector"):
            out = run_code(inp["connector"], inp["connector_action"], tenant_id, progress, ai,
                           data=inp.get("data") or {}, auth=decrypt_auth(inp))
        else:
            snapshot = _version_snapshot(inp["site_id"], inp["site_action"], inp.get("version"))
            out, stats = run_action(inp["site_id"], snapshot["plan"], tenant_id, inp.get("data") or {},
                                    decrypt_auth(inp), rep, ai, on_commit=on_commit)
            verify = snapshot["plan"].get("verify")
    except Exception as e:
        msg = str(e)[:500] or type(e).__name__
        committed = started["v"] or bool(getattr(e, "committed", False))
        finalize_write(record_id, job_id, "outcome_unknown" if committed else "failed", error=msg)
        if getattr(e, "stats", None):
            log.info("write_plan_failed", site_id=inp.get("site_id"), action=inp.get("site_action"), run=e.stats)
        raise JobFailed(msg) from e
    finalize_write(record_id, job_id, "succeeded", output=out)        # recorded before verifying: it happened
    result = {"data": out, "write": {"status": "succeeded", "submit_started": started["v"]}}
    if stats is not None:
        result["plan_run"] = stats
    if verify:
        rep("Verifying", {"action": verify["action"]})
        result["write"]["verify"] = verify_write(inp["site_id"], verify, out, tenant_id, rep, ai)
        result["write"]["verified"] = result["write"]["verify"]["verified"]
    return result
