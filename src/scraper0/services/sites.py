"""v2 sites and actions: identity, schemas, seeding of code actions, serialisation, versions and the contract.

Sites, actions, versions and site items are shared across tenants (what is learned about a public site serves
everyone). Tenant-specific state is only the AI verdict cache and write records.
"""
from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit

import structlog
from sqlalchemy import select

from scraper0.db.models import ActionVersion, Site, SiteAction, Tenant, now
from scraper0.db.session import session_scope

log = structlog.get_logger(__name__)

ACTION_NAME = re.compile(r"[a-z][a-z0-9_]{0,63}")
BROKEN_AFTER = 3            # consecutive failed refreshes → action is broken
PREVIEW_ITEMS = 20


class SiteError(Exception):
    def __init__(self, status: int, code: str, message: str, **extra):
        super().__init__(message)
        self.status, self.code, self.message, self.extra = status, code, message, extra


# ------------------------------------------------------------------ identity

def parse_base_url(url: str) -> tuple[str, str, str]:
    """→ (site_id, base_url, name). site_id = host without www., dots replaced by dashes."""
    if not isinstance(url, str) or not url.strip():
        raise SiteError(422, "invalid_input", "base_url is required")
    p = urlsplit(url.strip())
    if p.scheme not in ("http", "https") or not p.hostname:
        raise SiteError(422, "invalid_input", "base_url must be an http(s) URL")
    host = p.hostname.lower()
    netloc = host + (f":{p.port}" if p.port else "")
    name = host.removeprefix("www.")
    site_id = (name + (f"-{p.port}" if p.port else "")).replace(".", "-")
    return site_id, f"{p.scheme}://{netloc}", name


def site_id_of(url: str) -> str:
    return parse_base_url(url)[0]


def item_id(site_id: str, action: str, item_key: str) -> str:
    return "itm_" + hashlib.sha256(f"{site_id}|{action}|{item_key}".encode()).hexdigest()[:24]


# ------------------------------------------------------------------ schemas

_STR = {"type": "string"}
_NSTR = {"type": ["string", "null"]}
_NNUM = {"type": ["number", "null"]}
_TIME = {"type": ["string", "null"], "format": "date-time", "description": "ISO-8601 UTC"}

EVENT_DATA_SCHEMA = {
    "type": "object",
    "properties": {"name": _STR, "start_time": _TIME, "end_time": _TIME, "address": _NSTR, "link": _STR,
                   "organisation": _NSTR, "image": _NSTR,
                   "category": {"type": ["string", "null"], "description": "one of the caller's categories; null when none were sent"},
                   "description": _NSTR, "latitude": _NNUM, "longitude": _NNUM, "is_online": {"type": "boolean"},
                   "price": _NSTR, "source_url": _NSTR},
    "required": ["name", "start_time", "link"],
}

LIST_INPUT_SCHEMA = {
    "type": "object",
    "properties": {
        "options": {"type": "object", "properties": {
            "event_filter": {"type": "string", "description": "AI relevance filter"},
            "category_filter": {"type": "string", "description": "extra context for the filter"},
            "categories": {"type": "array", "items": _STR, "description": "the caller's category list"},
            "include_online": {"type": "boolean", "default": False},
            "from": {"type": "string", "format": "date", "description": "default: today (Asia/Kolkata)"}}},
        "freshness_hours": {"type": "number", "default": 48},
        "timeout_s": {"type": "number", "default": 60, "maximum": 300},
    },
}


def list_output_schema(entity_schema: dict) -> dict:
    return {"type": "object", "properties": {
        "site_id": _STR, "action": _STR, "version": {"type": "integer"},
        "items": {"type": "array", "items": {"type": "object", "properties": {
            "id": {"type": "string", "pattern": "^itm_[0-9a-f]{24}$"}, "updated_at": {"type": "string"},
            "data": entity_schema}}},
        "complete": {"type": "boolean"}, "scraped_at": _NSTR, "refresh_job_id": _NSTR,
        "health": {"enum": ["ok", "broken"]}, "ai_error": {"type": "string"}}}


AUTH_SCHEMA = {"type": "object", "properties": {"username": _STR, "password": _STR},
               "required": ["username", "password"]}

