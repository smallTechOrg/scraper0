"""Polite HTTP: browser-grade TLS fingerprint (curl_cffi), robots.txt, per-host throttle,
retry with backoff + Retry-After, and an in-process response cache."""
from __future__ import annotations

import ipaddress
import socket
import threading
import time
from dataclasses import dataclass, field
from urllib import robotparser
from urllib.parse import urljoin, urlsplit

from curl_cffi import requests

import structlog

log = structlog.get_logger(__name__)

USER_AGENT_NAME = "HashtagLocalBot"
_RETRY_STATUS = {429, 500, 502, 503, 504}
MAX_BYTES = 5_000_000      # refuse to parse anything bigger
MAX_CACHE = 500            # entries kept in the response cache


def is_public_url(url: str) -> bool:
    """SSRF guard: http(s) only, and every resolved address must be globally routable."""
    p = urlsplit(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    try:
        infos = socket.getaddrinfo(p.hostname, p.port or (443 if p.scheme == "https" else 80))
    except socket.gaierror:
        return False
    return all(ipaddress.ip_address(i[4][0]).is_global for i in infos)


@dataclass
class HTTPResult:
    status: int
    text: str
    content_type: str
    headers: dict = field(default_factory=dict)


class Fetcher:
    def __init__(self, min_host_interval: float = 1.0, timeout: int = 20, retries: int = 3,
                 respect_robots: bool = True, session=None, renderer=None):
        self.renderer = renderer                    # core.render.Renderer, or None = never render
        self.render_hosts: set[str] = set()         # hosts whose pages turned out to be JS shells
        self.min_host_interval = min_host_interval
        self.timeout = timeout
        self.retries = retries
        self.respect_robots = respect_robots
        self._session = session or requests.Session(impersonate="chrome120")
        self._lock = threading.Lock()
        self._last_hit: dict[str, float] = {}
        self._robots: dict[str, robotparser.RobotFileParser | None] = {}
        self._cache: dict[str, tuple[str, str]] = {}

    # -- browser rendering ---------------------------------------------------
    def render(self, url: str):
        """Render a JavaScript page in a headless browser → RenderResult | None. Same politeness and
        safety rules as get(): public URLs only, robots.txt, per-host throttle."""
        if self.renderer is None or not is_public_url(url) or not self.allowed(url):
            return None
        self._wait(url)
        return self.renderer.render(url)

    def open_items(self, url: str, texts: list[str]) -> list[dict]:
        """While learning: render `url`, click the elements showing `texts` and report where each leads
        ([{"text", "url", "canonical"}]) — for sites whose item cards are not links. Same rules as render()."""
        if self.renderer is None or not is_public_url(url) or not self.allowed(url):
            return []
        self._wait(url)
        res = self.renderer.render(url, click_texts=texts)
        return list(res.opened) if res is not None else []

    # -- robots -------------------------------------------------------------
    def allowed(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        p = urlsplit(url)
        origin = f"{p.scheme}://{p.netloc}"
        with self._lock:
            if origin not in self._robots:
                rp = robotparser.RobotFileParser()
                try:
                    r = self._session.get(f"{origin}/robots.txt", timeout=10)
                    if r.status_code == 200:
                        rp.parse(r.text.splitlines())
                        self._robots[origin] = rp
                    else:  # 404/other → no restrictions
                        self._robots[origin] = None
                except Exception:
                    self._robots[origin] = None
            rp = self._robots[origin]
        return True if rp is None else rp.can_fetch("*", url)

    def crawl_delay(self, url: str) -> float:
        p = urlsplit(url)
        rp = self._robots.get(f"{p.scheme}://{p.netloc}")
        return float(rp.crawl_delay("*") or 0) if rp else 0.0

    # -- throttle -----------------------------------------------------------
    def _wait(self, url: str):
        host = urlsplit(url).netloc
        gap = max(self.min_host_interval, self.crawl_delay(url))
        with self._lock:  # reserve a slot, sleep outside the lock
            now = time.monotonic()
            slot = max(now, self._last_hit.get(host, 0) + gap)
            self._last_hit[host] = slot
        if slot > now:
            time.sleep(slot - now)

    # -- get ----------------------------------------------------------------
    def get(self, url: str, **kw) -> tuple[str, str] | None:
        """→ (text, content_type) or None when blocked/failed. Never raises."""
        if url in self._cache:
            return self._cache[url]
        if not is_public_url(url):
            log.info(f"blocked non-public URL {url}")
            return None
        if not self.allowed(url):
            log.info(f"robots.txt disallows {url}")
            return None
        for attempt in range(self.retries + 1):
            self._wait(url)
            try:
                r = self._get_following(url, **kw)
            except Exception as e:
                log.info(f"{url}: {e} (attempt {attempt + 1})")
                time.sleep(min(2 ** attempt, 8))
                continue
            if r.status_code == 200:
                ctype = r.headers.get("content-type", "").lower()
                if ctype.startswith(("image/", "video/", "audio/")) or "pdf" in ctype or len(r.content) > MAX_BYTES:
                    log.info(f"{url}: skipped (non-text or >{MAX_BYTES} bytes)")
                    return None
                res = (r.text, ctype)
                if len(self._cache) >= MAX_CACHE:
                    self._cache.pop(next(iter(self._cache)))
                self._cache[url] = res
                return res
            if r.status_code in _RETRY_STATUS and attempt < self.retries:
                try:
                    wait = float(r.headers.get("retry-after", ""))
                except ValueError:
                    wait = 2 ** attempt
                time.sleep(min(wait, 30))
                continue
            log.info(f"{url}: HTTP {r.status_code}")
            return None
        return None

    # -- any method (plans) ----------------------------------------------------
    def request(self, method: str, url: str, headers: dict | None = None, data: str | bytes | None = None
                ) -> HTTPResult | None:
        """One request for a recipe plan (GET or POST; no cache). Same rules as get(): public URLs only (every
        redirect hop too), robots.txt, per-host throttle, retries on 429/5xx. → HTTPResult (any status), or
        None when blocked or unreachable. Never raises. Cookies persist in the session (warm-up requests)."""
        method = method.upper()
        if method not in ("GET", "POST"):
            return None
        if not is_public_url(url) or not self.allowed(url):
            log.info("request_blocked", url=url)
            return None
        for attempt in range(self.retries + 1):
            self._wait(url)
            try:
                if method == "GET":
                    r = self._get_following(url, headers=headers or {})
                else:
                    r = self._session.post(url, data=data, headers=headers or {}, timeout=self.timeout,
                                           allow_redirects=False)
            except Exception as e:  # noqa: BLE001
                log.info("request_failed", url=url, error=str(e)[:200], attempt=attempt + 1)
                time.sleep(min(2 ** attempt, 8))
                continue
            if r.status_code in _RETRY_STATUS and attempt < self.retries:
                try:
                    wait = float(r.headers.get("retry-after", ""))
                except ValueError:
                    wait = 2 ** attempt
                time.sleep(min(wait, 30))
                continue
            if len(r.content) > MAX_BYTES:
                return None
            return HTTPResult(r.status_code, r.text, r.headers.get("content-type", "").lower(),
                              {k.lower(): v for k, v in r.headers.items()})
        return None

    def cookie(self, name: str) -> str | None:
        try:
            return self._session.cookies.get(name)
        except Exception:  # noqa: BLE001
            return None

    def _get_following(self, url: str, **kw):
        """Follow redirects manually so every hop passes the SSRF guard."""
        for _ in range(5):
            r = self._session.get(url, timeout=self.timeout, allow_redirects=False, **kw)
            if r.status_code in (301, 302, 303, 307, 308) and r.headers.get("location"):
                url = urljoin(url, r.headers["location"])
                if not is_public_url(url):
                    raise ValueError(f"redirect to non-public URL blocked: {url}")
                continue
            return r
        raise ValueError("too many redirects")
