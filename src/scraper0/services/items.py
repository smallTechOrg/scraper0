"""Stored results: upsert per (tenant, entity, item_key) and an append-only change feed."""
from __future__ import annotations

import hashlib
import json

from sqlalchemy import func, or_, select

from scraper0.db.models import Item, ItemChange, now
from scraper0.db.session import session_scope

# fields that change on every scrape without the item really changing
_VOLATILE = {"_meta", "confidence", "extraction", "sources", "warnings", "image"}


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


def upsert_items(tenant_id: str, entity_key: str, items: list[dict], source_id: str | None = None) -> dict:
    from sqlalchemy.exc import IntegrityError
    if not items:
        return {"created": 0, "updated": 0, "unchanged": 0}
    seen, payload = set(), []
    for k, d in zip(batch_keys(items), items):
        if k not in seen:                      # exact duplicates within one batch count once
            seen.add(k)
            payload.append((k, content_hash(d), json.loads(json.dumps(d, default=str))))
    try:
        return _upsert(tenant_id, entity_key, payload, source_id)
    except IntegrityError:                     # a concurrent job inserted one of these keys first: retry as update
        return _upsert(tenant_id, entity_key, payload, source_id)


def _upsert(tenant_id, entity_key, payload, source_id) -> dict:
    counts = {"created": 0, "updated": 0, "unchanged": 0}
    with session_scope() as s:
        keys = [k for k, _, _ in payload]
        existing = {i.item_key: i for i in s.scalars(select(Item).where(
            Item.tenant_id == tenant_id, Item.entity_key == entity_key, Item.item_key.in_(keys)))}
        t = now()
        for k, h, data in payload:
            it = existing.get(k)
            if it is None:
                it = Item(tenant_id=tenant_id, entity_key=entity_key, item_key=k, data=data, content_hash=h,
                          source_id=source_id, first_seen_at=t, last_seen_at=t, changed_at=t)
                s.add(it)
                s.flush()
                existing[k] = it
                s.add(ItemChange(tenant_id=tenant_id, item_id=it.id, entity_key=entity_key, change="created"))
                counts["created"] += 1
            elif it.content_hash != h:
                it.data, it.content_hash, it.last_seen_at, it.changed_at = data, h, t, t
                s.add(ItemChange(tenant_id=tenant_id, item_id=it.id, entity_key=entity_key, change="updated"))
                counts["updated"] += 1
            else:
                it.last_seen_at = t
                counts["unchanged"] += 1
    return counts


def item_public(i: Item) -> dict:
    return {"id": i.id, "entity": i.entity_key.split(":", 1)[-1], "item_key": i.item_key, "data": i.data,
            "source_id": i.source_id, "first_seen_at": i.first_seen_at.isoformat(),
            "last_seen_at": i.last_seen_at.isoformat(), "changed_at": i.changed_at.isoformat()}


def list_items(tenant_id: str, entity_key: str | None, q: str | None, limit: int, offset: int) -> tuple[list[dict], int]:
    with session_scope() as s:
        base = select(Item).where(Item.tenant_id == tenant_id)
        if entity_key:
            base = base.where(Item.entity_key == entity_key)
        if q:
            like = f"%{q.lower()}%"
            from sqlalchemy import String, cast
            base = base.where(or_(func.lower(Item.item_key).like(like), func.lower(cast(Item.data, String)).like(like)))
        total = s.scalar(select(func.count()).select_from(base.subquery()))
        rows = s.scalars(base.order_by(Item.changed_at.desc()).limit(limit).offset(offset)).all()
        return [item_public(i) for i in rows], int(total or 0)


def changes(tenant_id: str, entity_key: str | None, cursor: int, limit: int) -> tuple[list[dict], int, bool]:
    with session_scope() as s:
        q = select(ItemChange, Item).join(Item, Item.id == ItemChange.item_id).where(
            ItemChange.tenant_id == tenant_id, ItemChange.id > cursor)
        if entity_key:
            q = q.where(ItemChange.entity_key == entity_key)
        rows = s.execute(q.order_by(ItemChange.id).limit(limit + 1)).all()
        more = len(rows) > limit
        rows = rows[:limit]
        out = [{"change": c.change, "item": item_public(i)} for c, i in rows]
        return out, (rows[-1][0].id if rows else cursor), more
