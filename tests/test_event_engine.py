import json
from datetime import datetime, timezone

import pytest

from scraper0.events.engine import Engine, Options
from scraper0.events.classify import classify_type
from scraper0.core.dates import parse_datetime
from scraper0.events.dedupe import dedupe
from scraper0.events.extractors import extract_all, extract_jsonld
from scraper0.core.fetch import is_public_url
from scraper0.events.schema import Event

NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)


def ld(*events):
    return "<html><head><script type='application/ld+json'>%s</script></head><body></body></html>" % json.dumps(
        {"@context": "https://schema.org", "@graph": list(events)})


CUBBON = {"@type": "SocialEvent", "name": "Cubbon Park Cleanup", "startDate": "2026-03-14T10:00:00+05:30",
          "endDate": "2026-03-14T12:00:00+05:30", "url": "/e/cubbon?utm_source=x",
          "location": {"@type": "Place", "name": "Cubbon Park", "address": {"streetAddress": "Kasturba Rd",
                       "addressLocality": "Bengaluru", "postalCode": "560001"},
                       "geo": {"latitude": "12.97", "longitude": "77.59"}},
          "organizer": {"@type": "Organization", "name": "Green Org"}, "image": "/img.jpg",
          "offers": {"price": "0"}}


class FakeFetcher:
    def __init__(self, pages):
        self.pages = pages
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        return self.pages.get(url)


def test_dates():
    assert parse_datetime("March 14, 2026 | 10:00 am IST") == "2026-03-14T04:30:00.000Z"
    assert parse_datetime("2026-03-14") == "2026-03-13T18:30:00.000Z"      # midnight IST
    assert parse_datetime("04-04-26") == "2026-04-03T18:30:00.000Z"        # dd-mm-yy
    assert parse_datetime("2026-04-04T11:00:00Z") == "2026-04-04T11:00:00.000Z"
    assert parse_datetime("garbage") is None and parse_datetime(None) is None


def test_jsonld_graph_full_fields():
    [e] = extract_jsonld(ld(CUBBON, {"@type": "WebPage", "name": "x"}), "https://ex.org/events")
    assert e.name == "Cubbon Park Cleanup"
    assert e.start_time == "2026-03-14T04:30:00.000Z" and e.end_time == "2026-03-14T06:30:00.000Z"
    assert e.address == "Cubbon Park, Kasturba Rd, Bengaluru, 560001"
    assert (e.latitude, e.longitude) == (12.97, 77.59)
    assert e.link == "https://ex.org/e/cubbon"            # utm stripped, resolved absolute
    assert e.image == "https://ex.org/img.jpg" and e.price == "Free" and e.organisation == "Green Org"
    assert e.confidence == 0.9  # fixture has no description


def test_jsonld_malformed_and_cancelled():
    html = "<script type='application/ld+json'>{\"@type\":\"Event\",\"name\":\"A\",\"startDate\":\"2026-05-01\",}</script>"
    assert len(extract_jsonld(html, "https://ex.org")) == 1
    c = dict(CUBBON, eventStatus="https://schema.org/EventCancelled")
    out = Engine(FakeFetcher({})).postprocess(extract_jsonld(ld(c), "https://ex.org"), Options(now=NOW))
    assert out == []


def test_ics():
    ics = ("BEGIN:VCALENDAR\r\nBEGIN:VEVENT\r\nSUMMARY:Lake Clean\\, Ulsoor\r\nDTSTART:20260314T043000Z\r\n"
           "DTEND:20260314T063000Z\r\nLOCATION:Ulsoor Lake\r\n GATE 2\r\nGEO:12.98;77.62\r\nUID:abc\r\n"
           "END:VEVENT\r\nEND:VCALENDAR")
    [e] = extract_all(ics, "https://ex.org/cal.ics")
    assert e.name == "Lake Clean, Ulsoor" and e.start_time == "2026-03-14T04:30:00.000Z"
    assert e.address == "Ulsoor LakeGATE 2" or e.address.startswith("Ulsoor Lake")
    assert e.latitude == 12.98 and e.link.endswith("#abc")


def test_rss_events_feed():
    rss = """<?xml version="1.0"?><rss xmlns:ev="http://purl.org/rss/1.0/modules/event/"><channel>
    <item><title>Tree Walk</title><link>https://ex.org/walk</link><ev:startdate>2026-04-01T09:00:00+05:30</ev:startdate>
    <ev:location>Lalbagh</ev:location></item><item><title>News post</title><link>https://ex.org/n</link></item>
    </channel></rss>"""
    [e] = extract_all(rss, "https://ex.org/feed")
    assert e.name == "Tree Walk" and e.address == "Lalbagh"


def test_classify():
    assert classify_type("Beach Cleanup Drive at Marina") == "BEACH_CLEANUP"
    assert classify_type("Sapling plantation day") == "TREEPLANTATION"
    assert classify_type("Nandi hills trek and plog") == "TREKANDPLOG"
    assert classify_type("Concert night") == "OTHER"


def test_dedupe_cross_source_merges_and_backfills():
    a = Event(name="Cubbon Park Cleanup 2026", link="https://a.org/1", start_time="2026-03-14T04:30:00.000Z",
              address="Cubbon Park, Bengaluru")
    b = Event(name="Cubbon Park Clean-up", link="https://b.org/9", start_time="2026-03-14T04:30:00.000Z",
              image="https://b.org/i.png", description="Join us", organisation="B")
    c = Event(name="Cubbon Park Cleanup", link="https://c.org", start_time="2026-03-21T04:30:00.000Z")
    out = dedupe([a, b, c])
    assert len(out) == 2
    m = next(e for e in out if e.start_time.startswith("2026-03-14"))
    assert m.address and m.image and m.organisation == "B" and len(m.sources) == 2


