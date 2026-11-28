"""Learn a plan from what a rendered page fetched (any method, any host): the search-API case.

1. Pick the captured call whose JSON best maps to the entity (heuristics; AI only if they fail, ≤ 3 calls).
2. Rebuild it as a `request` step: method, URL, body (JSON / form / text), safe headers only.
3. Replay it without the browser. If it fails, find what the browser had that the replay lacks: cookies from
   the page (→ a warm-up step) or a token from an earlier response (→ a `capture` step). Unfixable → no plan.
4. Paging: parameters named like offset/page/size are tried with changed values; only those that really page
   (new value → different items, same value → same items) become template parameters. A page-size parameter
   is raised when the API honours a larger one (fewer requests). When the same request returns a different
   subset each time (a sort with ties), a tiebreaker on the items' id is added to the sort if a replay
   confirms it makes the order stable; otherwise the list is flagged `unstable_order` and the runner sweeps
   it until every item has been seen. A body value equal to today's date becomes {today} only if changing it
   changes the results.
5. Image values that aren't URLs get a prefix template inferred from the page's <img> sources; an image
   shared by most items is noted as a placeholder.
6. Links: a template inferred from the page's anchors (a detail path wins), else a key of the item that holds
   its own URL, else URLs found in the page's scripts or by opening two items in the browser. Sampled links
   are fetched once to check they resolve.
7. Verify-before-keep: the finished plan is run from scratch (no browser, no AI) and must reproduce the items.
"""
from __future__ import annotations

import json
import re
from datetime import date, timedelta
from typing import Callable
from urllib.parse import parse_qsl, urlsplit, urlunsplit

import structlog

from scraper0.core import json_extract as jx
from scraper0.core import plans
from scraper0.core.entities import EntitySpec

log = structlog.get_logger(__name__)

_OFFSET = re.compile(r"^(offset|from|start|skip|startindex|start_index|currentrows|current_rows|first)$", re.I)
_PAGE = re.compile(r"^(page|pageno|page_no|pagenumber|page_number|pageindex|page_index|pg|p)$", re.I)
_SIZE = re.compile(r"^(limit|size|pagesize|page_size|perpage|per_page|rows|take|top|hitsperpage|hits_per_page|"
                   r"results_per_page|resultsperpage|maxresults|max_results|num|count)$", re.I)
_SORT = re.compile(r"^(sort|order|orderby|order_by|sortby|sort_by)$", re.I)
_ID_KEYS = ("id", "_id", "uuid", "uid", "key", "slug")
BIG_PAGES = (100, 50, 25)  # page sizes tried, largest first, when the API may allow more than the page shows
_TOTAL_KEYS = ("total", "total_count", "totalcount", "count", "total_results", "totalresults", "hits.total",
               "total_hits", "numfound", "num_found", "meta.total", "pagination.total")
_DATE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_KEEP_HEADERS = ("content-type", "accept", "origin", "referer", "x-requested-with", "accept-language")


class LearnFailed(Exception):
    pass


def _key(path: str) -> str:
    return re.sub(r"[^a-z0-9_]", "", path.rsplit(".", 1)[-1].lower())


def _set_path(obj, path: str, value):
    cur = obj
    segs = path.split(".")
    for s in segs[:-1]:
        cur = cur[int(s)] if isinstance(cur, list) else cur[s]
    last = segs[-1]
    if isinstance(cur, list):
        cur[int(last)] = value
    else:
        cur[last] = value


def _scalars(obj, prefix="", depth=8):
    """(path, value) for every scalar in a JSON body (lists included by index)."""
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, (dict, list)) and depth > 0:
                yield from _scalars(v, p, depth - 1)
            elif not isinstance(v, (dict, list)):
                yield p, v
    elif isinstance(obj, list) and depth > 0:
        for i, v in enumerate(obj):
            yield from _scalars(v, f"{prefix}.{i}" if prefix else str(i), depth - 1)


