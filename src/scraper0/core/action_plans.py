"""Action plans: a lookup or a write on a site, as data (spec/recipes.md, "Action plans").

A list plan (core/plans.py) fetches items over HTTP. An action plan drives a browser the way a person would —
open a page, log in (reusing the stored session), solve the login captcha, fill a form, submit, read the
acknowledgement — and is executed by one generic runner, so a site's "track complaint" or "report complaint" is
onboarded as data instead of a hand-written connector.

    {"plan_version": 1, "kind": "action", "mode": "write",          # lookup | write
     "inputs": {"category": {"type": "string", "required": true}, ...},
     "auth": {"required": true, "fields": ["username", "password"]},
     "block": ["**polyfill.io**"],
     "steps": [
       {"id": "open", "type": "browser", "do": "goto", "url": "https://site/home"},
       {"id": "session", "type": "session", "key": "{auth.username}",
        "logged_in": {"visible": {"text": "Welcome"}, "unless": {"visible": {"text": "Login"}}},
        "login": [... browser steps ..., {"type": "captcha", "uses_ai": true, ...}]},
       {"id": "desc", "type": "browser", "do": "fill", "target": [{"label": "Description"}, {"css": "textarea"}],
        "value": "{input.description}"},
       {"id": "submit", "type": "browser", "do": "click", "target": {"role": "button", "name": "Submit"},
        "commit": true},
       {"id": "ack", "type": "extract", "target": {"css": "p.ack"},
        "fields": {"id": {"regex": "Complaint ID\\\\s*(\\\\d+)", "required": true}}}],
     "output": {"tracking_id": {"from": "vars.ack.id", "type": "int"}},
     "verify": {"action": "track_issue", "input": {"tracking_id": "{output.tracking_id}"}}}

Rules the runner enforces:
- Templates are plain substitution (plans.render): {input.*}, {auth.*}, {vars.*}, {output.*}, {today}.
- Targets are ordered alternatives (label, role + name, text, placeholder, css); the first that matches wins and
  the run records which one. `any_of` does the same for whole steps (click the button, else press Enter).
- AI can run only inside a `captcha` step ("uses_ai": true), after the non-AI solver; calls are counted per step.
- `commit: true` marks the point of no return of a write. Exactly one step carries it. A failure before it —
  including not finding the control to click — means nothing was submitted; once the step has started, the outcome
  is unknown and the runner never retries it.
- `auth` values never reach logs, progress messages, run stats or error text.
"""
from __future__ import annotations

import json
import os
import re
import tempfile
import time
from dataclasses import dataclass, field
from typing import Callable
from urllib.parse import urljoin, urlsplit

import structlog

from scraper0.core.plans import PLAN_VERSION, MissingValue, PlanError, lookup_path, render, today_ist

log = structlog.get_logger(__name__)

KIND = "action"
MODES = {"lookup", "write"}
STEP_TYPES = {"browser", "session", "captcha", "extract", "expect", "any_of"}
AI_STEP_TYPES = {"captcha"}
BROWSER_DO = {"goto", "click", "fill", "select", "upload", "press", "check", "wait_for", "wait"}
LOCATOR_KEYS = ("label", "role", "text", "placeholder", "css", "test_id")
INPUT_TYPES = {"string", "number", "integer", "boolean"}

DEFAULT_TIMEOUT_MS = 60_000
MAX_TIMEOUT_MS = 120_000
MAX_WAIT_MS = 15_000
MAX_STEPS = 80
MAX_CAPTCHA_ATTEMPTS = 5
MAX_STEP_ATTEMPTS = 3
POLL_MS = 150
UPLOAD_EXTS = (".jpg", ".jpeg", ".png", ".pdf", ".webp", ".gif")


def _worded(message: str) -> PlanError:
    """A failure in the plan's own words (a `message` it supplied): never replaced by an outer step's message."""
    e = PlanError(str(message))
    e._worded = True
    return e


class ActionError(PlanError):
    """A run failed. `committed` says whether the commit step had started (the write may have gone through)."""

    def __init__(self, message: str, committed: bool = False, stats: dict | None = None):
        super().__init__(message)
        self.committed, self.stats = committed, stats or {}


# ------------------------------------------------------------------ validation

def is_action_plan(plan) -> bool:
    return isinstance(plan, dict) and plan.get("kind") == KIND


def uses_ai(plan: dict) -> bool:
    return any(st.get("type") in AI_STEP_TYPES for st in _walk(plan.get("steps") or []))


def _walk(steps):
    for st in steps:
        if not isinstance(st, dict):
            continue
        yield st
        for key in ("login", "submit"):
            yield from _walk(st.get(key) or [])
        for alt in st.get("alternatives") or []:
            yield from _walk(alt if isinstance(alt, list) else [alt])


def validate(plan: dict) -> dict:
    if not is_action_plan(plan):
        raise PlanError('an action plan is a JSON object with "kind": "action"')
    if plan.get("plan_version") != PLAN_VERSION:
        raise PlanError(f"unsupported plan_version {plan.get('plan_version')!r} (this runner knows {PLAN_VERSION})")
    if plan.get("mode") not in MODES:
        raise PlanError("mode must be lookup or write")
    inputs = plan.get("inputs", {})
    if not isinstance(inputs, dict):
        raise PlanError("inputs must be an object: name → {type, required, …}")
    for name, spec in inputs.items():
        if not isinstance(spec, dict) or spec.get("type", "string") not in INPUT_TYPES:
            raise PlanError(f"input {name}: type must be one of {', '.join(sorted(INPUT_TYPES))}")
        if "pattern" in spec:
            try:
                re.compile(spec["pattern"])
            except re.error as e:
                raise PlanError(f"input {name}: bad pattern ({e})") from e
    auth = plan.get("auth")
    if auth is not None and not (isinstance(auth, dict) and isinstance(auth.get("fields", []), list)):
        raise PlanError("auth must be {required, fields: [...]}")
    steps = plan.get("steps")
    if not isinstance(steps, list) or not steps:
        raise PlanError("a plan needs at least one step")
    if sum(1 for _ in _walk(steps)) > MAX_STEPS:
        raise PlanError(f"a plan has at most {MAX_STEPS} steps")
    ids: set[str] = set()
    for i, st in enumerate(steps):
        _validate_step(st, str(i), ids, top=True)
    commits = [st for st in steps if isinstance(st, dict) and st.get("commit")]
    if plan["mode"] == "write" and len(commits) != 1:
        raise PlanError("a write plan needs exactly one top-level step with \"commit\": true (the point of no return)")
    if plan["mode"] == "lookup" and commits:
        raise PlanError("a lookup plan can't have a commit step")
    if sum(1 for st in steps if st.get("type") == "session") > 1:
        raise PlanError("a plan has at most one session step")
    out = plan.get("output")
    if not isinstance(out, dict) or not out:
        raise PlanError("output must map each output field to where its value comes from")
    for name, spec in out.items():
        _validate_output(name, spec)
    ver = plan.get("verify")
    if ver is not None and not (isinstance(ver, dict) and isinstance(ver.get("action"), str)
                                and isinstance(ver.get("input", {}), dict)):
        raise PlanError("verify must be {action, input}")
    for pat in plan.get("block") or []:
        if not isinstance(pat, str):
            raise PlanError("block must be a list of URL globs")
    return plan


