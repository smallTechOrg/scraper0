"""v2 calls: read (list with freshness + shared store + per-tenant AI filter; lookup) and idempotent writes.

Request-side only: jobs do the scraping (services/site_runs.py). No DB session is held while waiting or
while calling the AI.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
from datetime import date, datetime, timedelta, timezone

import structlog
from sqlalchemy import select, update
from sqlalchemy.exc import IntegrityError

from scraper0.core.dates import IST
from scraper0.core.signed_urls import expires_at, expires_at_iso
from scraper0.db.models import AIVerdict, Item, Job, SiteAction, WriteRecord, new_id, now
from scraper0.db.session import session_scope
from scraper0.jobs import runner
from scraper0.services import sites as sites_svc
from scraper0.services.sites import SiteError

log = structlog.get_logger(__name__)

DEFAULT_FRESHNESS_H = 48
DEFAULT_READ_TIMEOUT_S = 60
MAX_READ_TIMEOUT_S = 300
DEFAULT_WRITE_TIMEOUT_S = 300
MAX_WRITE_TIMEOUT_S = 600
BROKEN_RETRY = timedelta(hours=1)    # a broken action re-tries its refresh at most this often
QUEUED_TOO_LONG = timedelta(minutes=15)   # a refresh still queued after this was lost (never dispatched)
# Stored items keep the site's image links, and signed ones (Airtable, S3…) expire within hours. A read whose
# items' images expire within IMAGE_EXPIRY_MARGIN rescrapes first, as if the data were stale, so callers get
# links they can still copy. MIN_IMAGE_RESCRAPE stops a loop on sites whose links outlive a scrape by less.
IMAGE_EXPIRY_MARGIN = timedelta(minutes=10)
MIN_IMAGE_RESCRAPE = timedelta(minutes=10)
AI_BATCH = 8
PROMPT_VERSION = 1
MAX_CATEGORIES = 100


def _aware(d: datetime | None) -> datetime | None:
    return d.replace(tzinfo=timezone.utc) if d is not None and d.tzinfo is None else d


def _iso(d: datetime | None) -> str | None:
    d = _aware(d)
    return d.isoformat() if d else None


def today_ist() -> date:
    return datetime.now(IST).date()


def _num(v, name: str, default: float, lo: float, hi: float | None = None) -> float:
    if v is None:
        return default
    if isinstance(v, bool) or not isinstance(v, (int, float)):
        raise SiteError(422, "invalid_input", f"{name} must be a number")
    if v < lo or (hi is not None and v > hi):
        raise SiteError(422, "invalid_input", f"{name} must be between {lo:g} and {hi:g}" if hi is not None
                        else f"{name} must be ≥ {lo:g}")
    return float(v)


# ------------------------------------------------------------------ options

def parse_options(o) -> dict:
    if o is None:
        o = {}
    if not isinstance(o, dict):
        raise SiteError(422, "invalid_input", "options must be an object")
    out = {}
    for k in ("event_filter", "category_filter"):
        v = o.get(k) or ""
        if not isinstance(v, str):
            raise SiteError(422, "invalid_input", f"options.{k} must be a string")
        out[k] = v.strip()[:2000]
    cats = o.get("categories") or []
    if not isinstance(cats, list) or not all(isinstance(c, str) and c.strip() for c in cats) or len(cats) > MAX_CATEGORIES:
        raise SiteError(422, "invalid_input", f"options.categories must be a list of at most {MAX_CATEGORIES} strings")
    out["categories"] = list(dict.fromkeys(c.strip()[:100] for c in cats))
    inc = o.get("include_online", False)
    if not isinstance(inc, bool):
        raise SiteError(422, "invalid_input", "options.include_online must be true or false")
    out["include_online"] = inc
    frm = o.get("from")
    if frm is None:
        out["from"] = today_ist()
    else:
        try:
            if not isinstance(frm, str) or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", frm):
                raise ValueError
            out["from"] = date.fromisoformat(frm)
        except ValueError:
            raise SiteError(422, "invalid_input", "options.from must be a date (YYYY-MM-DD)") from None
    return out


def options_hash(o: dict) -> str:
    basis = {"v": PROMPT_VERSION, "event_filter": o["event_filter"], "category_filter": o["category_filter"],
             "categories": o["categories"]}
    return hashlib.sha256(json.dumps(basis, sort_keys=True).encode()).hexdigest()


# ------------------------------------------------------------------ refresh: start or join (one per site+action)

def images_expiring(s, site_id: str, name: str, seq: int) -> bool:
    """Does any item of the last scrape carry an image link that expires within IMAGE_EXPIRY_MARGIN?"""
    if not seq:
        return False
    soon = now() + IMAGE_EXPIRY_MARGIN
    for data in s.scalars(select(Item.data).where(Item.site_id == site_id, Item.action == name,
                                                   Item.seen_seq == seq)):
        exp = image_expiry(data)
        if exp is not None and exp <= soon:
            return True
    return False


def image_expiry(data: dict | None) -> datetime | None:
    """From the stored field, or read off the link itself (items stored before the field existed)."""
    data = data or {}
    return _aware(_parse_iso(data.get("image_expires_at"))) or expires_at(data.get("image"))


def _parse_iso(v) -> datetime | None:
    try:
        return datetime.fromisoformat(str(v).replace("Z", "+00:00")) if v else None
    except ValueError:
        return None


def ensure_refresh(site_id: str, name: str, tenant_id: str, version: int, freshness_h: float,
                   force: bool = False) -> str | None:
    """→ id of the refresh job to wait for (started now or already running), or None when the data is fresh.
    Fresh means scraped within freshness_h *and* no stored image link about to expire."""
    cutoff = now() - timedelta(hours=freshness_h)
    start = None
    with session_scope() as s:
        a = sites_svc.get_action(s, site_id, name)
        scraped = _aware(a.last_scraped_at)
        if scraped is not None and scraped >= cutoff:
            if scraped > now() - MIN_IMAGE_RESCRAPE or not images_expiring(s, site_id, name, a.scrape_seq or 0):
                return None
            log.info("rescrape_for_expiring_images", site_id=site_id, action=name)
        if a.refresh_job_id:
            j = s.get(Job, a.refresh_job_id)
            if j is not None and j.status == "queued" and _aware(j.created_at) < now() - QUEUED_TOO_LONG:
                j.status, j.error, j.finished_at = "failed", "never started (dispatch lost)", now()
            if j is not None and j.status not in runner.TERMINAL:
                return j.id                                     # join the running refresh
            s.execute(update(SiteAction).where(SiteAction.id == a.id, SiteAction.refresh_job_id == a.refresh_job_id)
                      .values(refresh_job_id=None))             # left over by a crashed worker
        if (not force and a.status == "broken" and a.last_failure_at
                and _aware(a.last_failure_at) > now() - BROKEN_RETRY):
            return None
        jid = new_id()
        claimed = s.execute(update(SiteAction).where(SiteAction.id == a.id, SiteAction.refresh_job_id.is_(None))
                            .values(refresh_job_id=jid)).rowcount
        if claimed:
            runner.add_job(s, tenant_id, "site_refresh", {"site_id": site_id, "action": name, "version": version},
                           job_id=jid)
            start = jid
        else:
            s.expire_all()
            other = s.scalar(select(SiteAction.refresh_job_id).where(SiteAction.id == a.id))
            return other
    try:
        runner.dispatch(start)
    except Exception as e:          # e.g. Cloud Tasks unreachable: don't leave a claim nobody will run
        log.warning("refresh_dispatch_failed", site_id=site_id, action=name, error=str(e)[:200])
        with session_scope() as s:
            s.execute(update(Job).where(Job.id == start, Job.status == "queued").values(
                status="failed", error=f"dispatch failed: {str(e)[:200]}", finished_at=now()))
            s.execute(update(SiteAction).where(SiteAction.site_id == site_id, SiteAction.name == name,
                                               SiteAction.refresh_job_id == start).values(refresh_job_id=None))
        return None
    return start


def refresh_now(tenant_id: str, site_id: str, name: str, force: bool = True) -> str | None:
    """Refresh a locked list action now, ignoring freshness (dashboard "Refresh now", scheduled sources).
    Joins a refresh that is already running. `force=False` (schedules) keeps a broken action's hourly retry
    limit. → the job id, or None when nothing was started."""
    with session_scope() as s:
        a, v = sites_svc.get_locked(s, site_id, name)
        if a.mode != "list":
            raise SiteError(422, "invalid_input", f"{name} is a {a.mode} action; only list actions refresh")
        version = v.version
    return ensure_refresh(site_id, name, tenant_id, version, 0, force=force)


def wait_job(job_id: str, timeout_s: float) -> str | None:
    """Poll until the job ends or the timeout passes → its final status, or None if still running."""
    deadline = time.monotonic() + timeout_s
    delay = 0.05
    while True:
        with session_scope() as s:
            st = s.scalar(select(Job.status).where(Job.id == job_id))
        if st is None or st in runner.TERMINAL:
            return st or "failed"
        left = deadline - time.monotonic()
        if left <= 0:
            return None
        time.sleep(min(delay, left))
        delay = min(delay * 1.5, 1.0)


# ------------------------------------------------------------------ AI filter + categories (cached per tenant)

_PROMPT = """You review events scraped from a website for one client. Decide for each event whether it matches the
client's relevance filter{cat_task}.
Relevance filter: {event_filter}
Additional context: {category_filter}
{categories}Respond ONLY with a JSON array, one object per event: [{{"index": 0, "select": true{cat_field}}}]
Events:
{listing}"""


def _prompt(o: dict, batch: list[dict]) -> str:
    cats = o["categories"]
    listing = "\n".join(
        f"[{j}] {d.get('name') or ''} | {d.get('organisation') or ''} | {d.get('address') or ''} | "
        f"{d.get('start_time') or ''} | {(d.get('description') or '')[:300]}" for j, d in enumerate(batch))
    return _PROMPT.format(
        cat_task=", and pick exactly one category from the client's list" if cats else "",
        event_filter=o["event_filter"] or "(none: select every event)",
        category_filter=o["category_filter"] or "(none)",
        categories=f"Categories (pick exactly one, verbatim): {', '.join(cats)}\n" if cats else "",
        cat_field=', "category": "<one of the categories>"' if cats else "",
        listing=listing)


def _loads(text: str):
    t = text.strip().strip("`")
    t = re.sub(r"^json\s*", "", t)
    m = re.search(r"\[.*\]", t, re.S)
    return json.loads(m.group(0) if m else t)


def _truthy(v) -> bool:
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1")
    return bool(v)


def _pick_category(v, cats: list[str]) -> str | None:
    if not cats:
        return None
    if isinstance(v, str):
        exact = next((c for c in cats if c == v.strip()), None) or \
            next((c for c in cats if c.lower() == v.strip().lower()), None)
        if exact:
            return exact
    return next((c for c in cats if c.upper() == "OTHER"), cats[-1])


def ai_verdicts(tenant_id: str, rows: list[dict], o: dict) -> tuple[dict[str, tuple[bool, str | None]], str | None]:
    """rows = [{id, content_hash, data}] → ({item id: (selected, category)}, ai_error)."""
    from scraper0.core.llm import LLMError
    from scraper0.services.ai import AIClient, BudgetExceeded, NoProvider
    if not (o["event_filter"] or o["category_filter"] or o["categories"]):
        return {r["id"]: (True, None) for r in rows}, None
    oh = options_hash(o)
    out: dict[str, tuple[bool, str | None]] = {}
    with session_scope() as s:
        ids = [r["id"] for r in rows]
        cached = {}
        for i in range(0, len(ids), 500):
            for v in s.scalars(select(AIVerdict).where(AIVerdict.tenant_id == tenant_id, AIVerdict.options_hash == oh,
                                                       AIVerdict.item_id.in_(ids[i:i + 500]))):
                cached[(v.item_id, v.content_hash)] = (v.selected, v.category)
    todo = []
    for r in rows:
        hit = cached.get((r["id"], r["content_hash"]))
        if hit is not None:
            out[r["id"]] = hit
        else:
            todo.append(r)
    error = None
    client = AIClient(tenant_id)
    filtering = bool(o["event_filter"] or o["category_filter"])
    for n in range(0, len(todo), AI_BATCH):
        batch = todo[n:n + AI_BATCH]
        try:
            res = _loads(client.text(_prompt(o, [r["data"] for r in batch]), purpose="filter"))
            if not isinstance(res, list):
                raise ValueError("AI answer is not a JSON array")
            by_index = {}
            for v in res:
                if isinstance(v, dict) and str(v.get("index", "")).strip().isdigit():
                    by_index[int(str(v["index"]).strip())] = v
        except (NoProvider, BudgetExceeded) as e:      # nothing more will work this request
            error = str(e)
            for r in todo[n:]:
                out[r["id"]] = (True, None)
            break
        except (LLMError, ValueError, Exception) as e:
            error = f"AI filter failed: {str(e)[:300]}"
            for r in batch:
                out[r["id"]] = (True, None)
            continue
        fresh = []
        for j, r in enumerate(batch):
            v = by_index.get(j)
            if v is None:
                error = error or "AI returned no verdict for some items"
                out[r["id"]] = (True, None)
                continue
            verdict = (_truthy(v.get("select", True)) if filtering else True, _pick_category(v.get("category"), o["categories"]))
            out[r["id"]] = verdict
            fresh.append(AIVerdict(tenant_id=tenant_id, item_id=r["id"], content_hash=r["content_hash"],
                                   options_hash=oh, selected=verdict[0], category=verdict[1]))
        if fresh:
            try:
                with session_scope() as s:
                    for f in fresh:
                        s.merge(f)
            except IntegrityError:                     # a concurrent read cached the same verdicts first
                pass
    return out, error


# ------------------------------------------------------------------ read: list

def _starts_on_or_after(d: dict, day: date) -> bool:
    st = d.get("start_time")
    if not st:
        return False
    try:
        t = datetime.fromisoformat(str(st).replace("Z", "+00:00"))
    except ValueError:
        return False
    t = t if t.tzinfo else t.replace(tzinfo=timezone.utc)
    return t.astimezone(IST).date() >= day


def read_list(tenant_id: str, site_id: str, name: str, body: dict) -> dict:
    o = parse_options(body.get("options"))
    freshness = _num(body.get("freshness_hours"), "freshness_hours", DEFAULT_FRESHNESS_H, 0)
    timeout = _num(body.get("timeout_s"), "timeout_s", DEFAULT_READ_TIMEOUT_S, 0, MAX_READ_TIMEOUT_S)
    with session_scope() as s:
        a, v = sites_svc.get_locked(s, site_id, name)
        if a.mode != "list":
            raise SiteError(422, "invalid_input", f"{name} is a {a.mode} action: "
                            + ("send input" if a.mode == "lookup" else f"POST …/{name}/write"))
        version = v.version
    job_id = ensure_refresh(site_id, name, tenant_id, version, freshness)
    complete = True
    if job_id:
        complete = wait_job(job_id, timeout) is not None
    with session_scope() as s:
        a = sites_svc.get_action(s, site_id, name)
        seq, health, scraped_at = a.scrape_seq or 0, "broken" if a.status == "broken" else "ok", _iso(a.last_scraped_at)
        rows = [] if seq == 0 else [
            {"id": i.id, "updated_at": _iso(i.updated_at), "content_hash": i.content_hash, "data": dict(i.data)}
            for i in s.scalars(select(Item).where(Item.site_id == site_id, Item.action == name,
                                                  Item.seen_seq == seq))]
    rows = [r for r in rows if _starts_on_or_after(r["data"], o["from"])
            and (o["include_online"] or not r["data"].get("is_online"))]
    rows.sort(key=lambda r: (r["data"].get("start_time") or "9999", r["data"].get("name") or "", r["id"]))
    verdicts, ai_error = ai_verdicts(tenant_id, rows, o)
    items = []
    for r in rows:
        sel, cat = verdicts.get(r["id"], (True, None))
        if sel:
            data = {**r["data"], "category": cat}
            if "image_expires_at" not in data:
                data["image_expires_at"] = expires_at_iso(data.get("image"))
            items.append({"id": r["id"], "updated_at": r["updated_at"], "data": data})
    out = {"site_id": site_id, "action": name, "version": version, "items": items, "complete": complete,
           "scraped_at": scraped_at, "refresh_job_id": job_id, "health": health}
    if ai_error:
        out["ai_error"] = ai_error
    return out


# ------------------------------------------------------------------ read: lookup (input → output)

def read_lookup(tenant_id: str, site_id: str, name: str, body: dict) -> tuple[int, dict]:
    timeout = _num(body.get("timeout_s"), "timeout_s", DEFAULT_READ_TIMEOUT_S, 0, MAX_READ_TIMEOUT_S)
    inp, auth = body.get("input"), body.get("auth")
    if not isinstance(inp, dict):
        raise SiteError(422, "invalid_input", "input must be an object")
    if auth is not None and not isinstance(auth, dict):
        raise SiteError(422, "invalid_input", "auth must be an object")
    with session_scope() as s:
        a, v = sites_svc.get_locked(s, site_id, name)
        if a.mode != "lookup":
            raise SiteError(422, "invalid_input", f"{name} is not a lookup action")
        version, conn, conn_action = v.version, a.connector, a.connector_action
    job = {"connector": conn, "action": conn_action, "data": inp, "site_id": site_id, "site_action": name}
    if auth:
        job["auth"] = auth
    job_id = runner.submit(tenant_id, "connector", job)
    runner.dispatch(job_id)
    st = wait_job(job_id, timeout)
    if st is None:
        return 202, {"job_id": job_id}
    with session_scope() as s:
        j = s.get(Job, job_id)
        if j.status != "succeeded":
            raise SiteError(422, "action_failed", j.error or "the action failed")
        return 200, {"site_id": site_id, "action": name, "version": version, "output": (j.result or {}).get("data")}


# ------------------------------------------------------------------ write (idempotent)

def default_key(tenant_id: str, site_id: str, name: str, inp: dict) -> str:
    canon = json.dumps(inp, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(f"{tenant_id}|{site_id}|{name}|{canon}".encode()).hexdigest()


def _reconcile(s, rec: WriteRecord) -> None:
    """A record still 'running' whose job is gone or ended without finalising (worker crash)."""
    if rec.status != "running" or not rec.job_id:
        return
    j = s.get(Job, rec.job_id)
    if j is not None and j.status not in runner.TERMINAL:
        return
    if j is not None and j.status == "succeeded":
        rec.status, rec.output = "succeeded", (j.result or {}).get("data")
    else:
        rec.status = "outcome_unknown" if rec.submit_started else "failed"
        rec.error = (j.error if j else None) or "the write job ended without a result"
    rec.updated_at = now()


def _snap(rec: WriteRecord) -> dict:
    return {"status": rec.status, "output": rec.output, "idem_key": rec.idem_key, "job_id": rec.job_id,
            "error": rec.error}


def _answer(rec: dict, replayed: bool) -> tuple[int, dict]:
    """Called after the session closed (raising inside it would roll back the reconcile)."""
    if rec["status"] == "succeeded":
        return 200, {"status": "succeeded", "output": rec["output"], "idempotency_key": rec["idem_key"],
                     "job_id": rec["job_id"], "replayed": replayed}
    if rec["status"] == "running":
        return 202, {"status": "running", "idempotency_key": rec["idem_key"], "job_id": rec["job_id"],
                     "replayed": replayed}
    if rec["status"] == "outcome_unknown":
        raise SiteError(409, "outcome_unknown", (rec["error"] or "the write may have been submitted")
                        + " — check the site; retry only with force: true or a new Idempotency-Key",
                        job_id=rec["job_id"])
    raise SiteError(422, "action_failed", rec["error"] or "the write failed")


def write(tenant_id: str, site_id: str, name: str, body: dict, idem_key: str | None) -> tuple[int, dict]:
    timeout = _num(body.get("timeout_s"), "timeout_s", DEFAULT_WRITE_TIMEOUT_S, 0, MAX_WRITE_TIMEOUT_S)
    inp, auth, force = body.get("input"), body.get("auth"), body.get("force", False)
    if not isinstance(inp, dict):
        raise SiteError(422, "invalid_input", "input must be an object")
    if auth is not None and not isinstance(auth, dict):
        raise SiteError(422, "invalid_input", "auth must be an object")
    if not isinstance(force, bool):
        raise SiteError(422, "invalid_input", "force must be true or false")
    if idem_key is not None:
        idem_key = idem_key.strip()
        if not idem_key or len(idem_key) > 200:
            raise SiteError(422, "invalid_input", "Idempotency-Key must be 1–200 characters")
    with session_scope() as s:
        a, v = sites_svc.get_locked(s, site_id, name)
        if a.kind != "write":
            raise SiteError(422, "invalid_input", f"{name} is a read action: POST …/{name}/read")
        conn, conn_action = a.connector, a.connector_action
    key = idem_key or default_key(tenant_id, site_id, name, inp)
    job_id = record_id = replay = None
    for attempt in range(2):
        try:
            with session_scope() as s:
                rec = s.scalar(select(WriteRecord).where(WriteRecord.tenant_id == tenant_id, WriteRecord.site_id == site_id,
                                                         WriteRecord.action == name, WriteRecord.idem_key == key))
                if rec is not None and not force:
                    _reconcile(s, rec)
                    if rec.status != "failed":
                        replay = _snap(rec)
                        break
                if rec is None:
                    rec = WriteRecord(tenant_id=tenant_id, site_id=site_id, action=name, idem_key=key, status="running")
                    s.add(rec)
                    s.flush()
                job = {"site_id": site_id, "site_action": name, "connector": conn, "connector_action": conn_action,
                       "data": inp, "write_record_id": rec.id, "idempotency_key": key}
                if auth:
                    job["auth"] = auth
                j = runner.add_job(s, tenant_id, "site_write", job)
                rec.status, rec.job_id, rec.submit_started, rec.output, rec.error, rec.updated_at = \
                    "running", j.id, False, None, None, now()
                job_id, record_id = j.id, rec.id
            break
        except IntegrityError:          # a concurrent call with the same key created the record first
            if attempt:
                raise
    if replay is not None:
        return _answer(replay, replayed=True)
    runner.dispatch(job_id)
    st = wait_job(job_id, timeout)
    with session_scope() as s:
        if st is None:                  # timed out: it may have been submitted, never retried automatically
            s.execute(update(WriteRecord).where(WriteRecord.id == record_id, WriteRecord.job_id == job_id,
                                                WriteRecord.status == "running").values(
                status="outcome_unknown", error=f"the write did not finish within {timeout:g}s", updated_at=now()))
        rec = s.get(WriteRecord, record_id)
        s.refresh(rec)
        _reconcile(s, rec)
        snap = _snap(rec)
    return _answer(snap, replayed=False)