# ------------------------------------------------------------------ the request as a step

class Req:
    """A mutable request: method, url (without query), query dict, body kind + value, headers."""

    def __init__(self, method, url, headers, body_kind, body):
        p = urlsplit(url)
        self.method = method.upper()
        self.url = urlunsplit((p.scheme, p.netloc, p.path, "", ""))
        self.query = dict(parse_qsl(p.query, keep_blank_values=True))
        self.headers = headers
        self.body_kind, self.body = body_kind, body

    @classmethod
    def from_captured(cls, c) -> Req:
        headers = {k: v for k, v in (c.headers or {}).items() if k in _KEEP_HEADERS or
                   (k.startswith("x-") and k not in ("x-requested-with",))}
        ctype = (c.headers or {}).get("content-type", "")
        kind, body = None, None
        if c.body:
            if "json" in ctype or c.body.strip()[:1] in ("{", "["):
                try:
                    kind, body = "json", json.loads(c.body)
                except ValueError:
                    kind, body = "text", c.body
            elif "x-www-form-urlencoded" in ctype:
                kind, body = "form", dict(parse_qsl(c.body, keep_blank_values=True))
            else:
                kind, body = "text", c.body
        return cls(c.method, c.url, headers, kind, body)

    def clone(self) -> Req:
        r = Req(self.method, self.url, dict(self.headers), self.body_kind, json.loads(json.dumps(self.body)))
        r.query = dict(self.query)
        return r

    def params(self):
        """(where, path, value) of every scalar parameter: query, form or JSON body."""
        for k, v in self.query.items():
            yield "query", k, v
        if self.body_kind == "form":
            for k, v in self.body.items():
                yield "form", k, v
        elif self.body_kind == "json":
            for p, v in _scalars(self.body):
                yield "json", p, v

    def set(self, where: str, path: str, value) -> None:
        if where == "query":
            self.query[path] = value
        elif where == "form":
            self.body[path] = value
        else:
            _set_path(self.body, path, value)

    def step(self, sid: str = "list", items_path: str = "") -> dict:
        st = {"id": sid, "type": "request", "method": self.method, "url": self.url}
        if self.query:
            st["query"] = self.query
        if self.headers:
            st["headers"] = self.headers
        if self.body_kind:
            st["body"] = {self.body_kind: self.body}
        st["items"] = {"json": items_path}
        return st


def _replay(fetcher, req: Req, items_path: str, ctx: dict | None = None) -> tuple[list, object] | None:
    st = req.step(items_path=items_path)
    r = plans.Runner(fetcher)
    try:
        url, res = r._send(st, ctx or {"today": plans.today_ist()})
        data = _decode_strings(json.loads(res.text))
    except (plans.PlanError, ValueError):
        return None
    return [x for x in plans.json_items(data, items_path) if isinstance(x, dict)], data


def _sig(items: list) -> list[str]:
    return [json.dumps(x, sort_keys=True, default=str)[:300] for x in items[:10]]


def _overlap(a: list, b: list) -> float:
    """Share of items two responses have in common, ignoring order (APIs sorting on ties reorder freely)."""
    sa, sb = {json.dumps(x, sort_keys=True, default=str) for x in a}, {json.dumps(x, sort_keys=True, default=str) for x in b}
    return len(sa & sb) / max(1, len(sa | sb))                        # Jaccard


# ------------------------------------------------------------------ choose the call

def _decode_strings(data, depth: int = 2):
    """Decode JSON arrays/objects that a response carries as strings (top levels only), so they can be found."""
    if depth <= 0:
        return data
    if isinstance(data, dict):
        out = {}
        for k, v in data.items():
            v = plans._decoded(v) if isinstance(v, str) else v
            out[k] = _decode_strings(v, depth - 1) if isinstance(v, (dict, list)) else v
        return out
    return data


