"""Learn a page layout once, reuse it forever.

A *recipe* maps each entity field to an ordered list of extraction rules for one
(domain, URL path pattern, entity). Rules are induced from a page whose values we already know
(from the LLM), verified by re-applying them to that same page, and stored. Later pages that match
the pattern are extracted with plain CSS selectors — no LLM. When a recipe stops validating
(layout changed), it is marked stale and relearned.

Rule shape: {"sel": "css selector", "attr": "text" | any attribute name ("content", "src", "data-venue"…),
"decode": optional chain applied to the raw value, "re": optional regex with one group,
"parts": optional [rule, ...] whose outputs are joined (date + time in separate nodes)}

`decode` is a list of steps, applied in order, for values that are not plain visible text:
"html" (unescape entities), "url" (percent-decoding), "base64", and {"json": "dotted.path"} (parse JSON — the
whole value, or the first object/array inside it, e.g. `window.__DATA__ = {...};` — and take the path).
That covers values held in data-* attributes, <meta>/OpenGraph tags and inline JSON (`__NEXT_DATA__`, JSON
islands, JSON-in-attribute props).
"""
from __future__ import annotations

import base64
import binascii
import html as html_lib
import json
import re
from difflib import SequenceMatcher
from urllib.parse import unquote, urlsplit

from bs4 import BeautifulSoup, Tag
from soupsieve import escape as css_escape

from scraper0.core.entities import EntitySpec

_UNSTABLE = re.compile(r"\d{3,}|[a-f0-9]{8,}|^(css|sc|jsx|emotion|tw)-|__[a-zA-Z0-9]{5,}$|^ng-|^is-|^has-|active|selected|hover|open", re.I)
_SEMANTIC_ATTRS = ("itemprop", "data-testid", "data-test", "data-field", "data-inline-binding", "data-name", "name", "property")
_WS = re.compile(r"\s+")


def norm(s: str | None) -> str:
    return _WS.sub(" ", (s or "").replace("​", "")).strip()


# ------------------------------------------------------------------ URL patterns

_ID_SEG = re.compile(r"^(\d+|[0-9a-f]{8,}|[0-9a-f-]{20,})$", re.I)


def path_pattern(url: str) -> str:
    """/events/in-person/make-a-tangram → /events/in-person/*  ;  /e/123/edit → /e/*/edit.
    The last segment is always treated as variable (it's the item slug)."""
    segs = [s for s in urlsplit(url).path.split("/") if s]
    if not segs:
        return "/"
    out = ["*" if _ID_SEG.match(s) else s for s in segs[:-1]] + ["*"]
    return "/" + "/".join(out)


def pattern_matches(pattern: str, url: str) -> bool:
    a = [s for s in pattern.split("/") if s]
    b = [s for s in urlsplit(url).path.split("/") if s]
    return len(a) == len(b) and all(x == "*" or x == y for x, y in zip(a, b))


def domain_of(url: str) -> str:
    return (urlsplit(url).hostname or "").removeprefix("www.")


# ------------------------------------------------------------------ selector generation

def _stable(tok: str) -> bool:
    return bool(tok) and not _UNSTABLE.search(tok) and len(tok) < 40


def _seg(el: Tag) -> str:
    seg = el.name
    classes = [c for c in el.get("class", []) if _stable(c)][:2]
    seg += "".join("." + css_escape(c) for c in classes)
    parent = el.parent
    if parent is not None:
        same = [s for s in parent.find_all(el.name, recursive=False)]
        if len(same) > 1:
            seg += f":nth-of-type({same.index(el) + 1})"
    return seg


