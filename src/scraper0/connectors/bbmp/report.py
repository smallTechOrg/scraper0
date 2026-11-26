"""REPORT_ISSUE — raise a Sahaya 2.0 complaint on smartoneblr.com (port of legacy portals/bbmp_complaint.py).

Returns {"tracking_id": <int>}. Expected failures raise ConnectorError with the legacy messages.
"""
from __future__ import annotations

import os
import re
import tempfile
from typing import Callable
from urllib.parse import urljoin, urlsplit

import structlog

from scraper0.connectors.base import AI, ConnectorContext, ConnectorError
from scraper0.connectors.bbmp import login as bbmp_login

log = structlog.get_logger(__name__)

MEDIA_TIMEOUT = 30
MEDIA_MAX_REDIRECTS = 5
_IMAGE_EXTS = {".jpg", ".jpeg", ".png"}

REQUIRED_FIELDS = ("category", "sub_category")
OPTIONAL_FIELDS = ("description", "media_url", "latitude", "longitude")


# ---------------------------------------------------------------------------
# Input validation (models/schemas.py ReportIssueDataSchema + AuthSchema)
# ---------------------------------------------------------------------------

def validate_report(data: dict, auth: dict | None) -> tuple[dict, str, str]:
    if not isinstance(data, dict):
        raise ConnectorError("action.data must be an object")
    errors = []
    params: dict = {}
    for f in REQUIRED_FIELDS:
        v = data.get(f)
        if v is None:
            errors.append(f"{f}: Missing data for required field.")
        elif not isinstance(v, str):
            errors.append(f"{f}: Not a valid string.")
        else:
            params[f] = v
    for f in OPTIONAL_FIELDS:
        v = data.get(f)
        if v is None:
            params[f] = None
        elif f in ("latitude", "longitude") and isinstance(v, (int, float)) and not isinstance(v, bool):
            params[f] = str(v)
        elif not isinstance(v, str):
            errors.append(f"{f}: Not a valid string.")
        else:
            params[f] = v
    if errors:
        raise ConnectorError("Invalid action data: " + "; ".join(errors))

    media = params.get("media_url")
    if media and urlsplit(media).scheme not in ("http", "https"):
        raise ConnectorError("media_url must be an http(s) URL")

    auth = auth or {}
    username, password = auth.get("username"), auth.get("password")
    if not (isinstance(username, str) and username and isinstance(password, str) and password):
        raise ConnectorError("Missing auth: username and password are required for REPORT_ISSUE")
    return params, username, password


# ---------------------------------------------------------------------------
# Media download (SSRF-guarded, temp file)
# ---------------------------------------------------------------------------

def _max_bytes() -> int:
    try:
        from scraper0.config import get_settings
        return get_settings().max_upload_mb * 1024 * 1024
    except Exception:  # noqa: BLE001
        return 40 * 1024 * 1024


def download_media(url: str) -> str:
    """Download `url` (every hop checked with is_public_url) into a temp file; return its path.
    The caller deletes the file."""
    import httpx

    from scraper0.core.fetch import is_public_url

    limit = _max_bytes()
    current = url
    try:
        with httpx.Client(timeout=MEDIA_TIMEOUT, follow_redirects=False) as client:
            for _ in range(MEDIA_MAX_REDIRECTS + 1):
                if not is_public_url(current):
                    raise ConnectorError("media_url must be a public http(s) URL")
                with client.stream("GET", current) as resp:
                    if resp.is_redirect and resp.headers.get("location"):
                        current = urljoin(current, resp.headers["location"])
                        continue
                    if resp.status_code != 200:
                        raise ConnectorError(f"Failed to download image: {url}, status: {resp.status_code}")
                    ext = os.path.splitext(os.path.basename(urlsplit(current).path))[1].lower()
                    if ext not in _IMAGE_EXTS:
                        ext = ".jpg"
                    fd, path = tempfile.mkstemp(prefix="bbmp_upload_", suffix=ext)
                    size = 0
                    try:
                        with os.fdopen(fd, "wb") as f:
                            for chunk in resp.iter_bytes(65536):
                                size += len(chunk)
                                if size > limit:
                                    raise ConnectorError("media_url file is too large")
                                f.write(chunk)
                    except BaseException:
                        os.unlink(path)
                        raise
                    log.info("bbmp_media_downloaded", bytes=size)
                    return path
            raise ConnectorError("media_url redirected too many times")
    except httpx.HTTPError as e:
        raise ConnectorError(f"Failed to download image: {url}, error: {e}") from e


