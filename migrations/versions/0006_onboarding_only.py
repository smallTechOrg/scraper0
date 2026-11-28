"""onboarding only: sources become schedules on site actions; uploads are gone

- sources: (kind, config) → (site_id, action). A connector source maps to the code action of that connector; a
  URL (crawl) source maps to its site's `fetch_events` when that action is locked. Sources with no such locked
  action are deleted (the count is printed).
- job_files (upload bytes) is dropped. Private items (uploads; tenant_id set) and their change rows are deleted.
- Old crawl / connector / ingest jobs stay as read-only history.

Revision ID: 0006
Revises: 0005
"""
import json
from urllib.parse import urlsplit

import sqlalchemy as sa
from alembic import op


revision = '0006'
down_revision = '0005'
branch_labels = None
depends_on = None


def _json(v):
    return json.loads(v) if isinstance(v, str) else (v or {})


def _site_id(url):
    if not isinstance(url, str) or not url.startswith(("http://", "https://")):
        return None
    p = urlsplit(url)
    if not p.hostname:
        return None
    name = p.hostname.lower().removeprefix("www.")
    return (name + (f"-{p.port}" if p.port else "")).replace(".", "-")


def upgrade() -> None:
    bind = op.get_bind()
    with op.batch_alter_table('sources', schema=None) as batch_op:
        batch_op.add_column(sa.Column('site_id', sa.String(length=255), nullable=True))
        batch_op.add_column(sa.Column('action', sa.String(length=64), nullable=True))

    locked = {(r.site_id, r.name): r for r in bind.execute(sa.text(
        "SELECT site_id, name, mode, connector, connector_action FROM site_actions WHERE locked_version IS NOT NULL"))}
    by_connector = {(r.connector, r.connector_action): k for k, r in locked.items() if r.connector}
    kept, dropped = 0, 0
    for sid, kind, config in bind.execute(sa.text("SELECT id, kind, config FROM sources")).all():
        cfg = _json(config)
        target = None
        if kind == "connector":
            target = by_connector.get((cfg.get("connector"), cfg.get("action")))
        elif kind == "crawl":
            site = _site_id((cfg.get("urls") or [None])[0])
            if site and (site, "fetch_events") in locked:
                target = (site, "fetch_events")
        if target and locked[target].mode == "list":
            bind.execute(sa.text("UPDATE sources SET site_id = :s, action = :a WHERE id = :id"),
                         {"s": target[0], "a": target[1], "id": sid})
            kept += 1
        else:
            bind.execute(sa.text("DELETE FROM sources WHERE id = :id"), {"id": sid})
            dropped += 1
    if kept or dropped:
        print(f"0006: {kept} source(s) now schedule a site action, {dropped} without a locked site action deleted")

    with op.batch_alter_table('sources', schema=None) as batch_op:
        batch_op.alter_column('site_id', existing_type=sa.String(length=255), nullable=False)
        batch_op.alter_column('action', existing_type=sa.String(length=64), nullable=False)
        batch_op.drop_column('kind')
        batch_op.drop_column('config')

    bind.execute(sa.text("DELETE FROM item_changes WHERE item_id IN (SELECT id FROM items WHERE tenant_id IS NOT NULL)"))
    bind.execute(sa.text("DELETE FROM items WHERE tenant_id IS NOT NULL"))

    with op.batch_alter_table('job_files', schema=None) as batch_op:
        batch_op.drop_index(batch_op.f('ix_job_files_job_id'))
    op.drop_table('job_files')


def downgrade() -> None:
    op.create_table('job_files',
    sa.Column('id', sa.Integer(), autoincrement=True, nullable=False),
    sa.Column('job_id', sa.String(length=32), nullable=False),
    sa.Column('filename', sa.String(length=300), nullable=False),
    sa.Column('data', sa.LargeBinary(), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    with op.batch_alter_table('job_files', schema=None) as batch_op:
        batch_op.create_index(batch_op.f('ix_job_files_job_id'), ['job_id'], unique=False)

    bind = op.get_bind()
    with op.batch_alter_table('sources', schema=None) as batch_op:
        batch_op.add_column(sa.Column('kind', sa.String(length=32), nullable=True))
        batch_op.add_column(sa.Column('config', sa.JSON(), nullable=True))
    actions = {(r.site_id, r.name): r for r in bind.execute(sa.text(
        "SELECT a.site_id, a.name, a.connector, a.connector_action, s.base_url FROM site_actions a "
        "JOIN sites s ON s.site_id = a.site_id"))}
    src = sa.table("sources", sa.column("id", sa.String), sa.column("kind", sa.String), sa.column("config", sa.JSON))
    for sid, site_id, action in bind.execute(sa.text("SELECT id, site_id, action FROM sources")).all():
        a = actions.get((site_id, action))
        if a is not None and a.connector:
            kind, cfg = "connector", {"connector": a.connector, "action": a.connector_action, "data": {}}
        else:
            kind, cfg = "crawl", {"urls": [a.base_url if a is not None else f"https://{site_id}"], "entity": "event"}
        bind.execute(src.update().where(src.c.id == sid).values(kind=kind, config=cfg))
    with op.batch_alter_table('sources', schema=None) as batch_op:
        batch_op.alter_column('kind', existing_type=sa.String(length=32), nullable=False)
        batch_op.alter_column('config', existing_type=sa.JSON(), nullable=False)
        batch_op.drop_column('action')
        batch_op.drop_column('site_id')
