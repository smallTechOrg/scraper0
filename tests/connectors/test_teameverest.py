import pytest

from scraper0.connectors import teameverest as te
from scraper0.connectors.base import ConnectorError

from .conftest import FakeAI, FakeResponse, fixture_text

URL0 = "https://www.teameverest.ngo/events/in-person-volunteering/make-a-tangram"
URL1 = "https://www.teameverest.ngo/events/in-person-volunteering/popsicle-science-puzzles-chen"

EVENT0 = {
    "name": "Make Puzzle Sets and Help Students Learn through Play!",
    "image": ("https://v5.airtableusercontent.com/v3/u/59/59/1795507200000/FfpxctxGakmIQJBA5wZy4A/MuJk1LaGraYqAkRLBWZR"
              "1500yUCD1374ZGTjEe7PgrN4Owk9mF27DKEpJN44UUOO6zaPF8EDZIQHKHxQRYBuUNb8Fut3zNxC2LpiUqEZ0ccwKs5FjWXs829kvON"
              "mzQwIx0ten7UQLTfOKJgz9LPvfN5dhi97ghSSVOv5tU2hZLg/K48Vawr25eNNMS1K_w9ZbyMZJWWY9ElhjIc9HQ-jPVU"),
    "portal": "TEAMEVEREST",
    "start_time": "2026-11-28T05:30:00.000Z",
    "end_time": "2026-11-28T07:30:00.000Z",
    "address": "Father Agnel AshramBandstand Promenade, Mount Mary, Bandra West, Mumbai",
    "link": "http://www.teameverest.ngo/events/in-person-volunteering/make-a-tangram",
    "organisation": "Team Everest",
    "type": "In Person Volunteering",
}
EVENT1_PARTIAL = {
    "name": "Make Science Fun for School Students!",
    "portal": "TEAMEVEREST",
    "start_time": "2026-12-12T04:30:00.000Z",
    "end_time": "2026-12-12T06:30:00.000Z",
    "address": "Team Everest Office,13/19, Bujankarao Street, Radha Nagar, Chrompet.",
    "link": "http://www.teameverest.ngo/events/in-person-volunteering/popsicle-science-puzzles-chen",
    "organisation": "Team Everest",
    "type": "In Person Volunteering",
}


def routes():
    return {
        te.EVENTS_URL: FakeResponse(fixture_text("teameverest_list.html")),
        URL0: FakeResponse(fixture_text("teameverest_detail_0.html")),
        URL1: FakeResponse(fixture_text("teameverest_detail_1.html")),
    }


def test_list_page_yields_unique_inperson_urls():
    assert te.get_inperson_event_urls(fixture_text("teameverest_list.html")) == [URL0, URL1]


def test_parse_detail_fixtures_exact():
    assert te.parse_event_detail(fixture_text("teameverest_detail_0.html"), URL0) == EVENT0
    ev1 = te.parse_event_detail(fixture_text("teameverest_detail_1.html"), URL1)
    assert {k: ev1[k] for k in EVENT1_PARTIAL} == EVENT1_PARTIAL
    assert ev1["image"].startswith("https://v5.airtableusercontent.com/")


def test_parse_helpers():
    assert te.parse_ist_to_iso("March 14, 2026 | 10:00 am IST") == "2026-03-14T04:30:00.000Z"
    assert te.parse_ist_to_iso("March 14, 2026") == "2026-03-13T18:30:00.000Z"
    assert te.parse_ist_to_iso("soon") is None
    assert te.extract_hours("You just have to commit 3 hours of your time") == 3
    assert te.extract_hours("a while") == 2


def test_parse_detail_fallbacks():
    html = ('<html><head><title>Foo</title><meta property="og:title" content="In Person Volunteering | X"/>'
            '<meta property="og:url" content="https://t.ngo/e?utm_source=a&x=1"/></head><body></body></html>')
    ev = te.parse_event_detail(html, "https://t.ngo/events/in-person-volunteering/plant-trees-now")
    assert ev["name"] == "Plant Trees Now"
    assert ev["link"] == "https://t.ngo/e?x=1"
    assert ev["organisation"] == "Team Everest"
    assert ev["start_time"] is None and ev["end_time"] is None and ev["address"] is None and ev["image"] is None


def test_fetch_without_filter(fake_net, make_ctx):
    s = fake_net(routes())
    ctx = make_ctx(backend_url="https://backend.example")
    out = te.fetch_events({}, ctx)
    assert out["events"][0] == EVENT0
    assert [e["name"] for e in out["events"]] == [EVENT0["name"], EVENT1_PARTIAL["name"]]
    assert s.existing_calls == [("TEAMEVEREST", "https://backend.example")]
    assert all(kw["interface"] == "10.0.0.5" and kw["timeout"] == 15 for _, _, kw in s.calls)
    assert ctx.progress_log[-1][0] == "2 events matched"


def test_fetch_skips_existing_links_without_fetching(fake_net, make_ctx):
    # the backend stores og:url (http://), the list page links https:// — both must dedupe
    s = fake_net(routes(), existing={EVENT0["link"]})
    out = te.fetch_events({}, make_ctx())
    assert [e["name"] for e in out["events"]] == [EVENT1_PARTIAL["name"]]
    assert URL0 not in s.urls()


def test_fetch_with_llm_filter(fake_net, make_ctx):
    fake_net(routes())
    ai = FakeAI(['{"select_event": false, "type": "WORKSHOP"}', '```json\n{"select_event": true, "type": "WORKSHOP"}```'])
    out = te.fetch_events({"event_filter": "science", "category_filter": "education"}, make_ctx(ai=ai))
    assert len(out["events"]) == 1
    assert out["events"][0]["name"] == EVENT1_PARTIAL["name"]
    assert out["events"][0]["type"] == "WORKSHOP"
    assert "Make Puzzle Sets" in ai.text_prompts[0]


def test_fetch_llm_failure_rejects_all(fake_net, make_ctx):
    fake_net(routes())
    out = te.fetch_events({"event_filter": "x"}, make_ctx(ai=FakeAI(RuntimeError("no provider"))))
    assert out == {"events": []}


def test_detail_not_200_is_skipped_and_list_error_raises(fake_net, make_ctx):
    r = routes()
    r[URL1] = FakeResponse("gone", 404)
    fake_net(r)
    assert len(te.fetch_events({}, make_ctx())["events"]) == 1

    fake_net({te.EVENTS_URL: FakeResponse("down", 503)})
    with pytest.raises(ConnectorError, match="^Unexpected error: HTTP 503"):
        te.fetch_events({}, make_ctx())


def test_loopback_ipv4_not_bound(fake_net, make_ctx):
    s = fake_net(routes(), ipv4="127.0.0.1")
    te.fetch_events({}, make_ctx())
    assert all("interface" not in kw for _, _, kw in s.calls)
