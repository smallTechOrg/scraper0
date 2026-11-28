"""drop media: scraper0 stores image links only

Items whose image points at a stored copy (/v2/media/<id>) get their original link back, then the copies go.

Revision ID: 0005
Revises: 0004
"""
import json
import re

from alembic import op
import sqlalchemy as sa


revision = '0005'
down_revision = '0004'
branch_labels = None
depends_on = None

_MEDIA = re.compile(r"/v2/media/([0-9a-f]{40})$")


def upgrade() -> None:
    bind = op.get_bind()
    source = {mid: url for mid, url in bind.execute(sa.text("SELECT id, source_url FROM media"))}
    items = sa.table("items", sa.column("id", sa.String), sa.column("data", sa.JSON))
    for iid, data in bind.execute(sa.select(items.c.id, items.c.data)).all():
        if isinstance(data, str):
            data = json.loads(data)
        img = (data or {}).get("image")
        m = _MEDIA.search(img) if isinstance(img, str) else None
        if not m:
            continue
        data = dict(data)
        data["image"] = source.get(m.group(1))
        bind.execute(items.update().where(items.c.id == iid).values(data=data))
    op.drop_index(op.f('ix_media_source_key'), table_name='media')
    op.drop_table('media')


def downgrade() -> None:
    op.create_table('media',
    sa.Column('id', sa.String(length=40), nullable=False),
    sa.Column('content_type', sa.String(length=100), nullable=False),
    sa.Column('data', sa.LargeBinary(), nullable=False),
    sa.Column('size', sa.Integer(), nullable=False),
    sa.Column('source_url', sa.Text(), nullable=False),
    sa.Column('source_key', sa.String(length=64), nullable=False),
    sa.Column('created_at', sa.DateTime(timezone=True), nullable=False),
    sa.PrimaryKeyConstraint('id')
    )
    op.create_index(op.f('ix_media_source_key'), 'media', ['source_key'], unique=False)
