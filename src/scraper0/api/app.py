from __future__ import annotations

import time
import uuid
from contextlib import asynccontextmanager

import structlog
from fastapi import FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from starlette.exceptions import HTTPException as StarletteHTTPException

from scraper0 import __version__
from scraper0.api import routes_admin, routes_jobs, routes_legacy, routes_public, routes_tenant, routes_v2
from scraper0.api.deps import APIError, error_response, require_internal
from scraper0.config import get_settings

log = structlog.get_logger("scraper0.http")


def create_app(run_startup: bool = True) -> FastAPI:
    st = get_settings()

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        if run_startup:
            from starlette.concurrency import run_in_threadpool

            from scraper0.bootstrap import startup
            await run_in_threadpool(startup)
            if st.job_mode == "thread" and st.env != "test":
                from scraper0.jobs.scheduler import start_local_scheduler
                start_local_scheduler()
        yield

    app = FastAPI(title="scraper0", version=__version__, lifespan=lifespan,
                  docs_url="/api/docs", openapi_url="/api/openapi.json", redoc_url=None)
    app.add_middleware(CORSMiddleware, allow_origins=st.cors_origins, allow_credentials=True,
                       allow_methods=["*"], allow_headers=["*"])

    @app.middleware("http")
    async def access_log(request: Request, call_next):
        rid = request.headers.get("x-request-id") or uuid.uuid4().hex[:12]
        structlog.contextvars.bind_contextvars(request_id=rid)
        t = time.monotonic()
        try:
            resp = await call_next(request)
        finally:
            structlog.contextvars.unbind_contextvars("request_id")
        if request.url.path != "/api/health":
            log.info("http", method=request.method, path=request.url.path, status=resp.status_code,
                     ms=int((time.monotonic() - t) * 1000), request_id=rid)
        resp.headers["x-request-id"] = rid
        return resp

    @app.exception_handler(APIError)
    async def _api_error(request: Request, e: APIError):
        return error_response(e.status, e.code, e.message, e.extra)

    @app.exception_handler(RequestValidationError)
    async def _validation(request: Request, e: RequestValidationError):
        if request.url.path == "/api/v1/scrape":
            return routes_legacy.legacy_error("invalid request body")
        msg = "; ".join(f"{'.'.join(str(x) for x in err['loc'][1:])}: {err['msg']}" for err in e.errors())[:500]
        return error_response(422, "invalid_input", msg or "invalid request")

    @app.exception_handler(StarletteHTTPException)
    async def _http(request: Request, e: StarletteHTTPException):
        return error_response(e.status_code, "http_error", str(e.detail))

    @app.exception_handler(Exception)
    async def _crash(request: Request, e: Exception):
        log.exception("unhandled", path=request.url.path)
        return error_response(500, "internal_error", "internal error")

    for r in (routes_public, routes_tenant, routes_jobs, routes_admin, routes_legacy, routes_v2):
        app.include_router(r.router)

    @app.post("/internal/jobs/{job_id}/run")
    def internal_run(job_id: str, request: Request):
        require_internal(request)
        from scraper0.jobs.runner import run_job
        run_job(job_id)
        return {"data": {"job_id": job_id}}

    @app.post("/internal/tick")
    def internal_tick(request: Request):
        require_internal(request)
        from scraper0.jobs.scheduler import tick
        return {"data": tick()}

    return app
