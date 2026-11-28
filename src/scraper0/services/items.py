"""The one item store (table `items`) and its change feed.

Site items are shared by every tenant: whatever run scrapes a site — a v2 refresh, a v1 crawl or connector
job, by any tenant — upserts the same rows, keyed (site_id, action, item_key), with
id = itm_ + sha256(site_id|action|item_key)[:24]. Each run bumps `last_seen_at` on every item it saw and
`updated_at` only when the content changed. Private items (uploads, tenant-defined entities) belong to one
tenant and are visible to it only.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import String, cast, func, or_, select
from sqlalchemy.exc import IntegrityError

from scraper0.core.entities import EVENT_SPEC
from scraper0.db.models import Item, ItemChange, Site, now
from scraper0.db.session import session_scope

# fields that change on every scrape without the item really changing
_VOLATILE = {"_meta", "confidence", "extraction", "sources", "warnings", "image"}
SORTS = {"last_seen": "last_seen_at", "updated": "updated_at", "first_seen": "first_seen_at"}


# ------------------------------------------------------------------ keys and ids

def item_key(data: dict) -> str:
    link = data.get("link") or (data.get("_meta") or {}).get("url")
    if link and not str(link).startswith("upload:"):
        return str(link)[:500]
    basis = json.dumps({k: data.get(k) for k in sorted(data) if not k.startswith("_")}, sort_keys=True, default=str)
    return "fp:" + hashlib.sha256(basis.encode()).hexdigest()[:40]


def _identity(data: dict) -> str:
    ident = {k: data.get(k) for k in ("name", "title", "start_time") if data.get(k) is not None}
    return hashlib.sha256(json.dumps(ident, sort_keys=True, default=str).encode()).hexdigest()[:12]


def batch_keys(items: list[dict]) -> list[str]:
    """item_key per item; when several items share a link (a listing page whose events have no URLs
    of their own), disambiguate with a stable hash of their identity fields so none overwrite each other."""
    keys = [item_key(d) for d in items]
    dup = {k for k in keys if keys.count(k) > 1}
    return [f"{k}#{_identity(d)}"[:500] if k in dup else k for k, d in zip(keys, items)]


def content_hash(data: dict) -> str:
    stable = {k: v for k, v in data.items() if k not in _VOLATILE}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()


def site_item_id(site_id: str, action: str, key: str) -> str:
    return "itm_" + hashlib.sha256(f"{site_id}|{action}|{key}".encode()).hexdigest()[:24]


def private_item_id(tenant_id: str, entity_key: str, key: str) -> str:
    return "itm_" + hashlib.sha256(f"tenant:{tenant_id}|{entity_key}|{key}".encode()).hexdigest()[:24]


def is_shared_entity(entity_key: str) -> bool:
    """Built-in entities (event, …) are shared via sites; tenant-defined ones ("<tenant>:<name>") stay private."""
    return ":" not in entity_key


def action_for(entity_key: str) -> str:
    """The site action a v1 run of this entity feeds: fetch_events for events, else fetch_<entity>."""
    return "fetch_events" if entity_key == EVENT_SPEC.key else f"fetch_{entity_key}"


def _site_of(url: str | None) -> tuple[str, str, str] | None:
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    from scraper0.services.sites import SiteError, parse_base_url
    try:
        return parse_base_url(url)
    except SiteError:
        return None


# ------------------------------------------------------------------ writes

def _payload_event(datas: list[dict], site_id: str, action: str) -> list[tuple]:
    """Events take the same path as a v2 refresh (engine postprocess, EventData shape, past ones dropped), so
    a v1 run and a v2 refresh of a site produce the same ids and data."""
    from scraper0.services.site_runs import data_hash, event_data, normalise
    portal = next((str(d["portal"]) for d in datas if d.get("portal")), site_id.upper())
    norm = [event_data(d) for d in normalise(datas, portal)]
    return _payload(norm, data_hash, lambda k: site_item_id(site_id, action, k))


def _payload(datas: list[dict], hasher, id_of) -> list[tuple]:
    """→ [(item_key, id, content_hash, json-safe data)], exact duplicates within one batch counted once."""
    seen, out = set(), []
    for k, d in zip(batch_keys(datas), datas):
        if k not in seen:
            seen.add(k)
            out.append((k, id_of(k), hasher(d), json.loads(json.dumps(d, default=str))))
    return out


def upsert_rows(s, payload: list[tuple], *, entity_key: str, site_id: str | None, action: str | None,
                tenant_id: str | None, job_id: str | None, seq: int | None = None) -> dict:
    """Upsert in an open session. `seq` (v2 refreshes only) marks the rows as seen by that refresh; other runs
    leave seen_seq alone, so a v2 read keeps returning exactly what its last refresh saw."""
    counts = {"created": 0, "updated": 0, "unchanged": 0}
    ids = [i for _, i, _, _ in payload]
    existing: dict[str, Item] = {}
    for chunk in range(0, len(ids), 500):
        existing.update({r.id: r for r in s.scalars(select(Item).where(Item.id.in_(ids[chunk:chunk + 500])))})
    t = now()
    for key, iid, h, data in payload:
        it = existing.get(iid)
        if it is None:
            s.add(Item(id=iid, site_id=site_id, action=action, entity_key=entity_key, tenant_id=tenant_id,
                       item_key=key, data=data, content_hash=h, first_seen_at=t, last_seen_at=t, updated_at=t,
                       seen_seq=seq or 0, job_id=job_id))
            s.add(ItemChange(item_id=iid, change="created"))
            counts["created"] += 1
            continue
        if it.content_hash != h:
            it.content_hash, it.updated_at = h, t
            s.add(ItemChange(item_id=iid, change="updated"))
            counts["updated"] += 1
        else:
            counts["unchanged"] += 1
        if it.data != data:                       # e.g. a new image copy: not a content change
            it.data = data
        it.last_seen_at, it.job_id = t, job_id
        if seq is not None:
            it.seen_seq = seq
    return counts


def _write(payload, **kw) -> dict:
    try:
        with session_scope() as s:
            return upsert_rows(s, payload, **kw)
    except IntegrityError:                     # a concurrent run inserted one of these first: retry as update
        with session_scope() as s:
            return upsert_rows(s, payload, **kw)


def ensure_site(site_id: str, base_url: str, name: str, tenant_id: str | None) -> None:
    """Register a site seen by a v1 run (not onboarding: it gets no actions, so callers still can't read it
    through v2 until an admin onboards and locks an action)."""
    with session_scope() as s:
        if s.get(Site, site_id) is None:
            s.add(Site(site_id=site_id, base_url=base_url, name=name, created_by_tenant=tenant_id))


def upsert_site_items(site_id: str, action: str, entity_key: str, items: list[dict], job_id: str | None) -> dict:
    """v1 runs (crawl / connector) of a site: same rows a v2 refresh of that site + action writes."""
    if not items:
        return {"created": 0, "updated": 0, "unchanged": 0}
    if entity_key == EVENT_SPEC.key:
        payload = _payload_event(items, site_id, action)
    else:
        payload = _payload(items, content_hash, lambda k: site_item_id(site_id, action, k))
    return _write(payload, entity_key=entity_key, site_id=site_id, action=action, tenant_id=None, job_id=job_id)


def upsert_private_items(tenant_id: str, entity_key: str, items: list[dict], job_id: str | None = None,
                         site_id: str | None = None) -> dict:
    if not items:
        return {"created": 0, "updated": 0, "unchanged": 0}
    payload = _payload(items, content_hash, lambda k: private_item_id(tenant_id, entity_key, k))
    return _write(payload, entity_key=entity_key, site_id=site_id, action=None, tenant_id=tenant_id, job_id=job_id)


def _add(total: dict, part: dict) -> dict:
    for k, v in part.items():
        total[k] = total.get(k, 0) + v
    return total


def store_run_items(tenant_id: str, entity_key: str, items: list[dict], job_id: str | None = None,
                    seeds: list[str] | None = None, site: tuple[str, str] | None = None,
                    private: bool = False) -> dict:
    """Store what a v1 job returned → {created, updated, unchanged}.

    - `site=(site_id, action)`: a known site (a connector's code action).
    - `seeds`: a crawl — each item belongs to the site of the seed it came from (one seed: that one; several:
      the seed whose host matches the item's source/link, else the first).
    - neither: the site of each item's own link. Items with no site, uploads (`private`) and tenant-defined
      entities are stored privately for the tenant."""
    total = {"created": 0, "updated": 0, "unchanged": 0}
    if not items:
        return total
    if private or not is_shared_entity(entity_key):
        site_id = None
        if not private and seeds:
            got = _site_of(seeds[0])
            site_id = got[0] if got else None
        return upsert_private_items(tenant_id, entity_key, items, job_id, site_id)
    seed_sites = [g for g in (_site_of(u) for u in seeds or []) if g]
    groups: dict[tuple[str, str], list[dict]] = {}
    loose: list[dict] = []
    for d in items:
        if site is not None:
            groups.setdefault(site, []).append(d)
            continue
        own = _site_of(d.get("source_url")) or _site_of(d.get("link")) or _site_of((d.get("_meta") or {}).get("url"))
        if seed_sites:
            pick = next((g for g in seed_sites if own and g[0] == own[0]), seed_sites[0])
        else:
            pick = own
        if pick is None:
            loose.append(d)
            continue
        ensure_site(pick[0], pick[1], pick[2], tenant_id)
        groups.setdefault((pick[0], action_for(entity_key)), []).append(d)
    for (site_id, action), group in groups.items():
        _add(total, upsert_site_items(site_id, action, entity_key, group, job_id))
    if loose:
        _add(total, upsert_private_items(tenant_id, entity_key, loose, job_id))
    return total


# ------------------------------------------------------------------ reads

def _iso(d: datetime | None) -> str | None:
    if d is None:
        return None
    if d.tzinfo is None:                       # SQLite returns naive UTC
        d = d.replace(tzinfo=timezone.utc)
    return d.isoformat()


def item_public(i: Item) -> dict:
    return {"id": i.id, "entity": i.entity_key.split(":", 1)[-1], "item_key": i.item_key, "data": i.data,
            "site_id": i.site_id, "action": i.action, "private": i.tenant_id is not None, "job_id": i.job_id,
            "first_seen_at": _iso(i.first_seen_at), "last_seen_at": _iso(i.last_seen_at),
            "updated_at": _iso(i.updated_at), "changed_at": _iso(i.updated_at)}


def visible_to(tenant_id: str | None):
    """Shared site items, plus the tenant's own private items."""
    if tenant_id is None:
        return Item.tenant_id.is_(None)
    return or_(Item.tenant_id.is_(None), Item.tenant_id == tenant_id)


def list_items(tenant_id: str | None, entity_key: str | None, q: str | None, limit: int, offset: int,
               site_id: str | None = None, action: str | None = None, sort: str = "last_seen",
               shared_only: bool = False) -> tuple[list[dict], int]:
    col = getattr(Item, SORTS.get(sort, "last_seen_at"))
    with session_scope() as s:
        base = select(Item).where(Item.tenant_id.is_(None) if shared_only else visible_to(tenant_id))
        if entity_key:
            base = base.where(Item.entity_key == entity_key)
        if site_id:
            base = base.where(Item.site_id == site_id)
        if action:
            base = base.where(Item.action == action)
        if q:
            like = f"%{q.lower()}%"
            base = base.where(or_(func.lower(Item.item_key).like(like), func.lower(cast(Item.data, String)).like(like)))
        total = s.scalar(select(func.count()).select_from(base.subquery()))
        rows = s.scalars(base.order_by(col.desc(), Item.id).limit(limit).offset(offset)).all()
        return [item_public(i) for i in rows], int(total or 0)


def changes(tenant_id: str, entity_key: str | None, cursor: int, limit: int) -> tuple[list[dict], int, bool]:
    with session_scope() as s:
        q = select(ItemChange, Item).join(Item, Item.id == ItemChange.item_id).where(
            ItemChange.id > cursor, visible_to(tenant_id))
        if entity_key:
            q = q.where(Item.entity_key == entity_key)
        rows = s.execute(q.order_by(ItemChange.id).limit(limit + 1)).all()
        more = len(rows) > limit
        rows = rows[:limit]
        out = [{"change": c.change, "item": item_public(i)} for c, i in rows]
        return out, (rows[-1][0].id if rows else cursor), more


def site_filter_options(tenant_id: str | None) -> list[dict]:
    """Sites that hold items this tenant can see, with counts (for the Items page filter)."""
    with session_scope() as s:
        rows = s.execute(select(Item.site_id, func.count()).where(visible_to(tenant_id), Item.site_id.is_not(None))
                         .group_by(Item.site_id).order_by(Item.site_id)).all()
    return [{"site_id": sid, "items": int(n)} for sid, n in rows]
