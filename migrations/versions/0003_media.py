"""media: stored copies of item images

Revision ID: 0003
Revises: 0002
"""
from alembic import op
import sqlalchemy as sa


revision = '0003'
down_revision = '0002'
branch_labels = None
depends_on = None


def upgrade() -> None:
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


def downgrade() -> None:
    op.drop_index(op.f('ix_media_source_key'), table_name='media')
    op.drop_table('media')
