"""JavaScript-rendered sites: render the shell, read the JSON API it calls, learn that API, then call it directly."""
import json

import pytest
from conftest import json_calls

from scraper0.core import Crawler, CrawlOptions, EntitySpec, FieldSpec, Store
from scraper0.core import json_extract as jx
from scraper0.core.entities import EVENT_SPEC
from scraper0.core.render import RenderResult, is_js_shell
from scraper0.events.classify import address_is_online, classify_type

SHELL = '<html><head><title>Acts</title></head><body><div id="root"></div><script src="/static/main.js"></script></body></html>'
ORGS = [{"id": "o1", "name": "Lake Friends", "type": "ngo"}, {"id": "o2", "name": "Green BLR", "type": "ngo"},
        {"id": "o3", "name": "Unused Org", "type": "ngo"}]


def activity(i, name, date, time_, addr, org, online=False):
    return {"id": f"act-{i:04d}", "slug": f"{name.lower().replace(' ', '-')}-{i}", "name": name, "changemaker_id": org, "other_organizer_name": None,
            "description": f"<p>Join <b>{name}</b><br>Bring water.</p>", "date": date, "date_end": None, "time": None,
            "start_time": time_, "end_time": None, "image_url": None, "media_urls": [f"/files/{i}.webp"],
            "location": {"address": addr, "latitude": 12.9 + i / 100, "longitude": 77.6, "is_online": online},
            "registration_form_fields": [{"name": "phone", "placeholder": "Your phone"}], "status": "published"}


ACTS = [activity(1, "Ulsoor Lake Cleanup", "2026-12-05", "07:00", "Ulsoor Lake, Bengaluru", "o1"),
        activity(2, "Cubbon Tree Walk", "2026-12-06", "16:45", "Cubbon Park", "o2"),
        activity(3, "Waste Policy Webinar", "2026-12-07", "18:00", "Online", "o1", online=True),
        activity(4, "Ongoing Petition", None, None, "Online", "o2")]            # no date → not an event we can place


def rendered_html(url="https://acts.org/"):
    if url.endswith("/activities"):           # the list page links to real detail pages
        cards = "".join(f'<a href="/activities/{a["slug"]}">{a["name"]}</a>' for a in ACTS)
        return f"<html><body><h1>All activities</h1>{cards}</body></html>"
    cards = "".join(f'<a href="/activities?scrollTo={a["id"]}&activity_id={a["id"]}">{a["name"]}</a>' for a in ACTS)
    return f"<html><body><h1>Activities</h1>{cards}<a href='/about'>About</a></body></html>"


class JSFetcher:
    from conftest import no_cookie as cookie, request_via_get as request  # noqa: E402
    """Static HTML is an empty shell; render() returns the real DOM plus the JSON the page fetched."""
    def __init__(self, acts=None, api_up=True, has_browser=True):
        self.acts = acts if acts is not None else ACTS
        self.api_up, self.gets, self.renders = api_up, [], []
        self.render_hosts = set()
        if not has_browser:
            self.render = None

    def get(self, url):
        self.gets.append(url)
        if url == "https://acts.org/":
            return SHELL, "text/html"
        if url == "https://acts.org/api/activities" and self.api_up:
            return json.dumps(self.acts), "application/json"
        if url == "https://acts.org/api/changemakers":
            return json.dumps(ORGS), "application/json"
        return None

    def render(self, url):
        self.renders.append(url)
        jr = [("https://acts.org/api/changemakers", json.dumps(ORGS)),
              ("https://acts.org/api/activities", json.dumps(self.acts))]
        return RenderResult(html=rendered_html(url), final_url=url, json_responses=jr, requests=json_calls(jr))


@pytest.fixture
def store():
    return Store()


def no_llm(prompt):
    raise AssertionError("LLM must not be needed for a well-named JSON API")


def test_shell_detection():
    assert is_js_shell(SHELL, "Acts")
    assert not is_js_shell("<html><body>" + "<p>real words here</p>" * 30 + "<script src=x></script></body></html>", "real words here " * 30)
    assert not is_js_shell("BEGIN:VCALENDAR", "")


