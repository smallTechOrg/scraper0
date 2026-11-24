"""SMARTONEBLR — BBMP Sahaya 2.0 complaints on smartoneblr.com (REPORT_ISSUE, TRACK_ISSUE).

Needs a Playwright Chromium (imported lazily inside the actions). The portal session is kept in
ctx.state under "storage_state:<username>"; the login captcha is solved by bbmp.captcha.
"""
from scraper0.connectors.base import Connector, register
from scraper0.connectors.bbmp.report import report_issue
from scraper0.connectors.bbmp.track import track_issue

CONNECTOR = register(Connector(
    name="SMARTONEBLR",
    source="GOV_ISSUE_PORTAL",
    description="BBMP Sahaya 2.0 complaints on SmartOne BLR: report an issue, track its status",
    actions={"REPORT_ISSUE": report_issue, "TRACK_ISSUE": track_issue},
    needs_browser=True,
))
