"""unified items: one `items` table shared via sites (replaces v1 per-tenant items and v2 site_items)

- site_items rows move over as they are (same ids, so callers' dedupe on item id keeps working).
- old v1 items whose link is http(s) become site items (site = the link's host, registered in `sites` if
  new; action = fetch_events for events, else fetch_<entity>; tenant-defined entities stay private). When that
  id already exists (from site_items), the row seen most recently wins and keeps the v2 seen_seq.
- other v1 items (uploads, no link) become the tenant's private items.
- the change feed restarts: one "created" change per item, oldest first (old cursors are not valid any more).

Revision ID: 0004
Revises: 0003
"""
import hashlib
import json
from datetime import datetime, timezone
from urllib.parse import urlsplit

import sqlalchemy as sa
from alembic import op

revision = '0004'
down_revision = '0003'
branch_labels = None
depends_on = None

TS = sa.DateTime(timezone=True)
EVENT_FIELDS = ("name", "start_time", "end_time", "address", "link", "organisation", "image", "description",
                "latitude", "longitude", "is_online", "price", "source_url")


def _now():
    return datetime.now(timezone.utc)


def _aware(d):
    if isinstance(d, str):
        d = datetime.fromisoformat(d)
    if d is not None and d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d


def _json(v):
    return json.loads(v) if isinstance(v, str) else v


def _site_of(url):
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    p = urlsplit(url)
    if not p.hostname:
        return None
    host = p.hostname.lower()
    name = host.removeprefix("www.")
    site_id = (name + (f"-{p.port}" if p.port else "")).replace(".", "-")
    return site_id, f"{p.scheme}://{host}" + (f":{p.port}" if p.port else ""), name


def _event_data(d):
    out = {k: d.get(k) for k in EVENT_FIELDS}
    out["is_online"] = bool(out["is_online"])
    out["source_url"] = out["source_url"] or out["link"]
    return out


def _hash(data):
    stable = {k: v for k, v in data.items() if k != "image"}
    return hashlib.sha256(json.dumps(stable, sort_keys=True, default=str).encode()).hexdigest()


def _sid(site_id, action, key):
    return "itm_" + hashlib.sha256(f"{site_id}|{action}|{key}".encode()).hexdigest()[:24]


def _pid(tenant_id, entity_key, key):
    return "itm_" + hashlib.sha256(f"tenant:{tenant_id}|{entity_key}|{key}".encode()).hexdigest()[:24]


