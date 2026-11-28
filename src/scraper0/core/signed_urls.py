"""When does a signed URL stop working?

scraper0 stores image *links*, never the images: copying an image is the caller's job. Many sites hand out signed
links that expire within hours (Airtable, S3, Google Cloud Storage, CloudFront, Azure). `expires_at` reads the
expiry such a link carries, so items can report `image_expires_at` and reads can rescrape before stored links die.
Unknown or unsigned links → None (assumed not to expire).
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

_AIRTABLE_HOST = re.compile(r"(^|\.)airtableusercontent\.com$")
_EPOCH_MS = re.compile(r"^\d{13}$")


def _epoch(v: str | None) -> datetime | None:
    if not v or not v.isdigit():
        return None
    n = int(v)
    if n > 10**12:                       # milliseconds
        n //= 1000
    if not 10**9 <= n < 10**10:          # 2001 … 2286: anything else isn't a timestamp
        return None
    return datetime.fromtimestamp(n, timezone.utc)


def _amz_date(v: str | None) -> datetime | None:
    try:
        return datetime.strptime(v or "", "%Y%m%dT%H%M%SZ").replace(tzinfo=timezone.utc)
    except ValueError:
        return None


def _iso(v: str | None) -> datetime | None:
    try:
        d = datetime.fromisoformat((v or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def expires_at(url: str | None) -> datetime | None:
    if not url or not isinstance(url, str):
        return None
    try:
        p = urlsplit(url)
    except ValueError:
        return None
    q = {k.lower(): v[0] for k, v in parse_qs(p.query).items() if v}
    host = (p.hostname or "").lower()

    # Airtable: https://v5.airtableusercontent.com/v3/u/<n>/<n>/<expiry ms>/<sig>/...
    if _AIRTABLE_HOST.search(host):
        for seg in p.path.split("/"):
            if _EPOCH_MS.match(seg):
                return _epoch(seg)
    # S3 SigV4 / Google Cloud Storage V4: signed at X-…-Date, valid for X-…-Expires seconds
    for prefix in ("x-amz-", "x-goog-"):
        signed, ttl = _amz_date(q.get(prefix + "date")), q.get(prefix + "expires")
        if signed and ttl and ttl.isdigit():
            return signed + timedelta(seconds=int(ttl))
    # Azure SAS: se=<ISO time>
    if "sig" in q and "se" in q:
        return _iso(q["se"])
    # S3 SigV2, GCS V2, CloudFront canned policy: Expires=<epoch> next to a signature
    if "expires" in q and ({"signature", "x-amz-signature", "x-goog-signature", "key-pair-id", "googleaccessid",
                            "awsaccesskeyid", "sig", "token"} & q.keys()):
        return _epoch(q["expires"])
    return None


def expires_at_iso(url: str | None) -> str | None:
    d = expires_at(url)
    return d.strftime("%Y-%m-%dT%H:%M:%S.000Z") if d else None