def candidate_selectors(el: Tag, soup: BeautifulSoup, exclude_attr: str | None = None) -> list[str]:
    """Most semantic/robust first, full structural path last. Each must select `el` first. `exclude_attr` is
    the attribute the value is read from: it can't also identify the element."""
    cands: list[str] = []
    for a in _SEMANTIC_ATTRS:
        v = el.get(a)
        if a != exclude_attr and isinstance(v, str) and v and len(v) < 120 and not _UNSTABLE.search(v):
            cands.append(f'{el.name}[{a}="{v.replace(chr(34), chr(92) + chr(34))}"]')
    if el.get("id") and _stable(el["id"]):
        cands.append(f"#{css_escape(el['id'])}")
    if el.name == "script" and isinstance(el.get("type"), str) and "json" in el["type"]:
        cands.append(f'script[type="{el["type"]}"]')
    # Any other data-* attribute can be the template's key for this slot (site builders name their bindings
    # this way, sometimes encoded). Whether it is stable is unknowable from one page, so these are only
    # alternatives: a per-page value simply selects nothing on the next page and the next rule is tried.
    own_text = norm(el.get_text(" ")).lower()
    for a, v in el.attrs.items():
        if (a.startswith("data-") and a not in _SEMANTIC_ATTRS and a != exclude_attr and isinstance(v, str)
                and 0 < len(v) <= 200 and not v.startswith(("http://", "https://", "/", "{", "["))
                and not re.fullmatch(r"\d+|true|false", v)):
            plain = (_b64(v) or v).strip().lower()
            if plain in ("null", "none", "undefined") or (own_text and (plain == own_text or own_text in plain)):
                continue                           # the attribute repeats this page's value: not a key
            cands.append(f'{el.name}[{a}="{v.replace(chr(34), chr(92) + chr(34))}"]')
    classes = [c for c in el.get("class", []) if _stable(c)]
    if classes:
        cands.append(el.name + "".join("." + css_escape(c) for c in classes[:3]))
    if el.name in ("h1", "time") or (el.name == "meta"):
        cands.append(el.name)
    # structural path, anchored at the nearest stable id
    parts, cur = [], el
    while isinstance(cur, Tag) and cur.name not in ("[document]", "html"):
        if cur is not el and cur.get("id") and _stable(cur["id"]):
            parts.append(f"#{css_escape(cur['id'])}")
            break
        parts.append(_seg(cur))
        cur = cur.parent
    cands.append(" > ".join(reversed(parts)))
    good = []
    for s in dict.fromkeys(cands):
        try:
            hits = soup.select(s, limit=2)
        except Exception:
            continue
        if hits and hits[0] is el:
            good.append(s)
    return good


# ------------------------------------------------------------------ apply

def _read(el: Tag, attr: str, raw: bool = False) -> str | None:
    if attr == "text":
        if raw:                                    # script/JSON contents: keep them byte for byte
            return el.string if el.string is not None else el.get_text()
        return norm(el.get_text(" "))
    v = el.get(attr)
    if isinstance(v, list):
        v = " ".join(v)
    if not isinstance(v, str):
        return None
    return v if raw else norm(v)


def _b64(v: str) -> str | None:
    """Decoded text when `v` clearly is base64 of printable UTF-8 text, else None."""
    t = v.strip()
    if len(t) < 8 or len(t) % 4 == 1 or not re.fullmatch(r"[A-Za-z0-9+/_-]+={0,2}", t):
        return None
    try:
        out = base64.b64decode(t.replace("-", "+").replace("_", "/") + "=" * (-len(t) % 4), validate=True).decode("utf-8")
    except (binascii.Error, ValueError, UnicodeDecodeError):
        return None
    return out if out and sum(c.isprintable() or c.isspace() for c in out) / len(out) > 0.95 else None


def parse_json_loose(v: str):
    """JSON from a string: the whole value, else the first object/array inside it
    (`window.__STATE__ = {...};`). → parsed value or None."""
    t = v.strip()
    try:
        return json.loads(t)
    except ValueError:
        pass
    dec = json.JSONDecoder()
    for m in re.finditer(r"[{\[]", t):
        if m.start() > 4000:
            break
        try:
            obj, _ = dec.raw_decode(t[m.start():])
        except ValueError:
            continue
        if isinstance(obj, (dict, list)) and obj:
            return obj
    return None


