"""TRACK_ISSUE — complaint status on smartoneblr.com (port of legacy portals/complaint_scraper.py and
handle_track_issue). No login needed. Returns {"status": "OPEN"|"CLOSED", "meta_data": {...}}.
"""
from __future__ import annotations

import re

import structlog
from bs4 import BeautifulSoup

from scraper0.connectors.base import ConnectorContext, ConnectorError
from scraper0.connectors.bbmp import login as bbmp_login

log = structlog.get_logger(__name__)

DETAILS_URL = "https://www.smartoneblr.com/WssBBMPComplaintRequestDetails.htm"

STATUS_MAP = {"registered": "OPEN", "closed": "CLOSED"}
STAFF_NAME_FIELDS = ["Staff Name", "Assigned To", "Handler", "Assigned Officer"]


class ComplaintNotFound(Exception):
    pass


class FetchTimeout(Exception):
    pass


def validate_track(data: dict) -> str:
    if not isinstance(data, dict):
        raise ConnectorError("action.data must be an object")
    tid = data.get("tracking_id")
    if tid is None or tid == "":
        raise ConnectorError("Missing tracking_id in action_data")
    if isinstance(tid, bool) or not isinstance(tid, (str, int)):
        raise ConnectorError("Invalid action data: tracking_id: Not a valid string.")
    tid = str(tid).strip()
    if not re.fullmatch(r"[A-Za-z0-9-]+", tid):   # it is interpolated into a selector
        raise ConnectorError("Invalid tracking_id")
    return tid


def _extract_panel_data(panel) -> dict:
    data = {}
    for group in panel.find_all("div", class_="form-group"):
        label = group.find("label")
        if not label:
            continue
        value_container = label.find_next("div")
        if not value_container:
            continue
        key = label.get_text(strip=True).replace("\xa0", " ")
        if key:
            data[key] = value_container.get_text(strip=True)
        img = group.find("img")
        if img and img.get("src"):
            data["Image URL"] = img["src"]
    return data


def parse_complaint_details(html: str) -> dict:
    soup = BeautifulSoup(html, "html.parser")
    panels = soup.find_all("div", class_="panel panel-primary")
    if len(panels) < 2:
        raise ComplaintNotFound("Complaint details not found. Invalid ID?")
    return {"complaint_details": {}, "staff_details": _extract_panel_data(panels[1])}


def to_payload(staff: dict) -> dict:
    """Legacy mapping of the staff panel → {"status", "meta_data"}."""
    try:
        raw_status = staff["Grievance Status"].strip().lower()
    except KeyError:
        raise ConnectorError("Complaint status not found on the portal page") from None
    if raw_status not in STATUS_MAP:
        raise ConnectorError(f"Unknown grievance status: {raw_status}")
    meta: dict = {"remarks": staff.get("Staff Remarks")}
    for f in STAFF_NAME_FIELDS:
        if staff.get(f):
            meta["staff_name"] = staff[f]
            break
    meta["mobile_number"] = staff.get("Contact Details")
    return {"status": STATUS_MAP[raw_status], "meta_data": meta}


def fetch_status_html(tracking_id: str) -> str:
    """Search the complaint on the portal and return the result page HTML (Playwright)."""
    from playwright.sync_api import TimeoutError as PWTimeout
    from playwright.sync_api import sync_playwright

    try:
        with sync_playwright() as p:
            browser = bbmp_login.launch_chromium(p)
            try:
                page = browser.new_context().new_page()
                page.goto(DETAILS_URL, timeout=60000)
                page.fill("#complainantNo", tracking_id)
                page.click("button:has-text('search')")
                page.wait_for_selector(
                    f"div.panel.panel-primary:nth-of-type(1) div.col-sm-6:has-text('{tracking_id}')",
                    timeout=60000)
                return page.content()
            finally:
                browser.close()
    except PWTimeout as e:
        raise FetchTimeout() from e


def track_issue(data: dict, ctx: ConnectorContext) -> dict:
    tracking_id = validate_track(data)
    ctx.progress("Looking up complaint", {"tracking_id": tracking_id})
    try:
        html = fetch_status_html(tracking_id)
        staff = parse_complaint_details(html)["staff_details"]
    except FetchTimeout:
        raise ConnectorError("Request timed out while fetching complaint details.") from None
    except ComplaintNotFound as e:
        raise ConnectorError(str(e)) from None
    except ConnectorError:
        raise
    except Exception as e:  # noqa: BLE001
        log.warning("bbmp_track_error", exc_info=True)
        raise ConnectorError(f"Unexpected error: {e}") from e
    payload = to_payload(staff)
    ctx.progress(f"Complaint is {payload['status']}", {"status": payload["status"]})
    return payload