def _learn(fetcher=None, acts=None, hrefs=None):
    from scraper0.core import plan_learn
    jr = [("https://acts.org/api/changemakers", json.dumps(ORGS)), ("https://acts.org/api/activities", json.dumps(acts or ACTS))]
    hrefs = hrefs if hrefs is not None else [f"https://acts.org/activities?scrollTo={a['id']}&activity_id={a['id']}" for a in ACTS]
    return plan_learn.learn_plan(EVENT_SPEC, json_calls(jr), "https://acts.org/", fetcher or JSFetcher(), hrefs, [])


def test_api_plan_learned_without_llm():
    plan, items, _ = _learn()
    assert plan["learned_by"] == "heuristic" and plan["steps"][0]["url"] == "https://acts.org/api/activities"
    m = plan["mapping"]
    assert m["start_time"] == ["date", "start_time"]                       # date + time live in separate keys
    assert m["address"] == "location.address" and m["latitude"] == "location.latitude"
    assert m["image"] == "media_urls.0"                                    # image_url is always null here
    assert "placeholder" not in json.dumps(m)                              # "place" must not match "placeholder"
    assert plan["link_template"] == "https://acts.org/activities?scrollTo={id}&activity_id={id}"
    lookup = next(st for st in plan["steps"] if st["type"] == "lookup")
    assert lookup["key"] == "changemaker_id" and lookup["field"] == "organisation"
    assert len(items) == 3                                                 # the undated one is not an event
    a = items[1]
    assert a["name"] == "Cubbon Tree Walk" and a["start_time"] == "2026-12-06T11:15:00.000Z"    # 16:45 IST
    assert a["organisation"] == "Green BLR" and a["description"] == "Join Cubbon Tree Walk Bring water."
    assert a["image"] == "https://acts.org/files/2.webp" and a["latitude"] == 12.92
    assert a["link"] == "https://acts.org/activities?scrollTo=act-0002&activity_id=act-0002"


def test_detail_page_link_beats_scroll_to_link():
    hrefs = ([f"/activities?scrollTo={a['id']}&activity_id={a['id']}" for a in ACTS]
             + [f"https://acts.org/activities/{a['slug']}" for a in ACTS])
    assert jx.infer_link_template(ACTS, hrefs) == "https://acts.org/activities/{slug}"
    assert jx.link_in_query_only("/activities?scrollTo={id}") and not jx.link_in_query_only("/a/{slug}?x=1")


def test_time_only_value_is_never_a_date():
    """'16:45' alone would parse as today 16:45 — a start_time key holding only times must not be used alone."""
    import pytest as _pytest

    from scraper0.core import plan_learn
    acts = [dict(a, date=None) for a in ACTS[:3]]
    with _pytest.raises(plan_learn.LearnFailed):
        plan_learn.learn_plan(EVENT_SPEC, json_calls([("https://acts.org/api/a", json.dumps(acts))]),
                              "https://acts.org/", JSFetcher(), [], [])


def test_crawl_learns_a_plan_then_runs_it_without_browser_or_ai(store):
    from scraper0.core import plans
    f = JSFetcher()
    items, stats = Crawler(store, f, no_llm).crawl("https://acts.org/", EVENT_SPEC, CrawlOptions(workers=1))
    assert len(items) == 3 and stats.rendered == 1 and stats.by_method == {"api": 1} and stats.recipes_learned == 1
    # detail links were NOT walked; the list page was rendered once to find real detail links
    assert f.renders == ["https://acts.org/", "https://acts.org/activities"]
    assert items[0]["link"] == "https://acts.org/activities/ulsoor-lake-cleanup-1"
    assert all(i["_meta"]["extraction"] == "api" for i in items)
    assert stats.plan["link_template"] == "https://acts.org/activities/{slug}"

    f2 = JSFetcher()
    items2, run = plans.run_plan(stats.plan, f2, EVENT_SPEC, "https://acts.org/")
    assert f2.renders == [] and run.ai_calls == 0                           # no browser, no AI
    assert f2.gets == ["https://acts.org/api/activities", "https://acts.org/api/changemakers"]    # not even the page
    assert [i["name"] for i in items2] == [i["name"] for i in items]
    assert items2[0]["organisation"] == "Lake Friends"


