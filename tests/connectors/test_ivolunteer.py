import json
from urllib.parse import parse_qs

from scraper0.connectors import ivolunteer as iv

from .conftest import FakeAI, FakeResponse, fixture_json, fixture_text

SEARCH = fixture_json("ivolunteer_search.json")
OPPS = SEARCH["opportunities"]
NO_ADDR = OPPS[-1]   # the one opportunity with an empty Location
assert NO_ADDR["Location"] == ""


def test_parse_opportunity_exact():
    assert iv.parse_opportunity(OPPS[0]) == {
        "name": "Fund Raising Strategy",
        "image": "https://cdn0.handsonconnect.org/00006c/opportunities/3c88b9a80582498eb3ee77120949209f_thumb.jpg",
        "portal": "IVOLUNTEERIN",
        "start_time": "2026-09-17T00:01:00.000Z",
        "end_time": "2026-12-31T18:29:59.000Z",
        "address": "New Delhi, DL 110032",
        "link": "https://www.ivolunteer.in/opportunity/a0COX00004tt66h2AA",
        "organisation": "Edspectrum Foundation",
        "type": "OTHER",
    }
    ev = iv.parse_opportunity(NO_ADDR)
    assert ev["address"] is None and ev["image"] == iv.PLACEHOLDER_IMAGE


def test_parse_helpers():
    assert iv.parse_end_date(None, "S") == "S"
    assert iv.parse_end_date("31-02-26", "S") == "S"
    assert iv.parse_datetime("garbage") is None
    assert iv.opp_link({"OccurrenceUrl": "/opportunity/X?utm_source=a"}) == "https://www.ivolunteer.in/opportunity/X"


def test_address_extraction_from_detail():
    assert iv.address_from_detail_html(fixture_text("ivolunteer_detail.html")) is None   # real page, no address
    assert iv.address_from_detail_html(
        '<input class="occurrence-location-name" value=" Cubbon Park, Bengaluru "/>') == "Cubbon Park, Bengaluru"
    page = '<div id="content-page"><p>Hello</p><p>📍 Mode: In-person (locations across Bengaluru)</p></div>'
    assert iv.address_from_detail_html(page) == "Bengaluru"
    assert iv.extract_address_from_text("Venue: Online") is None
    assert iv.extract_address_from_text("Where: to be shared later") is None
    assert iv.extract_address_from_text("Intro\nLocation: Juhu Beach, Mumbai.") == "Juhu Beach, Mumbai"


def _routes(detail_html):
    def search(method, url, kw):
        rows = int(parse_qs(kw["data"])["currentRows"][0])
        return FakeResponse(json_data=SEARCH if rows == 0 else {"opportunities": [], "total": SEARCH["total"]})
    return {iv.SEARCH_API: search, iv.opp_link(NO_ADDR): FakeResponse(detail_html)}


def test_fetch_drops_events_without_address(fake_net, make_ctx):
    s = fake_net(_routes(fixture_text("ivolunteer_detail.html")))
    ctx = make_ctx()
    out = iv.fetch_events({}, ctx)
    assert len(out["events"]) == len(OPPS) - 1
    assert iv.opp_link(NO_ADDR) not in [e["link"] for e in out["events"]]
    assert s.urls("GET") == [iv.opp_link(NO_ADDR)]   # only the address-less one is enriched
    assert ctx.progress_log[-1][1]["dropped_no_address"] == 1
    assert s.calls[0][2]["headers"]["X-Requested-With"] == "XMLHttpRequest"
    assert s.calls[0][2]["interface"] == "10.0.0.5"


def test_fetch_enriches_missing_address(fake_net, make_ctx):
    html = fixture_text("ivolunteer_detail.html").replace(
        "Issue Area(s)", "Location: Dharavi Municipal School, Mumbai</p><p>Issue Area(s)")
    fake_net(_routes(html))
    out = iv.fetch_events({}, make_ctx())
    ev = next(e for e in out["events"] if e["link"] == iv.opp_link(NO_ADDR))
    assert ev["address"] == "Dharavi Municipal School, Mumbai"
    assert len(out["events"]) == len(OPPS)


def test_fetch_with_filter(fake_net, make_ctx):
    fake_net(_routes(fixture_text("ivolunteer_detail.html")))
    n = len(OPPS)   # 22 → batches 8, 8, 6

    def reply(prompt):
        if "Teaching & Mentoring Volunteer-Mumbai" in prompt:   # third batch: pick the address-less one
            return json.dumps([{"index": 5, "select_event": True, "type": "VOLUNTEERING"},
                               {"index": 0, "select_event": True, "type": "WORKSHOP"}])
        return "[]"
    ai = FakeAI(reply)
    out = iv.fetch_events({"category_filter": "teaching"}, make_ctx(ai=ai))
    assert len(ai.text_prompts) == 3 and n == 22
    # the address-less pick is dropped after failed enrichment; index 0 of batch 3 = OPPS[16]
    assert [e["name"] for e in out["events"]] == [OPPS[16]["Title"]]
    assert out["events"][0]["type"] == "WORKSHOP"
