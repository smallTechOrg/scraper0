"""Entities from JSON APIs.

Single-page apps load their data from JSON endpoints. Given the JSON responses a page fetched, find
the array that holds the entity, map its keys to the entity's fields (name heuristics first — free;
the LLM only when those are not enough), verify the mapping on the real items, and return rules that
are stored as an "API recipe": next time the endpoint is fetched directly — no browser, no LLM.

rules = {"api_url", "items_path", "mapping": {field: path | [paths]}, "link_template"?: str}
A path is dotted ("location.address", "media_urls.0"); a list of paths is joined with a space
(date + time kept in separate keys); {"first_of": [paths]} takes the first one that has a value.
"""
from __future__ import annotations

import json
import re
from typing import Callable
from urllib.parse import urljoin, urlsplit

import structlog

from scraper0.core.entities import EntitySpec, FieldSpec

log = structlog.get_logger(__name__)

MIN_ITEMS = 2
MIN_COVERAGE = 0.5        # share of items whose required fields must come out valid
SAMPLE = 40

# Likely key names per field name (most specific first). Custom fields always try their own name.
SYNONYMS: dict[str, list[str]] = {
    "name": ["name", "title", "heading", "event_name", "label"],
    "title": ["title", "name", "heading", "job_title", "position"],
    "start_time": ["start_time", "start", "start_date", "starts_at", "start_datetime", "date", "datetime", "event_date", "begin"],
    "end_time": ["end_time", "end", "end_date", "ends_at", "end_datetime", "date_end", "finish"],
    "address": ["address", "venue", "location", "place", "locality", "city"],
    "organisation": ["organisation", "organization", "organizer", "organiser", "organizer_name", "host", "company", "ngo"],
    "company": ["company", "company_name", "employer", "organisation", "organization"],
    "image": ["image", "image_url", "poster", "banner", "cover", "cover_image", "thumbnail", "media_urls", "images", "photo"],
    "description": ["description", "summary", "details", "about", "body", "content"],
    "price": ["price", "fee", "cost", "ticket_price", "amount"],
    "link": ["link", "url", "permalink", "web_url", "href"],
    "latitude": ["latitude", "lat"],
    "longitude": ["longitude", "lng", "lon", "long"],
    "is_online": ["is_online", "online", "is_virtual", "virtual"],
    "salary": ["salary", "pay", "compensation", "ctc"],
}
_TIME_KEYS = {"start": ["time", "start_time", "from_time", "starttime", "time_start"],
              "end": ["end_time", "to_time", "endtime", "time_end"]}
_NOT_TEXT_SUFFIX = ("_id", "_ids", "_url", "_link", "_website", "_logo", "_count", "_type", "_slug")
_DATE_ONLY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_TIME_ONLY = re.compile(r"^\d{1,2}:\d{2}(:\d{2})?\s*([ap]\.?m\.?)?$", re.I)
_HAS_DATE = re.compile(r"\d{4}-\d{1,2}-\d{1,2}|\d{1,2}[/.-]\d{1,2}[/.-]\d{2,4}|jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec", re.I)


# ------------------------------------------------------------------ JSON helpers

def get_path(obj, path: str):
    cur = obj
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


def flatten(obj, prefix: str = "", depth: int = 3) -> dict[str, object]:
    """Scalar leaves by dotted path. Lists contribute their first element only."""
    out: dict[str, object] = {}
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            if isinstance(v, (dict, list)):
                if depth > 0:
                    out.update(flatten(v, p, depth - 1))
            elif v is not None and v != "":
                out[p] = v
    elif isinstance(obj, list) and obj:
        first = obj[0]
        p = f"{prefix}.0"
        if isinstance(first, (dict, list)):
            if depth > 0:
                out.update(flatten(first, p, depth - 1))
        elif first is not None and first != "":
            out[p] = first
    return out


def candidate_arrays(data, max_depth: int = 4) -> list[tuple[str, list[dict]]]:
    """Every list of objects (≥ MIN_ITEMS dicts with ≥ 3 keys), with its dotted path ('' = root)."""
    found: list[tuple[str, list[dict]]] = []

    def walk(node, path, depth):
        if isinstance(node, list):
            dicts = [x for x in node if isinstance(x, dict) and len(x) >= 3]
            if len(dicts) >= MIN_ITEMS and len(dicts) >= 0.8 * len(node):
                found.append((path, dicts))
        elif isinstance(node, dict) and depth > 0:
            for k, v in node.items():
                if isinstance(v, (dict, list)):
                    walk(v, f"{path}.{k}" if path else str(k), depth - 1)
    walk(data, "", max_depth)
    return found


