"""Self-contained fakes for connector tests: AI, StateStore, HTTP session, ConnectorContext factory."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from scraper0.connectors.base import ConnectorContext

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "connectors"


def fixture_text(name: str) -> str:
    return (FIXTURES / name).read_text(encoding="utf-8")


def fixture_json(name: str):
    return json.loads(fixture_text(name))


class FakeAI:
    """`text_replies` / `vision_replies`: a list (consumed in order), a callable(prompt) or an Exception."""

    def __init__(self, text_replies=None, vision_replies=None):
        self.text_replies = text_replies if text_replies is not None else []
        self.vision_replies = vision_replies if vision_replies is not None else []
        self.text_prompts: list[str] = []
        self.vision_calls: list[tuple[str, bytes, str]] = []

    @staticmethod
    def _next(replies, *args):
        if isinstance(replies, Exception):
            raise replies
        if callable(replies):
            return replies(*args)
        if not replies:
            raise AssertionError("FakeAI: unexpected call")
        r = replies.pop(0)
        if isinstance(r, Exception):
            raise r
        return r

    def text(self, prompt: str) -> str:
        self.text_prompts.append(prompt)
        return self._next(self.text_replies, prompt)

    def vision(self, prompt: str, image: bytes, mime: str) -> str:
        self.vision_calls.append((prompt, image, mime))
        return self._next(self.vision_replies, prompt, image, mime)


class FakeState:
    def __init__(self, initial: dict | None = None):
        self.data: dict[str, str] = dict(initial or {})
        self.sets: list[tuple[str, str]] = []
        self.gets: list[str] = []

    def get(self, key):
        self.gets.append(key)
        return self.data.get(key)

    def set(self, key, value):
        self.sets.append((key, value))
        self.data[key] = value

    def delete(self, key):
        self.data.pop(key, None)


class FakeResponse:
    def __init__(self, text: str = "", status_code: int = 200, json_data=None):
        self.text = text if json_data is None else json.dumps(json_data)
        self.status_code = status_code
        self._json = json_data

    @property
    def ok(self):
        return 200 <= self.status_code < 400

    def json(self):
        return self._json if self._json is not None else json.loads(self.text)

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"HTTP {self.status_code}")


class FakeSession:
    """Routes: {url: FakeResponse | callable(method, url, kwargs) -> FakeResponse}. Records every call."""

    def __init__(self, routes: dict):
        self.routes = routes
        self.calls: list[tuple[str, str, dict]] = []

    def _do(self, method, url, kw):
        self.calls.append((method, url, kw))
        r = self.routes.get(url)
        if r is None:
            return FakeResponse("not found", 404)
        return r(method, url, kw) if callable(r) else r

    def get(self, url, **kw):
        return self._do("GET", url, kw)

    def post(self, url, **kw):
        return self._do("POST", url, kw)

    def urls(self, method=None):
        return [u for m, u, _ in self.calls if method in (None, m)]


@pytest.fixture
def make_ctx():
    def _make(ai=None, state=None, auth=None):
        progress: list[tuple[str, dict]] = []
        ctx = ConnectorContext(tenant_id="t1", ai=ai or FakeAI(), state=state or FakeState(), auth=auth,
                               progress=lambda m, d=None: progress.append((m, d)))
        ctx.progress_log = progress  # type: ignore[attr-defined]
        return ctx
    return _make


@pytest.fixture
def fake_net(monkeypatch):
    """Install a FakeSession for the event connectors and a fixed IPv4. Returns a function(routes) -> FakeSession."""
    from scraper0.connectors import _events_common as common

    def _install(routes: dict, ipv4: str = "10.0.0.5"):
        session = FakeSession(routes)
        monkeypatch.setattr(common, "make_session", lambda: session)
        monkeypatch.setattr(common, "local_ipv4", lambda: ipv4)
        return session
    return _install