def _create_items():
    op.create_table('items',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('site_id', sa.String(length=255), nullable=True),
    sa.Column('action', sa.String(length=64), nullable=True),
    sa.Column('entity_key', sa.String(length=120), nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=True),
    sa.Column('item_key', sa.String(length=500), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('first_seen_at', TS, nullable=False),
    sa.Column('last_seen_at', TS, nullable=False),
    sa.Column('updated_at', TS, nullable=False),
    sa.Column('seen_seq', sa.Integer(), nullable=False),
    sa.Column('job_id', sa.String(length=32), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('site_id', 'action', 'item_key', name='uq_item_site'),
    sa.UniqueConstraint('tenant_id', 'entity_key', 'item_key', name='uq_item_tenant')
    )
    with op.batch_alter_table('items', schema=None) as batch_op:
        batch_op.create_index('ix_items_action', ['site_id', 'action', 'seen_seq'], unique=False)
        batch_op.create_index('ix_items_last_seen', ['last_seen_at'], unique=False)
    op.create_table('item_changes',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('item_id', sa.String(length=32), nullable=False),
    sa.Column('change', sa.String(length=16), nullable=False),
    sa.Column('created_at', TS, nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('item_changes', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_item_changes_item_id'), ['item_id'], unique=False)


def upgrade() -> None:
    bind = op.get_bind()
    md = sa.MetaData()
    old_items = sa.Table('items', md, autoload_with=bind)
    site_items = sa.Table('site_items', md, autoload_with=bind)
    sites = sa.Table('sites', md, autoload_with=bind)
    v2_rows = [dict(r._mapping) for r in bind.execute(sa.select(site_items))]
    v1_rows = [dict(r._mapping) for r in bind.execute(sa.select(old_items))]
    known_sites = {r[0] for r in bind.execute(sa.select(sites.c.site_id))}

    new: dict[str, dict] = {}
    for r in v2_rows:
        new[r["id"]] = {"id": r["id"], "site_id": r["site_id"], "action": r["action"], "entity_key": "event",
                        "tenant_id": None, "item_key": r["item_key"], "data": _json(r["data"]),
                        "content_hash": r["content_hash"], "first_seen_at": _aware(r["first_seen_at"]),
                        "last_seen_at": _aware(r["last_seen_at"]), "updated_at": _aware(r["updated_at"]),
                        "seen_seq": r["seen_seq"] or 0, "job_id": r["job_id"]}
    new_sites = {}
    for r in v1_rows:
        data = _json(r["data"]) or {}
        ek, key = r["entity_key"], r["item_key"]
        link_site = _site_of(data.get("link"))
        site = link_site if ":" not in ek else None              # tenant-defined entities stay private
        row = {"entity_key": ek, "item_key": key, "first_seen_at": _aware(r["first_seen_at"]),
               "last_seen_at": _aware(r["last_seen_at"]), "updated_at": _aware(r["changed_at"]),
               "seen_seq": 0, "job_id": None}
        if site:
            action = "fetch_events" if ek == "event" else f"fetch_{ek}"
            if ek == "event":
                data = _event_data(data)
            row.update(id=_sid(site[0], action, key), site_id=site[0], action=action, tenant_id=None, data=data,
                       content_hash=_hash(data) if ek == "event" else r["content_hash"])
            if site[0] not in known_sites:
                new_sites.setdefault(site[0], {"site_id": site[0], "base_url": site[1], "name": site[2],
                                               "created_by_tenant": r["tenant_id"], "created_at": _now()})
        else:
            row.update(id=_pid(r["tenant_id"], ek, key), site_id=link_site[0] if link_site and ":" in ek else None,
                       action=None, tenant_id=r["tenant_id"],
                       data=data, content_hash=r["content_hash"])
        have = new.get(row["id"])
        if have is None:
            new[row["id"]] = row
        elif row["last_seen_at"] and have["last_seen_at"] and row["last_seen_at"] > have["last_seen_at"]:
            row["seen_seq"] = have["seen_seq"]                 # keep what the last v2 refresh saw
            row["first_seen_at"] = min(have["first_seen_at"], row["first_seen_at"])
            new[row["id"]] = row

    with op.batch_alter_table('site_items', schema=None) as batch_op:
        batch_op.drop_index('ix_site_items_action')
    op.drop_table('site_items')
    with op.batch_alter_table('item_changes', schema=None) as batch_op:
        batch_op.drop_index('ix_changes_tenant_id')
    op.drop_table('item_changes')
    op.drop_table('items')
    _create_items()

    md2 = sa.MetaData()
    items = sa.Table('items', md2, autoload_with=bind)
    changes = sa.Table('item_changes', md2, autoload_with=bind)
    if new_sites:
        bind.execute(sa.insert(sites), list(new_sites.values()))
    rows = sorted(new.values(), key=lambda r: (r["first_seen_at"] or _now(), r["id"]))
    if rows:
        bind.execute(sa.insert(items), rows)
        bind.execute(sa.insert(changes), [{"item_id": r["id"], "change": "created", "created_at": r["first_seen_at"]}
                                          for r in rows])


def downgrade() -> None:
    """Shared items go back to site_items, private items to the per-tenant items table. v1-origin shared items
    with no v2 action are kept in site_items too (they no longer belong to one tenant)."""
    bind = op.get_bind()
    md = sa.MetaData()
    items = sa.Table('items', md, autoload_with=bind)
    rows = [dict(r._mapping) for r in bind.execute(sa.select(items))]
    with op.batch_alter_table('item_changes', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_item_changes_item_id'))
    op.drop_table('item_changes')
    with op.batch_alter_table('items', schema=None) as batch_op:
        batch_op.drop_index('ix_items_last_seen')
        batch_op.drop_index('ix_items_action')
    op.drop_table('items')

    op.create_table('item_changes',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.Column('item_id', sa.Integer(), nullable=False),
    sa.Column('entity_key', sa.String(length=120), nullable=False),
    sa.Column('change', sa.String(length=16), nullable=False),
    sa.Column('created_at', TS, nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('item_changes', schema=None) as batch_op:
        batch_op.create_index('ix_changes_tenant_id', ['tenant_id', 'id'], unique=False)
    op.create_table('items',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('tenant_id', sa.String(length=32), nullable=False),
    sa.Column('entity_key', sa.String(length=120), nullable=False),
    sa.Column('item_key', sa.String(length=500), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('source_id', sa.String(length=32), nullable=True),
    sa.Column('first_seen_at', TS, nullable=False),
    sa.Column('last_seen_at', TS, nullable=False),
    sa.Column('changed_at', TS, nullable=False),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('tenant_id', 'entity_key', 'item_key', name='uq_item')
    )
    op.create_table('site_items',
    sa.Column('id', sa.String(length=32), nullable=False),
    sa.Column('site_id', sa.String(length=255), nullable=False),
    sa.Column('action', sa.String(length=64), nullable=False),
    sa.Column('item_key', sa.String(length=500), nullable=False),
    sa.Column('data', sa.JSON(), nullable=False),
    sa.Column('content_hash', sa.String(length=64), nullable=False),
    sa.Column('first_seen_at', TS, nullable=False),
    sa.Column('last_seen_at', TS, nullable=False),
    sa.Column('updated_at', TS, nullable=False),
    sa.Column('seen_seq', sa.Integer(), nullable=False),
    sa.Column('version', sa.Integer(), nullable=True),
    sa.Column('job_id', sa.String(length=32), nullable=True),
    sa.PrimaryKeyConstraint('id'),
    sa.UniqueConstraint('site_id', 'action', 'item_key', name='uq_site_item')
    )
    with op.batch_alter_table('site_items', schema=None) as batch_op:
        batch_op.create_index('ix_site_items_action', ['site_id', 'action', 'seen_seq'], unique=False)

    md2 = sa.MetaData()
    old_items = sa.Table('items', md2, autoload_with=bind)
    site_items = sa.Table('site_items', md2, autoload_with=bind)
    shared = [{"id": r["id"], "site_id": r["site_id"], "action": r["action"], "item_key": r["item_key"],
               "data": _json(r["data"]), "content_hash": r["content_hash"], "first_seen_at": r["first_seen_at"],
               "last_seen_at": r["last_seen_at"], "updated_at": r["updated_at"], "seen_seq": r["seen_seq"],
               "version": None, "job_id": r["job_id"]}
              for r in rows if r["tenant_id"] is None and r["site_id"] and r["action"]]
    private = [{"tenant_id": r["tenant_id"], "entity_key": r["entity_key"], "item_key": r["item_key"],
                "data": _json(r["data"]), "content_hash": r["content_hash"], "source_id": None,
                "first_seen_at": r["first_seen_at"], "last_seen_at": r["last_seen_at"], "changed_at": r["updated_at"]}
               for r in rows if r["tenant_id"] is not None]
    if shared:
        bind.execute(sa.insert(site_items), shared)
    if private:
        bind.execute(sa.insert(old_items), private)