# ---------------------------------------------------------------------------
# Browser flow
# ---------------------------------------------------------------------------

def _first(frame, selectors: list[str]):
    for sel in selectors:
        loc = frame.locator(sel)
        if loc.count() > 0:
            return loc.first
    return None


def submit_complaint_in_browser(params: dict, media_path: str | None, mobile: str, password: str, ai: AI,
                                storage_state: dict | None, save_state: Callable[[dict], None],
                                progress: Callable[[str, dict], None]) -> tuple[bool, object]:
    """Drive the portal. Returns (True, {"complaint_id", "timestamp"}) or (False, legacy message)."""
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            browser = bbmp_login.launch_chromium(p)
            try:
                context = browser.new_context(storage_state=storage_state) if storage_state else browser.new_context()
                page = context.new_page()
                page.route("**polyfill.io**", lambda route: route.abort())
                page.goto(bbmp_login.PORTAL_HOME, timeout=60000)
                page.wait_for_timeout(2000)

                if not bbmp_login.check_user_logged_in(page):
                    progress("Logging in to SmartOne BLR", {})
                    ok, message = bbmp_login.login_user(mobile, password, page, ai, save_state=save_state,
                                                        progress=progress)
                    if not ok:
                        return False, f"Re-login failed: {message}"
                    page.wait_for_timeout(2000)
                    if not bbmp_login.check_user_logged_in(page):
                        return False, "Session still invalid after re-login"
                progress("Logged in; filling complaint form", {})

                # javascript: href that swaps content — no navigation to wait for
                page.click("text=BBMP (SAHAYA 2.0)", timeout=60000, no_wait_after=True)
                page.wait_for_load_state("networkidle")
                page.wait_for_timeout(3000)

                frame = next((f for f in page.frames if "WssBBMPComplaintRequest" in f.url), None)
                if frame is None:
                    raise Exception("Could not find Sahaya complaint iframe")

                # category
                cat = frame.locator("select#typeId")
                cat.wait_for(state="visible", timeout=60000)
                cat.select_option(label=params["category"])
                cat.dispatch_event("change")
                cat.dispatch_event("input")
                frame.wait_for_timeout(1500)

                # subcategory
                sub = None
                for sel in ["select#subTypeId", "select#subCategoryId", "select[name*='sub']"]:
                    loc = frame.locator(sel)
                    if loc.count() > 0:
                        sub = loc
                        break
                if sub is None:
                    all_selects = frame.locator("select")
                    if all_selects.count() < 2:
                        raise Exception("Could not find subcategory dropdown")
                    sub = all_selects.nth(1)
                sub.wait_for(state="attached", timeout=60000)
                for _ in range(15):
                    if len(sub.locator("option").all_inner_texts()) > 1:
                        break
                    frame.wait_for_timeout(1000)
                if len(sub.locator("option").all_inner_texts()) <= 1:
                    raise Exception("Subcategory dropdown did not populate")
                options = sub.locator("option").evaluate_all(
                    "options => options.map(o => ({value: o.value, text: o.innerText.trim()}))")
                wanted = params["sub_category"].strip().lower()
                match = next((o for o in options if o["text"].strip().lower() == wanted and o["value"]), None)
                if match is None:
                    available = [o["text"] for o in options if o["value"]]
                    raise Exception(f"Subcategory '{params['sub_category']}' not found. Available: {available}")
                sub.select_option(value=match["value"])
                sub.dispatch_event("change")
                frame.wait_for_timeout(1000)

                # description
                desc = _first(frame, ["textarea#description", "textarea[name*='description']", "textarea",
                                      "input[name*='description']", "input[type='text']"])
                if desc is None:
                    raise Exception("Could not find description input field")
                desc.wait_for(state="attached", timeout=60000)
                desc.scroll_into_view_if_needed()
                desc.fill(params.get("description") or "")

                # image
                if media_path:
                    file_input = _first(frame, ["input#docOthFileName0", "input[name^='docOthFileName']",
                                                "input.file-input"])
                    if file_input is None:
                        raise Exception("Could not find correct file input element")
                    file_input.wait_for(state="attached", timeout=60000)
                    file_input.set_input_files(media_path)
                    for _ in range(2):
                        file_input.dispatch_event("change")
                        file_input.dispatch_event("input")
                        frame.wait_for_timeout(2000)

                # location
                checkbox = frame.locator("input[type='checkbox']")
                checkbox.wait_for(state="visible", timeout=60000)
                checkbox.check()
                addr = _first(frame, ["input#pac-inputnew", "input#pac-input", "input[placeholder='Address Search']"])
                if addr is None:
                    raise Exception("Could not find address search input")
                addr.wait_for(state="visible", timeout=60000)
                addr.fill(f"{params.get('latitude')}, {params.get('longitude')}")
                page.keyboard.press("Enter")
                frame.wait_for_timeout(2000)

                # submit
                progress("Submitting complaint", {})
                submit = frame.locator("button:has-text('Submit')")
                submit.wait_for(state="visible", timeout=60000)
                submit.scroll_into_view_if_needed()
                submit.click()
                popup_ok = page.locator("#popup_ok")
                popup_ok.wait_for(state="visible", timeout=60000)
                popup_ok.click()

                page.wait_for_selector("h5.mainHeading:has-text('Complaint Registration Acknowledgement')",
                                       timeout=60000)
                page.wait_for_selector("p.text-left", timeout=60000)
                ack = page.locator("p.text-left").first.inner_text()
                result = parse_acknowledgement(ack)
                page.wait_for_timeout(2000)
                return True, result
            finally:
                try:
                    browser.close()
                except Exception:  # noqa: BLE001
                    pass
    except PWTimeout:
        log.warning("bbmp_report_timeout", exc_info=True)
        return False, "Timeout while submitting complaint"
    except Exception as e:  # noqa: BLE001
        log.warning("bbmp_report_error", exc_info=True)
        return False, f"Error: {e}"