def test_dedupe_keeps_distinct_events_same_day():
    a = Event(name="Lalbagh Tree Walk", link="a", start_time="2026-03-14T04:30:00.000Z", address="Lalbagh")
    b = Event(name="Beach Cleanup Marina", link="b", start_time="2026-03-14T04:30:00.000Z", address="Chennai")
    assert len(dedupe([a, b])) == 2


def test_engine_listing_to_detail_pages_filters_and_ranks():
    listing = ('<html><body><a href="/events/cleanup">c</a><a href="/events/old">o</a>'
               '<a href="/events/webinar">w</a><a href="/about">x</a></body></html>')
    past = dict(CUBBON, name="Old Drive", startDate="2025-01-01T10:00:00+05:30", endDate="2025-01-01T12:00:00+05:30", url="/events/old")
    online = {"@type": "Event", "name": "Online Webinar on Waste", "startDate": "2026-04-01T10:00:00+05:30",
              "url": "/events/webinar", "location": {"@type": "VirtualLocation", "url": "https://z"}}
    pages = {
        "https://ex.org/events": (listing, "text/html"),
        "https://ex.org/events/cleanup": (ld(dict(CUBBON, url="/events/cleanup")), "text/html"),
        "https://ex.org/events/old": (ld(past), "text/html"),
        "https://ex.org/events/webinar": (ld(online), "text/html"),
    }
    f = FakeFetcher(pages)
    res = Engine(f, llm=lambda p: "{}").run(["https://ex.org/events"], Options(portal="X", now=NOW))
    names = [e["name"] for e in res["events"]]
    assert names == ["Cubbon Park Cleanup"]
    assert res["events"][0]["type"] == "CLEANLINESS_DRIVE" and res["events"][0]["portal"] == "X"
    assert res["stats"]["dropped"]["past"] == 1 and res["stats"]["dropped"]["online"] == 1
    assert "https://ex.org/about" not in f.calls


def test_engine_skips_known_links_before_fetching():
    listing = '<html><body><a href="/events/cleanup">c</a></body></html>'
    f = FakeFetcher({"https://ex.org/events": (listing, "text/html")})
    Engine(f).run(["https://ex.org/events"], Options(now=NOW, existing_links={"https://ex.org/events/cleanup"}))
    assert "https://ex.org/events/cleanup" not in f.calls


def test_engine_llm_fallback_and_filter():
    listing = '<html><body><a href="/events/plain">p</a></body></html>'
    plain = ("<html><body><h2>River Cleanup</h2><p>" + "Join our big river cleanup with volunteers. " * 3 +
             "</p><span>2 May 2026, 7 am</span><span>Old Bridge</span></body></html>")
    def llm(prompt):
        if prompt.startswith("EXTRACT"):
            return json.dumps({"found": True, "items": [{
                "name": {"value": "River Cleanup", "source": "River Cleanup"},
                "start_time": {"value": "2026-05-02T07:00", "source": "2 May 2026, 7 am"},
                "address": {"value": "Old Bridge", "source": "Old Bridge"}}]})
        return json.dumps([{"index": 0, "select": True, "type": "FOREST_CLEANUP"}])
    f = FakeFetcher({"https://ex.org/events": (listing, "text/html"), "https://ex.org/events/plain": (plain, "text/html")})
    res = Engine(f, llm=llm).run(["https://ex.org/events"], Options(now=NOW, event_filter="cleanups"))
    [e] = res["events"]
    assert e["extraction"] == "llm" and e["type"] == "FOREST_CLEANUP" and e["confidence"] <= 0.8
    assert e["start_time"] == "2026-05-02T01:30:00.000Z" and e["link"] == "https://ex.org/events/plain"
    assert res["stats"]["crawl"][0]["recipes_learned"] == 1


def test_engine_survives_broken_source_and_bad_llm():
    f = FakeFetcher({})
    res = Engine(f, llm=lambda p: "not json").run(["https://down.example/events"], Options(now=NOW))
    assert res["events"] == []


@pytest.mark.parametrize("url", ["http://127.0.0.1/x", "http://localhost/", "http://169.254.169.254/latest",
                                 "http://10.0.0.5/", "file:///etc/passwd", "ftp://x.org/"])
def test_ssrf_guard_blocks_private(url):
    assert not is_public_url(url)


def test_heuristic_labelled_page_with_time_range():
    html = """<html><head><link rel=canonical href="/e/lake?utm_x=1"><meta property="og:image" content="/i.jpg"></head><body>
    <h1>Ulsoor Lake Cleanup</h1><p>Date: March 14, 2026 10:00 am - 12:00 pm</p>
    <p>📍 Location: Ulsoor Lake, Bengaluru</p><p>Organised by: Lake Friends</p></body></html>"""
    [e] = extract_all(html, "https://ex.org/x")
    assert e.extraction == "heuristic" and e.name == "Ulsoor Lake Cleanup"
    assert e.start_time == "2026-03-14T04:30:00.000Z" and e.end_time == "2026-03-14T06:30:00.000Z"
    assert e.address == "Ulsoor Lake, Bengaluru" and e.organisation == "Lake Friends"
    assert e.link == "https://ex.org/e/lake" and e.image == "https://ex.org/i.jpg"


def test_heuristic_ignores_ordinary_pages_and_tba_location():
    assert extract_all("<html><body><h1>About us</h1><p>We love trees.</p></body></html>", "https://ex.org/a") == []
    [e] = extract_all("<h1>Walk</h1><p>When: 2026-06-01 7:00 am</p><p>Venue: TBA</p>", "https://ex.org/w")
    assert e.address is None
