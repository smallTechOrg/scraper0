"""Migration 0006 (onboarding only) on real data: SQLite, or Postgres when SCRAPER0_TEST_POSTGRES_URL is set."""
import json
from datetime import datetime, timezone

import sqlalchemy as sa
from alembic import command
from alembic.config import Config

from scraper0.bootstrap import REPO_ROOT
from scraper0.db import session as db_session
from scraper0.db.models import Base
from conftest import PG_URL

T = datetime(2026, 11, 28, tzinfo=timezone.utc)


def _cfg(engine) -> Config:
    cfg = Config(str(REPO_ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(REPO_ROOT / "migrations"))
    cfg.attributes["connection_engine"] = engine
    return cfg


def _engine(tmp_path):
    if PG_URL:
        eng = db_session.make_engine(PG_URL, pool_size=2, max_overflow=1)
        Base.metadata.drop_all(eng)
        with eng.begin() as c:
            c.exec_driver_sql("DROP TABLE IF EXISTS alembic_version, site_items, job_files, media")
        return eng
    return db_session.make_engine(f"sqlite:///{tmp_path}/mig.db")


def _seed(c):
    c.execute(sa.text("INSERT INTO sites (site_id, base_url, name, created_at) VALUES "
                      "('teameverest-ngo', 'https://www.teameverest.ngo', 'teameverest.ngo', :t), "
                      "('karmana-in', 'https://karmana.in', 'karmana.in', :t), "
                      "('draft-org', 'https://draft.org', 'draft.org', :t)"), {"t": T})
    acts = [("teameverest-ngo", "code", "TEAMEVEREST", "FETCH_EVENTS", 1), ("karmana-in", "learned", None, None, 2),
            ("draft-org", "learned", None, None, None)]
    for site, impl, conn, conn_action, locked in acts:
        c.execute(sa.text(
            "INSERT INTO site_actions (site_id, name, kind, mode, entity, implementation, connector, connector_action,"
            " seed_urls, status, locked_version, input_schema, output_schema, consecutive_failures, scrape_seq,"
            " created_at) VALUES (:s, 'fetch_events', 'read', 'list', 'event', :i, :c, :ca, '[]', :st, :lv, '{}',"
            " '{}', 0, 0, :t)"),
            {"s": site, "i": impl, "c": conn, "ca": conn_action, "st": "locked" if locked else "draft", "lv": locked,
             "t": T})
    sources = [("s1", "connector", {"connector": "TEAMEVEREST", "action": "FETCH_EVENTS", "data": {}}),
               ("s2", "crawl", {"urls": ["https://www.karmana.in/activities"], "entity": "event"}),
               ("s3", "crawl", {"urls": ["https://draft.org/events"]}),                     # not locked → dropped
               ("s4", "crawl", {"urls": ["https://unknown.example/x"]}),                    # no site → dropped
               ("s5", "connector", {"connector": "WEBEVENTS", "action": "FETCH_EVENTS"})]   # no code action → dropped
    for sid, kind, cfg in sources:
        c.execute(sa.text("INSERT INTO sources (id, tenant_id, name, kind, config, interval_minutes, enabled, created_at)"
                          " VALUES (:id, 't1', :id, :k, :cfg, 60, true, :t)"),
                  {"id": sid, "k": kind, "cfg": json.dumps(cfg), "t": T})
    c.execute(sa.text("INSERT INTO job_files (job_id, filename, data) VALUES ('j1', 'poster.png', :d)"), {"d": b"x"})
    for iid, tenant, site in (("itm_private", "t1", None), ("itm_shared", None, "karmana-in")):
        c.execute(sa.text(
            "INSERT INTO items (id, site_id, action, entity_key, tenant_id, item_key, data, content_hash, first_seen_at,"
            " last_seen_at, updated_at, seen_seq) VALUES (:id, :site, :act, 'event', :tn, :id, '{}', 'h', :t, :t, :t, 0)"),
            {"id": iid, "site": site, "act": "fetch_events" if site else None, "tn": tenant, "t": T})
        c.execute(sa.text("INSERT INTO item_changes (item_id, change, created_at) VALUES (:id, 'created', :t)"),
                  {"id": iid, "t": T})


def test_0006_sources_uploads_and_private_items(tmp_path):
    eng = _engine(tmp_path)
    cfg = _cfg(eng)
    command.upgrade(cfg, "0005")
    with eng.begin() as c:
        _seed(c)
    command.upgrade(cfg, "head")
    insp = sa.inspect(eng)
    assert "job_files" not in insp.get_table_names()
    cols = {c["name"] for c in insp.get_columns("sources")}
    assert {"site_id", "action"} <= cols and not {"kind", "config"} & cols
    with eng.connect() as c:
        rows = {r.id: (r.site_id, r.action) for r in c.execute(sa.text("SELECT id, site_id, action FROM sources"))}
        assert rows == {"s1": ("teameverest-ngo", "fetch_events"), "s2": ("karmana-in", "fetch_events")}
        assert [r[0] for r in c.execute(sa.text("SELECT id FROM items"))] == ["itm_shared"]
        assert [r[0] for r in c.execute(sa.text("SELECT item_id FROM item_changes"))] == ["itm_shared"]

    command.downgrade(cfg, "0005")                     # schedules come back as connector / crawl sources
    with eng.connect() as c:
        back = {r.id: (r.kind, json.loads(r.config) if isinstance(r.config, str) else r.config)
                for r in c.execute(sa.text("SELECT id, kind, config FROM sources"))}
    assert back["s1"][0] == "connector" and back["s1"][1]["connector"] == "TEAMEVEREST"
    assert back["s2"] == ("crawl", {"urls": ["https://karmana.in"], "entity": "event"})
    assert "job_files" in sa.inspect(eng).get_table_names()
    command.upgrade(cfg, "head")
    if PG_URL:                                          # leave the shared test database as the other tests expect
        Base.metadata.drop_all(eng)
        with eng.begin() as c:
            c.exec_driver_sql("DROP TABLE IF EXISTS alembic_version")
        Base.metadata.create_all(eng)
    eng.dispose()
