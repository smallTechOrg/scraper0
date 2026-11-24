"""Sources → jobs. `tick()` is called by Cloud Scheduler (POST /internal/tick) or, locally, by a thread."""
from __future__ import annotations

import threading
from datetime import timedelta

import structlog
from sqlalchemy import or_, select

from scraper0.db.models import Source, Tenant, now
from scraper0.db.session import session_scope
from scraper0.jobs import runner

log = structlog.get_logger(__name__)
MIN_INTERVAL = 15


def source_job_input(src_kind: str, config: dict) -> tuple[str, dict]:
    if src_kind not in ("crawl", "connector"):
        raise runner.JobInputError("source kind must be crawl or connector")
    return src_kind, dict(config or {})


def run_source(source_id: str) -> str:
    with session_scope() as s:
        src = s.get(Source, source_id)
        kind, inp = source_job_input(src.kind, src.config)
        tenant_id = src.tenant_id
    job_id = runner.submit(tenant_id, kind, inp, source_id=source_id)
    with session_scope() as s:
        src = s.get(Source, source_id)
        src.last_job_id = job_id
        src.next_run_at = now() + timedelta(minutes=max(src.interval_minutes, MIN_INTERVAL))
    runner.dispatch(job_id)
    return job_id


EVENT_RETENTION = timedelta(days=14)


def purge_old_events() -> int:
    """Progress events are only for live viewing; keep two weeks."""
    from sqlalchemy import delete

    from scraper0.db.models import JobEvent
    with session_scope() as s:
        return s.execute(delete(JobEvent).where(JobEvent.created_at < now() - EVENT_RETENTION)).rowcount


def tick() -> dict:
    stale = runner.fail_stale_jobs()
    try:
        purged = purge_old_events()
    except Exception as e:
        purged = 0
        log.warning("purge_failed", error=str(e))
    with session_scope() as s:
        due = s.scalars(select(Source.id).join(Tenant, Tenant.id == Source.tenant_id).where(
            Source.enabled.is_(True), Tenant.status == "active",
            or_(Source.next_run_at.is_(None), Source.next_run_at <= now()))).all()
    started = []
    for sid in due:
        try:
            started.append(run_source(sid))
        except Exception as e:
            log.warning("source_run_failed", source_id=sid, error=str(e))
    if due or stale:
        log.info("tick", due=len(due), started=len(started), stale_failed=stale)
    return {"due": len(due), "started": started, "stale_failed": stale, "events_purged": purged}


_stop = threading.Event()


def start_local_scheduler(interval_s: int = 60) -> threading.Thread:
    def loop():
        while not _stop.wait(interval_s):
            try:
                tick()
            except Exception:
                log.exception("tick_failed")
    t = threading.Thread(target=loop, name="scheduler", daemon=True)
    t.start()
    return t


def stop_local_scheduler():
    _stop.set()