def _validate_output(name: str, spec) -> None:
    if not isinstance(spec, dict):
        raise PlanError(f"output {name}: must be an object")
    if "object" in spec:
        if not isinstance(spec["object"], dict):
            raise PlanError(f"output {name}: object must be an object")
        for k, v in spec["object"].items():
            _validate_output(f"{name}.{k}", v)
        return
    if not any(k in spec for k in ("from", "first_of", "template", "value")):
        raise PlanError(f"output {name}: needs from, first_of, template, value or object")
    if spec.get("type", "str") not in ("str", "int", "float", "bool"):
        raise PlanError(f"output {name}: type must be str, int, float or bool")


def _validate_target(spec, where: str) -> None:
    alts = spec.get("any") if isinstance(spec, dict) and "any" in spec else spec if isinstance(spec, list) else [spec]
    if not isinstance(alts, list) or not alts:
        raise PlanError(f"{where}: a target is one locator, a list of alternatives, or {{any: [...], in: [...]}}")
    for a in alts:
        if not isinstance(a, dict) or sum(1 for k in LOCATOR_KEYS if k in a) != 1:
            raise PlanError(f"{where}: each target alternative needs exactly one of {', '.join(LOCATOR_KEYS)}")
    if isinstance(spec, dict) and "in" in spec:
        _validate_scope(spec["in"], where)


def _validate_scope(scope, where: str) -> None:
    if not isinstance(scope, list) or not all(
            isinstance(s, dict) and sum(1 for k in ("frame", "frame_url", "css") if k in s) == 1 for s in scope):
        raise PlanError(f"{where}: in must be a list of {{frame: css}} | {{frame_url: text}} | {{css, has?}}")


def _validate_cond(c, where: str) -> None:
    if not isinstance(c, dict):
        raise PlanError(f"{where}: a condition must be an object")
    known = {"visible", "hidden", "url_contains", "text_contains", "input", "var", "not", "any", "all"}
    if not known & c.keys():
        raise PlanError(f"{where}: condition needs one of {', '.join(sorted(known))}")
    for k in ("visible", "hidden"):
        if k in c:
            _validate_target(c[k], where)
    for k in ("not", "unless"):
        if k in c:
            _validate_cond(c[k], where)
    for k in ("any", "all"):
        for x in c.get(k) or []:
            _validate_cond(x, where)


def _validate_step(st, sid: str, ids: set, top: bool = False) -> None:
    if not isinstance(st, dict):
        raise PlanError(f"step {sid} is not an object")
    sid = st.get("id") or sid
    if top:
        if not isinstance(st.get("id"), str) or not st["id"] or st["id"] in ids:
            raise PlanError(f"step {sid}: top-level steps need a unique string id")
        ids.add(st["id"])
    t = st.get("type")
    if t not in STEP_TYPES:
        raise PlanError(f"step {sid}: unknown step type {t!r} (known: {', '.join(sorted(STEP_TYPES))})")
    if st.get("uses_ai") and t not in AI_STEP_TYPES:
        raise PlanError(f"step {sid}: only {', '.join(sorted(AI_STEP_TYPES))} steps may use AI")
    if st.get("commit") and (not top or t != "browser" or st.get("retry") or st.get("optional")):
        raise PlanError(f"step {sid}: commit goes on one top-level browser step, without retry or optional")
    r = st.get("retry")
    if r is not None and not (isinstance(r, dict) and isinstance(r.get("attempts", 1), int)
                              and 1 <= r.get("attempts", 1) <= MAX_STEP_ATTEMPTS):
        raise PlanError(f"step {sid}: retry.attempts must be 1..{MAX_STEP_ATTEMPTS}")
    if "when" in st:
        _validate_cond(st["when"], f"step {sid}.when")
    if "in" in st:
        _validate_scope(st["in"], f"step {sid}")
    if t == "browser":
        do = st.get("do")
        if do not in BROWSER_DO:
            raise PlanError(f"step {sid}: do must be one of {', '.join(sorted(BROWSER_DO))}")
        if do == "goto" and not isinstance(st.get("url"), str):
            raise PlanError(f"step {sid}: goto needs a url")
        if do in ("click", "fill", "select", "upload", "check"):
            _validate_target(st.get("target"), f"step {sid}")
        if do == "fill" and "value" not in st:
            raise PlanError(f"step {sid}: fill needs a value")
        if do == "select" and not ("label" in st or "value" in st):
            raise PlanError(f"step {sid}: select needs label or value")
        if do == "upload" and not (isinstance(st.get("file"), dict) and isinstance(st["file"].get("url"), str)):
            raise PlanError(f"step {sid}: upload needs file: {{url}}")
        if do == "press" and not isinstance(st.get("key"), str):
            raise PlanError(f"step {sid}: press needs a key")
        if do == "wait_for" and not ("target" in st or "load_state" in st):
            raise PlanError(f"step {sid}: wait_for needs a target or load_state")
        if do in ("press", "wait_for") and "target" in st:
            _validate_target(st["target"], f"step {sid}")
        if do == "wait" and not isinstance(st.get("ms"), int):
            raise PlanError(f"step {sid}: wait needs ms")
    elif t == "session":
        if not top or not isinstance(st.get("key"), str) or not isinstance(st.get("login"), list) or not st["login"]:
            raise PlanError(f"step {sid}: a session is a top-level step with key and login steps")
        _validate_cond(st.get("logged_in"), f"step {sid}.logged_in")
        for k, sub in enumerate(st["login"]):
            _validate_step(sub, f"{sid}.login.{k}", ids)
    elif t == "captcha":
        if st.get("uses_ai") is not True:
            raise PlanError(f"step {sid}: captcha steps can call AI and must declare \"uses_ai\": true")
        for key in ("image", "into"):
            _validate_target(st.get(key), f"step {sid}.{key}")
        if "refresh" in st:
            _validate_target(st["refresh"], f"step {sid}.refresh")
        if not isinstance(st.get("submit"), list) or not st["submit"]:
            raise PlanError(f"step {sid}: captcha needs submit steps")
        for k, sub in enumerate(st["submit"]):
            _validate_step(sub, f"{sid}.submit.{k}", ids)
        _validate_cond(st.get("success"), f"step {sid}.success")
        if not 1 <= int(st.get("attempts", 3)) <= MAX_CAPTCHA_ATTEMPTS:
            raise PlanError(f"step {sid}: attempts must be 1..{MAX_CAPTCHA_ATTEMPTS}")
        w = st.get("watch")
        if w is not None and not (isinstance(w, dict) and isinstance(w.get("url"), str)):
            raise PlanError(f"step {sid}: watch needs a url glob")
    elif t == "extract":
        if "target" in st:
            _validate_target(st["target"], f"step {sid}")
        if "labels" in st:
            lb = st["labels"]
            if not (isinstance(lb, dict) and isinstance(lb.get("container"), str)):
                raise PlanError(f"step {sid}: labels needs a container selector")
        elif not (isinstance(st.get("fields"), dict) and st["fields"]):
            raise PlanError(f"step {sid}: extract needs fields or labels")
        for name, f in (st.get("fields") or {}).items():
            if not isinstance(f, dict) or not any(k in f for k in ("regex", "css", "json", "text")):
                raise PlanError(f"step {sid}: field {name} needs regex, css, json or text")
            if "regex" in f:
                try:
                    re.compile(f["regex"])
                except re.error as e:
                    raise PlanError(f"step {sid}: field {name}: bad regex ({e})") from e
    elif t == "expect":
        _validate_cond({k: v for k, v in st.items() if k not in ("id", "type", "message", "when", "in")}, f"step {sid}")
    elif t == "any_of":
        alts = st.get("alternatives")
        if not isinstance(alts, list) or len(alts) < 2:
            raise PlanError(f"step {sid}: any_of needs at least two alternatives")
        for k, alt in enumerate(alts):
            for j, sub in enumerate(alt if isinstance(alt, list) else [alt]):
                if isinstance(sub, dict) and (sub.get("type") == "any_of" or sub.get("commit")):
                    raise PlanError(f"step {sid}: alternatives can't nest any_of or carry commit")
                _validate_step(sub, f"{sid}.{k}.{j}", ids)