def _wrapped_arrays(data, max_depth: int = 4) -> list[tuple[str, list[dict]]]:
    """Lists whose elements wrap the item under one key (Elasticsearch `records[*]._source`, GraphQL-ish
    `edges[*].node`) → ("records.*._source", [inner items])."""
    out = []

    def walk(node, path, depth):
        if isinstance(node, list):
            dicts = [x for x in node if isinstance(x, dict)]
            if len(dicts) >= jx.MIN_ITEMS:
                keys = [k for k, v in dicts[0].items() if isinstance(v, dict) and len(v) >= 3]
                for k in keys:
                    inner = [x[k] for x in dicts if isinstance(x.get(k), dict)]
                    if len(inner) >= 0.8 * len(dicts):
                        out.append((f"{path}.*.{k}" if path else f"*.{k}", inner))
        elif isinstance(node, dict) and depth > 0:
            for k, v in node.items():
                if isinstance(v, (dict, list)):
                    walk(v, f"{path}.{k}" if path else str(k), depth - 1)
    walk(data, "", max_depth)
    return out


# ------------------------------------------------------------------ choose the call

MAX_AI_MAPPINGS = 3       # arrays the AI is asked to map per learn (each distinct API call at most once)


def _plausible(spec: EntitySpec, arr: list[dict]) -> bool:
    """Could these objects be the entity at all? Every required field needs some leaf of a fitting kind (a date
    for a datetime, a string with letters for a text), so analytics pings and config lists never reach the AI."""
    samples = jx._samples(arr)
    leaves = [v for vals in samples.values() for v in vals]
    for f in spec.fields:
        if not f.required:
            continue
        if f.type == "datetime":
            if not any(isinstance(v, str) and jx._HAS_DATE.search(v) and spec.coerce(f.name, v) for v in leaves) and \
                    not any(isinstance(v, (int, float)) and not isinstance(v, bool) and 10**9 < v < 10**13 for v in leaves):
                return False
        elif f.type in ("text", "address"):
            if not any(isinstance(v, str) and re.search(r"[^\W\d_]{3}", v) for v in leaves):
                return False
    return True


def _candidates(spec: EntitySpec, calls, llm, asked: set | None = None):
    asked = asked if asked is not None else set()
    best, unmapped = None, []
    for c in calls:
        if not 200 <= c.status < 300 or not c.response:
            continue
        try:
            data = _decode_strings(json.loads(c.response))
        except ValueError:
            continue
        for path, arr in jx.candidate_arrays(data) + _wrapped_arrays(data):
            mapping = jx.heuristic_mapping(spec, arr, c.url)
            cov = jx.coverage(spec, arr, mapping, c.url)
            if cov >= jx.MIN_COVERAGE:
                score = cov * min(len(arr), 200)
                if best is None or score > best[0]:
                    best = (score, c, path, arr, mapping, "heuristic")
            else:
                unmapped.append((c, path, arr))
    if best is None and llm is not None:
        for c, path, arr in sorted(unmapped, key=lambda t: -len(t[2])):
            key = (c.method, c.url.split("?")[0], path)
            if key in asked or not _plausible(spec, arr):
                continue
            if len(asked) >= MAX_AI_MAPPINGS:
                break
            asked.add(key)
            mapping = jx.llm_mapping(spec, arr, llm)
            if mapping and jx.coverage(spec, arr, mapping, c.url) >= jx.MIN_COVERAGE:
                best = (1.0, c, path, arr, mapping, "llm")
                break
    return best


# ------------------------------------------------------------------ learn

