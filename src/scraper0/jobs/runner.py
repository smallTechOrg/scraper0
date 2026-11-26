"""Jobs: submit → dispatch → run (claim atomically, execute, store results) with progress events.

Execution modes (SCRAPER0_JOB_MODE):
  thread       local worker pool in this process (dev, or Cloud Run with CPU always allocated)
  inline       run inside the submitting request
  cloud_tasks  Cloud Tasks calls POST /internal/jobs/{id}/run on this service (scale-to-zero friendly).
               Cloud Tasks' HTTP deadline is 30 min, but the Cloud Run request keeps running up to the
               service timeout (60 min); a retry finds the job already claimed and returns immediately.
"""
from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import timedelta

import structlog
from sqlalchemy import delete, select, update

from scraper0.config import get_settings
from scraper0.core.crypto import decrypt, encrypt
from scraper0.db.models import Job, JobEvent, JobFile, new_id, now
from scraper0.db.session import session_scope

log = structlog.get_logger(__name__)
TERMINAL = {"succeeded", "failed", "cancelled"}
KINDS = {"crawl", "connector", "ingest", "site_refresh", "site_learn", "site_write"}
STALE_AFTER = timedelta(minutes=10)

_pool: ThreadPoolExecutor | None = None
_pool_lock = threading.Lock()


class JobInputError(ValueError):
    pass


# ------------------------------------------------------------------ public view

def job_public(j: Job, include_result: bool = True) -> dict:
    inp = {k: v for k, v in (j.input or {}).items() if k not in ("auth_enc",)}
    if (j.input or {}).get("auth_enc"):
        inp["auth"] = "***"
    d = {"id": j.id, "kind": j.kind, "status": j.status, "input": inp, "error": j.error, "progress": j.progress or {},
         "source_id": j.source_id, "created_at": j.created_at.isoformat() if j.created_at else None,
         "started_at": j.started_at.isoformat() if j.started_at else None,
         "finished_at": j.finished_at.isoformat() if j.finished_at else None}
    if include_result:
        d["result"] = j.result
    return d


# ------------------------------------------------------------------ submit

def submit(tenant_id: str, kind: str, input: dict, source_id: str | None = None,
           files: list[tuple[str, bytes]] | None = None) -> str:
    if kind not in KINDS:
        raise JobInputError(f"kind must be one of {sorted(KINDS)}")
    input = dict(input or {})
    _validate(kind, input, files)
    with session_scope() as s:
        j = add_job(s, tenant_id, kind, input, source_id)
        for name, data in files or []:
            s.add(JobFile(job_id=j.id, filename=name[:300], data=data))
        return j.id


def add_job(s, tenant_id: str, kind: str, input: dict, source_id: str | None = None, job_id: str | None = None) -> Job:
    """Add a queued job inside the caller's session (so it can commit atomically with other rows).
    `input["auth"]` is encrypted; the caller dispatches the job after committing."""
    input = dict(input or {})
    auth = input.pop("auth", None)
    if auth:
        input["auth_enc"] = encrypt(json.dumps(auth))      # portal passwords never stored in clear
    j = Job(id=job_id or new_id(), tenant_id=tenant_id, kind=kind, input=input, source_id=source_id,
            progress={"message": "queued"})
    s.add(j)
    s.flush()
    return j


def _validate(kind: str, inp: dict, files) -> None:
    if kind == "crawl":
        urls = inp.get("urls")
        if not isinstance(urls, list) or not urls or not all(isinstance(u, str) and u.startswith(("http://", "https://")) for u in urls):
            raise JobInputError("input.urls must be a non-empty list of http(s) URLs")
        if len(urls) > 20:
            raise JobInputError("at most 20 urls per job")
        if not isinstance(inp.get("options", {}), dict):
            raise JobInputError("input.options must be an object")
    elif kind == "connector":
        if not inp.get("connector") or not inp.get("action"):
            raise JobInputError("input.connector and input.action are required")
        if not isinstance(inp.get("data", {}), dict):
            raise JobInputError("input.data must be an object")
    elif kind == "ingest":
        if not (files or (inp.get("text") or "").strip() or inp.get("urls")):
            raise JobInputError("provide at least one of: files, text, urls")


# ------------------------------------------------------------------ dispatch

def dispatch(job_id: str) -> None:
    mode = get_settings().job_mode
    if mode == "inline":
        run_job(job_id)
    elif mode == "cloud_tasks":
        from scraper0.jobs.cloud_tasks import enqueue
        enqueue(job_id)
    else:
        _executor().submit(_safe_run, job_id)


def _executor() -> ThreadPoolExecutor:
    global _pool
    with _pool_lock:
        if _pool is None:
            _pool = ThreadPoolExecutor(max_workers=get_settings().job_workers, thread_name_prefix="job")
        return _pool


