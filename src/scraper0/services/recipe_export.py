"""Recipes as files: export every locked action version to JSON and import them back.

What scraper0 learns about a site lives in the database. These files are the copy that survives losing it:
one deterministic, diff-friendly JSON document per site action (`<dir>/<site_id>/<action>.json`) plus
`index.json`, meant to be kept in a git repository. Nothing here pushes to git.

A document holds the site, the action, the locked version number and who locked it, and the implementation:
the plans of a learned action, or the connector reference of a code action. It never holds run state
(freshness, failures, previews, learn statistics) or secrets (credential headers are stripped).
"""
from __future__ import annotations

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path

import structlog
from sqlalchemy import select

from scraper0.db.models import ActionVersion, Site, SiteAction, now
from scraper0.db.session import session_scope
from scraper0.services import sites as sites_svc
from scraper0.services.sites import SiteError

log = structlog.get_logger(__name__)

FORMAT = "scraper0.recipe/1"
BUNDLE_FORMAT = "scraper0.recipes/1"
INDEX = "index.json"
SECRET_HEADERS = {"authorization", "proxy-authorization", "cookie", "set-cookie", "x-api-key", "x-auth-token",
                  "x-csrf-token", "x-xsrf-token"}
_KEEP_LEARNED = ("implementation", "entity", "seed_urls", "plans")     # not: learn stats, check_run


# ------------------------------------------------------------------ document

def _iso(d: datetime | None) -> str | None:
    if d is None:
        return None
    if d.tzinfo is None:
        d = d.replace(tzinfo=timezone.utc)
    return d.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def scrub(value):
    """Drop credential headers anywhere in a plan (a learned plan should never hold one; this makes sure
    an exported file can't)."""
    if isinstance(value, dict):
        out = {}
        for k, v in value.items():
            if k == "headers" and isinstance(v, dict):
                v = {h: hv for h, hv in v.items() if str(h).lower() not in SECRET_HEADERS}
            out[k] = scrub(v)
        return out
    if isinstance(value, list):
        return [scrub(v) for v in value]
    return value


def implementation_of(snapshot: dict | None, action: SiteAction) -> dict:
    """The part of a version snapshot that defines how the action runs."""
    snap = snapshot or {}
    if action.implementation == "code" or snap.get("implementation") == "code":
        return {"implementation": "code", "connector": snap.get("connector") or action.connector,
                "action": snap.get("action") or action.connector_action}
    out = {k: snap[k] for k in _KEEP_LEARNED if k in snap}
    out["implementation"] = "learned"
    return scrub(json.loads(json.dumps(out, default=str)))


def document(site: Site, a: SiteAction, v: ActionVersion) -> dict:
    return {
        "format": FORMAT,
        "site": {"site_id": site.site_id, "base_url": site.base_url, "name": site.name},
        "action": {"name": a.name, "kind": a.kind, "mode": a.mode, "entity": a.entity,
                   "seed_urls": list(a.seed_urls or []), "input_schema": a.input_schema or {},
                   "output_schema": a.output_schema or {}},
        "version": v.version,
        "locked_at": _iso(v.locked_at),
        "locked_by": v.locked_by,
        "implementation": implementation_of(v.implementation_snapshot, a),
    }


def dumps(doc) -> str:
    """Stable text: sorted keys, two-space indent, one trailing newline."""
    return json.dumps(doc, sort_keys=True, indent=2, ensure_ascii=False) + "\n"