def _samples(items: list[dict]) -> dict[str, list]:
    """path → up to 5 non-empty sample values, across the first SAMPLE items."""
    out: dict[str, list] = {}
    for it in items[:SAMPLE]:
        for p, v in flatten(it).items():
            s = out.setdefault(p, [])
            if len(s) < 5:
                s.append(v)
    return out


def _seg(path: str) -> str:
    segs = [s for s in path.split(".") if not s.isdigit()]
    return re.sub(r"[^a-z0-9_]", "", (segs[-1] if segs else "").lower())


# ------------------------------------------------------------------ mapping

def _fits(f: FieldSpec, spec: EntitySpec, values: list, base: str) -> float:
    """Share of sample values that coerce to the field's type (dates must actually contain a date)."""
    if not values:
        return 0.0
    ok = 0
    for v in values:
        if f.type == "datetime" and isinstance(v, str) and not _HAS_DATE.search(v):
            continue      # "16:45" alone would parse as *today* 16:45
        if f.type in ("text", "address") and not isinstance(v, str):
            continue
        if f.type in ("number",) and isinstance(v, bool):
            continue
        if spec.coerce(f.name, v, base) not in (None, ""):
            ok += 1
    return ok / len(values)


def heuristic_mapping(spec: EntitySpec, items: list[dict], base: str = "") -> dict:
    samples = _samples(items)
    mapping: dict = {}
    for f in spec.all_fields:
        aliases = list(dict.fromkeys([f.name] + SYNONYMS.get(f.name, [])))
        best: tuple | None = None
        ranked: list[tuple] = []
        for path, vals in samples.items():
            seg = _seg(path)
            if seg in aliases:
                rank = aliases.index(seg)
            elif f.type in ("text", "address") and "." not in path and not seg.endswith(_NOT_TEXT_SUFFIX) and \
                    any(a in seg.split("_") for a in aliases):   # whole-word match on a top-level key
                rank = len(aliases) + 1          # "other_organizer_name" for organisation
            else:
                continue
            fit = _fits(f, spec, vals, base)
            if fit < 0.6:
                continue
            key = (rank, path.count("."), -fit)
            ranked.append((key, path))
            if best is None or key < best[0]:
                best = (key, path)
        if best is None:
            continue
        path = best[1]
        if f.type != "datetime" and len(ranked) > 1:   # keep fallbacks: image_url, else media_urls.0
            mapping[f.name] = {"first_of": [p for _, p in sorted(ranked)[:3]]}
            continue
        if f.type == "datetime":      # date and time in separate keys → join
            vals = [v for v in samples[path] if isinstance(v, str)]
            if vals and all(_DATE_ONLY.match(v) for v in vals):
                parent = path.rsplit(".", 1)[0] + "." if "." in path else ""
                for tk in _TIME_KEYS["end" if "end" in f.name else "start"]:
                    tv = [v for v in samples.get(parent + tk, []) if isinstance(v, str)]
                    if tv and all(_TIME_ONLY.match(v.strip()) for v in tv):
                        mapping[f.name] = [path, parent + tk]
                        break
        mapping.setdefault(f.name, path)
    return mapping


_LLM_PROMPT = """MAP JSON KEYS. Below are sample values of a JSON array's items, as dotted key paths.
Decide whether each item is {entity_desc}, and map the entity's fields to key paths.

Fields:
{fields}

Key paths with sample values:
{paths}

Respond ONLY with JSON: {{"is_entity_list": true|false, "mapping": {{"<field>": "<path>" or ["<path>", "<path>"]}}}}
Use a list of paths when a value is split across keys (e.g. date and time). Only use paths listed above.
Omit fields that have no matching key."""


def llm_mapping(spec: EntitySpec, items: list[dict], llm: Callable[[str], str]) -> dict | None:
    samples = _samples(items)
    paths = "\n".join(f"- {p}: {json.dumps([str(v)[:60] for v in vals[:3]], ensure_ascii=False)}"
                      for p, vals in list(samples.items())[:120])
    fields = "\n".join(f"- {f.name} ({f.type}{', REQUIRED' if f.required else ''}): {f.description}"
                       for f in spec.all_fields)
    try:
        raw = llm(_LLM_PROMPT.format(entity_desc=spec.description, fields=fields, paths=paths))
        m = re.search(r"\{.*\}", raw.strip().strip("`"), re.S)
        data = json.loads(m.group(0) if m else raw)
    except Exception as e:  # noqa: BLE001
        log.info("json_llm_mapping_failed", error=str(e)[:200])
        return None
    if not isinstance(data, dict) or not data.get("is_entity_list") or not isinstance(data.get("mapping"), dict):
        return None
    out = {}
    for k, v in data["mapping"].items():
        if spec.any_field(k) is None:
            continue
        ps = v if isinstance(v, list) else [v]
        if ps and all(isinstance(p, str) and p in samples for p in ps):
            out[k] = ps if len(ps) > 1 else ps[0]
    return out or None