# ------------------------------------------------------------------ inputs and schemas

def check_inputs(plan: dict, input: dict | None, auth: dict | None) -> tuple[dict, dict]:
    """Validate and normalise the caller's input and auth against the plan → (input, auth). PlanError names
    every problem; nothing has touched the site yet."""
    input, errors, out = dict(input or {}), [], {}
    for name, spec in (plan.get("inputs") or {}).items():
        v = input.get(name)
        if v is None or v == "":
            if spec.get("required"):
                errors.append(f"{name}: required")
            elif "default" in spec:
                out[name] = spec["default"]
            continue
        t = spec.get("type", "string")
        if t == "string":
            if isinstance(v, (int, float)) and not isinstance(v, bool) and spec.get("coerce"):
                v = str(v)
            if not isinstance(v, str):
                errors.append(f"{name}: not a valid string")
                continue
            v = v.strip() if spec.get("strip", True) else v
            if "pattern" in spec and not re.fullmatch(spec["pattern"], v):
                errors.append(f"{name}: invalid value")
                continue
            if len(v) > int(spec.get("max_length", 5000)):
                errors.append(f"{name}: too long")
                continue
            if spec.get("format") == "url" and urlsplit(v).scheme not in ("http", "https"):
                errors.append(f"{name}: must be an http(s) URL")
                continue
        elif t in ("number", "integer"):
            if isinstance(v, str) and spec.get("coerce"):
                try:
                    v = float(v) if t == "number" else int(v)
                except ValueError:
                    pass
            if isinstance(v, bool) or not isinstance(v, (int, float)) or (t == "integer" and not isinstance(v, int)):
                errors.append(f"{name}: not a valid {t}")
                continue
        elif not isinstance(v, bool):
            errors.append(f"{name}: not a valid boolean")
            continue
        if "enum" in spec and v not in spec["enum"]:
            errors.append(f"{name}: must be one of {spec['enum']}")
            continue
        out[name] = v
    a = plan.get("auth") or {}
    auth_out = {}
    for f in a.get("fields") or []:
        v = (auth or {}).get(f)
        if isinstance(v, str) and v:
            auth_out[f] = v
        elif a.get("required"):
            errors.append(f"auth.{f}: required")
    if errors:
        raise PlanError("invalid input: " + "; ".join(errors))
    return out, auth_out


_JSON_TYPE = {"str": "string", "int": "integer", "float": "number", "bool": "boolean"}


def schemas(plan: dict) -> tuple[dict, dict]:
    """(input_schema, output_schema) of the action's v2 call, derived from the plan."""
    props, required = {}, []
    for name, spec in (plan.get("inputs") or {}).items():
        t = spec.get("type", "string")
        p: dict = {"type": [t, "number"] if t == "string" and spec.get("coerce") else t}
        for k in ("pattern", "enum", "description", "default", "format"):
            if k in spec:
                p[k] = spec[k]
        if spec.get("required"):
            required.append(name)
        else:
            p["type"] = (p["type"] if isinstance(p["type"], list) else [p["type"]]) + ["null"]
        props[name] = p
    body: dict = {"input": {"type": "object", "properties": props, "required": required}}
    req = ["input"]
    a = plan.get("auth") or {}
    if a.get("fields"):
        body["auth"] = {"type": "object", "properties": {f: {"type": "string"} for f in a["fields"]},
                        "required": list(a["fields"]) if a.get("required") else []}
        if a.get("required"):
            req.append("auth")
    if plan.get("mode") == "write":
        body["force"] = {"type": "boolean", "default": False}
        body["timeout_s"] = {"type": "number", "default": 300}
    else:
        body["timeout_s"] = {"type": "number", "default": 60, "maximum": 300}

    def out_schema(spec: dict) -> dict:
        if "object" in spec:
            return {"type": "object", "properties": {k: out_schema(v) for k, v in spec["object"].items()}}
        if "map" in spec:
            return {"enum": sorted(set(spec["map"].values()))}
        t = _JSON_TYPE[spec.get("type", "str")]
        return {"type": t if spec.get("required") else [t, "null"]}
    return ({"type": "object", "properties": body, "required": req},
            {"type": "object", "properties": {k: out_schema(v) for k, v in plan["output"].items()}})


