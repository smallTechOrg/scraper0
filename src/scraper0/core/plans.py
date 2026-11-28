"""Recipe plans: how to get a site's items, as data (spec/recipes.md).

A plan is a short list of steps a generic runner executes. Learning a site (AI allowed) produces a plan;
running a locked plan never calls the AI and never opens a browser (this phase has no browser steps), so a
site that has been onboarded once is scraped again at zero tokens.

    {"plan_version": 1, "entity": "event",
     "steps": [{"id": "list", "type": "request", "method": "POST", "url": "...", "headers": {...},
                "body": {"json": {... "page": "{page.value}" ...}},
                "paginate": {"mode": "page", "start": 1, "max_pages": 20, "stop": {"total_path": "total"}},
                "items": {"json": "records.*._source"}},
               {"id": "orgs", "type": "lookup", "request": {...}, "items": {"json": "data"},
                "field": "organisation", "key": "changemaker_id", "id_key": "id", "value": "name"},
               {"id": "detail", "type": "for_each", "over": "list", "max": 60, "optional": true,
                "request": {"url": "{item.url}"}, "extract": {"selectors": [...], "structured": true}}],
     "output": "list",                       # whose items are the result (default: the first items step)
     "mapping": {"name": "event_name", "start_time": ["date", "time"], "image": {"template": ".../{img}"}},
     "link_template": "https://site/events/{slug}", "link_base": "https://site/"}

Templates are plain substitution only — "{a.b}" looks up a dotted path in the context (seed, today, from,
input, page, captures, item); there is no expression language, so a stored plan can't run code. A string that
is exactly one placeholder keeps the value's type (numbers stay numbers in JSON bodies).
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime
from typing import Callable
from urllib.parse import urlencode, urljoin, urlsplit

import structlog

from scraper0.core.dates import IST
from scraper0.core.entities import EntitySpec

log = structlog.get_logger(__name__)

PLAN_VERSION = 1
# Minimum AI: AI may run only inside steps that declare it ("uses_ai": true) — `captcha` (non-AI solver first,
# AI fallback) and `ai` (fills fields the non-AI steps left empty). Every other step never receives an AI handle,
# and a plan with no such step runs at zero tokens. Calls and tokens are counted per step (RunStats.ai).
AI_STEP_TYPES = {"ai", "captcha"}
STEP_TYPES = {"request", "for_each", "lookup", "any_of"} | AI_STEP_TYPES
# Browser automation, logins and writes are action plans ("kind": "action", core/action_plans.py), not list
# plans. Recognised here so the error points there instead of saying "unknown".
ACTION_STEP_TYPES = {"browser", "session", "extract", "expect"}
RESERVED_STEP_TYPES = {"code"}
# Failure handling, valid on any step: retry (transient failures only), expect (post-checks), on_failure
# (fail | skip | {"goto": step}), when (declarative condition), fallback_only (runs only when jumped to).
MAX_RETRY_ATTEMPTS = 5
MAX_BACKOFF_S = 30.0
DEFAULT_RETRY = {"attempts": 2, "backoff_s": 1.0}
WHEN_OPS = {"exists", "missing", "equals", "not_equals", "in"}
PAGINATE_MODES = {"offset", "page", "cursor", "next_link"}

MAX_REQUESTS = 250        # per plan run
MAX_PAGES = 25            # per paginated step
MAX_ITEMS = 2000          # per step
MAX_FOR_EACH = 120        # detail requests per for_each step
_PLACEHOLDER = re.compile(r"\{([A-Za-z_][\w.\-]*)\}")


class PlanError(Exception):
    """The plan is invalid, or a run could not complete."""


# ------------------------------------------------------------------ validation

def validate(plan: dict) -> dict:
    if not isinstance(plan, dict):
        raise PlanError("a plan is a JSON object")
    if plan.get("kind") == "action":
        from scraper0.core import action_plans
        return action_plans.validate(plan)
    if plan.get("plan_version") != PLAN_VERSION:
        raise PlanError(f"unsupported plan_version {plan.get('plan_version')!r} (this runner knows {PLAN_VERSION})")
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        raise PlanError("a plan needs at least one step")
    ids: list[str] = []
    gotos: list[tuple[str, str]] = []
    for i, st in enumerate(steps):
        _validate_step(st, i, ids, gotos)
    for sid, target in gotos:
        if target not in ids or ids.index(target) <= ids.index(sid):
            raise PlanError(f"step {sid}: on_failure.goto must name a later step")
    if plan.get("output") is not None and plan["output"] not in ids:
        raise PlanError(f"output {plan['output']!r} is not a step id")
    if not isinstance(plan.get("mapping", {}), dict):
        raise PlanError("mapping must be an object")
    entry = plan.get("entry")
    if entry is not None:
        if not isinstance(entry, dict) or not isinstance(entry.get("direct"), str):
            raise PlanError("entry needs a direct URL")
        for hop in entry.get("navigate") or []:
            link = (hop or {}).get("link") or {}
            if not (link.get("text") or link.get("href_contains")):
                raise PlanError("each navigate hop needs link.text or link.href_contains alternatives")
    return plan


def _validate_step(st, i, ids: list[str], gotos: list, alt_of: str | None = None) -> None:
    if not isinstance(st, dict):
        raise PlanError(f"step {i} is not an object")
    t = st.get("type")
    sid = st.get("id") or (f"{alt_of}.{i}" if alt_of else None)
    if t in ACTION_STEP_TYPES:
        raise PlanError(f"step {sid or i}: {t!r} steps belong in an action plan (\"kind\": \"action\"), not a list plan")
    if t in RESERVED_STEP_TYPES:
        raise PlanError(f"step {sid or i}: step type {t!r} is reserved for a later version and not supported yet")
    if t not in STEP_TYPES:
        raise PlanError(f"step {sid or i}: unknown step type {t!r} (known: {', '.join(sorted(STEP_TYPES))})")
    if not alt_of:
        if not isinstance(sid, str) or not sid or sid in ids:
            raise PlanError(f"step {i}: needs a unique string id")
        ids.append(sid)
    _validate_common(st, sid, gotos)
    if t == "any_of":
        alts = st.get("alternatives")
        if not isinstance(alts, list) or len(alts) < 2:
            raise PlanError(f"step {sid}: any_of needs at least two alternatives")
        for k, alt in enumerate(alts):
            if isinstance(alt, dict) and alt.get("type") == "any_of":
                raise PlanError(f"step {sid}: any_of alternatives can't nest any_of")
            _validate_step(alt, k, ids[:-1] + [x for x in ids[-1:] if x != sid], gotos, alt_of=sid)
        return
    if t in AI_STEP_TYPES:
        if st.get("uses_ai") is not True:
            raise PlanError(f"step {sid}: {t} steps can call AI and must declare \"uses_ai\": true")
        if t == "ai" and (st.get("over") not in [x for x in ids if x != sid] or not isinstance(st.get("fields"), list)):
            raise PlanError(f"step {sid}: an ai step needs over (an earlier step) and fields (a list)")
        return
    if st.get("uses_ai"):
        raise PlanError(f"step {sid}: only {', '.join(sorted(AI_STEP_TYPES))} steps may use AI")
    req = st if t == "request" else st.get("request")
    if not isinstance(req, dict) or not isinstance(req.get("url"), str):
        raise PlanError(f"step {sid}: needs a request with a url")
    if str(req.get("method", "GET")).upper() not in ("GET", "POST"):
        raise PlanError(f"step {sid}: method must be GET or POST")
    body = req.get("body")
    if body is not None and not (isinstance(body, dict) and len(body) == 1 and next(iter(body)) in ("json", "form", "text")):
        raise PlanError(f"step {sid}: body must be {{json|form|text: ...}}")
    pg = st.get("paginate")
    if pg is not None and (not isinstance(pg, dict) or pg.get("mode") not in PAGINATE_MODES):
        raise PlanError(f"step {sid}: paginate.mode must be one of {', '.join(sorted(PAGINATE_MODES))}")
    if t == "for_each" and st.get("over") not in [x for x in ids if x != sid]:
        raise PlanError(f"step {sid}: for_each.over must name an earlier step")
    if t == "lookup" and not all(isinstance(st.get(k), str) for k in ("field", "key", "id_key", "value")):
        raise PlanError(f"step {sid}: lookup needs field, key, id_key and value")
    for c in st.get("capture") or []:
        if c.get("from") not in ("json", "regex", "header", "cookie") or not c.get("name"):
            raise PlanError(f"step {sid}: capture needs a name and from = json|regex|header|cookie")


def _validate_common(st: dict, sid: str, gotos: list) -> None:
    r = st.get("retry")
    if r is not None:
        if not isinstance(r, dict) or not isinstance(r.get("attempts", 1), int) or not 1 <= r.get("attempts", 1) <= MAX_RETRY_ATTEMPTS:
            raise PlanError(f"step {sid}: retry.attempts must be 1..{MAX_RETRY_ATTEMPTS}")
        if not 0 <= float(r.get("backoff_s", 0)) <= MAX_BACKOFF_S:
            raise PlanError(f"step {sid}: retry.backoff_s must be 0..{MAX_BACKOFF_S:g}")
    e = st.get("expect")
    if e is not None:
        if not isinstance(e, dict) or set(e) - {"min_items", "status", "coverage"}:
            raise PlanError(f"step {sid}: expect allows min_items, status, coverage")
        cov = e.get("coverage")
        if cov is not None and not (isinstance(cov, dict) and isinstance(cov.get("fields"), list)):
            raise PlanError(f"step {sid}: expect.coverage needs fields (a list) and min_ratio")
    of = st.get("on_failure")
    if of is not None:
        if isinstance(of, dict) and isinstance(of.get("goto"), str):
            gotos.append((sid, of["goto"]))
        elif of not in ("fail", "skip"):
            raise PlanError(f"step {sid}: on_failure is fail, skip or {{\"goto\": \"<later step>\"}}")
    w = st.get("when")
    if w is not None:
        if not isinstance(w, dict) or not ("captured" in w or "step" in w):
            raise PlanError(f"step {sid}: when needs captured (+ op/value) or step (+ outcome)")
        if "captured" in w and w.get("op", "exists") not in WHEN_OPS:
            raise PlanError(f"step {sid}: when.op must be one of {', '.join(sorted(WHEN_OPS))}")
        if "step" in w and w.get("outcome", "ok") not in ("ok", "failed", "skipped"):
            raise PlanError(f"step {sid}: when.outcome must be ok, failed or skipped")


def uses_ai(plan: dict) -> bool:
    """True when some step declares AI. Callers pass an AI client to the runner only then."""
    return any(st.get("type") in AI_STEP_TYPES for st in plan.get("steps") or [])


# ------------------------------------------------------------------ templating

def lookup_path(ctx, path: str):
    cur = ctx
    for seg in path.split("."):
        if isinstance(cur, dict):
            cur = cur.get(seg)
        elif isinstance(cur, list) and seg.isdigit():
            cur = cur[int(seg)] if int(seg) < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


class MissingValue(PlanError):
    pass


def render(value, ctx: dict):
    """Substitute {dotted.paths} from ctx in strings, recursively in dicts/lists. Nothing else is evaluated."""
    if isinstance(value, str):
        whole = _PLACEHOLDER.fullmatch(value)
        if whole:
            v = lookup_path(ctx, whole.group(1))
            if v is None:
                raise MissingValue(f"no value for {{{whole.group(1)}}}")
            return v

        def sub(m):
            v = lookup_path(ctx, m.group(1))
            if v is None:
                raise MissingValue(f"no value for {{{m.group(1)}}}")
            return str(v)
        return _PLACEHOLDER.sub(sub, value)
    if isinstance(value, dict):
        return {k: render(v, ctx) for k, v in value.items()}
    if isinstance(value, list):
        return [render(v, ctx) for v in value]
    return value


def _decoded(v):
    """A JSON array/object sent as a string inside JSON ("listing": "[{...}]") is read as what it encodes."""
    if isinstance(v, str) and v[:1] in "[{" and len(v) > 1:
        try:
            return json.loads(v)
        except ValueError:
            return v
    return v


def json_items(data, path: str | None) -> list:
    """'records.*._source' → [r['_source'] for r in data['records']]. '' / None = the root."""
    if not path:
        data = _decoded(data)
        return data if isinstance(data, list) else []
    head, star, tail = path.partition(".*")
    arr = _decoded(lookup_path(data, head) if head else data)
    if not star:
        return arr if isinstance(arr, list) else []
    if not isinstance(arr, list):
        return []
    tail = tail.lstrip(".")
    out = []
    for x in arr:
        v = lookup_path(x, tail) if tail else x
        if v is not None:
            out.append(v)
    return out


# ------------------------------------------------------------------ runner

@dataclass
class RunStats:
    requests: int = 0
    pages: int = 0
    items: int = 0
    detail_failures: int = 0
    ai_calls: int = 0
    ai_tokens: int = 0
    ai: dict = field(default_factory=dict)      # step id → {"calls", "tokens"}
    entry: str = "direct"                        # direct | navigate
    entry_changed_to: str | None = None          # the direct URL failed; navigating from the root found this
    alternatives: dict = field(default_factory=dict)   # any_of step id → index of the alternative that worked
    retries: dict = field(default_factory=dict)        # step id → retries used
    expect_failures: list = field(default_factory=list)
    outcomes: dict = field(default_factory=dict)       # step id → ok | failed | skipped | fallback
    log: list = field(default_factory=list)

    def as_dict(self) -> dict:
        return {"requests": self.requests, "pages": self.pages, "items": self.items,
                "detail_failures": self.detail_failures, "ai_calls": self.ai_calls, "ai_tokens": self.ai_tokens,
                "ai_by_step": self.ai, "entry": self.entry, "entry_changed_to": self.entry_changed_to,
                "alternatives": self.alternatives, "retries": self.retries,
                "expect_failures": self.expect_failures, "outcomes": self.outcomes}


@dataclass
class Record:
    raw: dict                       # JSON item, or {"url": ...} for a link
    fields: dict = field(default_factory=dict)    # entity fields read from HTML (selectors / structured)
    base: str = ""                  # URL to resolve relative values against


def today_ist() -> str:
    return datetime.now(IST).date().isoformat()


class Runner:
    """Executes a validated plan. Needs only a fetcher with request(method, url, headers, data) and cookie(name);
    the optional `ai` client (services.ai.AIClient-like: text_fn, calls, last_usage) is reachable only from
    AI-declared steps, so every AI call is attributable to one."""

    def __init__(self, fetcher, structured: Callable[[str, str], list[dict]] | None = None,
                 progress: Callable[[str, dict], None] | None = None, ai=None):
        self.fetcher = fetcher
        self._ai = ai                          # only ever handed to AI-declared steps (see _ai_step)
        self.structured = structured          # (html, url) → entity dicts from JSON-LD/microdata, or None
        self.progress = progress
        self.stats = RunStats()
        self._retry: dict | None = None        # the current step's retry policy
        self._expect: dict | None = None
        self._step_id = "-"

    # -------------------------------------------------------------- one request
    def _send(self, req: dict, ctx: dict):
        if self.stats.requests >= MAX_REQUESTS:
            raise PlanError(f"request cap reached ({MAX_REQUESTS})")
        url = render(req["url"], ctx)
        query = render(req.get("query") or {}, ctx)
        if query:
            url = url + ("&" if "?" in url else "?") + urlencode({k: v for k, v in query.items() if v is not None})
        headers = {k: str(v) for k, v in render(req.get("headers") or {}, ctx).items()}
        data = None
        body = req.get("body")
        if body:
            kind, val = next(iter(body.items()))
            val = render(val, ctx)
            if kind == "json":
                data = json.dumps(val)
                headers.setdefault("content-type", "application/json")
            elif kind == "form":
                data = urlencode({k: "" if v is None else v for k, v in val.items()})
                headers.setdefault("content-type", "application/x-www-form-urlencoded; charset=UTF-8")
            else:
                data = str(val)
        method = str(req.get("method", "GET")).upper()
        policy = self._retry or DEFAULT_RETRY
        attempts = min(int(policy.get("attempts", 1)), MAX_RETRY_ATTEMPTS)
        for attempt in range(attempts):
            self.stats.requests += 1
            res = self.fetcher.request(method, url, headers=headers, data=data)
            transient = res is None or res.status >= 500 or res.status == 429
            if transient and attempt + 1 < attempts:       # timeouts, connection errors, 5xx, 429 only
                self.stats.retries[self._step_id] = self.stats.retries.get(self._step_id, 0) + 1
                wait = float(policy.get("backoff_s", 1.0)) * (2 ** attempt)
                ra = (getattr(res, "headers", None) or {}).get("retry-after") if res is not None else None
                if ra and str(ra).replace(".", "", 1).isdigit():
                    wait = float(ra)
                time.sleep(min(wait, MAX_BACKOFF_S))
                continue
            break
        if res is None:
            raise PlanError(f"{method} {url}: no response (blocked or unreachable)")
        want = (self._expect or {}).get("status")
        if (res.status != int(want)) if want else not 200 <= res.status < 300:
            raise PlanError(f"{method} {url}: HTTP {res.status}")
        return url, res

    def _capture(self, step: dict, res, ctx: dict) -> None:
        for c in step.get("capture") or []:
            v = None
            if c["from"] == "json":
                try:
                    v = lookup_path(json.loads(res.text), c["path"])
                except ValueError:
                    v = None
            elif c["from"] == "regex":
                m = re.search(c["pattern"], res.text)
                v = (m.group(1) if m.groups() else m.group(0)) if m else None
            elif c["from"] == "header":
                v = (res.headers or {}).get(c["header"].lower())
            elif c["from"] == "cookie":
                v = self.fetcher.cookie(c["cookie"])
            if v in (None, "") and not c.get("optional"):
                raise PlanError(f"step {step['id']}: capture {c['name']!r} found nothing")
            ctx.setdefault("captures", {})[c["name"]] = v

    def _items(self, step: dict, url: str, res) -> tuple[list[Record], object]:
        spec_items = step.get("items")
        if not spec_items:
            return [], None
        if "json" in spec_items:
            try:
                data = json.loads(res.text)
            except ValueError as e:
                raise PlanError(f"step {step['id']}: response is not JSON") from e
            return [Record(x, base=url) for x in json_items(data, spec_items["json"]) if isinstance(x, dict)], data
        if "links" in spec_items:
            from bs4 import BeautifulSoup

            from scraper0.core.recipes import pattern_matches
            pats = spec_items["links"].get("patterns") or []
            host = urlsplit(url).netloc
            seen, out = set(), []
            for a in BeautifulSoup(res.text, "html.parser").find_all("a", href=True):
                u = urljoin(url, a["href"]).split("#")[0]
                if urlsplit(u).netloc != host or u in seen:
                    continue
                if pats and not any(pattern_matches(p, u) for p in pats):
                    continue
                seen.add(u)
                out.append(Record({"url": u}, base=u))
            return out, res.text
        raise PlanError(f"step {step['id']}: items needs json or links")

    # -------------------------------------------------------------- steps
    def _run_request(self, step: dict, ctx: dict) -> list[Record]:
        pg = step.get("paginate")
        out: list[Record] = []
        if not pg:
            url, res = self._send(step, ctx)
            self.stats.pages += 1
            self._capture(step, res, ctx)
            got, _ = self._items(step, url, res)
            return got[:MAX_ITEMS]
        mode = pg["mode"]
        max_pages = min(int(pg.get("max_pages") or MAX_PAGES), MAX_PAGES)
        stop = pg.get("stop") or {}
        value = pg.get("start", 0 if mode == "offset" else 1)
        step_by = int(pg.get("step") or 1)
        as_str = pg.get("type") == "str"
        next_url = None
        prev_keys: set | None = None
        for n in range(max_pages):
            if mode == "next_link":
                req = dict(step, url=next_url) if next_url else step
            else:
                ctx["page"] = {"value": str(value) if as_str and value is not None else value, "number": n + 1}
                req = step
            try:
                url, res = self._send(req, ctx)
            except MissingValue:
                if mode == "cursor" and n > 0:
                    break
                raise
            except PlanError:
                if n == 0:
                    raise
                log.info("plan_page_failed", step=step["id"], page=n + 1)
                break
            if n == 0:
                self._capture(step, res, ctx)
            got, data = self._items(step, url, res)
            keys = {json.dumps(r.raw, sort_keys=True, default=str)[:400] for r in got}
            if not got or (prev_keys is not None and keys == prev_keys):     # empty, or the param was ignored
                break
            self.stats.pages += 1
            prev_keys = keys
            out += got
            if self.progress:
                self.progress(f"{step['id']}: page {n + 1} → {len(got)} item(s)", {"pages": self.stats.pages})
            if len(out) >= MAX_ITEMS:
                break
            if stop.get("fewer_than") and len(got) < int(stop["fewer_than"]):
                break
            if stop.get("total_path") and isinstance(data, (dict, list)):
                total = lookup_path(data, stop["total_path"])
                if isinstance(total, (int, float, str)) and str(total).isdigit() and len(out) >= int(total):
                    break
            if mode == "offset":
                value = int(value) + (step_by if pg.get("step") else len(got))
            elif mode == "page":
                value = int(value) + 1
            elif mode == "cursor":
                cur = lookup_path(data, pg["cursor_path"]) if isinstance(data, (dict, list)) else None
                if cur in (None, ""):
                    break
                value = cur
            elif mode == "next_link":
                from scraper0.core.discovery import next_page
                next_url = next_page(res.text, url)
                if not next_url:
                    break
        return out[:MAX_ITEMS]

    def _run_for_each(self, step: dict, ctx: dict, records: list[Record]) -> None:
        cap = min(int(step.get("max") or MAX_FOR_EACH), MAX_FOR_EACH)
        ext = step.get("extract") or {}
        keep: list[Record] = []
        for rec in records[:cap]:
            ictx = dict(ctx, item=rec.raw)
            try:
                url, res = self._send(step["request"], ictx)
            except PlanError as e:
                self.stats.detail_failures += 1
                if step.get("optional"):
                    keep.append(rec)
                    continue
                log.info("plan_detail_dropped", step=step["id"], error=str(e)[:160])
                continue
            rec.base = url
            if step.get("items", {}).get("json") is not None:
                try:
                    detail = lookup_path(json.loads(res.text), step["items"]["json"]) if step["items"]["json"] \
                        else json.loads(res.text)
                except ValueError:
                    detail = None
                if isinstance(detail, dict):
                    rec.raw = {**rec.raw, **detail}
            fields = self._extract_html(ext, res.text, url)
            if fields:
                rec.fields.update({k: v for k, v in fields.items() if v not in (None, "")})
            keep.append(rec)
            if self.progress and len(keep) % 10 == 0:
                self.progress(f"{step['id']}: {len(keep)} page(s) read", {"pages": self.stats.pages})
        records[:] = keep + (records[cap:] if step.get("optional") else [])

    def _extract_html(self, ext: dict, html: str, url: str) -> dict:
        from scraper0.core.recipes import apply_recipe, pattern_matches
        out: dict = {}
        if ext.get("structured") and self.structured:
            try:
                got = self.structured(html, url)
            except Exception:  # noqa: BLE001
                got = []
            if got:
                out.update({k: v for k, v in got[0].items() if not k.startswith("_") and v not in (None, "")})
        for sel in ext.get("selectors") or []:
            if sel.get("path_pattern") and not pattern_matches(sel["path_pattern"], url):
                continue
            item, _ = apply_recipe(sel["rules"], html, url, self._spec)
            for k, v in item.items():
                out.setdefault(k, v)
        return out

    def _ai_step(self, step: dict, ctx: dict, records: list[Record], plan: dict, spec: EntitySpec, seed: str) -> None:
        """Fill `fields` that the non-AI steps left empty, from each record's page, with the AI. Records that
        already have every field cost nothing (cheapest route first)."""
        if self._ai is None:
            raise PlanError(f"step {step['id']}: declares AI but no AI client was provided")
        from scraper0.core.llm_extract import llm_extract
        wanted = list(step["fields"])
        cap = min(int(step.get("max") or 20), MAX_FOR_EACH)
        acct = self.stats.ai.setdefault(step["id"], {"calls": 0, "tokens": 0})
        llm = self._ai.text_fn("extract")

        def counted(prompt):
            before = getattr(self._ai, "calls", 0)
            text = llm(prompt)
            usage = getattr(self._ai, "last_usage", None) or {}
            n = max(1, getattr(self._ai, "calls", before + 1) - before)
            tokens = int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
            acct["calls"] += n
            acct["tokens"] += tokens
            self.stats.ai_calls += n
            self.stats.ai_tokens += tokens
            return text
        done = 0
        for rec in records:
            if done >= cap:
                break
            current = self._entities(plan, spec, [rec], {}, seed, validate_items=False)
            have = current[0] if current else {}
            if all(have.get(f) not in (None, "") for f in wanted):
                continue
            req = step.get("request") or {"method": "GET", "url": "{item.url}"}
            try:
                url, res = self._send(req, dict(ctx, item=rec.raw))
            except PlanError:
                continue
            done += 1
            got = llm_extract(res.text, url, spec, counted, require_valid=False)
            if got:
                item, _ = got[0]
                for f in wanted:
                    if have.get(f) in (None, "") and item.get(f) not in (None, ""):
                        rec.fields[f] = item[f]

    def _run_lookup(self, step: dict, ctx: dict) -> dict:
        url, res = self._send(step["request"], ctx)
        self.stats.pages += 1
        try:
            arr = json_items(json.loads(res.text), (step.get("items") or {}).get("json"))
        except ValueError:
            return {}
        return {str(o.get(step["id_key"])): o.get(step["value"]) for o in arr if isinstance(o, dict)}

    # -------------------------------------------------------------- whole plan
    def run(self, plan: dict, spec: EntitySpec, seed: str, options: dict | None = None,
            input: dict | None = None) -> list[dict]:
        validate(plan)
        self._spec, self._plan, self._seed = spec, plan, seed
        options = options or {}
        entry = plan.get("entry") or {}
        ctx: dict = {"seed": seed, "today": today_ist(), "from": str(options.get("from") or today_ist()),
                     "input": input or {}, "captures": {}, "options": options, "entry": entry.get("direct") or seed}
        uses_entry = "{entry}" in json.dumps(plan.get("steps"))
        try:
            records, tables = self._run_steps(plan, ctx)
            failed = None
        except PlanError as e:
            records, tables, failed = [], {}, e
        if not records and uses_entry and entry.get("navigate"):
            found = self._navigate(entry, ctx)
            if found and found.rstrip("/") != str(ctx["entry"]).rstrip("/"):
                log.warning("plan_entry_changed", old=ctx["entry"], new=found)
                self.stats.entry, self.stats.entry_changed_to = "navigate", found
                ctx = dict(ctx, entry=found, captures={})
                records, tables = self._run_steps(plan, ctx)
                failed = None
        if failed is not None:
            raise failed
        self.stats.items = len(records)
        return self._entities(plan, spec, records, tables, seed)

    def _run_steps(self, plan: dict, ctx: dict) -> tuple[list[Record], dict]:
        steps = plan["steps"]
        index = {st["id"]: i for i, st in enumerate(steps)}
        results: dict[str, list[Record]] = {}
        tables: dict[str, tuple[dict, dict]] = {}
        output = plan.get("output")
        jumped: dict[str, str] = {}        # fallback step id → the failed step it stands in for
        i = 0
        while i < len(steps):
            st = steps[i]
            sid = st["id"]
            i += 1
            if st.get("fallback_only") and sid not in jumped:
                self.stats.outcomes[sid] = "skipped"
                continue
            if st.get("when") and not self._when(st["when"], ctx):
                self.stats.outcomes[sid] = "skipped"
                continue
            try:
                out = self._exec_any(st, ctx, results, tables, plan)
            except PlanError as e:
                self.stats.outcomes[sid] = "failed"
                policy = st.get("on_failure") or ("skip" if st["type"] == "lookup" else "fail")
                if policy == "skip":
                    log.info("plan_step_skipped", step=sid, error=str(e)[:200])
                    continue
                if isinstance(policy, dict):
                    target = policy["goto"]
                    log.info("plan_step_fallback", step=sid, to=target, error=str(e)[:200])
                    jumped[target] = jumped.get(sid, sid)
                    i = index[target]
                    continue
                raise PlanError(f"step {sid}: {e}") from e
            self.stats.outcomes[sid] = "fallback" if sid in jumped else "ok"
            if out is not None:
                results[sid] = out
                if sid in jumped:                  # the fallback's items stand in for the failed step's
                    results[jumped[sid]] = out
                if output is None and (st.get("items") or st["type"] == "any_of"):
                    output = sid
        return results.get(output) or [], tables

    def _exec_any(self, st: dict, ctx: dict, results: dict, tables: dict, plan: dict):
        if st["type"] != "any_of":
            return self._exec(st, ctx, results, tables, plan)
        errors = []
        for k, alt in enumerate(st["alternatives"]):
            alt = dict(alt, id=alt.get("id") or f"{st['id']}.{k}")
            try:
                out = self._exec(alt, ctx, results, tables, plan, outer=st)
            except PlanError as e:
                errors.append(f"#{k}: {e}")
                continue
            self.stats.alternatives[st["id"]] = k
            return out
        raise PlanError("every alternative failed: " + "; ".join(errors)[:400])

    def _exec(self, st: dict, ctx: dict, results: dict, tables: dict, plan: dict, outer: dict | None = None):
        """One step with its retry policy and post-checks. → records for item steps, else None."""
        self._step_id = (outer or st)["id"]
        self._retry = st.get("retry") or (outer or {}).get("retry")
        self._expect = st.get("expect") or (outer or {}).get("expect")
        t, out = st["type"], None
        if t == "request":
            out = self._run_request(st, ctx)
        elif t == "for_each":
            self._run_for_each(st, ctx, results[st["over"]])
            out = results[st["over"]]
        elif t == "lookup":
            tables[st["field"]] = (st, self._run_lookup(st, ctx))
        elif t == "ai":
            self._ai_step(st, ctx, results[st["over"]], plan, self._spec, self._seed)
        elif t == "captcha":
            raise PlanError("captcha steps run in action plans (\"kind\": \"action\"), next to the browser steps they need")
        self._check(st, out)
        return out if t in ("request",) else None

    def _check(self, st: dict, records: list[Record] | None) -> None:
        exp = self._expect
        if not exp or records is None:
            return
        n = len(records)
        if exp.get("min_items") is not None and n < int(exp["min_items"]):
            return self._expect_failed(f"{n} item(s), expected at least {exp['min_items']}")
        cov = exp.get("coverage")
        if cov and n:
            ents = self._entities(self._plan, self._spec, records, {}, self._seed, validate_items=False)
            fields = cov["fields"]
            ok = sum(1 for e in ents if all(e.get(f) not in (None, "") for f in fields))
            ratio = ok / max(1, len(ents))
            if ratio < float(cov.get("min_ratio", 0.8)):
                return self._expect_failed(f"only {ratio:.0%} of items have {', '.join(fields)}")
        return None

    def _expect_failed(self, why: str):
        self.stats.expect_failures.append({"step": self._step_id, "reason": why})
        raise PlanError(f"expect failed: {why}")

    def _when(self, w: dict, ctx: dict) -> bool:
        if "step" in w:
            return self.stats.outcomes.get(w["step"], "skipped") == w.get("outcome", "ok")
        v = (ctx.get("captures") or {}).get(w["captured"])
        op = w.get("op", "exists")
        if op == "exists":
            return v not in (None, "")
        if op == "missing":
            return v in (None, "")
        if op == "equals":
            return str(v) == str(w.get("value"))
        if op == "not_equals":
            return str(v) != str(w.get("value"))
        return str(v) in [str(x) for x in (w.get("value") or [])]

    def _navigate(self, entry: dict, ctx: dict) -> str | None:
        """Follow entry.navigate from the site root: each hop picks the first link matching one of its text or
        href alternatives. → the URL reached, or None."""
        from bs4 import BeautifulSoup
        url = entry.get("root") or _root(str(entry.get("direct") or ctx.get("seed")))
        self._step_id, self._retry, self._expect = "navigate", None, None
        for hop in entry.get("navigate") or []:
            try:
                url, res = self._send({"method": "GET", "url": url}, ctx)
            except PlanError:
                return None
            link = hop.get("link") or {}
            texts = [t.lower() for t in link.get("text") or []]
            hrefs = [h.lower() for h in link.get("href_contains") or []]
            nxt = None
            anchors = BeautifulSoup(res.text, "html.parser").find_all("a", href=True)
            for want in texts:                     # alternatives in order: exact text first, then contains
                nxt = next((a["href"] for a in anchors if " ".join(a.get_text(" ").split()).lower() == want), None) \
                    or next((a["href"] for a in anchors if want in " ".join(a.get_text(" ").split()).lower()), None)
                if nxt:
                    break
            if not nxt:
                nxt = next((a["href"] for h in hrefs for a in anchors if h in a["href"].lower()), None)
            if not nxt:
                return None
            url = urljoin(url, nxt)
        return url

    def _entities(self, plan, spec, records, tables, seed, validate_items: bool = True) -> list[dict]:
        from scraper0.core.json_extract import _link, apply_mapping
        mapping = plan.get("mapping") or {}
        link_base = plan.get("link_base") or seed
        out = []
        for rec in records:
            item = apply_mapping(spec, rec.raw, mapping, rec.base or seed) if mapping else {}
            item.update(rec.fields)
            for fname, (st, table) in tables.items():
                if fname not in item:
                    v = spec.coerce(fname, table.get(str(lookup_path(rec.raw, st["key"]))), rec.base or seed)
                    if v not in (None, ""):
                        item[fname] = v
            link = _link(plan.get("link_template"), rec.raw, link_base) if plan.get("link_template") else None
            if link:
                item["link"] = link
            elif not item.get("link") and rec.raw.get("url") and len(rec.raw) == 1:
                item["link"] = rec.raw["url"]
            if validate_items and spec.validate(item):
                continue
            out.append(item)
        return out


def _root(url: str) -> str:
    p = urlsplit(url)
    return f"{p.scheme}://{p.netloc}/"


def run_plan(plan: dict, fetcher, spec: EntitySpec, seed: str, options: dict | None = None,
             structured=None, progress=None, ai=None) -> tuple[list[dict], RunStats]:
    """`ai` is used only if the plan declares an AI step; otherwise it's dropped here, so the run can't spend."""
    r = Runner(fetcher, structured=structured, progress=progress, ai=ai if uses_ai(plan) else None)
    items = r.run(plan, spec, seed, options)
    return items, r.stats


# ------------------------------------------------------------------ today's recipes as plans

def from_selectors(layouts: list[dict], seed: str, patterns: list[str], entity: str = "event",
                   navigate: list[dict] | None = None, max_list_pages: int = 5, max_detail: int = 60) -> dict:
    """A server-rendered site: the list page (and its next pages) → links matching the detail-page patterns →
    each detail page, fields from structured data first, then the CSS layout rules learned for its pattern."""
    plan = {
        "plan_version": PLAN_VERSION, "entity": entity,
        "entry": {"direct": seed, "root": _root(seed), "navigate": navigate or []},
        "steps": [
            {"id": "list", "type": "request", "method": "GET", "url": "{entry}",
             "paginate": {"mode": "next_link", "max_pages": max_list_pages},
             "items": {"links": {"patterns": list(patterns)}}, "expect": {"min_items": 1}},
            {"id": "detail", "type": "for_each", "over": "list", "max": max_detail,
             "request": {"method": "GET", "url": "{item.url}"},
             "extract": {"structured": True,
                         "selectors": [{"path_pattern": r["path_pattern"], "rules": r["rules"]} for r in layouts
                                       if r.get("path_pattern") in patterns]}},
        ],
        "output": "list"}
    if not navigate:
        del plan["entry"]["navigate"]
    return validate(plan)


def learn_navigate(fetcher, entry_url: str) -> list[dict]:
    """How a person reaches `entry_url` from the site root: the link(s) on the home page that point at it, kept as
    text and href alternatives. [] when the entry is the root or no home-page link leads there."""
    from bs4 import BeautifulSoup
    root = _root(entry_url)
    if urlsplit(entry_url).path.strip("/") == "":
        return []
    res = fetcher.request("GET", root) if hasattr(fetcher, "request") else None
    if res is None or res.status != 200:
        return []
    want = entry_url.split("#")[0].rstrip("/").split("://", 1)[-1].removeprefix("www.")
    texts, hrefs = [], []
    for a in BeautifulSoup(res.text, "html.parser").find_all("a", href=True):
        u = urljoin(root, a["href"]).split("#")[0].rstrip("/").split("://", 1)[-1].removeprefix("www.")
        if u != want:
            continue
        t = " ".join(a.get_text(" ").split())
        if t and t not in texts and len(t) <= 60:
            texts.append(t)
        path = urlsplit(entry_url).path
        if path and path not in hrefs:
            hrefs.append(path)
    if not (texts or hrefs):
        return []
    return [{"link": {"text": texts[:5], "href_contains": hrefs[:2]}}]


def describe(plan: dict) -> list[str]:
    """One line per step, for logs and the dashboard."""
    out = []
    for st in plan.get("steps") or []:
        if st["type"] in AI_STEP_TYPES:
            out.append(f"{st['id']}: {st['type']} (uses AI) · fields {st.get('fields') or '-'}")
            continue
        req = st if st["type"] == "request" else st.get("request") or {}
        line = f"{st['id']}: {st['type']} {str(req.get('method', 'GET')).upper()} {req.get('url')}"
        if st.get("paginate"):
            line += f" · paginate {st['paginate']['mode']}"
        if st.get("items"):
            line += f" · items {json.dumps(st['items'])[:80]}"
        out.append(line)
    return out