def learn_plan(spec: EntitySpec, calls, page_url: str, fetcher, hrefs: list[str] | None = None,
               img_srcs: list[str] | None = None, llm: Callable[[str], str] | None = None,
               asked: set | None = None) -> tuple[dict, list[dict], list[dict]]:
    """→ (plan, items, raw first-page objects) or raises LearnFailed(reason). `asked` remembers, across the pages
    of one learn, which API calls the AI was already asked to map."""
    best = _candidates(spec, calls, llm, asked)
    if best is None:
        raise LearnFailed("no request the page made returned a list of " + spec.name + "s")
    _, call, items_path, arr, mapping, learned_by = best
    req = Req.from_captured(call)
    pre_steps: list[dict] = []

    # 3. replay without the browser
    got = _replay(fetcher, req, items_path)
    if not got or not got[0]:
        # cookies set by the page itself?
        warm = {"id": "warmup", "type": "request", "method": "GET", "url": page_url}
        try:
            plans.Runner(fetcher)._send(warm, {})
        except plans.PlanError:
            pass
        got = _replay(fetcher, req, items_path)
        if got and got[0]:
            pre_steps.append(warm)
        else:
            cap = _find_capture(call, calls, req, fetcher, items_path)
            if cap is None:
                hint = f" (the browser also sent {', '.join(call.secret_headers)})" if call.secret_headers else ""
                raise LearnFailed(f"replaying {call.method} {call.url} without the browser fails{hint}")
            pre_steps.append(cap["step"])
            req = cap["req"]
            got = _replay(fetcher, req, items_path, cap["ctx"])
    first, data = got

    # 4. paging + dates
    paginate, req = _learn_paging(fetcher, req, items_path, first, data)
    pctx = {"today": plans.today_ist(), "captures": {}}
    if paginate:
        start = paginate["start"]
        pctx["page"] = {"value": str(start) if paginate.get("type") == "str" else start}
    req, bigger = _learn_page_size(fetcher, req, items_path, first, pctx)
    if bigger and paginate:                      # paging stays, in bigger steps
        paginate["stop"]["fewer_than"] = bigger
        if paginate["mode"] == "offset":
            paginate["step"] = bigger
    if paginate and paginate.get("unstable_order"):
        fixed = _stabilise_order(fetcher, req, items_path, first, pctx)
        if fixed is not None:
            req = fixed
            del paginate["unstable_order"]
    req = _learn_dates(fetcher, req, items_path, first, pctx)

    # 5. image prefix: bare file names ("abc.jpg") need the CDN path the page's <img> tags show
    img_field = mapping.get("image")
    members = [img_field] if isinstance(img_field, str) else list((img_field or {}).get("first_of") or []) \
        if isinstance(img_field, dict) else []
    fixed = []
    for m in members:
        vals = [v for v in (jx.get_path(x, m) for x in arr[:20]) if isinstance(v, str) and v] if isinstance(m, str) else []
        if vals and not any(v.startswith(("http://", "https://", "//", "/")) for v in vals):
            tmpl = _infer_prefix(arr, m, img_srcs or [])
            if tmpl:
                fixed.append({"template": tmpl})
            continue                       # no prefix known: the bare name would resolve against the API's URL
        fixed.append(m)
    if members and fixed != members:
        if not fixed:
            mapping.pop("image", None)
        else:
            mapping["image"] = fixed[0] if len(fixed) == 1 else {"first_of": fixed}

    st = req.step(items_path=items_path)
    if paginate:
        st["paginate"] = paginate
    st["expect"] = {"min_items": 1}
    lookups = []
    gets = [(c.url, c.response) for c in calls if c.method == "GET" and c.response and c is not call]
    for fname, lk in jx.find_lookups(spec, arr, mapping, call.url, gets).items():
        lookups.append({"id": f"lookup_{fname}", "type": "lookup", "request": {"method": "GET", "url": lk["api_url"]},
                        "items": {"json": lk.get("items_path") or ""}, "field": fname, "key": lk["key"],
                        "id_key": lk["id"], "value": lk["value"]})
    plan = {"plan_version": plans.PLAN_VERSION, "entity": spec.name, "steps": pre_steps + [st] + lookups,
            "output": "list", "mapping": mapping, "link_base": page_url, "learned_by": learned_by}
    link = jx.infer_link_template(arr, hrefs or [])
    if link is None or jx.link_in_query_only(link):
        own = jx.item_link_field(arr, page_url)            # the item carries its own URL: the most direct source
        if own:
            link = "{" + own + "}"
    if link:
        plan["link_template"] = link
    img_name = next((f.name for f in spec.fields if f.type == "image"), None)
    if img_name:
        imgs = [jx.apply_mapping(spec, x, {img_name: mapping[img_name]}, page_url).get(img_name) if img_name in mapping
                else None for x in arr]
        stand_ins = jx.placeholder_images(imgs)
        if stand_ins:
            plan["placeholders"] = {img_name: stand_ins}
        step = _detail_image_step(fetcher, plan, arr, imgs, stand_ins, img_name, page_url)
        if step:
            plan["steps"].append(step)
    plans.validate(plan)

    # 6. verify before keep: a cold run (new runner, no browser, no AI) must reproduce the first page's items
    items, stats = plans.run_plan(plan, fetcher, spec, page_url)
    expected = [jx.apply_mapping(spec, x, mapping, call.url) for x in first]
    expected = [e for e in expected if not spec.validate(e)]
    names = {e.get("name") for e in expected}
    if not items or (names and not names & {i.get("name") for i in items}):
        raise LearnFailed("the learned plan did not reproduce the page's items when run without the browser")
    log.info("plan_learned", url=call.url, method=call.method, items=len(items), pages=stats.pages,
             paginate=(paginate or {}).get("mode"), by=learned_by, truncated=stats.truncated or None,
             unstable=stats.paging or None)
    return plan, items, list(arr)