def test_api_gone_fails_the_run_so_repair_can_relearn(store):
    from scraper0.core import plans
    _, stats = Crawler(store, JSFetcher(), no_llm).crawl("https://acts.org/", EVENT_SPEC, CrawlOptions(workers=1))
    with pytest.raises(plans.PlanError, match="HTTP 404"):
        plans.run_plan(stats.plan, JSFetcher(api_up=False), EVENT_SPEC, "https://acts.org/")


def test_no_browser_available_degrades_to_static(store):
    f = JSFetcher(has_browser=False)
    items, stats = Crawler(store, f, lambda p: json.dumps({"found": False, "items": []})).crawl(
        "https://acts.org/", EVENT_SPEC, CrawlOptions(workers=1))
    assert items == [] and stats.rendered == 0


def test_llm_maps_unusual_keys_for_custom_entity(store):
    job = EntitySpec(name="job", description="a job posting", tenant_id="t1", fields=[
        FieldSpec("title", "text", True), FieldSpec("company", "text", True), FieldSpec("salary", "number")])
    rows = [{"rolle": "Data Engineer", "arbeitgeber": "Acme", "verguetung": "120000", "ref": "j-1001"},
            {"rolle": "Designer", "arbeitgeber": "Globex", "verguetung": "90000", "ref": "j-1002"}]

    class F(JSFetcher):
        def get(self, url):
            if url == "https://acts.org/api/stellen":
                return json.dumps({"data": {"rows": rows}}), "application/json"
            return super().get(url)

        def render(self, url):
            self.renders.append(url)
            jr = [("https://acts.org/api/stellen", json.dumps({"data": {"rows": rows}}))]
            return RenderResult(html="<html><body><a href='/jobs/j-1001'>a</a><a href='/jobs/j-1002'>b</a></body></html>",
                                final_url=url, json_responses=jr, requests=json_calls(jr))
    calls = []

    def llm(prompt):
        calls.append(prompt)
        assert prompt.startswith("MAP JSON KEYS") and "arbeitgeber" in prompt
        return json.dumps({"is_entity_list": True, "mapping": {"title": "rolle", "company": "arbeitgeber",
                                                               "salary": "verguetung", "bogus": "nope"}})
    items, stats = Crawler(store, F(), llm).crawl("https://acts.org/", job, CrawlOptions(workers=1))
    assert len(calls) == 1 and stats.llm_calls == 1 and stats.plan["learned_by"] == "llm"
    assert {(i["title"], i["company"], i["salary"]) for i in items} == {("Data Engineer", "Acme", 120000.0), ("Designer", "Globex", 90000.0)}
    assert items[0]["link"] == "https://acts.org/jobs/j-1001"
    assert stats.plan["steps"][0]["items"] == {"json": "data.rows"} and stats.plan["entity"] == "job"


def test_event_engine_on_js_site_filters_online_and_past(store):
    from datetime import datetime, timezone

    from scraper0.events.engine import Engine, Options
    res = Engine(JSFetcher(), llm=no_llm, store=store).run(
        ["https://acts.org/"], Options(portal="ACTS", now=datetime(2026, 12, 1, tzinfo=timezone.utc)))
    assert [e["name"] for e in res["events"]] == ["Ulsoor Lake Cleanup", "Cubbon Tree Walk"]
    e = res["events"][0]
    assert e["extraction"] == "api" and e["organisation"] == "Lake Friends" and e["latitude"] == 12.91
    assert e["type"] == "CLEANLINESS_DRIVE" and e["link"].endswith("/activities/ulsoor-lake-cleanup-1")
    assert res["stats"]["dropped"]["online"] == 1 and res["stats"]["crawl"][0]["rendered"] == 1


def test_online_address_and_title_first_classification():
    assert address_is_online("Online") and address_is_online(" Zoom meeting ") and not address_is_online("Online Cafe, MG Road")
    assert classify_type("Eco Comic Art Competition", "x " * 200 + "reduce waste and litter") == "OTHER"
    assert classify_type("Eco Comic Art Competition", "A cleanup drive for the lake") == "CLEANLINESS_DRIVE"
    assert classify_type("Lake cleanup", "anything") == "CLEANLINESS_DRIVE"
