"""Shared helpers for the event-portal connectors (TEAMEVEREST, MYBHARATGOVIN, IVOLUNTEERIN).

Ported from the legacy portals/*.py + portals/llm_client.py:
- LLM relevance filter / type classifier, single-page and batch, using the legacy prompts
  (prompts/event_filter*.txt) through ctx.ai.text(). Any AI or parse failure falls back to the
  legacy "reject" defaults.
- existing-links dedupe callback to the caller's backend (X-Backend-Url, validated upstream).
- curl_cffi session + IPv4 `interface=` binding (some portals block or misroute IPv6 egress).
"""
from __future__ import annotations

import json
import re
import socket
from pathlib import Path

import structlog
from curl_cffi import requests

from scraper0.services.ai import BudgetExceeded, NoProvider
from scraper0.connectors.base import AI
from scraper0.core.urls import strip_utm_params  # noqa: F401  (re-exported for the connectors)

log = structlog.get_logger(__name__)

# Mirrors legacy models.enums.EventTypeEnum (same order: it is rendered into the prompt).
EVENT_TYPES: list[str] = [
    "CLEANLINESS_DRIVE", "BEACH_CLEANUP", "ROAD_CLEANUP", "FOREST_CLEANUP", "TREEPLANTATION",
    "TREKANDPLOG", "VOLUNTEERING", "WORKSHOP", "OTHER",
]
OTHER = "OTHER"

_PROMPTS = Path(__file__).parent / "prompts"
SINGLE_PROMPT = (_PROMPTS / "event_filter.txt").read_text(encoding="utf-8")
BATCH_PROMPT = (_PROMPTS / "event_filter_batch.txt").read_text(encoding="utf-8")

BATCH_SIZE = 8            # events per LLM call (legacy)
SINGLE_TEXT_CAP = 6000    # chars of page text sent for a single-event decision (legacy)
BATCH_TEXT_CAP = 1500     # chars per event inside a batch (legacy)
EXISTING_LINKS_TIMEOUT = 10


# ---------------------------------------------------------------------------
# HTTP session + IPv4 binding
# ---------------------------------------------------------------------------

def make_session() -> requests.Session:
    return requests.Session(impersonate="chrome120")


def local_ipv4() -> str | None:
    """The container's IPv4 address, used as curl's `interface=` so requests leave over IPv4."""
    try:
        return socket.gethostbyname(socket.gethostname())
    except OSError as e:
        log.warning("local_ipv4_unavailable", error=str(e))
        return None


def bind_kwargs(ipv4: str | None) -> dict:
    """`interface=` kwarg for curl_cffi, skipped for loopback (binding there can't reach the internet)."""
    if ipv4 and not ipv4.startswith("127."):
        return {"interface": ipv4}
    return {}


# ---------------------------------------------------------------------------
# Backend dedupe callback
# ---------------------------------------------------------------------------

def fetch_existing_links(portal: str, backend_url: str | None) -> set[str]:
    """Links the caller's backend already stores for `portal` (GET {backend}/api/v1/events/links).

    Returns an empty set when no backend_url was given or on any failure, so callers proceed
    without skipping anything (legacy behaviour).
    """
    if not backend_url:
        return set()
    try:
        resp = requests.get(f"{backend_url}/api/v1/events/links", params={"portal": portal},
                            timeout=EXISTING_LINKS_TIMEOUT)
        if resp.ok:
            data = resp.json().get("data", [])
            log.info("existing_links_loaded", portal=portal, count=len(data))
            return set(data)
        log.warning("existing_links_failed", portal=portal, status=resp.status_code)
    except Exception as e:  # noqa: BLE001 — dedupe is best-effort
        log.warning("existing_links_failed", portal=portal, error=str(e))
    return set()


# ---------------------------------------------------------------------------
# LLM filter
# ---------------------------------------------------------------------------

def _loads_tolerant(raw: str):
    """Legacy cleanup (strip whitespace/backticks, drop a leading 'json' fence tag), then json.loads.
    If that fails, retry on the outermost {...} / [...] span (models sometimes add prose)."""
    text = raw.strip().strip("`").replace("json\n", "")
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        pass
    m = re.search(r"(\[.*\]|\{.*\})", text, re.DOTALL)
    if not m:
        raise json.JSONDecodeError("no JSON found", text, 0)
    return json.loads(m.group(1))