def example_input(plan: dict) -> dict:
    """Placeholder values for the contract's curl."""
    out = {}
    for name, spec in (plan.get("inputs") or {}).items():
        t = spec.get("type", "string")
        out[name] = spec.get("example", f"<{name}>" if t == "string" else 0 if t != "boolean" else False)
    return out


# ------------------------------------------------------------------ output

def build_output(spec: dict, ctx: dict) -> dict:
    return {name: _output_value(name, s, ctx) for name, s in spec.items()
            if not (s.get("omit_if_missing") and _output_value(name, dict(s, required=False), ctx) is None)}


def _output_value(name: str, spec: dict, ctx: dict):
    if "object" in spec:
        return build_output(spec["object"], ctx)
    v = None
    if "value" in spec:
        v = spec["value"]
    elif "template" in spec:
        try:
            v = render(spec["template"], ctx)
        except MissingValue:
            v = None
    else:
        for path in ([spec["from"]] if "from" in spec else spec.get("first_of") or []):
            v = lookup_path(ctx, path)
            if v not in (None, ""):
                break
            v = None
    if isinstance(v, str):
        v = v.strip()
    if v in (None, ""):
        if spec.get("required"):
            raise PlanError(spec.get("message") or f"{name} was not found on the page")
        return None
    if "map" in spec:
        table = {str(k).lower(): val for k, val in spec["map"].items()}
        key = str(v).lower()
        if key not in table:
            raise PlanError((spec.get("unknown_message") or f"unexpected {name}: {{value}}").replace("{value}", key))
        return table[key]
    t = spec.get("type", "str")
    try:
        if t == "int":
            return int(str(v).strip())
        if t == "float":
            return float(str(v).strip())
        if t == "bool":
            return bool(v)
    except ValueError as e:
        raise PlanError(f"{name}: {v!r} is not a valid {t}") from e
    return v if not isinstance(v, (dict, list)) else v


# ------------------------------------------------------------------ helpers

def glob_re(pattern: str) -> re.Pattern:
    """A URL glob as Playwright writes them: ** = anything, * = anything but '/'."""
    out = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**", i):
            out += ".*"
            i += 2
        elif pattern[i] == "*":
            out += "[^/]*"
            i += 1
        else:
            out += re.escape(pattern[i])
            i += 1
    return re.compile(out)


def download_to_temp(url: str, allow: Callable[[str], bool] | None = None, max_bytes: int | None = None,
                     exts: tuple[str, ...] = UPLOAD_EXTS, default_ext: str = ".jpg") -> str:
    """Download `url` into a temp file for an upload step; every redirect hop passes the SSRF guard and the size
    is capped. → path (the runner deletes it)."""
    import httpx

    from scraper0.core.fetch import is_public_url
    allow = allow or is_public_url
    if max_bytes is None:
        from scraper0.config import get_settings
        max_bytes = get_settings().max_upload_mb * 1024 * 1024
    current = url
    try:
        with httpx.Client(timeout=30, follow_redirects=False) as client:
            for _ in range(6):
                if urlsplit(current).scheme not in ("http", "https") or not allow(current):
                    raise PlanError("the file URL must be a public http(s) URL")
                with client.stream("GET", current) as resp:
                    if resp.is_redirect and resp.headers.get("location"):
                        current = urljoin(current, resp.headers["location"])
                        continue
                    if resp.status_code != 200:
                        raise PlanError(f"could not download the file (HTTP {resp.status_code})")
                    ext = os.path.splitext(os.path.basename(urlsplit(current).path))[1].lower()
                    fd, path = tempfile.mkstemp(prefix="s0_upload_", suffix=ext if ext in exts else default_ext)
                    size = 0
                    try:
                        with os.fdopen(fd, "wb") as f:
                            for chunk in resp.iter_bytes(65536):
                                size += len(chunk)
                                if size > max_bytes:
                                    raise PlanError("the file is too large")
                                f.write(chunk)
                    except BaseException:
                        os.unlink(path)
                        raise
                    return path
            raise PlanError("the file URL redirected too many times")
    except httpx.HTTPError as e:
        raise PlanError(f"could not download the file: {type(e).__name__}") from e


class _CountingAI:
    """The AI handle a captcha step gets: counts every call into the step's account."""

    def __init__(self, ai, account: dict, stats: ActionStats):
        self._ai, self._acct, self._stats = ai, account, stats

    def vision(self, prompt: str, image: bytes, mime: str = "image/png") -> str:
        if self._ai is None:
            raise RuntimeError("no AI client")
        before = getattr(self._ai, "calls", 0)
        try:
            return self._ai.vision(prompt, image, mime)
        finally:
            n = max(1, getattr(self._ai, "calls", before + 1) - before)
            usage = getattr(self._ai, "last_usage", None) or {}
            tokens = int(usage.get("input_tokens", 0)) + int(usage.get("output_tokens", 0))
            self._acct["calls"] += n
            self._acct["tokens"] += tokens
            self._stats.ai_calls += n
            self._stats.ai_tokens += tokens

    def text(self, prompt: str) -> str:
        raise RuntimeError("captcha steps only read images")


# ------------------------------------------------------------------ runner

@dataclass
class ActionStats:
    outcomes: dict = field(default_factory=dict)       # step id → ok | skipped | failed
    alternatives: dict = field(default_factory=dict)   # any_of step id → index of the alternative that worked
    targets: dict = field(default_factory=dict)        # step id → index of the target alternative that matched (>0 only)
    retries: dict = field(default_factory=dict)        # step id → retries used
    captcha: dict = field(default_factory=dict)        # step id → {"attempts", "ai_calls"}
    ai_calls: int = 0
    ai_tokens: int = 0
    ai: dict = field(default_factory=dict)             # step id → {"calls", "tokens"}
    session: dict = field(default_factory=dict)        # {"reused": bool, "logged_in": bool}
    committed: bool = False
    failed_step: str | None = None
    ms: int = 0

    def as_dict(self) -> dict:
        return {"outcomes": self.outcomes, "alternatives": self.alternatives, "targets": self.targets,
                "retries": self.retries, "captcha": self.captcha, "ai_calls": self.ai_calls,
                "ai_tokens": self.ai_tokens, "ai_by_step": self.ai, "session": self.session,
                "committed": self.committed, "failed_step": self.failed_step, "ms": self.ms}