TRACK_INPUT_SCHEMA = {"type": "object", "properties": {
    "input": {"type": "object", "properties": {"tracking_id": _STR}, "required": ["tracking_id"]},
    "timeout_s": {"type": "number", "default": 60, "maximum": 300}}, "required": ["input"]}
TRACK_OUTPUT_SCHEMA = {"type": "object", "properties": {"status": {"enum": ["OPEN", "CLOSED"]},
                                                        "meta_data": {"type": "object"}}}
REPORT_INPUT_SCHEMA = {"type": "object", "properties": {
    "input": {"type": "object", "properties": {
        "category": _STR, "sub_category": _STR, "description": _NSTR, "media_url": _NSTR,
        "latitude": {"type": ["number", "string", "null"]}, "longitude": {"type": ["number", "string", "null"]}},
        "required": ["category", "sub_category"]},
    "auth": AUTH_SCHEMA, "force": {"type": "boolean", "default": False},
    "timeout_s": {"type": "number", "default": 300}}, "required": ["input", "auth"]}
REPORT_OUTPUT_SCHEMA = {"type": "object", "properties": {"tracking_id": {"type": "integer"}}}

# the code actions seeded at startup as locked version 1
CODE_ACTIONS = [
    ("https://www.teameverest.ngo", "fetch_events", "read", "list", "event", "TEAMEVEREST", "FETCH_EVENTS"),
    ("https://mybharat.gov.in", "fetch_events", "read", "list", "event", "MYBHARATGOVIN", "FETCH_EVENTS"),
    ("https://www.ivolunteer.in", "fetch_events", "read", "list", "event", "IVOLUNTEERIN", "FETCH_EVENTS"),
    ("https://www.smartoneblr.com", "track_issue", "read", "lookup", "issue_status", "SMARTONEBLR", "TRACK_ISSUE"),
    ("https://www.smartoneblr.com", "report_issue", "write", "write", "issue", "SMARTONEBLR", "REPORT_ISSUE"),
]


def schemas_for(mode: str, connector_action: str | None = None) -> tuple[dict, dict]:
    if mode == "list":
        return LIST_INPUT_SCHEMA, list_output_schema(EVENT_DATA_SCHEMA)
    if connector_action == "TRACK_ISSUE":
        return TRACK_INPUT_SCHEMA, TRACK_OUTPUT_SCHEMA
    if connector_action == "REPORT_ISSUE":
        return REPORT_INPUT_SCHEMA, REPORT_OUTPUT_SCHEMA
    return {"type": "object"}, {"type": "object"}


def seed_code_actions() -> int:
    """Idempotent: create the code sites/actions (locked version 1) that do not exist yet."""
    created = 0
    with session_scope() as s:
        for base, name, kind, mode, entity, conn, conn_action in CODE_ACTIONS:
            site_id, base_url, host = parse_base_url(base)
            if s.get(Site, site_id) is None:
                s.add(Site(site_id=site_id, base_url=base_url, name=host))
                s.flush()
            if s.scalar(select(SiteAction).where(SiteAction.site_id == site_id, SiteAction.name == name)):
                continue
            ins, outs = schemas_for(mode, conn_action)
            a = SiteAction(site_id=site_id, name=name, kind=kind, mode=mode, entity=entity, implementation="code",
                           connector=conn, connector_action=conn_action, seed_urls=[base_url], status="locked",
                           locked_version=1, input_schema=ins, output_schema=outs)
            s.add(a)
            s.flush()
            s.add(ActionVersion(action_id=a.id, version=1, status="locked", locked_at=now(), locked_by="system",
                                implementation_snapshot={"implementation": "code", "connector": conn,
                                                         "action": conn_action}))
            created += 1
    if created:
        log.info("code_actions_seeded", created=created)
    return created


# ------------------------------------------------------------------ serialisation

def _iso(d: datetime | None) -> str | None:
    if d is not None and d.tzinfo is None:          # SQLite returns naive UTC
        d = d.replace(tzinfo=timezone.utc)
    return d.isoformat() if d else None


def action_public(a: SiteAction) -> dict:
    return {"site_id": a.site_id, "name": a.name, "kind": a.kind, "entity": a.entity,
            "implementation": a.implementation, "seed_urls": list(a.seed_urls or []), "status": a.status,
            "locked_version": a.locked_version, "input_schema": a.input_schema or {},
            "output_schema": a.output_schema or {}, "last_success_at": _iso(a.last_success_at),
            "last_failure_at": _iso(a.last_failure_at), "consecutive_failures": a.consecutive_failures or 0}