def parse_acknowledgement(ack_text: str) -> dict:
    id_match = re.search(r"Complaint ID\s*(\d+)", ack_text)
    time_match = re.search(r"on\s*(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2})", ack_text)
    if not id_match:
        raise Exception("Could not extract Complaint ID")
    if not time_match:
        raise Exception("Could not extract timestamp")
    return {"complaint_id": id_match.group(1), "timestamp": time_match.group(1)}


# ---------------------------------------------------------------------------
# Action
# ---------------------------------------------------------------------------

def report_issue(data: dict, ctx: ConnectorContext) -> dict:
    params, username, password = validate_report(data, ctx.auth)

    media_path = None
    if params.get("media_url"):
        ctx.progress("Downloading media", {})
        media_path = download_media(params["media_url"])
    else:
        log.warning("bbmp_report_without_media")

    try:
        storage_state = bbmp_login.load_storage_state(ctx.state, username)
        ctx.progress("Opening SmartOne BLR", {"session_reused": storage_state is not None})
        ok, result = submit_complaint_in_browser(
            params, media_path, username, password, ctx.ai,
            storage_state=storage_state,
            save_state=lambda st: bbmp_login.save_storage_state(ctx.state, username, st),
            progress=ctx.progress,
        )
    finally:
        if media_path:
            try:
                os.unlink(media_path)
            except OSError:
                pass

    if not ok:
        raise ConnectorError(result if isinstance(result, str) else "Failed to raise complaint")
    ctx.progress("Complaint registered", {"tracking_id": result["complaint_id"]})
    return {"tracking_id": int(result["complaint_id"])}
