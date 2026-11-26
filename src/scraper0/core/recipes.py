"""Learn a page layout once, reuse it forever.

A *recipe* maps each entity field to an ordered list of extraction rules for one
(domain, URL path pattern, entity). Rules are induced from a page whose values we already know
(from the LLM), verified by re-applying them to that same page, and stored. Later pages that match
the pattern are extracted with plain CSS selectors — no LLM. When a recipe stops validating
(layout changed), it is marked stale and relearned.

Rule shape: {"sel": "css selector", "attr": "text"|"content"|"src"|"href"|"datetime", "re": optional regex
with one group, "parts": optional [rule, ...] whose outputs are joined (date + time in separate nodes)}
"""
from __future__ import annotations

import re
from difflib import SequenceMatcher
from urllib.parse import urlsplit

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


def candidate_selectors(el: Tag, soup: BeautifulSoup) -> list[str]:
    """Most semantic/robust first, full structural path last. Each must select `el` first."""
    cands: list[str] = []
    for a in _SEMANTIC_ATTRS:
        v = el.get(a)
        if isinstance(v, str) and v and len(v) < 120 and not _UNSTABLE.search(v):
            cands.append(f'{el.name}[{a}="{v.replace(chr(34), chr(92) + chr(34))}"]')
    if el.get("id") and _stable(el["id"]):
        cands.append(f"#{css_escape(el['id'])}")
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

def _read(el: Tag, attr: str) -> str | None:
    if attr == "text":
        return norm(el.get_text(" "))
    v = el.get(attr)
    return norm(v) if isinstance(v, str) else None


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
        if len(t) < best_len:
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
    return len(c) <= 30 and not re.search(r"\d", c)


def _rule_for(soup: BeautifulSoup, el: Tag, attr: str, snippet: str) -> list[dict]:
    """Candidate rules for `el`, label-stripping regex variant first, whole-text variant second.
    Callers verify which ones actually reproduce the value."""
    sels = candidate_selectors(el, soup)
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
    return [{"sel": sel, "attr": attr, **({"re": rx} if rx else {})} for rx in variants for sel in sels[:3]]


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
        verified = [r for r in cands if _same(spec, fname, spec.coerce(fname, apply_rule(r, soup), page_url), want)]
        if verified:
            rules[fname] = verified
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