def apply_mapping(spec: EntitySpec, item: dict, mapping: dict, base: str) -> dict:
    out = {}
    for fname, path in mapping.items():
        if isinstance(path, dict):          # {"first_of": [...]} → first path with a value
            path = next((p for p in path.get("first_of", []) if get_path(item, p) not in (None, "", [])), None)
            if path is None:
                continue
        if isinstance(path, list):
            parts = [get_path(item, p) for p in path]
            raw = " ".join(str(p) for p in parts if p not in (None, "")) if parts and parts[0] not in (None, "") else None
        else:
            raw = get_path(item, path)
            if isinstance(raw, list):
                raw = raw[0] if raw else None
        if isinstance(raw, (dict, list)):
            continue
        f = spec.any_field(fname)
        if f and f.type == "datetime" and isinstance(raw, str) and not _HAS_DATE.search(raw):
            continue
        if f and f.type in ("text", "address") and isinstance(raw, str) and "<" in raw and ">" in raw:
            from bs4 import BeautifulSoup
            raw = BeautifulSoup(raw, "html.parser").get_text(" ")
        val = spec.coerce(fname, raw, base)
        if val not in (None, ""):
            out[fname] = val
    return out


def coverage(spec: EntitySpec, items: list[dict], mapping: dict, base: str) -> float:
    sample = items[:SAMPLE]
    if not sample or any(r not in mapping for r in spec.required):
        return 0.0
    return sum(1 for it in sample if not spec.validate(apply_mapping(spec, it, mapping, base))) / len(sample)


# ------------------------------------------------------------------ item links

def infer_link_template(items: list[dict], hrefs: list[str]) -> str | None:
    """Find how the site links to an item: an href containing one of the item's own values (id, slug…),
    generalised to a template like '/activities/{slug}'. It must hold for two different items. A template
    with the value in the URL path (a detail page) beats one with it only in the query or fragment, which
    is usually a list page scrolled to the item ('/activities?scrollTo={id}')."""
    if not hrefs or len(items) < 2:
        return None
    href_set = set(hrefs)
    paths = [p for p, v in flatten(items[0], depth=1).items()
             if not isinstance(v, (bool, float)) and len(str(v)) >= 4 and " " not in str(v)]
    paths.sort(key=lambda p: (0 if _seg(p) in ("id", "slug", "uuid", "_id", "handle") else 1, p))
    found: list[str] = []
    for path in paths[:12]:
        by_val = {str(get_path(it, path)): it for it in items if get_path(it, path) not in (None, "")}
        if len(by_val) < 2:
            continue
        for h in sorted(hrefs, key=len):
            sv = next((v for v in by_val if len(v) >= 4 and v in h), None)
            if sv is None:
                continue
            template = h.replace(sv, "{" + path + "}")
            if any(v != sv and template.replace("{" + path + "}", v) in href_set for v in by_val):
                found.append(template)
                break
    return min(found, key=_template_rank) if found else None


def _template_rank(template: str) -> tuple[int, int]:
    return (1 if link_in_query_only(template) else 0, len(template))


def link_in_query_only(template: str | None) -> bool:
    """True when the item's value appears only in the query/fragment, not the path."""
    return bool(template) and "{" not in urlsplit(template).path


def _link(template: str | None, item: dict, page_url: str) -> str | None:
    if not template:
        return None
    m = re.findall(r"\{([^{}]+)\}", template)
    out = template
    for path in m:
        v = get_path(item, path)
        if v in (None, ""):
            return None
        out = out.replace("{" + path + "}", str(v))
    return urljoin(page_url, out)


# ------------------------------------------------------------------ learn + apply

WHO_FIELDS = ("organisation", "organization", "company", "organiser", "organizer", "host", "author")


def find_lookups(spec: EntitySpec, arr: list[dict], mapping: dict, api_url: str,
                 json_responses: list[tuple[str, str]]) -> dict:
    """An item often carries only an id for its organiser/company ("changemaker_id") while another API the
    page loaded has the names. If a *_id key's values match the ids of another array that has names,
    return {field: {"key", "api_url", "items_path", "id", "value"}} for an unmapped who-field."""
    target = next((f.name for f in spec.all_fields if f.name in WHO_FIELDS and f.name not in mapping), None)
    if target is None:
        return {}
    fk_keys = [k for k, v in arr[0].items() if k.endswith("_id") and isinstance(v, str)]
    for other_url, body in json_responses:
        try:
            data = json.loads(body)
        except ValueError:
            continue
        for path, other in candidate_arrays(data):
            if other_url == api_url or "id" not in other[0]:
                continue
            name_key = next((k for k in ("name", "title", "display_name") if isinstance(other[0].get(k), str)), None)
            if not name_key:
                continue
            ids = {str(o.get("id")) for o in other}
            for k in fk_keys:
                vals = [str(a[k]) for a in arr[:SAMPLE * 3] if a.get(k)]
                if len(vals) >= MIN_ITEMS and sum(v in ids for v in vals) / len(vals) >= 0.6:
                    return {target: {"key": k, "api_url": other_url, "items_path": path, "id": "id", "value": name_key}}
    return {}


