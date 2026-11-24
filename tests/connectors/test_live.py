"""Live smoke tests against the real event portals (deselected by default; run with -m network)."""
import re

import pytest

from scraper0.connectors import ivolunteer, mybharat, teameverest

FIELDS = {"name", "image", "portal", "start_time", "end_time", "address", "link", "organisation", "type"}
ISO = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}\.000Z$")

pytestmark = pytest.mark.network


def _check(events, portal):
    assert events, f"{portal}: no events returned"
    for e in events:
        assert set(e) == FIELDS
        assert e["portal"] == portal
        assert e["link"].startswith("http") and "utm_" not in e["link"]
        assert e["start_time"] is None or ISO.match(e["start_time"])


@pytest.mark.parametrize("module", [teameverest, mybharat, ivolunteer], ids=lambda m: m.PORTAL)
def test_live_fetch_events_without_filter(module, make_ctx):
    ctx = make_ctx()
    out = module.fetch_events({}, ctx)
    _check(out["events"], module.PORTAL)
    if module is ivolunteer:
        assert all(e["address"] for e in out["events"])