def version_public(v: ActionVersion) -> dict:
    return {"version": v.version, "status": v.status, "created_at": _iso(v.created_at), "locked_at": _iso(v.locked_at),
            "locked_by": v.locked_by, "preview": v.preview, "implementation_snapshot": v.implementation_snapshot or {},
            "job_id": v.job_id}


def _actions(s, site_id: str) -> list[SiteAction]:
    return list(s.scalars(select(SiteAction).where(SiteAction.site_id == site_id).order_by(SiteAction.name)))


def site_summary(s, site: Site, actions: list[SiteAction] | None = None) -> dict:
    actions = actions if actions is not None else _actions(s, site.site_id)
    return {"site_id": site.site_id, "base_url": site.base_url, "name": site.name,
            "actions": [{"name": a.name, "kind": a.kind, "status": a.status} for a in actions]}


def site_public(s, site: Site) -> dict:
    actions = _actions(s, site.site_id)
    d = site_summary(s, site, actions)
    t = s.get(Tenant, site.created_by_tenant) if site.created_by_tenant else None
    d.update({"created_at": _iso(site.created_at), "created_by_tenant": t.slug if t else None,
              "actions": [action_public(a) for a in actions]})
    return d


# ------------------------------------------------------------------ lookups

def get_site(s, site_id: str) -> Site:
    site = s.get(Site, site_id)
    if site is None:
        raise SiteError(404, "site_not_onboarded", f"site {site_id!r} is not onboarded")
    return site


def get_action(s, site_id: str, name: str) -> SiteAction:
    get_site(s, site_id)
    a = s.scalar(select(SiteAction).where(SiteAction.site_id == site_id, SiteAction.name == name))
    if a is None:
        raise SiteError(404, "action_not_available", f"site {site_id!r} has no action {name!r}")
    return a


def get_locked(s, site_id: str, name: str) -> tuple[SiteAction, ActionVersion]:
    a = get_action(s, site_id, name)
    v = None
    if a.locked_version is not None:
        v = s.scalar(select(ActionVersion).where(ActionVersion.action_id == a.id,
                                                 ActionVersion.version == a.locked_version))
    if v is None:
        raise SiteError(404, "action_not_available", f"action {name!r} on {site_id!r} has no locked version")
    return a, v


# ------------------------------------------------------------------ mutations

def create_site(base_url: str, tenant_id: str | None) -> tuple[dict, bool]:
    site_id, base, name = parse_base_url(base_url)
    with session_scope() as s:
        site = s.get(Site, site_id)
        if site is not None:
            return site_public(s, site), True
        site = Site(site_id=site_id, base_url=base, name=name, created_by_tenant=tenant_id)
        s.add(site)
        s.flush()
        return site_public(s, site), False


def create_action(site_id: str, body: dict, tenant_id: str | None) -> dict:
    from scraper0.core.entities import BUILTIN_SPECS
    name = body.get("name")
    if not isinstance(name, str) or not ACTION_NAME.fullmatch(name):
        raise SiteError(422, "invalid_input", "name must be snake_case (a-z, 0-9, _; ≤ 64 characters)")
    kind = body.get("kind", "read")
    if kind == "write":
        raise SiteError(422, "invalid_input", "write actions are code only for now (connectors)")
    if kind != "read":
        raise SiteError(422, "invalid_input", "kind must be read or write")
    entity = body.get("entity", "event")
    if entity not in BUILTIN_SPECS:
        raise SiteError(422, "invalid_input", f"entity must be one of {sorted(BUILTIN_SPECS)}")
    with session_scope() as s:
        site = get_site(s, site_id)
        seeds = body.get("seed_urls") or [site.base_url]
        if not isinstance(seeds, list) or len(seeds) > 20:
            raise SiteError(422, "invalid_input", "seed_urls must be a list of at most 20 URLs")
        for u in seeds:
            try:
                ok = site_id_of(u) == site_id
            except SiteError:
                ok = False
            if not ok:
                raise SiteError(422, "invalid_input", f"seed URL {u!r} is not on {site.base_url}")
        if s.scalar(select(SiteAction).where(SiteAction.site_id == site_id, SiteAction.name == name)):
            raise SiteError(409, "action_exists", f"site {site_id!r} already has an action {name!r}")
        ins, outs = schemas_for("list")
        a = SiteAction(site_id=site_id, name=name, kind="read", mode="list", entity=entity, implementation="learned",
                       seed_urls=list(dict.fromkeys(seeds)), status="draft", input_schema=ins, output_schema=outs,
                       created_by_tenant=tenant_id)
        s.add(a)
        s.flush()
        return action_public(a)