def json_path(obj, path: str):
    cur = obj
    for seg in [x for x in path.split(".") if x != ""]:
        if isinstance(cur, dict):
            cur = cur.get(seg)
        elif isinstance(cur, list) and seg.isdigit():
            cur = cur[int(seg)] if int(seg) < len(cur) else None
        else:
            return None
        if cur is None:
            return None
    return cur


def decode_value(v, chain: list) -> str | None:
    for op in chain or []:
        if v is None:
            return None
        if op == "html":
            v = html_lib.unescape(str(v))
        elif op == "url":
            v = unquote(str(v))
        elif op == "base64":
            v = _b64(str(v))
        elif isinstance(op, dict) and "json" in op:
            obj = v if isinstance(v, (dict, list)) else parse_json_loose(str(v))
            v = json_path(obj, op["json"]) if obj is not None else None
        else:
            return None
    if isinstance(v, (dict, list)) or v is None:
        return None
    return str(v)


def apply_rule(rule: dict, soup: BeautifulSoup) -> str | None:
    if "parts" in rule:
        vals = [apply_rule(r, soup) for r in rule["parts"]]
        return " ".join(v for v in vals if v) if all(vals) else None
    try:
        el = soup.select_one(rule["sel"])
    except Exception:
        return None
    if el is None:
        return None
    if rule.get("decode"):
        v = decode_value(_read(el, rule.get("attr", "text"), raw=True), rule["decode"])
        v = norm(v) if v else None
    else:
        v = _read(el, rule.get("attr", "text"))
    if v and rule.get("re"):
        m = re.search(rule["re"], v, re.I | re.S)
        v = norm(m.group(1)) if m else None
    return v or None


def apply_recipe(rules: dict, html: str, page_url: str, spec: EntitySpec) -> tuple[dict, dict]:
    """→ (item, used) where used[field] = index of the alternative rule that produced it."""
    soup = BeautifulSoup(html, "html.parser")
    item, used = {}, {}
    for fname, alts in rules.items():
        for i, rule in enumerate(alts):
            val = spec.coerce(fname, apply_rule(rule, soup), page_url)
            if val not in (None, ""):
                item[fname], used[fname] = val, i
                break
    return item, used


# ------------------------------------------------------------------ learn

_SKIP_TAGS = {"script", "style", "noscript", "head", "title", "template"}


def _find_text_node(soup: BeautifulSoup, snippet: str) -> tuple[Tag, str] | None:
    """Deepest element whose text contains `snippet`.
    Fast path: a single text node contains it. Slow path: snippet spans inline children
    ("<b>Date:</b> 14 March"); scan elements, preferring the smallest. Then attributes."""
    s = norm(snippet)
    if len(s) < 2:
        return None
    low = s.lower()
    for node in soup.find_all(string=True):
        p = node.parent
        if p is None or p.name in _SKIP_TAGS or any(a.name in _SKIP_TAGS for a in p.parents if a.name):
            continue
        if low in norm(str(node)).lower():
            return p, "text"
    best, best_len = None, 10 ** 9
    for el in soup.find_all(True):
        if el.name in _SKIP_TAGS:
            continue
        t = norm(el.get_text(" ")).lower()
        if len(t) > max(400, len(low) * 4) or low not in t:
            continue
        if len(t) <= best_len:          # equal text: the later element is the deeper one
            best, best_len = el, len(t)
    if best is not None:
        return best, "text"
    for el in soup.find_all(["meta", "time", "img", "a", "input"]):
        for attr in ("content", "datetime", "value", "alt", "title"):
            v = el.get(attr)
            if isinstance(v, str) and norm(v).lower() == low:
                return el, attr
    return None