class ActionRunner:
    """Executes one validated action plan in a browser. `run(browser, allowed)` must be called on the browser
    thread (Renderer.session). `ai` is reachable only from captcha steps."""

    def __init__(self, plan: dict, input: dict | None = None, auth: dict | None = None, *, ai=None, state=None,
                 progress: Callable[[str, dict], None] | None = None, on_commit: Callable[[], None] | None = None,
                 solver: Callable | None = None, download: Callable[[str], str] | None = None,
                 timeout_ms: int | None = None, deadline_s: float | None = None):
        validate(plan)
        self.plan = plan
        self.input, self.auth = check_inputs(plan, input, auth)
        self._ai = ai if uses_ai(plan) else None
        self.state = state                      # StateStore-like (get/set/delete), encrypted at rest; None = no reuse
        self.progress = progress or (lambda message, data=None: None)
        self.on_commit = on_commit
        self._solver = solver
        self._download = download
        self._timeout = min(int(timeout_ms or plan.get("timeout_ms") or DEFAULT_TIMEOUT_MS), MAX_TIMEOUT_MS)
        self._deadline = time.monotonic() + deadline_s if deadline_s else None
        self.cancelled = False
        self.stats = ActionStats()
        self.ctx = {"input": self.input, "auth": self.auth, "vars": {}, "output": {}, "today": today_ist()}
        self._secrets = sorted({str(v) for v in self.auth.values() if len(str(v)) >= 3}, key=len, reverse=True)
        self._tmp: list[str] = []
        self._block = [glob_re(p) for p in plan.get("block") or []]
        self._session_step = next((st for st in plan["steps"] if st["type"] == "session"), None)

    # -------------------------------------------------------------- entry
    def run(self, browser, allowed: Callable[[str], bool]) -> dict:
        t0 = time.monotonic()
        self._allowed = allowed
        storage = self._load_session()
        kw = {"viewport": {"width": 1280, "height": 1600}, "locale": "en-IN", "timezone_id": "Asia/Kolkata"}
        context = browser.new_context(storage_state=storage, **kw) if storage else browser.new_context(**kw)
        self.stats.session = {"reused": False, "logged_in": False} if self._session_step else {}
        self._had_state = storage is not None
        try:
            def guard(route):
                url = route.request.url
                if any(p.search(url) for p in self._block) or not allowed(url):
                    route.abort()
                else:
                    route.continue_()
            context.route("**/*", guard)
            self.context = context
            self.page = context.new_page()
            self._steps(self.plan["steps"])
            out = build_output(self.plan["output"], self.ctx)
            self.ctx["output"] = out
            return out
        except PlanError as e:
            raise PlanError(self._scrub(str(e))) from None
        finally:
            self.stats.ms = int((time.monotonic() - t0) * 1000)
            for p in self._tmp:
                try:
                    os.unlink(p)
                except OSError:
                    pass
            try:
                context.close()
            except Exception:  # noqa: BLE001
                pass

    # -------------------------------------------------------------- step loop
    def _steps(self, steps: list[dict], prefix: str = "") -> None:
        for i, st in enumerate(steps):
            self._one(st, st.get("id") or f"{prefix}{i}")

    def _one(self, st: dict, sid: str) -> None:
        failed_before = self.stats.failed_step
        if self.cancelled or (self._deadline and time.monotonic() > self._deadline):
            raise PlanError("the run was stopped: it ran out of time")
        if "when" in st and not self._cond(st["when"], wait=False):
            self.stats.outcomes[sid] = "skipped"
            return
        if st.get("note"):
            self.progress(str(st["note"]), {"step": sid})
        if st.get("commit"):
            if "target" in st:                   # the control must be there first: a missing one is a clean failure
                try:
                    self._find(st["target"], st.get("in"), sid, st.get("state", "visible"), self._t(st))
                except PlanError as e:
                    self.stats.outcomes[sid] = "failed"
                    self.stats.failed_step = sid
                    located = PlanError(f"step {sid}: {st.get('message') or e}")
                    located._located = True
                    raise located from None
            self.stats.committed = True          # persisted by the caller before the step runs
            if self.on_commit:
                self.on_commit()
        attempts = 1 if st.get("commit") else min(int((st.get("retry") or {}).get("attempts", 1)), MAX_STEP_ATTEMPTS)
        for n in range(attempts):
            try:
                self._exec(st, sid)
                self.stats.outcomes[sid] = "ok"
                return
            except PlanError as e:
                err = e
            except Exception as e:  # noqa: BLE001 — Playwright errors: keep the first line, never the call log
                err = PlanError(self._scrub(str(e).strip().splitlines()[0][:300] if str(e).strip() else type(e).__name__))
            if n + 1 < attempts:
                self.stats.failed_step = failed_before
                self.stats.retries[sid] = self.stats.retries.get(sid, 0) + 1
                self.page.wait_for_timeout(min(int(float((st.get("retry") or {}).get("backoff_s", 1)) * 1000), MAX_WAIT_MS))
                continue
            if st.get("message") and not getattr(err, "_located", False) and not getattr(err, "_worded", False):
                err = PlanError(str(st["message"]))          # the plan's own wording for this failure
            if st.get("optional"):
                self.stats.failed_step = failed_before
                self.stats.outcomes[sid] = "skipped"
                log.info("action_step_skipped", step=sid, error=self._scrub(str(err))[:200])
                return
            self.stats.outcomes[sid] = "failed"
            self.stats.failed_step = self.stats.failed_step or sid
            if getattr(err, "_located", False):
                raise err
            located = PlanError(f"step {sid}: {err}")
            located._located = True
            raise located

    def _exec(self, st: dict, sid: str) -> None:
        t = st["type"]
        if t == "browser":
            self._browser(st, sid)
        elif t == "session":
            self._session(st, sid)
        elif t == "captcha":
            self._captcha(st, sid)
        elif t == "extract":
            self._extract(st, sid)
        elif t == "expect":
            cond = {k: v for k, v in st.items() if k not in ("id", "type", "message", "when", "in", "note")}
            if not self._cond(cond, scope=st.get("in")):
                raise _worded(st.get("message") or "the page is not in the expected state")
        elif t == "any_of":
            errors, before = [], self.stats.failed_step
            for k, alt in enumerate(st["alternatives"]):
                try:
                    self._steps(alt if isinstance(alt, list) else [alt], prefix=f"{sid}.{k}.")
                except PlanError as e:
                    errors.append(f"#{k}: {e}")
                    self.stats.failed_step = before          # an alternative failing is not the run failing
                    continue
                self.stats.alternatives[sid] = k
                return
            raise PlanError("every alternative failed: " + "; ".join(errors)[:400])

    # -------------------------------------------------------------- locating
    def _r(self, value):
        return render(value, self.ctx)

    def _t(self, st: dict) -> int:
        return min(int(st.get("timeout_ms") or self._timeout), MAX_TIMEOUT_MS)

    def _scope(self, scope: list | None):
        """page → the frame / container the step works in. None while a frame is not there yet."""
        root = self.page
        for s in scope or []:
            if "frame_url" in s:
                want = str(self._r(s["frame_url"]))
                root = next((f for f in self.page.frames if want in f.url), None)
                if root is None:
                    return None
            elif "frame" in s:
                root = root.frame_locator(self._r(s["frame"]))
            else:
                root = root.locator(self._r(s["css"]), has=root.locator(self._r(s["has"]))) if s.get("has") \
                    else root.locator(self._r(s["css"]))
        return root

    def _locator(self, root, alt: dict):
        exact = bool(alt.get("exact", False))
        if "css" in alt:
            loc = root.locator(str(self._r(alt["css"])))
        elif "label" in alt:
            loc = root.get_by_label(str(self._r(alt["label"])), exact=exact)
        elif "role" in alt:
            loc = root.get_by_role(alt["role"], name=str(self._r(alt["name"])), exact=exact) if alt.get("name") \
                else root.get_by_role(alt["role"])
        elif "text" in alt:
            loc = root.get_by_text(str(self._r(alt["text"])), exact=exact)
        elif "placeholder" in alt:
            loc = root.get_by_placeholder(str(self._r(alt["placeholder"])), exact=exact)
        else:
            loc = root.get_by_test_id(str(self._r(alt["test_id"])))
        return loc.nth(int(alt["nth"])) if "nth" in alt else loc

    @staticmethod
    def _alts(spec, scope: list | None) -> tuple[list[dict], list | None]:
        if isinstance(spec, dict) and "any" in spec:
            return spec["any"], spec.get("in", scope)
        if isinstance(spec, dict) and "in" in spec:
            return [{k: v for k, v in spec.items() if k != "in"}], spec["in"]
        return (spec if isinstance(spec, list) else [spec]), scope

    def _probe(self, spec, scope: list | None, state: str = "visible"):
        """One pass over the alternatives, no waiting → (locator, index) or (None, -1)."""
        alts, scope = self._alts(spec, scope)
        root = self._scope(scope)
        if root is None:
            return None, -1
        for k, alt in enumerate(alts):
            try:
                loc = self._locator(root, alt)
                n = loc.count()
                if n and state == "attached":
                    return loc.first, k
                for i in range(min(n, 5)):          # several matches: the first visible one
                    if loc.nth(i).is_visible():
                        return loc.nth(i), k
            except PlanError:
                raise
            except Exception:  # noqa: BLE001 — detached frame, navigation in flight: look again
                continue
        return None, -1

    def _find(self, spec, scope: list | None, sid: str | None = None, state: str = "visible", timeout_ms: int | None = None):
        deadline = time.monotonic() + (timeout_ms if timeout_ms is not None else self._timeout) / 1000
        while True:
            loc, k = self._probe(spec, scope, state)
            if loc is not None:
                if sid and k > 0:
                    self.stats.targets[sid] = k
                return loc
            if time.monotonic() >= deadline or self.cancelled:
                raise PlanError(f"no element matched {json.dumps(spec)[:160]}")
            self.page.wait_for_timeout(POLL_MS)

    # -------------------------------------------------------------- conditions
    def _cond(self, c: dict, wait: bool = True, scope: list | None = None) -> bool:
        if "not" in c:
            return not self._cond(c["not"], wait, scope)
        if "any" in c:
            return any(self._cond(x, wait, scope) for x in c["any"])
        if "all" in c:
            return all(self._cond(x, wait, scope) for x in c["all"])
        if "input" in c:
            return self.input.get(c["input"]) not in (None, "")
        if "var" in c:
            return lookup_path(self.ctx["vars"], c["var"]) not in (None, "")
        timeout = min(int(c.get("timeout_ms", 5000)), MAX_TIMEOUT_MS) if wait else 0
        deadline = time.monotonic() + timeout / 1000
        while True:
            if self._check(c, scope):
                return True
            if "unless" in c and self._cond(c["unless"], wait=False, scope=scope):
                return False
            if time.monotonic() >= deadline or self.cancelled:
                return False
            self.page.wait_for_timeout(POLL_MS)

    def _check(self, c: dict, scope: list | None) -> bool:
        if "visible" in c:
            return self._probe(c["visible"], scope)[0] is not None
        if "hidden" in c:
            return self._probe(c["hidden"], scope)[0] is None
        if "url_contains" in c:
            return str(self._r(c["url_contains"])) in self.page.url
        if "text_contains" in c:
            try:
                return str(self._r(c["text_contains"])) in self._text(self._scope(scope))
            except Exception:  # noqa: BLE001
                return False
        return False

    # -------------------------------------------------------------- browser steps
    def _browser(self, st: dict, sid: str) -> None:
        do, scope, t = st["do"], st.get("in"), self._t(st)
        state = st.get("state", "attached" if do == "upload" else "visible")
        if do == "goto":
            url = str(self._r(st["url"]))
            if urlsplit(url).scheme not in ("http", "https") or not self._allowed(url):
                raise PlanError("the plan tried to open a URL that is not public http(s)")
            self.page.goto(url, timeout=t, wait_until=st.get("wait_until", "load"))
        elif do == "click":
            loc = self._find(st["target"], scope, sid, state, t)
            self._into_view(loc)
            loc.click(timeout=t, no_wait_after=bool(st.get("no_wait_after", False)))
        elif do == "fill":
            value = self._r(st["value"])
            loc = self._find(st["target"], scope, sid, state, t)
            self._into_view(loc)
            loc.fill("" if value is None else str(value), timeout=t)
        elif do == "check":
            self._find(st["target"], scope, sid, state, t).check(timeout=t)
        elif do == "select":
            self._select(st, sid, scope, t)
        elif do == "upload":
            url = str(self._r(st["file"]["url"]))
            loc = self._find(st["target"], scope, sid, state, t)
            path = (self._download or (lambda u: download_to_temp(u, exts=tuple(st["file"].get("exts") or UPLOAD_EXTS))))(url)
            self._tmp.append(path)
            loc.set_input_files(path, timeout=t)
        elif do == "press":
            if "target" in st:
                self._find(st["target"], scope, sid, state, t).press(st["key"], timeout=t)
            else:
                self.page.keyboard.press(st["key"])
        elif do == "wait_for":
            if "load_state" in st:
                self.page.wait_for_load_state(st["load_state"], timeout=t)
            elif st.get("state") == "hidden":
                if not self._cond({"hidden": st["target"], "timeout_ms": t}, scope=scope):
                    raise PlanError(f"still visible: {json.dumps(st['target'])[:160]}")
            else:
                self._find(st["target"], scope, sid, state, t)
        elif do == "wait":
            self.page.wait_for_timeout(min(int(st["ms"]), MAX_WAIT_MS))
        if do in ("select", "upload") or st.get("dispatch"):
            self._dispatch(st, sid, scope)
        if st.get("wait_load"):
            try:
                self.page.wait_for_load_state(st["wait_load"], timeout=t)
            except Exception:  # noqa: BLE001 — pages that poll never go idle: carry on with what loaded
                pass
        if st.get("settle_ms"):
            self.page.wait_for_timeout(min(int(st["settle_ms"]), MAX_WAIT_MS))

    @staticmethod
    def _into_view(loc) -> None:
        try:
            loc.scroll_into_view_if_needed(timeout=3000)
        except Exception:  # noqa: BLE001
            pass

    def _dispatch(self, st: dict, sid: str, scope) -> None:
        events = st.get("dispatch", ["change", "input"])
        if not events or "target" not in st:
            return
        loc, _ = self._probe(st["target"], scope, "attached")
        for _ in range(min(int(st.get("dispatch_times", 1)), 3)):
            for ev in events:
                try:
                    (loc or self._find(st["target"], scope, None, "attached", 3000)).dispatch_event(ev)
                except Exception:  # noqa: BLE001
                    pass
            if st.get("dispatch_gap_ms"):
                self.page.wait_for_timeout(min(int(st["dispatch_gap_ms"]), MAX_WAIT_MS))

    def _select(self, st: dict, sid: str, scope, t: int) -> None:
        by = "label" if "label" in st else "value"
        wanted = str(self._r(st[by])).strip()
        fold = (lambda s: s.casefold()) if st.get("ignore_case", True) else (lambda s: s)
        loc = self._find(st["target"], scope, sid, "attached", t)
        deadline = time.monotonic() + t / 1000
        options: list[dict] = []
        while True:
            try:
                options = loc.locator("option").evaluate_all(
                    "os => os.map(o => ({value: o.value, text: (o.innerText || '').trim()}))")
            except Exception:  # noqa: BLE001
                options = []
            match = next((o for o in options if fold(o["text" if by == "label" else "value"].strip()) == fold(wanted)
                          and (o["value"] or by == "value")), None)
            if match is not None:
                loc.select_option(value=match["value"], timeout=t)
                return
            if time.monotonic() >= deadline or self.cancelled:
                available = [o["text"] for o in options if o["value"]][:40]
                raise PlanError(f"option {wanted!r} not found. Available: {available}")
            self.page.wait_for_timeout(300)

    # -------------------------------------------------------------- session
    def _state_key(self) -> str | None:
        if self._session_step is None or self.state is None:
            return None
        import hashlib
        try:
            key = str(self._r(self._session_step["key"]))
        except MissingValue:
            return None
        return "session:" + hashlib.sha256(key.encode()).hexdigest()[:40]

    def _load_session(self) -> dict | None:
        key = self._state_key()
        if not key:
            return None
        try:
            raw = self.state.get(key)
            data = json.loads(raw) if raw else None
            return data if isinstance(data, dict) else None
        except Exception:  # noqa: BLE001 — a corrupt stored session is the same as none
            log.warning("action_session_state_unreadable")
            return None

    def _session(self, st: dict, sid: str) -> None:
        if self._cond(st["logged_in"], scope=st.get("in")):
            self.stats.session = {"reused": self._had_state, "logged_in": False}
            return
        self.progress("Logging in", {"step": sid})
        self._steps(st["login"], prefix=f"{sid}.login.")
        if st.get("settle_ms"):
            self.page.wait_for_timeout(min(int(st["settle_ms"]), MAX_WAIT_MS))
        if not self._cond(st["logged_in"], scope=st.get("in")):
            raise _worded(st.get("message") or "login failed: the site still shows the logged-out page")
        self.stats.session = {"reused": False, "logged_in": True}
        key = self._state_key()
        if key:
            try:
                self.state.set(key, json.dumps(self.context.storage_state()))
            except Exception as e:  # noqa: BLE001 — the run itself is fine without a saved session
                log.warning("action_session_save_failed", error=type(e).__name__)

    # -------------------------------------------------------------- captcha
    def _captcha(self, st: dict, sid: str) -> None:
        scope = st.get("in")
        attempts = min(int(st.get("attempts", 3)), MAX_CAPTCHA_ATTEMPTS)
        acct = self.stats.ai.setdefault(sid, {"calls": 0, "tokens": 0})
        info = self.stats.captcha.setdefault(sid, {"attempts": 0, "ai_calls": 0})
        ai = _CountingAI(self._ai, acct, self.stats)
        solver = self._solver
        if solver is None:
            from scraper0.connectors.bbmp.captcha import solve_captcha as solver      # EasyOCR first, then the AI
        for n in range(1, attempts + 1):
            info["attempts"] = n
            self.progress(f"Captcha attempt {n}", {"step": sid, "attempt": n})
            image = self._find(st["image"], scope, None, "visible").screenshot()
            text = solver(image, ai) or ""
            info["ai_calls"] = acct["calls"]
            if not text:
                self._refresh_captcha(st, scope)
                continue
            box = self._find(st["into"], scope, None, "visible")
            self._into_view(box)
            box.fill(text)
            watch = st.get("watch")

            def submit():
                self._steps(st["submit"], prefix=f"{sid}.submit.")
            if not watch:
                submit()
            else:
                got = self._watched(watch, submit)
                if got is None:                  # the site rejected the captcha before sending anything
                    self._refresh_captcha(st, scope)
                    continue
                need = watch.get("fail_unless_contains")
                if need and need not in got:     # the captcha was right; the answer says the request was refused
                    raise _worded(watch.get("fail_message") or "the site refused the request")
            if self._cond(st["success"], scope=None):
                return
            self._refresh_captcha(st, scope)
        raise _worded(st.get("message") or f"the captcha could not be solved in {attempts} attempt(s)")

    def _refresh_captcha(self, st: dict, scope) -> None:
        if "refresh" in st:
            try:
                self._find(st["refresh"], scope, None, "visible", 5000).click()
            except Exception:  # noqa: BLE001
                pass
        self.page.wait_for_timeout(min(int(st.get("refresh_settle_ms", 700)), MAX_WAIT_MS))

    def _watched(self, watch: dict, act: Callable[[], None]) -> str | None:
        """Run `act` while reading the body of the first response to a URL matching watch.url. → body, or None
        when no such request was made in time. (The body is read through route.fetch(): a page that navigates the
        moment the response lands gives no later chance.)"""
        got = {"received": False, "body": None}

        def handle(route):
            if not self._allowed(route.request.url):
                route.abort()
                return
            response = route.fetch()
            try:
                got["body"] = response.text()
            except Exception:  # noqa: BLE001
                got["body"] = ""
            got["received"] = True
            route.fulfill(response=response)
        self.page.route(watch["url"], handle)
        try:
            act()
            waited, limit = 0, min(int(watch.get("timeout_ms", 15000)), MAX_TIMEOUT_MS)
            while not got["received"] and waited < limit:
                self.page.wait_for_timeout(200)
                waited += 200
        finally:
            try:
                self.page.unroute(watch["url"], handle)
            except Exception:  # noqa: BLE001
                pass
        return (got["body"] or "") if got["received"] else None

    # -------------------------------------------------------------- extract
    @staticmethod
    def _text(root) -> str:
        kind = type(root).__name__
        return root.first.inner_text() if kind == "Locator" else root.locator("body").inner_text()

    @staticmethod
    def _html(root) -> str:
        kind = type(root).__name__
        if kind in ("Page", "Frame"):
            return root.content()
        loc = root.first if kind == "Locator" else root.locator("html").first
        return loc.evaluate("e => e.outerHTML")

    def _extract(self, st: dict, sid: str) -> None:
        scope, t = st.get("in"), self._t(st)
        root = self._find(st["target"], scope, sid, st.get("state", "visible"), t) if "target" in st else self._scope(scope)
        if root is None:
            raise PlanError("the frame to read from is not on the page")
        out: dict = {}
        if "labels" in st:
            out = self._labels(st["labels"], self._html(root))
        text = html = None
        for name, f in (st.get("fields") or {}).items():
            v = None
            if "regex" in f:
                if f.get("from") == "html":
                    html = html if html is not None else self._html(root)
                    src = html
                else:
                    text = text if text is not None else self._text(root)
                    src = text
                m = re.search(f["regex"], src)
                v = (m.group(int(f.get("group", 1))) if m.groups() else m.group(0)) if m else None
            elif "css" in f:
                from bs4 import BeautifulSoup
                html = html if html is not None else self._html(root)
                el = BeautifulSoup(html, "html.parser").select_one(f["css"])
                if el is not None:
                    v = el.get(f["attr"]) if f.get("attr") else el.get_text(" ", strip=True)
            elif "json" in f:
                text = text if text is not None else self._text(root)
                try:
                    v = lookup_path(json.loads(text), f["json"]) if f["json"] else json.loads(text)
                except ValueError:
                    v = None
            else:
                text = text if text is not None else self._text(root)
                v = text.strip()
            if v in (None, "") and f.get("required"):
                raise _worded(f.get("message") or f"could not read {name} from the page")
            out[name] = v
        self.ctx["vars"][st.get("into") or sid] = out

    @staticmethod
    def _labels(spec: dict, html: str) -> dict:
        """A details panel as {label text: value text}: every `group` in the chosen `container` whose `label`
        element is followed by a value element."""
        from bs4 import BeautifulSoup
        boxes = BeautifulSoup(html, "html.parser").select(spec["container"])
        idx = int(spec.get("index", 0))
        if len(boxes) < max(int(spec.get("min_containers", 1)), idx + 1):
            raise _worded(spec.get("missing_message") or "the details were not found on the page")
        data = {}
        for group in boxes[idx].select(spec.get("group", "div")):
            label = group.select_one(spec.get("label", "label"))
            if label is None:
                continue
            value = label.find_next(spec.get("value_tag", "div"))
            key = label.get_text(strip=True).replace("\xa0", " ")
            if key and value is not None:
                data[key] = value.get_text(strip=True)
        return data

    # -------------------------------------------------------------- safety
    def _scrub(self, message: str) -> str:
        for secret in self._secrets:
            message = message.replace(secret, "***")
        return message


