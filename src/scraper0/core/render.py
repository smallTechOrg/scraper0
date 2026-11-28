"""Headless-browser rendering for JavaScript-only pages (React/Vue/Angular shells).

Returns the rendered HTML *and* the JSON API responses the page fetched while rendering — those
are usually the cleanest source of data (see core/json_extract.py).

Playwright's sync API is bound to the thread that created it and cannot run on an asyncio loop,
so all rendering happens on one dedicated worker thread with one shared browser; callers queue
requests and wait. Renders are therefore serial: slower, but safe and light on memory.
"""
from __future__ import annotations

import queue
import threading
import time
from dataclasses import dataclass, field
from urllib.parse import urlsplit

import structlog

from scraper0.core.fetch import is_public_url

log = structlog.get_logger(__name__)

IDLE_SHUTDOWN_S = 90          # close the browser after this long without work
NAV_TIMEOUT_MS = 30_000
MAX_JSON_BYTES = 5_000_000
MAX_JSON_RESPONSES = 40
MAX_CAPTURED = 60             # requests recorded per render (any method), for learning plans
MAX_CAPTURE_BYTES = 2_000_000
# Headers never recorded: the browser's own, and anything that can carry a credential. A learned plan that
# needs one of these gets it from a `capture` step at run time instead (core/plans.py).
_DROP_HEADERS = {"cookie", "authorization", "proxy-authorization", "user-agent", "accept-encoding", "connection",
                 "content-length", "host", "priority"}
_SECRET_HINT = ("token", "auth", "session", "csrf", "xsrf", "secret", "signature", "api-key", "apikey")
_BLOCK_TYPES = {"image", "media", "font"}      # not needed to read the page; saves time and bandwidth
_LAUNCH_ARGS = ["--no-sandbox", "--disable-dev-shm-usage", "--disable-gpu"]


@dataclass
class RenderResult:
    html: str
    final_url: str
    json_responses: list[tuple[str, str]] = field(default_factory=list)   # (url, body) for GET xhr/fetch JSON
    requests: list[CapturedRequest] = field(default_factory=list)         # every xhr/fetch, any method/host


@dataclass
class CapturedRequest:
    """One xhr/fetch call the page made. `headers` is filtered (no cookies or credentials); `secret_headers`
    lists the names that were dropped, so a learner knows what a replay is missing."""
    method: str
    url: str
    headers: dict
    body: str | None
    status: int
    content_type: str
    response: str | None
    secret_headers: list[str] = field(default_factory=list)
    response_headers: dict = field(default_factory=dict)
    # Values of the dropped credential headers, kept in memory only while learning (to find where a token comes
    # from, e.g. an earlier response); a plan never contains them — it captures them at run time instead.
    raw_secret_values: dict = field(default_factory=dict, repr=False)


def clean_headers(headers: dict) -> tuple[dict, list[str]]:
    keep, dropped = {}, []
    for k, v in (headers or {}).items():
        lk = k.lower()
        if lk.startswith(":") or lk.startswith("sec-"):
            continue
        if lk in _DROP_HEADERS or any(h in lk for h in _SECRET_HINT):
            if lk not in ("user-agent", "accept-encoding", "connection", "content-length", "host", "priority"):
                dropped.append(lk)
            continue
        keep[lk] = v
    return keep, dropped


def is_js_shell(html: str, visible_text: str) -> bool:
    """A page that needs a browser: almost no visible text, but scripts to produce it."""
    low = html[:20000].lower()
    return len(visible_text) < 200 and "<script" in low and ("<html" in low or "<body" in low or "<div" in low)


def _site(host: str) -> str:
    parts = (host or "").removeprefix("www.").split(".")
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