def _find_url_node(soup: BeautifulSoup, url: str, page_url: str) -> tuple[Tag, str] | None:
    from urllib.parse import urljoin
    target = urljoin(page_url, url)
    for el in soup.find_all(["img", "meta", "a", "source", "link"]):
        for attr in ("src", "content", "href", "data-src", "srcset"):
            v = el.get(attr)
            if isinstance(v, str) and v and urljoin(page_url, v.split(" ")[0]) == target:
                return el, attr
    return None


def _is_label(ctx: str) -> bool:
    """'Venue:' / 'Date -' / '| IST' are labels; '7:00 am' or prose are other values."""
    c = ctx.strip()
    if len(c) > 30 or re.search(r"\d", c):
        return False
    return len(c.split()) <= 3 or c[-1:] in ":-–|" or c[:1] in ":-–|("        # not the rest of a sentence


def _rule_for(soup: BeautifulSoup, el: Tag, attr: str, snippet: str) -> list[dict]:
    """Candidate rules for `el`, label-stripping regex variant first, whole-text variant second.
    Callers verify which ones actually reproduce the value."""
    sels = candidate_selectors(el, soup, exclude_attr=None if attr == "text" else attr)
    if attr == "text":
        # a wrapper holding exactly the same text may be the element the template names (a bound <span> in a <p>)
        own, cur, keyed = norm(el.get_text(" ")), el.parent, []
        for _ in range(3):
            if not isinstance(cur, Tag) or cur.name in ("body", "html") or norm(cur.get_text(" ")) != own:
                break
            keyed += [x for x in candidate_selectors(cur, soup) if "[" in x or x.startswith("#")]
            cur = cur.parent
        named = [x for x in sels if "[" in x or x.startswith("#")]
        sels = list(dict.fromkeys(named + keyed + sels))
    if not sels:
        return []
    variants: list[str | None] = []
    if attr == "text" and snippet:
        full, s = norm(el.get_text(" ")), norm(snippet)
        idx = full.lower().find(s.lower())
        if full.lower() != s.lower() and idx >= 0:
            prefix, suffix = full[:idx], full[idx + len(s):]
            if _is_label(prefix) and _is_label(suffix):
                pre = re.escape(prefix.strip()) + r"\s*" if prefix.strip() else r"^\s*"
                post = r"\s*" + re.escape(suffix.strip()) if suffix.strip() else r"\s*$"
                variants.append(pre + r"(.+?)" + post)
    variants.append(None)
    return [{"sel": sel, "attr": attr, **({"re": rx} if rx else {})} for rx in variants for sel in sels[:4]]


def _same(spec: EntitySpec, fname: str, a, b) -> bool:
    if a is None or b is None:
        return False
    if spec.field(fname).type in ("text", "address"):
        return SequenceMatcher(None, str(a).lower(), str(b).lower()).ratio() >= 0.9
    return a == b


def _common_node(soup: BeautifulSoup, snippets: list[str]) -> Tag | None:
    hits = [_find_text_node(soup, sn) for sn in snippets]
    if all(hits) and len({id(h[0]) for h in hits}) == 1 and hits[0][1] == "text":
        return hits[0][0]
    return None


def _find_value_node(soup: BeautifulSoup, value: str) -> Tag | None:
    """Smallest element whose whole text is (nearly) `value`: a long description the AI quoted only part of."""
    want = norm(value).lower()
    if len(want) < 20:
        return None
    head = want[:24]
    best, best_len = None, 10 ** 9
    for el in soup.find_all(True):
        if el.name in _SKIP_TAGS or el.name in ("html", "body"):
            continue
        t = norm(el.get_text(" ")).lower()
        if not (0.8 * len(want) <= len(t) <= 1.25 * len(want)) or head not in t or len(t) >= best_len:
            continue
        if SequenceMatcher(None, t, want).ratio() >= 0.9:
            best, best_len = el, len(t)
    return best


_TITLE_SEP = r"|–—·:»"