def run_action_plan(plan: dict, renderer, input: dict | None = None, auth: dict | None = None, *, ai=None, state=None,
                    progress=None, on_commit=None, solver=None, download=None, timeout_s: float = 300,
                    timeout_ms: int | None = None) -> tuple[dict, dict]:
    """Run an action plan in the renderer's browser → (output, run stats). Raises ActionError, whose `committed`
    tells a write's caller whether the outcome is "nothing was submitted" or unknown."""
    runner = ActionRunner(plan, input, auth, ai=ai, state=state, progress=progress, on_commit=on_commit,
                          solver=solver, download=download, timeout_ms=timeout_ms, deadline_s=timeout_s)
    try:
        out = renderer.session(runner.run, timeout_s=timeout_s + 15)
    except PlanError as e:
        raise ActionError(str(e), runner.stats.committed, runner.stats.as_dict()) from None
    except TimeoutError as e:
        runner.cancelled = True
        raise ActionError(str(e), runner.stats.committed, runner.stats.as_dict()) from None
    except Exception as e:  # noqa: BLE001 — browser unavailable or crashed
        raise ActionError(runner._scrub(str(e).strip().splitlines()[0][:300] if str(e).strip() else type(e).__name__),
                          runner.stats.committed, runner.stats.as_dict()) from None
    return out, runner.stats.as_dict()


def describe(plan: dict) -> list[str]:
    """One line per top-level step, for logs and the dashboard."""
    out = []
    for st in plan.get("steps") or []:
        t = st["type"]
        if t == "browser":
            line = f"{st['id']}: {st['do']}" + (f" {st['url']}" if st.get("url") else "")
            if st.get("target") is not None:
                line += f" {json.dumps(st['target'])[:70]}"
        elif t == "session":
            line = f"{st['id']}: session (reuse or log in, {len(st['login'])} login step(s))"
        elif t == "captcha":
            line = f"{st['id']}: captcha (uses AI after the non-AI solver)"
        elif t == "any_of":
            line = f"{st['id']}: any_of ({len(st['alternatives'])} alternatives)"
        else:
            line = f"{st['id']}: {t}"
        if st.get("commit"):
            line += " · COMMIT"
        out.append(line)
    return out
