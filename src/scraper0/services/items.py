"""The one item store (table `items`) and its change feed.

Every item belongs to an onboarded site + action and is shared by every tenant: whatever run scrapes a
site — a v2 refresh or a code connector run, by any tenant — upserts the same rows, keyed
(site_id, action, item_key), with id = itm_ + sha256(site_id|action|item_key)[:24]. Each run bumps
`last_seen_at` on every item it saw and `updated_at` only when the content changed.
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone

from sqlalchemy import String, cast, func, or_, select

from scraper0.db.models import Item, ItemChange, now
from scraper0.db.session import session_scope

# fields that change on every scrape without the item really changing
_VOLATILE = {"_meta", "confidence", "extraction", "sources", "warnings", "image"}
SORTS = {"last_seen": "last_seen_at", "updated": "updated_at", "first_seen": "first_seen_at"}


# ------------------------------------------------------------------ keys and ids

def item_key(data: dict) -> str:
    link = data.get("link") or (data.get("_meta") or {}).get("url")
    if link:
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


# ------------------------------------------------------------------ writes

def upsert_rows(s, payload: list[tuple], *, entity_key: str, site_id: str, action: str, job_id: str | None,
                seq: int) -> dict:
    """Upsert [(item_key, id, content_hash, data)] in an open session; `seq` marks the rows as seen by this
    refresh, so a read returns exactly what the last refresh saw."""
    counts = {"created": 0, "updated": 0, "unchanged": 0}
    ids = [i for _, i, _, _ in payload]
    existing: dict[str, Item] = {}
    for chunk in range(0, len(ids), 500):
        existing.update({r.id: r for r in s.scalars(select(Item).where(Item.id.in_(ids[chunk:chunk + 500])))})
    t = now()
    for key, iid, h, data in payload:
        it = existing.get(iid)
        if it is None:
            s.add(Item(id=iid, site_id=site_id, action=action, entity_key=entity_key, item_key=key, data=data,
                       content_hash=h, first_seen_at=t, last_seen_at=t, updated_at=t, seen_seq=seq, job_id=job_id))
            s.add(ItemChange(item_id=iid, change="created"))
            counts["created"] += 1
            continue
        if it.content_hash != h:
            it.content_hash, it.updated_at = h, t
            s.add(ItemChange(item_id=iid, change="updated"))
            counts["updated"] += 1
        else:
            counts["unchanged"] += 1
        if it.data != data:                       # e.g. a fresh signed image link: not a content change
            it.data = data
        it.last_seen_at, it.job_id, it.seen_seq = t, job_id, seq
    return counts


# ------------------------------------------------------------------ reads

def _iso(d: datetime | None) -> str | None:
    if d is None:
        return None
    if d.tzinfo is None:                       # SQLite returns naive UTC
        d = d.replace(tzinfo=timezone.utc)
    return d.isoformat()


def item_public(i: Item) -> dict:
    return {"id": i.id, "entity": i.entity_key.split(":", 1)[-1], "item_key": i.item_key, "data": i.data,
            "site_id": i.site_id, "action": i.action, "job_id": i.job_id,
            "first_seen_at": _iso(i.first_seen_at), "last_seen_at": _iso(i.last_seen_at),
            "updated_at": _iso(i.updated_at), "changed_at": _iso(i.updated_at)}


def list_items(entity_key: str | None, q: str | None, limit: int, offset: int, site_id: str | None = None,
               action: str | None = None, sort: str = "last_seen") -> tuple[list[dict], int]:
    col = getattr(Item, SORTS.get(sort, "last_seen_at"))
    with session_scope() as s:
        base = select(Item)
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


def changes(entity_key: str | None, cursor: int, limit: int) -> tuple[list[dict], int, bool]:
    with session_scope() as s:
        q = select(ItemChange, Item).join(Item, Item.id == ItemChange.item_id).where(ItemChange.id > cursor)
        if entity_key:
            q = q.where(Item.entity_key == entity_key)
        rows = s.execute(q.order_by(ItemChange.id).limit(limit + 1)).all()
        more = len(rows) > limit
        rows = rows[:limit]
        out = [{"change": c.change, "item": item_public(i)} for c, i in rows]
        return out, (rows[-1][0].id if rows else cursor), more


def site_filter_options() -> list[dict]:
    """Sites that hold items, with counts (for the Items page filter)."""
    with session_scope() as s:
        rows = s.execute(select(Item.site_id, func.count()).group_by(Item.site_id).order_by(Item.site_id)).all()
    return [{"site_id": sid, "items": int(n)} for sid, n in rows]