def _title_rules(soup: BeautifulSoup, want: str) -> list[dict]:
    """A short value that is one part of the page <title> ("In Person Volunteering | Team Everest"): its last
    or first part, as a rule on the title."""
    title = norm(soup.title.get_text()) if soup.title else ""
    parts = [p.strip() for p in re.split(rf"\s[{_TITLE_SEP}-]\s|[{_TITLE_SEP}]", title) if p.strip()]
    if len(parts) < 2 or len(want) > 80:
        return []
    out = []
    if SequenceMatcher(None, parts[-1].lower(), want.lower()).ratio() >= 0.9:
        out.append({"sel": "title", "attr": "text", "re": rf"(?:\s[{_TITLE_SEP}-]\s|[{_TITLE_SEP}])\s*([^{_TITLE_SEP}]+?)\s*$"})
    if SequenceMatcher(None, parts[0].lower(), want.lower()).ratio() >= 0.9:
        out.append({"sel": "title", "attr": "text", "re": rf"^\s*(.+?)\s*(?:\s[{_TITLE_SEP}-]\s|[{_TITLE_SEP}])"})
    return out


def _decodings(raw: str):
    """(chain, decoded value) for every way a raw attribute/script value can be read, plain first."""
    yield [], raw
    seen = {raw}
    for chain, fn in ((["url"], lambda v: unquote(v) if "%" in v else None), (["base64"], _b64),
                      (["html"], lambda v: html_lib.unescape(v) if "&" in v else None)):
        try:
            v = fn(raw)
        except Exception:  # noqa: BLE001
            v = None
        if v and v not in seen:
            seen.add(v)
            yield chain, v


def _json_leaves(obj, prefix: str = "", depth: int = 10, budget: list | None = None):
    """(path, scalar) for every leaf, lists by index (first 30 members)."""
    budget = budget if budget is not None else [20000]
    if budget[0] <= 0:
        return
    if isinstance(obj, dict):
        for k, v in obj.items():
            p = f"{prefix}.{k}" if prefix else str(k)
            if "." in str(k):
                continue                      # a key with a dot can't be addressed by a dotted path
            if isinstance(v, (dict, list)):
                if depth > 0:
                    yield from _json_leaves(v, p, depth - 1, budget)
            elif v not in (None, ""):
                budget[0] -= 1
                yield p, v
    elif isinstance(obj, list):
        for i, v in enumerate(obj[:30]):
            p = f"{prefix}.{i}" if prefix else str(i)
            if isinstance(v, (dict, list)):
                if depth > 0:
                    yield from _json_leaves(v, p, depth - 1, budget)
            elif v not in (None, ""):
                budget[0] -= 1
                yield p, v


_ATTR_SKIP = {"class", "style", "id", "href", "src", "srcset", "sizes", "width", "height", "rel", "type", "onclick",
              "onerror", "onload", "target", "role", "tabindex", "loading", "decoding", "lang", "dir"}