def _fill(template: str, event_filter: str, category_filter: str, key: str, content: str) -> str:
    return (template
            .replace("{event_types}", ", ".join(EVENT_TYPES))
            .replace("{event_filter}", event_filter)
            .replace("{category_filter}", category_filter)
            .replace(key, content))


def classify_single(ai: AI, page_text: str, event_filter: str, category_filter: str) -> dict:
    """Does this event page match the filter, and which type is it? Default: reject, OTHER."""
    default = {"select_event": False, "type": OTHER}
    try:
        prompt = _fill(SINGLE_PROMPT, event_filter, category_filter, "{event_content}", page_text[:SINGLE_TEXT_CAP])
        result = _loads_tolerant(ai.text(prompt))
        if not isinstance(result, dict):
            log.warning("llm_filter_not_object")
            return default
        if result.get("type") not in EVENT_TYPES:
            result["type"] = OTHER
        return result
    except json.JSONDecodeError:
        log.warning("llm_filter_bad_json")
        return default
    except (BudgetExceeded, NoProvider):
        raise                      # surface "out of budget / no provider" instead of silently returning nothing
    except Exception as e:  # noqa: BLE001 — legacy: any AI failure rejects the event
        log.warning("llm_filter_error", error=str(e), error_type=type(e).__name__)
        return default


def classify_batch(ai: AI, event_texts: list[str], event_filter: str, category_filter: str) -> list[dict]:
    """One LLM call for a batch of event summaries. Returns [{"index", "select_event", "type"}];
    on any failure every event is rejected."""
    default = [{"index": i, "select_event": False, "type": OTHER} for i in range(len(event_texts))]
    block = "\n\n".join(f"--- Event {i} ---\n{t[:BATCH_TEXT_CAP]}" for i, t in enumerate(event_texts))
    try:
        results = _loads_tolerant(ai.text(_fill(BATCH_PROMPT, event_filter, category_filter, "{events_batch}", block)))
        if not isinstance(results, list):
            log.warning("llm_batch_not_list")
            return default
        out = []
        for r in results:
            if not isinstance(r, dict):
                continue
            if r.get("type") not in EVENT_TYPES:
                r["type"] = OTHER
            out.append(r)
        return out
    except json.JSONDecodeError:
        log.warning("llm_batch_bad_json")
        return default
    except (BudgetExceeded, NoProvider):
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("llm_batch_error", error=str(e), error_type=type(e).__name__)
        return default


def verdicts_by_index(results: list[dict]) -> dict[int, dict]:
    """Map batch results by their integer index; entries without a usable index are ignored."""
    out: dict[int, dict] = {}
    for r in results:
        idx = r.get("index")
        if isinstance(idx, bool):
            continue
        if isinstance(idx, str) and idx.strip().isdigit():
            idx = int(idx)
        if isinstance(idx, int):
            out[idx] = r
    return out


def is_selected(verdict: dict) -> bool:
    """Legacy truthiness of select_event, except that the strings "false"/"no"/"0" count as no."""
    v = verdict.get("select_event", False)
    if isinstance(v, str):
        return v.strip().lower() in ("true", "yes", "1")
    return bool(v)


def filter_in_batches(ai: AI, items: list, to_text, event_filter: str, category_filter: str,
                      progress=None, label: str = "events") -> list[tuple[object, str]]:
    """Run the batch filter over `items`; return [(item, type)] for the selected ones, in order."""
    selected: list[tuple[object, str]] = []
    total = len(items)
    for start in range(0, total, BATCH_SIZE):
        batch = items[start:start + BATCH_SIZE]
        log.info("llm_batch", start=start + 1, end=start + len(batch), total=total)
        verdicts = verdicts_by_index(classify_batch(ai, [to_text(x) for x in batch], event_filter, category_filter))
        for i, item in enumerate(batch):
            v = verdicts.get(i, {"select_event": False})
            if is_selected(v):
                selected.append((item, v.get("type", OTHER)))
        if progress:
            progress(f"Filtered {min(start + BATCH_SIZE, total)}/{total} {label}",
                     {"done": min(start + BATCH_SIZE, total), "total": total, "matched": len(selected)})
    return selected
