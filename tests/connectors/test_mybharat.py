import json

import pytest

from scraper0.connectors import mybharat as mb
from scraper0.connectors.base import ConnectorError

from .conftest import FakeAI, FakeResponse, fixture_json

PAGE = fixture_json("mybharat_page1.json")
RECORDS = [r["_source"] for r in PAGE["records"]]


def test_parse_records_exact():
    assert mb.parse_record(RECORDS[0]) == {
        "name": "Morbi- Viksit Bharat Yuva Samvad- Samvidhan Diwas Edition",
        "image": "https://cdn-prod.mybharats.in/events/17951872234091f5o1j5n4234726.jpg",
        "portal": "MYBHARATGOVIN",
        "start_time": "2026-11-25T04:30:00.000Z",
        "end_time": "2026-11-25T05:30:00.000Z",
        "address": "363641",
        "link": ("https://mybharat.gov.in/pages/event_detail?event_name=Morbi--Viksit-Bharat-Yuva-Samvad--Samvidhan-"
                 "Diwas-Edition&key=945234726286"),   # utm_medium=whatsapp stripped
        "organisation": "Mera Yuva Bharat (MYBharat)",
        "type": "OTHER",
    }
    ev = mb.parse_record(RECORDS[1])
    assert ev["address"] == "Suri Vidyasagar College, 731101"
    assert (ev["start_time"], ev["end_time"]) == ("2026-11-25T05:30:00.000Z", "2026-11-25T11:30:00.000Z")
    assert all("utm_" not in mb.parse_record(r)["link"] for r in RECORDS)


def test_parse_fallbacks():
    rec = {"id": 7, "event_name": "Clean Park Drive", "event_from_date": "2026-03-28", "event_to_date": "bad",
           "event_address": "  Park Rd ", "district": "Pune", "state": "MH", "partner_name": "P"}
    ev = mb.parse_record(rec)
    assert ev["link"] == "https://mybharat.gov.in/pages/event_detail?event_name=Clean-Park-Drive&key=7"
    assert ev["start_time"] == "2026-03-27T18:30:00.000Z" and ev["end_time"] is None
    assert ev["address"] == "Park Rd, Pune, MH"
    assert ev["organisation"] == "P" and ev["image"] is None
    assert mb.parse_record({})["organisation"] == "MY Bharat"


def test_build_query_and_event_text():
    q = mb.build_query("Ongoing", page=3, today="2026-11-24")
    assert q["page"] == "3" and q["limit"] == 12
    assert q["query"]["bool"]["must"] == [{"term": {"type": "vo"}}, {"range": {"event_to_date": {"gte": "2026-11-24"}}},
                                          {"range": {"event_from_date": {"lte": "2026-11-24"}}}]
    txt = mb.build_event_text(RECORDS[0])
    assert txt.startswith("Event Name: Morbi- Viksit")
    assert "Description: Rajkot- Viksit Bharat Yuva Samvad" in txt and "&amp;" not in txt


def _search_route(pages_by_tab):
    """POST handler serving pages per tab (detected from the query)."""
    def handler(method, url, kw):
        body = kw["json"]
        tab = "Ongoing" if any("event_to_date" in f.get("range", {}) for f in body["query"]["bool"]["must"]) \
            else "Upcoming"
        pages = pages_by_tab.get(tab, [])
        n = int(body["page"])
        return FakeResponse(json_data=pages[n - 1] if n <= len(pages) else {"total": 0, "records": []})
    return handler


def test_fetch_paginates_and_dedupes_without_filter(fake_net, make_ctx):
    p1 = {"total": 14, "records": PAGE["records"]}
    p2 = {"total": 14, "records": PAGE["records"][:2]}   # duplicates of page 1 → deduped by id
    ongoing = {"total": 1, "records": PAGE["records"][:1]}
    s = fake_net({mb.SEARCH_API: _search_route({"Upcoming": [p1, p2], "Ongoing": [ongoing]})},
                 existing={mb.record_link(RECORDS[0])})
    out = mb.fetch_events({}, make_ctx())
    assert len(out["events"]) == 12
    assert out["events"][0] == mb.parse_record(RECORDS[0])   # no filter → legacy: no backend dedupe
    assert s.existing_calls == []
    assert len(s.calls) == 3
    kw = s.calls[0][2]
    assert kw["interface"] == "10.0.0.5" and kw["timeout"] == 15 and kw["headers"]["Origin"] == mb.BASE_URL


def test_pagination_cap(fake_net, make_ctx, monkeypatch):
    monkeypatch.setattr(mb, "MAX_PAGES", 3)
    endless = {"total": 10_000, "records": PAGE["records"][:1]}
    s = fake_net({mb.SEARCH_API: lambda m, u, kw: FakeResponse(json_data=endless)})
    mb.fetch_events({}, make_ctx())
    assert len(s.calls) == 6   # 3 pages × 2 tabs


def test_fetch_with_filter_dedupes_then_batches(fake_net, make_ctx):
    p1 = {"total": 12, "records": PAGE["records"]}
    fake_net({mb.SEARCH_API: _search_route({"Upcoming": [p1]})},
             existing={mb.record_link(RECORDS[0]), mb.record_link(RECORDS[1])})
    # 10 new records → batches of 8 + 2
    ai = FakeAI([json.dumps([{"index": 0, "select_event": True, "type": "VOLUNTEERING"},
                             {"index": 5, "select_event": True, "type": "BOGUS"}]),
                 "this is not json"])
    out = mb.fetch_events({"event_filter": "youth"}, make_ctx(ai=ai, backend_url="https://b"))
    assert [e["name"] for e in out["events"]] == [RECORDS[2]["event_name"], RECORDS[7]["event_name"]]
    assert [e["type"] for e in out["events"]] == ["VOLUNTEERING", "OTHER"]
    assert len(ai.text_prompts) == 2
    assert RECORDS[0]["event_name"] not in ai.text_prompts[0]   # skipped before the LLM


def test_fetch_error_maps_to_connector_error(fake_net, make_ctx):
    fake_net({mb.SEARCH_API: FakeResponse("boom", 500)})
    with pytest.raises(ConnectorError, match="^Unexpected error:"):
        mb.fetch_events({}, make_ctx())


def test_empty_returns_no_events(fake_net, make_ctx):
    fake_net({mb.SEARCH_API: FakeResponse(json_data={"total": 0, "records": []})})
    assert mb.fetch_events({"event_filter": "x"}, make_ctx()) == {"events": []}