def _hidden_rules(soup: BeautifulSoup, spec: EntitySpec, fname: str, want, page_url: str, limit: int = 6) -> list[dict]:
    """Rules for a value that is not visible text: an attribute (data-*, content, datetime, value…), possibly
    url/base64/html-encoded or JSON, or inline JSON in a <script> (`__NEXT_DATA__`, JSON islands, `x = {...}`).
    Unverified candidates, most direct first; the caller keeps those that reproduce the value."""
    f = spec.field(fname)
    out: list[dict] = []

    def hit(raw) -> bool:
        if isinstance(raw, bool) or raw in (None, ""):
            return False
        if f.type == "datetime" and not (isinstance(raw, str) and re.search(r"\d", raw) and len(raw) <= 80):
            return False
        if f.type in ("text", "address") and not isinstance(raw, str):
            return False
        if f.type in ("text", "address"):
            if "<" in raw and ">" in raw:
                raw = BeautifulSoup(raw, "html.parser").get_text(" ")
            if not 0.7 * len(str(want)) <= len(raw) <= 1.4 * len(str(want)):
                return False
        return _same(spec, fname, spec.coerce(fname, raw, page_url), want)

    def add(el: Tag, attr: str, chain: list) -> None:
        for sel in candidate_selectors(el, soup, exclude_attr=None if attr == "text" else attr)[:3]:
            out.append({"sel": sel, "attr": attr, **({"decode": chain} if chain else {})})

    for el in soup.find_all(True):
        if len(out) >= limit * 3:
            break
        if el.name in ("script", "style", "noscript", "template", "html", "body", "head"):
            continue
        for attr, raw in el.attrs.items():
            if attr in _ATTR_SKIP and not (f.type in ("url", "image") and attr in ("href", "src")):
                continue
            if not isinstance(raw, str) or not raw.strip() or len(raw) > 20000:
                continue
            for chain, val in _decodings(raw):
                if val[:1] in "{[" and len(val) > 2:
                    obj = parse_json_loose(val)
                    for path, leaf in (_json_leaves(obj) if obj is not None else ()):
                        if hit(leaf):
                            add(el, attr, chain + [{"json": path}])
                            break
                elif (chain or attr not in ("alt", "title")) and hit(val):
                    add(el, attr, chain)
    for el in soup.find_all("script"):
        if len(out) >= limit * 3:
            break
        if el.get("src") or "ld+json" in (el.get("type") or ""):
            continue                           # JSON-LD is read by the structured-data extractor
        text = el.string or ""
        if len(text) < 20 or len(text) > 3_000_000 or not ("{" in text or "[" in text):
            continue
        probe = str(want)[:40]
        if f.type in ("text", "address") and json.dumps(probe)[1:-1][:20] not in text and probe[:20] not in text:
            continue
        obj = parse_json_loose(text)
        if obj is None:
            continue
        for path, leaf in _json_leaves(obj):
            if hit(leaf):
                add(el, "text", [{"json": path}])
                break
    return out[: limit * 3]


def template_signature(html: str, soup: BeautifulSoup | None = None) -> frozenset[str]:
    """What a page's template looks like, ignoring its content: the stable class names and binding attributes of
    its elements. Two pages rendered from one template share almost all of them, whatever their URLs."""
    soup = soup or BeautifulSoup(html, "html.parser")
    toks: set[str] = set()
    for el in soup.find_all(True):
        if el.name in ("script", "style", "noscript", "head", "meta", "link"):
            continue
        for c in el.get("class", []) or []:
            if _stable(c):
                toks.add(f"{el.name}.{c}")
        for a in _SEMANTIC_ATTRS:
            v = el.get(a)
            if isinstance(v, str) and v and len(v) < 80 and not _UNSTABLE.search(v):
                toks.add(f"{el.name}[{a}={v}]")
        if len(toks) > 3000:
            break
    return frozenset(toks)