def _lookup_tables(rules: dict, bodies: dict[str, str]) -> dict[str, dict]:
    tables = {}
    for fname, lk in (rules.get("lookups") or {}).items():
        body = bodies.get(lk["api_url"])
        if not body:
            continue
        try:
            data = json.loads(body)
        except ValueError:
            continue
        arr = get_path(data, lk["items_path"]) if lk.get("items_path") else data
        if isinstance(arr, list):
            tables[fname] = {str(o.get(lk["id"])): o.get(lk["value"]) for o in arr if isinstance(o, dict)}
    return tables


def items_from_rules(spec: EntitySpec, rules: dict, body: str, page_url: str,
                     bodies: dict[str, str] | None = None) -> list[dict]:
    """`bodies` = {api_url: body} for lookup endpoints (missing ones just leave that field empty)."""
    data = json.loads(body)
    arr = get_path(data, rules["items_path"]) if rules.get("items_path") else data
    if not isinstance(arr, list):
        return []
    base = rules.get("api_url") or page_url
    tables = _lookup_tables(rules, bodies or {})
    out = []
    for raw in arr:
        if not isinstance(raw, dict):
            continue
        item = apply_mapping(spec, raw, rules["mapping"], base)
        if spec.validate(item):
            continue
        for fname, table in tables.items():
            if fname not in item:
                v = spec.coerce(fname, table.get(str(raw.get(rules["lookups"][fname]["key"]))), base)
                if v not in (None, ""):
                    item[fname] = v
        link = _link(rules.get("link_template"), raw, page_url)
        if link:
            item["link"] = link
        out.append(item)
    return out


def learn_api_rules(spec: EntitySpec, json_responses: list[tuple[str, str]], page_url: str,
                    hrefs: list[str] | None = None, llm: Callable[[str], str] | None = None) -> dict | None:
    """Pick the JSON array that best yields this entity and return verified rules for it (or None)."""
    best: tuple[float, dict] | None = None
    unmapped: list[tuple[str, str, list[dict]]] = []
    for api_url, body in json_responses:
        try:
            data = json.loads(body)
        except ValueError:
            continue
        for path, arr in candidate_arrays(data):
            mapping = heuristic_mapping(spec, arr, api_url)
            cov = coverage(spec, arr, mapping, api_url)
            if cov >= MIN_COVERAGE:
                score = cov * min(len(arr), 200)
                if best is None or score > best[0]:
                    best = (score, {"api_url": api_url, "items_path": path, "mapping": mapping, "learned_by": "heuristic"})
            else:
                unmapped.append((api_url, path, arr))
    if best is None and llm is not None:
        for api_url, path, arr in sorted(unmapped, key=lambda t: -len(t[2]))[:3]:     # at most 3 LLM calls
            mapping = llm_mapping(spec, arr, llm)
            if mapping and coverage(spec, arr, mapping, api_url) >= MIN_COVERAGE:
                best = (1.0, {"api_url": api_url, "items_path": path, "mapping": mapping, "learned_by": "llm"})
                break
    if best is None:
        return None
    rules = best[1]
    body = next(b for u, b in json_responses if u == rules["api_url"])
    data = json.loads(body)
    arr = get_path(data, rules["items_path"]) if rules["items_path"] else data
    dicts = [x for x in arr if isinstance(x, dict)]
    template = infer_link_template(dicts, hrefs or [])
    if template:
        rules["link_template"] = template
    lookups = find_lookups(spec, dicts, rules["mapping"], rules["api_url"], json_responses)
    if lookups:
        rules["lookups"] = lookups
    return rules


def relink(rules: dict, json_responses: list[tuple[str, str]], hrefs: list[str]) -> bool:
    """Re-infer rules['link_template'] with more hrefs (e.g. from the list page the current template points
    at). Keeps the new template only if it ranks better. → True when it changed."""
    body = next((b for u, b in json_responses if u == rules["api_url"]), None)
    if body is None:
        return False
    data = json.loads(body)
    arr = get_path(data, rules["items_path"]) if rules["items_path"] else data
    dicts = [x for x in arr if isinstance(x, dict)] if isinstance(arr, list) else []
    template = infer_link_template(dicts, hrefs)
    old = rules.get("link_template")
    if template and (old is None or _template_rank(template) < _template_rank(old)):
        rules["link_template"] = template
        return True
    return False

