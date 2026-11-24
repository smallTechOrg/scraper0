from alembic import context

from scraper0.db.models import Base
from scraper0.db.session import get_engine

target_metadata = Base.metadata


def run() -> None:
    engine = context.config.attributes.get("connection_engine") or get_engine()
    with engine.connect() as conn:
        context.configure(connection=conn, target_metadata=target_metadata, render_as_batch=conn.dialect.name == "sqlite",
                          compare_type=True)
        with context.begin_transaction():
            context.run_migrations()
        conn.commit()


run()