def template_similarity(a: frozenset, b: frozenset) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def learn_recipe(html: str, page_url: str, spec: EntitySpec, item: dict, sources: dict) -> dict:
    """Induce rules for every field we can locate, then keep only rules that reproduce the
    known value on this page. `item` = coerced values; `sources` = field → verbatim snippet(s)
    from the page (the LLM is asked to return these). Returns {field: [rules]} (may be partial)."""
    soup = BeautifulSoup(html, "html.parser")
    rules: dict[str, list[dict]] = {}
    for fname, want in item.items():
        f = spec.field(fname)
        if f is None or want in (None, ""):
            continue
        src = sources.get(fname)
        snippets = src if isinstance(src, list) else [src] if src else []
        if f.type in ("url", "image"):
            hit = _find_url_node(soup, str(want), page_url)
            cands = _rule_for(soup, hit[0], hit[1], "") if hit else []
        elif len(snippets) > 1 and _common_node(soup, snippets) is not None:
            el = _common_node(soup, snippets)          # "14 March 2026 7:00 am" in one node
            cands = _rule_for(soup, el, "text", "")
        elif len(snippets) > 1:  # value spread across nodes, e.g. date + time
            parts = []
            for sn in snippets:
                hit = _find_text_node(soup, sn)
                r = _rule_for(soup, hit[0], hit[1], sn) if hit else []
                if not r:
                    parts = []
                    break
                parts.append(r[0])
            cands = [{"parts": parts}] if parts else []
        else:
            sn = snippets[0] if snippets else (str(want) if f.type in ("text", "address") else None)
            hit = _find_text_node(soup, sn) if sn else None
            cands = _rule_for(soup, hit[0], hit[1], sn) if hit else []
        def ok(r):
            got = spec.coerce(fname, apply_rule(r, soup), page_url)
            if _same(spec, fname, got, want):
                return True
            # A text the AI shortened or summarised: the rule is right if it yields the element its quote is in.
            if f.type in ("text", "address") and got and snippets and not f.required:
                quote, text = norm(" ".join(snippets)).lower(), norm(str(got)).lower()
                return len(quote) >= 12 and len(text) <= 3000 and \
                    (text.startswith(quote) or (quote in text and len(quote) >= 0.4 * len(text)))
            return False
        verified = [r for r in cands if ok(r)]
        if not verified and f.type in ("text", "address"):       # the AI quoted a part; the element holds it all
            el = _find_value_node(soup, str(want))
            verified = [r for r in (_rule_for(soup, el, "text", "") if el is not None else []) if ok(r)]
        if not verified and f.type == "text":                    # "Page name | Site name": a part of the <title>
            verified = [r for r in _title_rules(soup, str(want)) if ok(r)]
        if not verified:                                         # not visible text: attributes, meta, inline JSON
            verified = [r for r in _hidden_rules(soup, spec, fname, want, page_url) if ok(r)][:4]
        if verified:
            rules[fname] = verified
    for f in spec.fields:                      # a page's share image is its picture unless the AI found a better one
        if f.type == "image" and f.name not in rules:
            og = soup.select_one('meta[property="og:image"][content]')
            if og is not None and og["content"].strip():
                rules[f.name] = [{"sel": 'meta[property="og:image"]', "attr": "content"}]
    return rules


def merge_rules(old: dict, new: dict) -> dict:
    """Newest verified rules first, keep older alternatives as fallbacks (max 5 per field)."""
    out = {}
    for f in set(old) | set(new):
        seen, alts = set(), []
        for r in new.get(f, []) + old.get(f, []):
            k = repr(sorted(r.items()))
            if k not in seen:
                seen.add(k)
                alts.append(r)
        out[f] = alts[:5]
    return out


def _keyed(rule: dict) -> bool:
    """A rule that names its element (an attribute or id), not just its position in the page."""
    if "parts" in rule:
        return all(_keyed(r) for r in rule["parts"])
    sel = rule.get("sel", "")
    return " > " not in sel and ":nth-" not in sel and ("[" in sel or sel.startswith("#") or sel == "title")


def share_rules(layouts: list[dict]) -> list[dict]:
    """Layouts of one site often bind the same slots ("About the Program") even where the AI, on the one page
    it read for a layout, did not report that field. Give each layout the other layouts' rules for the fields
    it lacks — only rules that name their element, so they select nothing on a page without that slot."""
    out = []
    for lay in layouts:
        rules = {k: list(v) for k, v in lay["rules"].items()}
        for other in layouts:
            if other is lay:
                continue
            for fname, alts in other["rules"].items():
                if fname not in lay["rules"]:
                    extra = [r for r in alts if _keyed(r) and r not in rules.get(fname, [])]
                    if extra:
                        rules.setdefault(fname, []).extend(extra)
        out.append({**lay, "rules": {k: v[:6] for k, v in rules.items()}})
    return out