def _close(a, b) -> bool:
    """Two reported totals of a live list: equal, or nearly (items are added while we look)."""
    try:
        a, b = int(a), int(b)
    except (TypeError, ValueError):
        return False
    return abs(a - b) <= max(2, 0.02 * max(a, b))


def _learn_paging(fetcher, req: Req, items_path: str, first: list, data) -> tuple[dict | None, Req]:
    n = len(first)
    total_path = next((k for k in _TOTAL_KEYS if isinstance(jx.get_path(data, k), (int, str))
                       and str(jx.get_path(data, k)).isdigit() and int(jx.get_path(data, k)) > n), None)
    if total_path is None and isinstance(data, dict):
        total_path = next((k for k, v in data.items() if "total" in k.lower() and isinstance(v, int) and v > n), None)
    for where, path, value in list(req.params()):
        k = _key(path)
        is_str = isinstance(value, str)
        if isinstance(value, bool) or not str(value).lstrip("-").isdigit():
            continue
        v = int(value)
        if _OFFSET.match(k):
            mode, nxt, start, step = "offset", v + n, v, n
        elif _PAGE.match(k):
            mode, nxt, start, step = "page", v + 1, v, 1
        else:
            continue
        trial = req.clone()
        trial.set(where, path, str(nxt) if is_str else nxt)
        got = _replay(fetcher, trial, items_path)
        if not got or not got[0] or _overlap(got[0], first) > 0.5:       # the next value must give other items
            continue
        again = _replay(fetcher, req, items_path)          # the same value should give the same items…
        if not again or not again[0]:
            continue
        stable = _overlap(again[0], first) >= 0.8
        if not stable:
            # …unless the API sorts on ties and returns a different subset each time. A paging-named parameter
            # that changes the results while the reported total stays put is still paging; mark the plan so the
            # risk (items missed or repeated between pages) is visible.
            same_total = total_path is not None and _close(jx.get_path(again[1], total_path), jx.get_path(data, total_path))
            if not (same_total and len(again[0]) == n):
                continue
        templ = req.clone()
        templ.set(where, path, "{page.value}")
        pg = {"mode": mode, "start": start, "type": "str" if is_str else "int", "stop": {"fewer_than": n}}
        if not stable:
            pg["unstable_order"] = True
            log.warning("plan_paging_unstable", param=path, url=req.url)
        if mode == "offset":
            pg["step"] = step
        if total_path:
            pg["stop"]["total_path"] = total_path
        return pg, templ
    return None, req


