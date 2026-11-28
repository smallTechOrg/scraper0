"""Connector contract: site-specific integrations (portals) that need hand-written logic —
logins, form submission, private APIs — as opposed to the generic learning crawler.

A connector exposes named actions. Each action takes the request's `data` dict and a context,
and returns the payload that goes under `"data"` in the response. Raise ConnectorError with a
user-facing message for expected failures (bad input, login failed, portal down).

Connectors must not touch the filesystem for state (Cloud Run is ephemeral): use ctx.state.
They must not call an LLM directly: use ctx.ai, which picks the tenant's provider, enforces the
budget and meters usage.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Callable, Protocol


class ConnectorError(Exception):
    """Expected failure with a message safe to show the caller."""


class AI(Protocol):
    def text(self, prompt: str) -> str: ...
    def vision(self, prompt: str, image: bytes, mime: str) -> str: ...


class StateStore(Protocol):
    """Encrypted key/value store scoped to (tenant, connector)."""
    def get(self, key: str) -> str | None: ...
    def set(self, key: str, value: str) -> None: ...
    def delete(self, key: str) -> None: ...


@dataclass
class ConnectorContext:
    tenant_id: str
    ai: AI
    state: StateStore
    auth: dict | None = None                # {"username": ..., "password": ...} when the action needs a portal login
    progress: Callable[[str, dict], None] = lambda message, data=None: None   # job progress events


Action = Callable[[dict, ConnectorContext], dict]


@dataclass
class Connector:
    name: str                               # portal id, e.g. "TEAMEVEREST" (matches the legacy API's "portal")
    source: str                             # "EVENT_PORTAL" | "GOV_ISSUE_PORTAL"
    description: str
    actions: dict[str, Action] = field(default_factory=dict)   # "FETCH_EVENTS" | "REPORT_ISSUE" | "TRACK_ISSUE" ...
    needs_browser: bool = False


_REGISTRY: dict[str, Connector] = {}


def register(connector: Connector) -> Connector:
    _REGISTRY[connector.name] = connector
    return connector


def get(name: str) -> Connector | None:
    load_all()
    return _REGISTRY.get(name)


def all_connectors() -> list[Connector]:
    load_all()
    return list(_REGISTRY.values())


_loaded = False


def load_all() -> None:
    """Import every connector module once so they self-register."""
    global _loaded
    if _loaded:
        return
    _loaded = True
    import importlib
    import pkgutil

    import scraper0.connectors as pkg
    for m in pkgutil.walk_packages(pkg.__path__, prefix="scraper0.connectors."):
        if m.name.endswith((".base",)) or ".tests" in m.name:
            continue
        importlib.import_module(m.name)
