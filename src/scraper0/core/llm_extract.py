"""Spec-driven LLM extraction. Asks for each value AND the verbatim page text it came from,
so recipes can be learned from the answer."""
from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Callable
from urllib.parse import urljoin

from bs4 import BeautifulSoup

from scraper0.core.dates import IST
from scraper0.core.entities import EntitySpec
from scraper0.core.recipes import norm

import structlog

log = structlog.get_logger(__name__)

LLMFn = Callable[[str], str]
MAX_PAGE_CHARS = 7000

PROMPT = """EXTRACT {entity_desc} from this web page.
Today is {today} (India, IST). Page URL: {url}

Fields:
{fields}

Respond ONLY with JSON:
{{"found": true|false, "items": [{{"<field>": {{"value": <value>, "source": "<exact text copied from the page>"}}, ...}}]}}

Rules:
- "source" must be copied character-for-character from the page text below (a short span, not a paraphrase).
  If a value is assembled from two places (e.g. date and time shown separately), give "source" as a list of both spans.
- A name or title is the page's own heading for the item: a whole line of PAGE TEXT, copied in full, never a
  phrase picked from inside a sentence.
- For image fields, "value" is one of the image URLs listed under IMAGES; set "source" to that URL.
- Datetimes: "value" as "YYYY-MM-DDTHH:MM" in local India time. If no year is shown, use the next upcoming date.
- Never invent values. Omit fields that are not on the page.
- found=false if the page does not describe {entity_name} items (e.g. it is a category, login or about page).
- One item per distinct {entity_name}. A detail page normally has exactly one.

PAGE TEXT:
{text}

IMAGES:
{images}"""


_CHROME_IMG = re.compile(r"logo|icon|sprite|avatar|favicon|badge|pixel|spacer|\.svg(\?|$)", re.I)


def _site_chrome(img) -> bool:
    """Logos, icons and tracking pixels, and anything in the page's header, navigation or footer: never an
    item's own picture."""
    if _CHROME_IMG.search(img.get("src", "")) or _CHROME_IMG.search(" ".join(img.get("class", []) or [])):
        return True
    if str(img.get("width", "")) in ("0", "1") or str(img.get("height", "")) in ("0", "1"):
        return True
    return any(getattr(p, "name", None) in ("header", "nav", "footer") for p in img.parents)


def page_text(html: str) -> tuple[str, list[str], BeautifulSoup]:
    soup = BeautifulSoup(html, "html.parser")
    images = []
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        images.append(og["content"])
    for img in soup.find_all("img", src=True):
        if len(images) >= 25:
            break
        if img["src"].startswith("data:") or _site_chrome(img):
            continue
        images.append(img["src"])
    body = BeautifulSoup(html, "html.parser")
    for t in body(["script", "style", "noscript", "svg", "template"]):
        t.decompose()
    lines = [norm(ln) for ln in body.get_text("\n").splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return text, list(dict.fromkeys(images)), soup


def _rebalance(t: str) -> str:
    """Close brackets a model left open or closed in the wrong order ('{"a": [{"b": 1]}' → '{"a": [{"b": 1}]}')."""
    out, stack, in_str, esc = [], [], False, False
    pair = {"}": "{", "]": "["}
    close = {"{": "}", "[": "]"}
    for ch in t:
        if in_str:
            out.append(ch)
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch in "{[":
            stack.append(ch)
        elif ch in "}]":
            while stack and stack[-1] != pair[ch]:
                out.append(close[stack.pop()])
            if not stack:
                continue
            stack.pop()
        out.append(ch)
    if in_str:
        out.append('"')
    while stack:
        out.append(close[stack.pop()])
    return "".join(out)


def _parse_json(raw: str):
    t = raw.strip().strip("`")
    t = re.sub(r"^json\s*", "", t)
    m = re.search(r"\{.*\}", t, re.S)
    t = m.group(0) if m else t
    try:
        return json.loads(t)
    except ValueError:
        return json.loads(_rebalance(t))


def llm_extract(html: str, url: str, spec: EntitySpec, llm: LLMFn, require_valid: bool = True,
                info: dict | None = None) -> list[tuple[dict, dict]]:
    """→ [(item with coerced values, sources per field)]. Empty list when nothing found/failed. With
    require_valid=False partial items are returned too (an `ai` plan step filling a few fields).
    `info["found"]` is set to False only when the AI itself said the page has no such items."""
    text, images, _ = page_text(html)
    if len(text) < 60:
        return []
    fields = "\n".join(
        f"- {f.name} ({f.type}{', one of ' + '/'.join(f.choices) if f.choices else ''}"
        f"{', REQUIRED' if f.required else ''}): {f.description}" for f in spec.fields)
    prompt = PROMPT.format(entity_desc=spec.description, entity_name=spec.name, url=url, fields=fields,
                           today=datetime.now(IST).strftime("%A %Y-%m-%d"), text=text[:MAX_PAGE_CHARS],
                           images="\n".join(images[:25]) or "(none)")
    try:
        data = _parse_json(llm(prompt))
    except Exception as e:
        log.info(f"{url}: unusable response ({e})")
        return []
    if not isinstance(data, dict) or not data.get("found"):
        if info is not None and isinstance(data, dict) and data.get("found") is False:
            info["found"] = False
        return []
    out = []
    for raw in data.get("items") or []:
        if not isinstance(raw, dict):
            continue
        item, sources = {}, {}
        for fname, cell in raw.items():
            if spec.field(fname) is None:
                continue
            if isinstance(cell, dict):
                val, src = cell.get("value"), cell.get("source")
            else:
                val, src = cell, None
            v = spec.coerce(fname, val, url)
            if spec.field(fname).type == "image" and v and v not in {urljoin(url, i) for i in images}:
                continue   # hallucinated image URL
            if v not in (None, ""):
                item[fname] = v
                if src:
                    sources[fname] = src
        if item and (not require_valid or not spec.validate(item)):
            out.append((item, sources))
    return out
