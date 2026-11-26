"""Find event data from just a seed URL: feeds, ICS links, sitemaps, event-looking links, pagination."""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlsplit

from bs4 import BeautifulSoup

_EVENT_PATH = re.compile(r"/(events?|event-details?|calendar|workshops?|meetups?|activities|drives?|"
                         r"volunteer(?:ing)?|opportunit(?:y|ies)|whats-on|programs?)(/|$|\?)", re.I)
_DETAIL_HINT = re.compile(r"/(events?|opportunit(?:y|ies)|activities|drives?|workshops?)/[^/?#]+", re.I)
_SKIP = re.compile(r"\.(jpe?g|png|gif|svg|webp|pdf|css|js|zip|mp4)(\?|$)|/(tag|category|author|login|signup|cart)/", re.I)


def find_feeds(html: str, page_url: str) -> list[str]:
    """<link rel=alternate> RSS/Atom/ICS + anchors ending in .ics/webcal."""
    soup = BeautifulSoup(html, "html.parser")
    found: list[str] = []
    for lk in soup.find_all("link", href=True):
        t = (lk.get("type") or "").lower()
        if "rss" in t or "atom" in t or "calendar" in t:
            found.append(urljoin(page_url, lk["href"]))
    for a in soup.find_all("a", href=True):
        h = a["href"]
        if h.lower().endswith(".ics") or h.lower().startswith("webcal:"):
            found.append(urljoin(page_url, h.replace("webcal:", "https:", 1)))
    return list(dict.fromkeys(found))


def event_links(html: str, page_url: str, limit: int = 200) -> list[str]:
    """Same-site links that look like event detail pages. Detail-shaped paths rank first."""
    soup = BeautifulSoup(html, "html.parser")
    host = urlsplit(page_url).netloc.removeprefix("www.")
    strong, weak = [], []
    for a in soup.find_all("a", href=True):
        url = urljoin(page_url, a["href"]).split("#")[0]
        p = urlsplit(url)
        if p.scheme not in ("http", "https") or p.netloc.removeprefix("www.") != host or _SKIP.search(url):
            continue
        if url.rstrip("/") == page_url.rstrip("/"):
            continue
        if _DETAIL_HINT.search(p.path):
            strong.append(url)
        elif _EVENT_PATH.search(p.path + "/"):
            weak.append(url)
    return list(dict.fromkeys(strong + weak))[:limit]


def next_page(html: str, page_url: str) -> str | None:
    soup = BeautifulSoup(html, "html.parser")
    nxt = soup.find("link", rel="next") or soup.find("a", rel="next")
    if nxt and nxt.get("href"):
        return urljoin(page_url, nxt["href"])
    for a in soup.find_all("a", href=True):
        if re.fullmatch(r"\s*(next|older|more events|›|»|next page)\s*", a.get_text(), re.I):
            return urljoin(page_url, a["href"])
    return None


def sitemap_event_urls(xml_text: str, base: str) -> tuple[list[str], list[str]]:
    """→ (event page urls, child sitemap urls). Child sitemaps are only kept if event-ish."""
    try:
        root = ET.fromstring(xml_text.encode("utf-8"))
    except ET.ParseError:
        return [], []
    pages, children = [], []
    for el in root.iter():
        if el.tag.split("}")[-1] != "loc" or not el.text:
            continue
        u = el.text.strip()
        is_index_child = u.endswith(".xml") or "sitemap" in urlsplit(u).path
        if is_index_child:
            if re.search(r"event|calendar|opportunit|activit", u, re.I):
                children.append(u)
        elif _DETAIL_HINT.search(urlsplit(u).path):
            pages.append(u)
    return pages, children


def sitemap_candidates(seed: str) -> list[str]:
    p = urlsplit(seed)
    o = f"{p.scheme}://{p.netloc}"
    return [f"{o}/sitemap.xml", f"{o}/sitemap_index.xml", f"{o}/event-sitemap.xml", f"{o}/events-sitemap.xml"]


def site_links(html: str, page_url: str, limit: int = 400) -> list[str]:
    """All same-site, non-asset links (generic entities have no URL vocabulary to rely on)."""
    soup = BeautifulSoup(html, "html.parser")
    host = urlsplit(page_url).netloc.removeprefix("www.")
    out = []
    for a in soup.find_all("a", href=True):
        url = urljoin(page_url, a["href"]).split("#")[0]
        p = urlsplit(url)
        if p.scheme in ("http", "https") and p.netloc.removeprefix("www.") == host and not _SKIP.search(url) \
                and url.rstrip("/") != page_url.rstrip("/"):
            out.append(url)
    return list(dict.fromkeys(out))[:limit]