class Renderer:
    def __init__(self, headless: bool = True):
        self.headless = headless
        self._q: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._lock = threading.Lock()
        self._host_ok: dict[str, bool] = {}
        self.unavailable: str | None = None     # set when Chromium/Playwright can't start

    # ---------------------------------------------------------------- public
    def render(self, url: str, timeout_s: float = 75) -> RenderResult | None:
        """Never raises. None when rendering is unavailable, blocked or failed."""
        if self.unavailable or not is_public_url(url):
            return None
        box: dict = {}
        done = threading.Event()
        self._q.put((url, box, done))
        self._ensure_thread()
        if not done.wait(timeout_s):
            log.warning("render_timeout", url=url)
            return None
        return box.get("result")

    # ---------------------------------------------------------------- worker
    def _ensure_thread(self):
        with self._lock:
            if self._thread is None or not self._thread.is_alive():
                self._thread = threading.Thread(target=self._run, name="renderer", daemon=True)
                self._thread.start()

    def _run(self):
        try:
            from playwright.sync_api import sync_playwright
        except Exception as e:  # noqa: BLE001
            self._fail_all(f"playwright not installed: {e}")
            return
        try:
            with sync_playwright() as pw:
                try:
                    browser = pw.chromium.launch(headless=self.headless, args=_LAUNCH_ARGS)
                except Exception as e:  # noqa: BLE001
                    self._fail_all(f"chromium not available (run `playwright install chromium`): {str(e)[:200]}")
                    return
                try:
                    while True:
                        try:
                            url, box, done = self._q.get(timeout=IDLE_SHUTDOWN_S)
                        except queue.Empty:
                            return
                        try:
                            box["result"] = self._render_one(browser, url)
                        except Exception as e:  # noqa: BLE001
                            log.warning("render_failed", url=url, error=str(e)[:300])
                        finally:
                            done.set()
                finally:
                    browser.close()
        except Exception as e:  # noqa: BLE001
            self._fail_all(str(e)[:200])

    def _fail_all(self, reason: str):
        self.unavailable = reason
        log.warning("renderer_unavailable", reason=reason)
        while True:
            try:
                _, _, done = self._q.get_nowait()
                done.set()
            except queue.Empty:
                return

    def _allowed(self, url: str) -> bool:
        host = urlsplit(url).netloc
        if host not in self._host_ok:
            self._host_ok[host] = is_public_url(url)     # SSRF guard for every sub-request, cached per host
        return self._host_ok[host]

    def _render_one(self, browser, url: str) -> RenderResult:
        t0 = time.monotonic()
        ctx = browser.new_context(viewport={"width": 1280, "height": 2000}, locale="en-IN", timezone_id="Asia/Kolkata")
        captured: list[tuple[str, str]] = []
        calls: list[CapturedRequest] = []
        site = _site(urlsplit(url).hostname or "")
        try:
            page = ctx.new_page()

            def route(r):
                req = r.request
                if req.resource_type in _BLOCK_TYPES or not self._allowed(req.url):
                    r.abort()
                else:
                    r.continue_()
            page.route("**/*", route)

            def on_response(resp):
                try:
                    req = resp.request
                    if (req.resource_type in ("xhr", "fetch") and req.method == "GET" and resp.status == 200
                            and "json" in (resp.headers.get("content-type") or "")
                            and _site(urlsplit(resp.url).hostname or "") == site
                            and len(captured) < MAX_JSON_RESPONSES):
                        body = resp.text()
                        if len(body) <= MAX_JSON_BYTES:
                            captured.append((resp.url, body))
                except Exception:  # noqa: BLE001 — a body can be unavailable after navigation
                    pass
                try:
                    req = resp.request
                    if req.resource_type not in ("xhr", "fetch") or len(calls) >= MAX_CAPTURED:
                        return
                    ctype = (resp.headers.get("content-type") or "").lower()
                    text = None
                    if any(t in ctype for t in ("json", "text", "html", "xml", "javascript")):
                        text = resp.text()
                        if len(text) > MAX_CAPTURE_BYTES:
                            text = None
                    headers, dropped = clean_headers(req.headers)
                    raw = {k.lower(): v for k, v in req.headers.items() if k.lower() in dropped and k.lower() != "cookie"}
                    calls.append(CapturedRequest(req.method, req.url, headers, req.post_data, resp.status, ctype,
                                                 text, dropped, {k.lower(): v for k, v in resp.headers.items()}, raw))
                except Exception:  # noqa: BLE001
                    pass
            page.on("response", on_response)

            try:
                page.goto(url, wait_until="networkidle", timeout=NAV_TIMEOUT_MS)
            except Exception as e:  # noqa: BLE001 — pages that poll forever never go idle: use what loaded
                log.info("render_no_idle", url=url, error=str(e)[:120])
            try:  # trigger lazy-loaded lists once
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(700)
            except Exception:  # noqa: BLE001
                pass
            html = page.content()
            final = page.url
        finally:
            ctx.close()
        log.info("rendered", url=url, ms=int((time.monotonic() - t0) * 1000), html_chars=len(html), json=len(captured),
                 calls=len(calls))
        return RenderResult(html=html, final_url=final, json_responses=captured, requests=calls)


_shared: Renderer | None = None
_shared_lock = threading.Lock()


def shared_renderer() -> Renderer:
    global _shared
    with _shared_lock:
        if _shared is None:
            from scraper0.config import get_settings
            _shared = Renderer(headless=get_settings().browser_headless)
        return _shared