def _learn_page_size(fetcher, req: Req, items_path: str, first: list, ctx: dict) -> tuple[Req, int | None]:
    """A page-size parameter (limit/size/rows… equal to the number of items returned) is raised when the API
    really returns more for a larger value: fewer requests per run, and fewer page boundaries to lose items at.
    → (request, new page size or None). A list that fits in one bigger page keeps its paging, for when it grows."""
    n = len(first)
    for where, path, value in list(req.params()):
        if isinstance(value, bool) or not _SIZE.match(_key(path)) or not str(value).isdigit() or int(value) != n:
            continue
        for size in BIG_PAGES:                     # APIs cap this differently (and say so with a 4xx)
            if size <= n:
                break
            trial = req.clone()
            trial.set(where, path, str(size) if isinstance(value, str) else size)
            got = _replay(fetcher, trial, items_path, ctx)
            if got and n < len(got[0]) <= size:
                log.info("plan_page_size", param=path, was=n, now=size, url=req.url)
                return trial, size
        return req, None
    return req, None


def _stabilise_order(fetcher, req: Req, items_path: str, first: list, ctx: dict) -> Req | None:
    """The same request returned different subsets: the sort has ties. If the request carries a sort list
    (`"sort": [{"date": {"order": "asc"}}]` or `["date"]`), add the items' id as a last sort key in the same
    shape — kept only if two replays of the same page then agree."""
    if req.body_kind != "json" or not isinstance(req.body, dict) or not first:
        return None
    sort_key = next((k for k, v in req.body.items() if _SORT.match(k) and isinstance(v, list) and v), None)
    if sort_key is None:
        return None
    like = req.body[sort_key][-1]
    for idk in [k for k in _ID_KEYS if k in first[0] and not isinstance(first[0][k], (dict, list))]:
        if isinstance(like, dict) and len(like) == 1:
            extra = {idk: json.loads(json.dumps(next(iter(like.values()))))}
        elif isinstance(like, str):
            extra = idk
        else:
            return None
        trial = req.clone()
        trial.body[sort_key] = list(req.body[sort_key]) + [extra]
        a, b = _replay(fetcher, trial, items_path, ctx), _replay(fetcher, trial, items_path, ctx)
        if a and b and a[0] and len(a[0]) == len(b[0]) and _overlap(a[0], b[0]) >= 0.99:
            log.info("plan_order_stabilised", sort=sort_key, tiebreaker=idk, url=req.url)
            return trial
    return None


def _learn_dates(fetcher, req: Req, items_path: str, first: list, ctx: dict) -> Req:
    today = plans.today_ist()
    for where, path, value in list(req.params()):
        if not (isinstance(value, str) and _DATE.match(value) and value == today):
            continue
        later = (date.fromisoformat(today) + timedelta(days=60)).isoformat()
        trial = req.clone()
        trial.set(where, path, later)
        got = _replay(fetcher, trial, items_path, ctx)
        if got is not None and _overlap(got[0], first) < 0.8:   # the date really filters → follow today
            out = req.clone()
            out.set(where, path, "{today}")
            req = out
    return req


_OG_IMAGE = {"sel": 'meta[property="og:image"]', "attr": "content"}


