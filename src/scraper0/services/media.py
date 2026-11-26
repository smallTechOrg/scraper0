"""Image copies: scraper0 keeps its own copy of every stored item's image.

Many sites serve images from signed URLs that expire within hours (Airtable, S3/CDN signatures), so a
stored item's image link would die long before its freshness window ends. Each image is downloaded once
while storing items, shrunk to at most MAX_SIDE px, and kept in the `media` table under the sha256 of the
stored bytes. Items then point at GET /v2/media/{id}, a permanent public URL. Failures keep the original URL.
"""
from __future__ import annotations

import hashlib
import io
from concurrent.futures import ThreadPoolExecutor
from urllib.parse import urljoin, urlsplit

import httpx
import structlog
from sqlalchemy import select

from scraper0.config import get_settings
from scraper0.core.fetch import is_public_url
from scraper0.db.models import Media
from scraper0.db.session import session_scope

log = structlog.get_logger(__name__)

MAX_DOWNLOAD = 15_000_000
MAX_SIDE = 1600
ROUTE = "/v2/media/"


def media_url(media_id: str) -> str:
    return get_settings().public_base_url.rstrip("/") + ROUTE + media_id


def is_ours(url: str | None) -> bool:
    return bool(url) and ROUTE in url and url.startswith(get_settings().public_base_url.rstrip("/"))


def source_key(url: str) -> str:
    """Same image, same key: the URL without its query (signatures and cache-busters live there)."""
    p = urlsplit(url)
    return hashlib.sha256(f"{p.scheme}://{p.netloc}{p.path}".encode()).hexdigest()


def download(url: str) -> tuple[bytes, str] | None:
    """GET with the SSRF guard on every redirect hop. → (bytes, content type) or None."""
    with httpx.Client(timeout=20, follow_redirects=False,
                      headers={"User-Agent": "Mozilla/5.0 (compatible; scraper0)"}) as c:
        for _ in range(4):
            if not is_public_url(url):
                return None
            r = c.get(url)
            if r.is_redirect and "location" in r.headers:
                url = urljoin(url, r.headers["location"])
                continue
            if r.status_code != 200 or len(r.content) > MAX_DOWNLOAD:
                return None
            return r.content, r.headers.get("content-type", "")
    return None


def shrink(raw: bytes) -> tuple[bytes, str] | None:
    """→ (bytes, content type): at most MAX_SIDE px, JPEG (PNG when it has transparency). Animated or
    unknown formats that Pillow can't re-encode safely are kept as they are."""
    from PIL import Image
    try:
        im = Image.open(io.BytesIO(raw))
        im.load()
    except Exception:  # noqa: BLE001 - not an image
        return None
    if getattr(im, "is_animated", False):
        return raw, Image.MIME.get(im.format or "", "image/gif")
    im.thumbnail((MAX_SIDE, MAX_SIDE))
    out = io.BytesIO()
    if im.mode == "P" and "transparency" in im.info:
        im = im.convert("RGBA")
    if im.mode in ("RGBA", "LA") and im.getchannel("A").getextrema()[0] < 255:     # really transparent
        im.save(out, "PNG", optimize=True)
        return out.getvalue(), "image/png"
    im.convert("RGB").save(out, "JPEG", quality=85, optimize=True, progressive=True)
    return out.getvalue(), "image/jpeg"


def _fetch_one(url: str):
    try:
        got = download(url)
        if got is None:
            return None
        small = shrink(got[0])
        if small is None:
            return None
        data, ctype = small
        return hashlib.sha256(data).hexdigest()[:40], ctype, data
    except Exception as e:  # noqa: BLE001 - best effort: the original URL stays
        log.info("image_copy_failed", url=url, error=str(e))
        return None


def copy_images(urls: list[str]) -> dict[str, str]:
    """Original URL → our permanent URL, for every image that could be copied. Network work happens with
    no DB session open; then one short session looks up known sources and stores new images."""
    if not get_settings().copy_images:
        return {}
    todo = sorted({u for u in urls if u and u.startswith(("http://", "https://")) and not is_ours(u)})
    if not todo:
        return {}
    with session_scope() as s:
        known = {k: i for k, i in s.execute(select(Media.source_key, Media.id).where(
            Media.source_key.in_([source_key(u) for u in todo])))}
    out = {u: media_url(known[source_key(u)]) for u in todo if source_key(u) in known}
    fetch = [u for u in todo if u not in out]
    with ThreadPoolExecutor(max_workers=4) as pool:
        got = list(pool.map(_fetch_one, fetch))
    new = {}
    for u, g in zip(fetch, got):
        if g:
            mid, ctype, data = g
            new.setdefault(mid, (u, ctype, data))
            out[u] = media_url(mid)
    if new:
        with session_scope() as s:
            have = set(s.scalars(select(Media.id).where(Media.id.in_(list(new)))))
            for mid, (u, ctype, data) in new.items():
                if mid not in have:
                    s.add(Media(id=mid, content_type=ctype, data=data, size=len(data),
                                source_url=u[:2000], source_key=source_key(u)))
    log.info("images_copied", requested=len(todo), copied=len(out), downloaded=len(new))
    return out


def swap_images(items: list[dict], field: str = "image") -> None:
    """Point each item's image at our copy, in place (callers that return the same dicts show it too)."""
    copies = copy_images([i.get(field) for i in items if isinstance(i, dict) and isinstance(i.get(field), str)])
    for i in items:
        if isinstance(i, dict) and i.get(field) in copies:
            i[field] = copies[i[field]]


def get(media_id: str) -> tuple[bytes, str] | None:
    with session_scope() as s:
        m = s.get(Media, media_id)
        return (m.data, m.content_type) if m else None