def content_hash(doc: dict) -> str:
    """Identity of what a recipe *does*: site, action definition and implementation (not who locked it or when)."""
    core = {"site": doc.get("site"), "action": doc.get("action"), "implementation": doc.get("implementation")}
    return hashlib.sha256(json.dumps(core, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


# ------------------------------------------------------------------ export

def export_action(site_id: str, name: str) -> dict:
    with session_scope() as s:
        a, v = sites_svc.get_locked(s, site_id, name)
        return document(s.get(Site, site_id), a, v)


def export_all() -> list[dict]:
    docs = []
    with session_scope() as s:
        rows = s.execute(
            select(Site, SiteAction, ActionVersion)
            .join(SiteAction, SiteAction.site_id == Site.site_id)
            .join(ActionVersion, (ActionVersion.action_id == SiteAction.id)
                  & (ActionVersion.version == SiteAction.locked_version))
            .order_by(Site.site_id, SiteAction.name)).all()
        for site, a, v in rows:
            docs.append(document(site, a, v))
    return docs


def bundle() -> dict:
    return {"format": BUNDLE_FORMAT, "recipes": export_all()}


def _write_if_changed(path: Path, text: str) -> bool:
    if path.exists() and path.read_text(encoding="utf-8") == text:
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8")
    tmp.replace(path)
    return True


def _safe(part: str) -> str:
    if not part or part in (".", "..") or "/" in part or "\\" in part or "\x00" in part:
        raise ValueError(f"unsafe path part {part!r}")
    return part


def _path(root: Path, doc: dict) -> Path:
    return root / _safe(doc["site"]["site_id"]) / (_safe(doc["action"]["name"]) + ".json")


def recipe_files(root: Path) -> list[Path]:
    return sorted(p for p in root.glob("*/*.json") if p.is_file())


def write_index(root: Path) -> bool:
    """index.json lists the recipe files on disk, so it is correct whoever wrote them last."""
    entries = []
    for p in recipe_files(root):
        try:
            doc = json.loads(p.read_text(encoding="utf-8"))
            impl = doc.get("implementation") or {}
            entries.append({"file": p.relative_to(root).as_posix(), "site_id": doc["site"]["site_id"],
                            "action": doc["action"]["name"], "kind": doc["action"].get("kind"),
                            "implementation": impl.get("implementation"), "version": doc.get("version"),
                            "sha256": content_hash(doc)})
        except (ValueError, KeyError, TypeError):
            entries.append({"file": p.relative_to(root).as_posix(), "error": "not a recipe file"})
    return _write_if_changed(root / INDEX, dumps({"format": BUNDLE_FORMAT, "recipes": entries}))


def export_dir(directory: str | Path, prune: bool = False) -> dict:
    """Write every locked version under `directory`. Unchanged recipes leave their files untouched.
    With `prune`, recipe files of actions that are no longer locked are deleted."""
    root = Path(directory)
    root.mkdir(parents=True, exist_ok=True)
    report = {"written": 0, "unchanged": 0, "removed": 0, "files": []}
    keep = set()
    for doc in export_all():
        p = _path(root, doc)
        keep.add(p)
        changed = _write_if_changed(p, dumps(doc))
        report["written" if changed else "unchanged"] += 1
        report["files"].append(p.relative_to(root).as_posix())
    if prune:
        for p in recipe_files(root):
            if p not in keep:
                p.unlink()
                report["removed"] += 1
    write_index(root)
    return report


def export_on_lock(site_id: str, name: str) -> None:
    """The lock hook: when SCRAPER0_RECIPE_EXPORT_DIR is set, write the newly locked recipe there.
    Best effort: a failed export never fails the lock."""
    from scraper0.config import get_settings
    directory = get_settings().recipe_export_dir
    if not directory:
        return
    try:
        root = Path(directory)
        doc = export_action(site_id, name)
        changed = _write_if_changed(_path(root, doc), dumps(doc))
        write_index(root)
        log.info("recipe_exported", site_id=site_id, action=name, version=doc["version"], changed=changed)
    except Exception as e:  # noqa: BLE001
        log.warning("recipe_export_failed", site_id=site_id, action=name, error=str(e)[:300])


# ------------------------------------------------------------------ import

class RecipeError(Exception):
    pass


def validate_document(doc) -> dict:
    """→ the document, with its plans validated. Raises RecipeError naming what is wrong."""
    from scraper0.connectors.base import get as get_connector
    from scraper0.core import plans
    if not isinstance(doc, dict) or doc.get("format") != FORMAT:
        raise RecipeError(f"not a {FORMAT} document")
    site, action, impl = doc.get("site"), doc.get("action"), doc.get("implementation")
    if not isinstance(site, dict) or not isinstance(action, dict) or not isinstance(impl, dict):
        raise RecipeError("site, action and implementation are required")
    try:
        site_id, base_url, _ = sites_svc.parse_base_url(site.get("base_url"))
    except SiteError as e:
        raise RecipeError(f"site.base_url: {e.message}") from None
    if site.get("site_id") != site_id:
        raise RecipeError(f"site_id {site.get('site_id')!r} does not match base_url ({site_id!r})")
    name = action.get("name")
    if not isinstance(name, str) or not sites_svc.ACTION_NAME.fullmatch(name):
        raise RecipeError("action.name must be snake_case")
    if action.get("kind") not in ("read", "write") or action.get("mode") not in ("list", "lookup", "write"):
        raise RecipeError("action.kind / action.mode are invalid")
    kind = impl.get("implementation")
    if kind == "code":
        conn = get_connector(str(impl.get("connector") or ""))
        if conn is None or impl.get("action") not in conn.actions:
            raise RecipeError(f"unknown connector action {impl.get('connector')}/{impl.get('action')}")
    elif kind == "learned":
        entries = impl.get("plans")
        if not isinstance(entries, list) or not entries:
            raise RecipeError("a learned recipe needs at least one plan")
        for i, entry in enumerate(entries):
            if not isinstance(entry, dict) or not isinstance(entry.get("seed"), str):
                raise RecipeError(f"plans[{i}] needs a seed URL and a plan")
            try:
                plans.validate(entry.get("plan"))
            except plans.PlanError as e:
                raise RecipeError(f"plans[{i}]: {e}") from None
        if scrub(impl) != impl:
            raise RecipeError("a plan carries a credential header (authorization, cookie, …)")
    else:
        raise RecipeError("implementation.implementation must be learned or code")
    return doc


def import_document(doc: dict, lock: bool = False, locked_by: str = "import") -> str:
    """Bring one recipe into the database → "created" | "updated" | "unchanged".

    - no such action yet: the site and action are created ("created");
    - the action's locked version already does the same thing: nothing changes ("unchanged");
    - otherwise the recipe is added as the next version ("updated").
    A new version is a draft unless `lock` is set (then it becomes the locked one)."""
    validate_document(doc)
    site, action, impl = doc["site"], doc["action"], doc["implementation"]
    site_id, base_url, host = sites_svc.parse_base_url(site["base_url"])
    with session_scope() as s:
        if s.get(Site, site_id) is None:
            s.add(Site(site_id=site_id, base_url=base_url, name=site.get("name") or host))
            s.flush()
        a = s.scalar(select(SiteAction).where(SiteAction.site_id == site_id, SiteAction.name == action["name"]))
        outcome = "updated"
        if a is None:
            a = SiteAction(site_id=site_id, name=action["name"], status="draft")
            s.add(a)
            outcome = "created"
        elif a.locked_version is not None:
            cur = s.scalar(select(ActionVersion).where(ActionVersion.action_id == a.id,
                                                       ActionVersion.version == a.locked_version))
            if cur is not None and content_hash(document(s.get(Site, site_id), a, cur)) == content_hash(doc):
                return "unchanged"
        a.kind, a.mode, a.entity = action["kind"], action["mode"], action.get("entity") or "event"
        a.implementation = impl["implementation"]
        a.connector = impl.get("connector") if impl["implementation"] == "code" else None
        a.connector_action = impl.get("action") if impl["implementation"] == "code" else None
        a.seed_urls = list(action.get("seed_urls") or [base_url])
        a.input_schema, a.output_schema = action.get("input_schema") or {}, action.get("output_schema") or {}
        s.flush()
        last = s.scalar(select(ActionVersion.version).where(ActionVersion.action_id == a.id)
                        .order_by(ActionVersion.version.desc()).limit(1)) or 0
        v = ActionVersion(action_id=a.id, version=last + 1, status="draft", implementation_snapshot=impl,
                          preview={"items": [], "count": 0, "checked_at": None,
                                   "imported": {"version": doc.get("version"), "locked_by": doc.get("locked_by")}})
        s.add(v)
        if lock:
            for old in s.scalars(select(ActionVersion).where(ActionVersion.action_id == a.id,
                                                             ActionVersion.status == "locked")):
                old.status = "retired"
            v.status, v.locked_at, v.locked_by = "locked", now(), locked_by
            a.locked_version, a.status, a.consecutive_failures = last + 1, "locked", 0
            a.last_scraped_at, a.last_item_count = None, None
        return outcome


def load_documents(path: str | Path) -> list[tuple[str, dict | None, str | None]]:
    """→ [(label, document | None, read error | None)] from a directory of recipe files or a bundle file."""
    p = Path(path)
    if p.is_dir():
        out = []
        for f in recipe_files(p):
            try:
                out.append((f.relative_to(p).as_posix(), json.loads(f.read_text(encoding="utf-8")), None))
            except ValueError as e:
                out.append((f.relative_to(p).as_posix(), None, f"invalid JSON: {e}"))
        return out
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, ValueError) as e:
        raise RecipeError(f"cannot read {p}: {e}") from None
    if isinstance(data, dict) and data.get("format") == BUNDLE_FORMAT and isinstance(data.get("recipes"), list):
        return [(f"recipes[{i}]", d, None) for i, d in enumerate(data["recipes"])]
    return [(p.name, data, None)]


def import_path(path: str | Path, lock: bool = False, locked_by: str = "import") -> dict:
    """Import every recipe under `path`. One bad file never stops the others."""
    report = {"created": 0, "updated": 0, "unchanged": 0, "errors": []}
    for label, doc, err in load_documents(path):
        try:
            if err:
                raise RecipeError(err)
            report[import_document(doc, lock=lock, locked_by=locked_by)] += 1
        except RecipeError as e:
            report["errors"].append({"file": label, "error": str(e)})
    return report