def _detail_image_step(fetcher, plan: dict, arr: list[dict], imgs: list, stand_ins: list[str], field: str,
                       page_url: str) -> dict | None:
    """Many items come without a picture (or with the site's stand-in) while their own page has one. Open two
    such items: if their share image is a real, different picture, the plan fetches the item page — only for
    items still lacking an image — and reads it from there."""
    template = plan.get("link_template")
    lacking = [x for x, im in zip(arr, imgs) if not im or im in stand_ins]
    if not template or len(lacking) < 2 or len(lacking) < 0.3 * len(arr):
        return None
    from bs4 import BeautifulSoup
    found = []
    for x in lacking[:3]:
        url = jx._link(template, x, plan.get("link_base") or page_url)
        res = fetcher.request("GET", url) if url else None
        if res is None or res.status != 200:
            continue
        og = BeautifulSoup(res.text, "html.parser").select_one('meta[property="og:image"][content]')
        if og is not None and og["content"].strip():
            found.append(og["content"].strip())
    real = [u for u in found if u not in stand_ins and not jx.placeholder_images([u])]
    if len(set(real)) < 2:                     # nothing there, a stand-in, or one picture shared by every page
        return None
    url_t = re.sub(r"\{([^{}]+)\}", r"{item.\1}", template)
    base = plan.get("link_base") or page_url
    filled = re.sub(r"\{([^{}]+)\}", lambda m: str(jx.get_path(lacking[0], m.group(1)) or ""), template)
    if not filled.startswith(("http://", "https://")):          # relative links: make the request URL absolute
        p = urlsplit(base)
        origin = f"{p.scheme}://{p.netloc}"
        url_t = origin + url_t if filled.startswith("/") else origin + p.path.rsplit("/", 1)[0] + "/" + url_t
    log.info("plan_detail_images", url=page_url, sampled=len(found))
    return {"id": "detail_image", "type": "for_each", "over": "list", "missing": [field], "optional": True, "max": 60,
            "on_failure": "skip", "request": {"method": "GET", "url": url_t},
            "extract": {"selectors": [{"rules": {field: [dict(_OG_IMAGE)]}}]}}


def _infer_prefix(items: list[dict], path: str, srcs: list[str]) -> str | None:
    by_val = {str(jx.get_path(it, path)): it for it in items if jx.get_path(it, path)}
    for src in srcs:
        hit = next((v for v in by_val if len(v) >= 4 and src.endswith(v)), None)
        if hit is None:
            continue
        tmpl = src[: len(src) - len(hit)] + "{" + path + "}"
        if any(v != hit and (tmpl.replace("{" + path + "}", v) in srcs) for v in by_val) or len(by_val) == 1:
            return tmpl
        return tmpl                      # one matching <img> is enough evidence for a CDN prefix
    return None


def _find_capture(call, calls, req: Req, fetcher, items_path: str) -> dict | None:
    """The browser sent a credential-like header the replay lacks. If an earlier response the page fetched
    contains that value, capture it from there at run time."""
    raw_headers = getattr(call, "raw_secret_values", None) or {}
    for name, value in raw_headers.items():
        token = value.split(" ", 1)[1] if value.lower().startswith("bearer ") else value
        for c in calls:
            if c is call or not c.response or token not in c.response:
                continue
            step = {"id": "token", "type": "request", "method": c.method, "url": c.url}
            cap = None
            try:
                data = json.loads(c.response)
                path = next((p for p, v in _scalars(data) if v == token), None)
                if path:
                    cap = {"name": "token", "from": "json", "path": path}
            except ValueError:
                pass
            if cap is None:
                i = c.response.index(token)
                before = re.escape(c.response[max(0, i - 24):i])
                cap = {"name": "token", "from": "regex", "pattern": before + r"([A-Za-z0-9._\-]+)"}
            step["capture"] = [cap]
            r2 = req.clone()
            r2.headers[name] = ("Bearer {captures.token}" if value.lower().startswith("bearer ") else "{captures.token}")
            ctx = {"today": plans.today_ist(), "captures": {}}
            try:
                runner = plans.Runner(fetcher)
                _, res = runner._send(step, ctx)
                runner._capture(step, res, ctx)
            except plans.PlanError:
                continue
            got = _replay(fetcher, r2, items_path, ctx)
            if got and got[0]:
                return {"step": step, "req": r2, "ctx": ctx}
    return None