def _safe_run(job_id: str):
    try:
        run_job(job_id)
    except Exception:
        log.exception("job_crashed", job_id=job_id)


def wait(job_id: str, timeout_s: float = 3300) -> dict:
    deadline = time.monotonic() + timeout_s
    delay = 0.3
    while True:
        with session_scope() as s:
            j = s.get(Job, job_id)
            if j is None or j.status in TERMINAL or time.monotonic() > deadline:
                return job_public(j) if j else {}
        time.sleep(delay)
        delay = min(delay * 1.5, 2.0)


# ------------------------------------------------------------------ progress / cancel

class Reporter:
    """Progress events (throttled DB writes) + heartbeat + cancellation check."""

    def __init__(self, job_id: str):
        self.job_id = job_id
        self._last = 0.0
        self._lock = threading.Lock()
        self.state: dict = {"message": "running", "pages": 0, "items": 0, "llm_calls": 0, "recipes_learned": 0}

    def __call__(self, message: str, data: dict | None = None, force: bool = False):
        with self._lock:
            self.state.update({k: v for k, v in (data or {}).items() if isinstance(v, (int, float, str, bool))})
            self.state["message"] = message[:300]
            nowt = time.monotonic()
            if not force and nowt - self._last < 1.0:
                return
            self._last = nowt
            snap = dict(self.state)
        with session_scope() as s:
            s.add(JobEvent(job_id=self.job_id, type="progress", data=snap))
            s.execute(update(Job).where(Job.id == self.job_id).values(progress=snap, heartbeat_at=now()))

    def cancelled(self) -> bool:
        with session_scope() as s:
            return bool(s.scalar(select(Job.cancel_requested).where(Job.id == self.job_id)))


def request_cancel(job_id: str) -> None:
    with session_scope() as s:
        j = s.get(Job, job_id)
        if j is None or j.status in TERMINAL:
            return
        if j.status == "queued":
            j.status, j.finished_at, j.error = "cancelled", now(), "cancelled before start"
            s.add(JobEvent(job_id=job_id, type="done", data={"status": "cancelled"}))
        else:
            j.cancel_requested = True


# ------------------------------------------------------------------ run

def run_job(job_id: str) -> None:
    with session_scope() as s:   # atomic claim: only one worker ever runs a job
        claimed = s.execute(update(Job).where(Job.id == job_id, Job.status == "queued").values(
            status="running", started_at=now(), heartbeat_at=now(), attempts=Job.attempts + 1)).rowcount
        if not claimed:
            return
        j = s.get(Job, job_id)
        tenant_id, kind, inp, source_id = j.tenant_id, j.kind, dict(j.input or {}), j.source_id
    rep = Reporter(job_id)
    rep("started", force=True)
    beat_stop = threading.Event()

    def beat():   # heartbeat even when a step (e.g. a browser login) reports nothing for minutes
        while not beat_stop.wait(60):
            with session_scope() as s:
                s.execute(update(Job).where(Job.id == job_id).values(heartbeat_at=now()))
    threading.Thread(target=beat, daemon=True, name=f"beat-{job_id[:6]}").start()
    log.info("job_start", job_id=job_id, kind=kind, tenant_id=tenant_id)
    status, result, error = "succeeded", None, None
    try:
        from scraper0.jobs import execute
        result = execute.run(job_id, tenant_id, kind, inp, source_id, rep)
    except execute.JobCancelled:
        status, error = "cancelled", "cancelled by user"
    except execute.JobFailed as e:
        status, error = "failed", str(e)
    except Exception as e:
        log.exception("job_failed", job_id=job_id)
        status, error = "failed", f"internal error: {type(e).__name__}: {str(e)[:300]}"
    finally:
        beat_stop.set()
        with session_scope() as s:
            s.execute(delete(JobFile).where(JobFile.job_id == job_id))
    with session_scope() as s:
        j = s.get(Job, job_id)
        j.status, j.result, j.error, j.finished_at = status, result, error, now()
        prog = dict(rep.state)
        prog["message"] = {"succeeded": "done", "failed": "failed", "cancelled": "cancelled"}[status]
        j.progress = prog
        s.add(JobEvent(job_id=job_id, type="done", data={"status": status, "error": error}))
    log.info("job_end", job_id=job_id, status=status)


def decrypt_auth(inp: dict) -> dict | None:
    return json.loads(decrypt(inp["auth_enc"])) if inp.get("auth_enc") else None


def fail_stale_jobs() -> int:
    cutoff = now() - STALE_AFTER
    with session_scope() as s:
        rows = s.scalars(select(Job).where(Job.status == "running", Job.heartbeat_at < cutoff)).all()
        for j in rows:
            j.status, j.error, j.finished_at = "failed", "worker lost (no heartbeat for 10 minutes)", now()
            s.add(JobEvent(job_id=j.id, type="done", data={"status": "failed", "error": j.error}))
        return len(rows)
