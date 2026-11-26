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
- For image fields, "value" is one of the image URLs listed under IMAGES; set "source" to that URL.
- Datetimes: "value" as "YYYY-MM-DDTHH:MM" in local India time. If no year is shown, use the next upcoming date.
- Never invent values. Omit fields that are not on the page.
- found=false if the page does not describe {entity_name} items (e.g. it is a category, login or about page).
- One item per distinct {entity_name}. A detail page normally has exactly one.

PAGE TEXT:
{text}

IMAGES:
{images}"""


def page_text(html: str) -> tuple[str, list[str], BeautifulSoup]:
    soup = BeautifulSoup(html, "html.parser")
    images = []
    og = soup.find("meta", property="og:image")
    if og and og.get("content"):
        images.append(og["content"])
    for img in soup.find_all("img", src=True)[:25]:
        if not img["src"].startswith("data:"):
            images.append(img["src"])
    body = BeautifulSoup(html, "html.parser")
    for t in body(["script", "style", "noscript", "svg", "template"]):
        t.decompose()
    lines = [norm(ln) for ln in body.get_text("\n").splitlines()]
    text = "\n".join(ln for ln in lines if ln)
    return text, list(dict.fromkeys(images)), soup


def _parse_json(raw: str):
    t = raw.strip().strip("`")
    t = re.sub(r"^json\s*", "", t)
    m = re.search(r"\{.*\}", t, re.S)
    return json.loads(m.group(0) if m else t)


def llm_extract(html: str, url: str, spec: EntitySpec, llm: LLMFn) -> list[tuple[dict, dict]]:
    """→ [(item with coerced values, sources per field)]. Empty list when nothing found/failed."""
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
        if not spec.validate(item):
            out.append((item, sources))
    return out