def start_learn(site_id: str, name: str, tenant_id: str) -> dict:
    """Create a draft version and the job that fills its preview and snapshot."""
    from scraper0.jobs import runner
    with session_scope() as s:
        a = get_action(s, site_id, name)
        if a.mode != "list":
            raise SiteError(422, "invalid_input", "only list read actions can be learned or repaired")
        last = s.scalar(select(ActionVersion.version).where(ActionVersion.action_id == a.id)
                        .order_by(ActionVersion.version.desc()).limit(1)) or 0
        j = runner.add_job(s, tenant_id, "site_learn", {"site_id": site_id, "action": name, "version": last + 1})
        s.add(ActionVersion(action_id=a.id, version=last + 1, status="draft", job_id=j.id,
                            implementation_snapshot={}))
        job_id, version = j.id, last + 1
    runner.dispatch(job_id)
    return {"job_id": job_id, "version": version}


def list_versions(site_id: str, name: str) -> list[dict]:
    with session_scope() as s:
        a = get_action(s, site_id, name)
        rows = s.scalars(select(ActionVersion).where(ActionVersion.action_id == a.id)
                         .order_by(ActionVersion.version.desc()))
        return [version_public(v) for v in rows]


def lock_version(site_id: str, name: str, version: int, locked_by: str) -> None:
    with session_scope() as s:
        a = get_action(s, site_id, name)
        v = s.scalar(select(ActionVersion).where(ActionVersion.action_id == a.id, ActionVersion.version == version))
        if v is None:
            raise SiteError(404, "not_found", f"action {name!r} has no version {version}")
        if v.status == "broken":
            raise SiteError(422, "invalid_input", f"version {version} failed to learn and cannot be locked")
        if v.preview is None and a.implementation == "learned":
            raise SiteError(422, "invalid_input", f"version {version} is still being learned")
        if v.status == "locked":
            return
        for old in s.scalars(select(ActionVersion).where(ActionVersion.action_id == a.id,
                                                         ActionVersion.status == "locked")):
            old.status = "retired"
        v.status, v.locked_at, v.locked_by = "locked", now(), locked_by
        a.locked_version, a.status, a.consecutive_failures = version, "locked", 0
        a.last_scraped_at, a.last_item_count = None, None      # the next read refreshes with this version


def contract(site_id: str, name: str, base: str) -> dict:
    with session_scope() as s:
        a, v = get_locked(s, site_id, name)
        verb = "write" if a.kind == "write" else "read"
        endpoint = f"{base.rstrip('/')}/v2/sites/{site_id}/actions/{name}/{verb}"
        if a.mode == "list":
            body = {"options": {}, "freshness_hours": 48, "timeout_s": 60}
            extra = ""
        elif a.mode == "lookup":
            body = {"input": {"tracking_id": "<tracking_id>"} if a.connector_action == "TRACK_ISSUE" else {},
                    "timeout_s": 60}
            extra = ""
        else:
            body = {"input": {"category": "<category>", "sub_category": "<sub_category>", "description": "<text>",
                              "media_url": "https://…/photo.jpg", "latitude": 12.97, "longitude": 77.59}
                    if a.connector_action == "REPORT_ISSUE" else {},
                    "auth": {"username": "<username>", "password": "<password>"}, "force": False, "timeout_s": 300}
            extra = " \\\n  -H 'Idempotency-Key: <your-unique-key>'"
        curl = (f"curl -sS -X POST '{endpoint}' \\\n  -H \"X-API-Key: $SCRAPER0_API_KEY\" \\\n"
                f"  -H 'Content-Type: application/json'{extra} \\\n  -d '{json.dumps(body)}'")
        return {"site_id": site_id, "action": name, "kind": a.kind, "version": v.version,
                "input_schema": a.input_schema or {}, "output_schema": a.output_schema or {},
                "endpoint": endpoint, "curl": curl}
