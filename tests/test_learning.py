"""The scraper learns a layout once (via LLM), stores it, and reuses it without the LLM."""
import json
import re

import pytest

from scraper0.core import CrawlOptions, Crawler, EntitySpec, FieldSpec, Store
from scraper0.core.entities import EVENT_SPEC
from scraper0.core.recipes import path_pattern, pattern_matches


class FakeFetcher:
    from conftest import no_cookie as cookie, request_via_get as request  # noqa: E402
    def __init__(self, pages): self.pages, self.calls = pages, []
    def get(self, url):
        self.calls.append(url)
        p = self.pages.get(url)
        return (p, "text/html") if p is not None else None


def detail(title, date, time_, venue, img, layout="v1"):
    if layout == "v1":
        return f"""<html><body><nav><a href="/">Home</a></nav>
        <div id="main"><h1 class="ev-title">{title}</h1>
        <div class="meta"><span class="date">{date}</span> <span class="time">{time_}</span></div>
        <p class="venue">Venue: {venue}</p><img class="banner" src="{img}">
        <p>Lots of words about this lovely event so the page has enough text to be worth reading at all.</p></div></body></html>"""
    return f"""<html><body><section class="hero"><h2 class="headline">{title}</h2>
        <ul><li class="when">{date} {time_}</li><li class="where">{venue}</li></ul>
        <p>Lots of words about this lovely event so the page has enough text to be worth reading at all.</p></section></body></html>"""


SITE = {
    "a": ("Lake Cleanup", "14 March 2026", "7:00 am", "Ulsoor Lake", "/i/a.jpg"),
    "b": ("Tree Planting Day", "21 March 2026", "8:30 am", "Cubbon Park", "/i/b.jpg"),
    "c": ("Beach Plog", "28 March 2026", "6:00 am", "Marina Beach", "/i/c.jpg"),
}


class FakeLLM:
    """Answers like a good model: values + verbatim sources, read from SITE by URL."""
    def __init__(self, layout="v1"): self.calls, self.layout = 0, layout
    def __call__(self, prompt):
        self.calls += 1
        url = re.search(r"Page URL: (\S+)", prompt).group(1)
        slug = url.rstrip("/").split("/")[-1]
        if slug not in SITE:
            return json.dumps({"found": False, "items": []})
        t, d, tm, v, img = SITE[slug]
        h, m = re.match(r"(\d+):(\d+)", tm).groups()
        hh = int(h) + (12 if "pm" in tm and int(h) != 12 else 0)
        day = re.match(r"(\d+)", d).group(1)
        return json.dumps({"found": True, "items": [{
            "name": {"value": t, "source": t},
            "start_time": {"value": f"2026-03-{int(day):02d}T{hh:02d}:{m}", "source": [d, tm]},
            "address": {"value": v, "source": v},
            "image": {"value": f"https://ex.org{img}", "source": f"https://ex.org{img}"},
        }]})


def site(layout="v1"):
    listing = '<html><body>' + "".join(f'<a href="/events/{k}">{k}</a>' for k in SITE) + '<a href="/about">about</a></body></html>'
    pages = {"https://ex.org/events": listing}
    for k, (t, d, tm, v, img) in SITE.items():
        pages[f"https://ex.org/events/{k}"] = detail(t, d, tm, v, img, layout)
    return pages


def crawler(store, pages, llm):
    return Crawler(store, FakeFetcher(pages), llm)


def opts(**kw):
    return CrawlOptions(workers=1, link_hint=re.compile(r"/events/"), **kw)


@pytest.fixture
def store():
    return Store()


def test_url_patterns():
    assert path_pattern("https://x.org/events/in-person/make-a-tangram") == "/events/in-person/*"
    assert path_pattern("https://x.org/e/12345/details") == "/e/*/*"
    assert pattern_matches("/events/*", "https://x.org/events/foo")
    assert not pattern_matches("/events/*", "https://x.org/events/foo/bar")


def test_learns_once_then_scrapes_rest_of_site_without_llm(store):
    llm = FakeLLM()
    items, stats = crawler(store, site(), llm).crawl("https://ex.org/events", EVENT_SPEC, opts())
    assert llm.calls == 1                                   # only the first detail page needed the model
    assert stats.recipes_learned == 1 and stats.by_method.get("recipe") == 2
    by = {i["name"]: i for i in items}
    assert set(by) == {"Lake Cleanup", "Tree Planting Day", "Beach Plog"}
    assert by["Beach Plog"]["start_time"] == "2026-03-28T00:30:00.000Z"     # date+time from two nodes
    assert by["Beach Plog"]["address"] == "Marina Beach"                    # "Venue: " label stripped
    assert by["Beach Plog"]["image"] == "https://ex.org/i/c.jpg"
    [r] = store.list_recipes("ex.org")
    assert r["path_pattern"] == "/events/*" and r["successes"] == 2 and r["status"] == "active"


def test_second_crawl_uses_page_cache(store):
    crawler(store, site(), FakeLLM()).crawl("https://ex.org/events", EVENT_SPEC, opts())
    llm = FakeLLM()
    items, stats = crawler(store, site(), llm).crawl("https://ex.org/events", EVENT_SPEC, opts())
    assert llm.calls == 0 and stats.by_method.get("cache") == 3 and len(items) == 3


def test_learned_layout_becomes_a_plan_that_runs_without_ai(store):
    """Learn once (one AI call), freeze it as a plan, then scrape again with no AI at all."""
    from scraper0.core import plans
    items, _ = crawler(store, site(), FakeLLM()).crawl("https://ex.org/events", EVENT_SPEC, opts(use_cache=False))
    layouts = [r for r in store.list_recipes("ex.org") if r["strategy"] == "selectors"]
    plan = plans.from_selectors(layouts, "https://ex.org/events", patterns=["/events/*"])
    again, run = plans.run_plan(plan, FakeFetcher(site()), EVENT_SPEC, "https://ex.org/events")
    assert run.ai_calls == 0 and sorted(i["name"] for i in again) == sorted(i["name"] for i in items)


def test_layout_change_triggers_relearn_and_keeps_fallbacks(store):
    crawler(store, site("v1"), FakeLLM()).crawl("https://ex.org/events", EVENT_SPEC, opts())
    llm = FakeLLM()
    items, stats = crawler(store, site("v2"), llm).crawl("https://ex.org/events", EVENT_SPEC, opts())
    assert len(items) == 3
    assert llm.calls == 1 and stats.by_method.get("recipe") == 2      # relearned on first changed page
    [r] = store.list_recipes("ex.org")
    assert len(r["rules"]["name"]) >= 2                               # new + old selectors kept as alternatives


def test_recipe_goes_stale_after_repeated_failures(store):
    rid = store.save_recipe("ex.org", "/events/*", "event", "selectors", {"name": [{"sel": "h9"}]}, "u")
    for _ in range(3):
        store.record_recipe_result(rid, False)
    assert store.list_recipes()[0]["status"] == "stale"
    store.record_recipe_result(rid, True)
    assert store.list_recipes()[0]["status"] == "active"


def test_does_not_learn_when_required_field_cannot_be_located(store):
    def llm(prompt):  # model paraphrases instead of copying → can't be located in the DOM
        return json.dumps({"found": True, "items": [{"name": {"value": "Lake Cleanup", "source": "Lake Cleanup"},
                                                     "start_time": {"value": "2026-03-14T07:00", "source": "on the fourteenth"}}]})
    items, stats = crawler(store, site(), llm).crawl("https://ex.org/events", EVENT_SPEC, opts())
    assert stats.recipes_learned == 0 and store.list_recipes() == [] and len(items) == 3


def test_hallucinated_image_is_dropped():
    html = detail("Lake Cleanup", "14 March 2026", "7:00 am", "Ulsoor Lake", "/i/a.jpg")
    from scraper0.core.llm_extract import llm_extract
    def fake(p):
        return json.dumps({"found": True, "items": [{"name": {"value": "Lake Cleanup"},
                                                                "start_time": {"value": "2026-03-14T07:00"},
                                                                "image": {"value": "https://evil.example/x.png"}}]})
    [(item, _)] = llm_extract(html, "https://ex.org/events/a", EVENT_SPEC, fake)
    assert "image" not in item


def test_crawl_hints_skip_dead_patterns(store):
    pages = site()
    pages["https://ex.org/events"] += "".join(f'<a href="/events/archive/x{i}">t</a>' for i in range(10))
    for i in range(10):
        pages[f"https://ex.org/events/archive/x{i}"] = "<html><body><p>" + "nothing here at all " * 10 + "</p></body></html>"
    store.update_hints("https://ex.org/events", "event", {"/events/archive/*": False})
    store.update_hints("https://ex.org/events", "event", {"/events/archive/*": False})
    store.update_hints("https://ex.org/events", "event", {"/events/archive/*": False})
    c = crawler(store, pages, FakeLLM())
    c.crawl("https://ex.org/events", EVENT_SPEC, opts())
    tag_fetches = [u for u in c.fetcher.calls if "/archive/" in u]
    assert len(tag_fetches) == 3                                       # explore budget only
    assert c.fetcher.calls.index("https://ex.org/events/a") < c.fetcher.calls.index(tag_fetches[0])


def test_generic_entity_no_event_code():
    job = EntitySpec(name="job", description="a job posting", fields=[
        FieldSpec("title", "text", True, "job title"), FieldSpec("company", "text", True),
        FieldSpec("salary", "number"), FieldSpec("apply_url", "url")])
    pages = {"https://jobs.org/list": '<html><body><a href="/jobs/1">1</a><a href="/jobs/2">2</a></body></html>'}
    data = {"1": ("Data Engineer", "Acme", "120000"), "2": ("Designer", "Globex", "90000")}
    for k, (t, co, sal) in data.items():
        pages[f"https://jobs.org/jobs/{k}"] = (f'<html><body><h1>{t}</h1><div class="co">{co}</div>'
                                               f'<b class="pay">Salary: INR {sal}</b><a class="apply" href="/apply/{k}">Apply</a>'
                                               + "<p>" + "Great role with a great team doing great work. " * 3 + "</p></body></html>")
    calls = []
    def llm(prompt):
        calls.append(1)
        k = re.search(r"Page URL: \S+/(\d+)", prompt).group(1)
        t, co, sal = data[k]
        return json.dumps({"found": True, "items": [{"title": {"value": t, "source": t}, "company": {"value": co, "source": co},
                                                     "salary": {"value": sal, "source": f"INR {sal}"}}]})
    s = Store()
    items, stats = Crawler(s, FakeFetcher(pages), llm).crawl("https://jobs.org/list", job, CrawlOptions(workers=1))
    assert len(calls) == 1 and stats.by_method.get("recipe") == 1
    assert {(i["title"], i["company"], i["salary"]) for i in items} == {("Data Engineer", "Acme", 120000.0), ("Designer", "Globex", 90000.0)}


def test_spec_validation():
    with pytest.raises(ValueError):
        EntitySpec(name="x", description="", fields=[FieldSpec("a")])            # no required field
    with pytest.raises(ValueError):
        FieldSpec("a", "colour")
    with pytest.raises(ValueError):
        EntitySpec(name="Bad Name", description="", fields=[FieldSpec("a", required=True)])


def test_cache_sees_jsonld_only_changes(store):
    """A page whose data is only in JSON-LD has no visible text — the cache must still notice edits."""
    from scraper0.core.crawler import page_fingerprint
    from scraper0.core.llm_extract import page_text
    a = '<html><head><script type="application/ld+json">{"@type":"Event","name":"A","startDate":"2026-03-14"}</script></head><body></body></html>'
    b = a.replace("2026-03-14", "2026-03-21")
    assert page_text(a)[0] == page_text(b)[0] == ""
    assert page_fingerprint(a, "") != page_fingerprint(b, "")


def test_store_write_failure_does_not_fail_crawl(store, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("db hiccup")
    monkeypatch.setattr(store, "cache_page", boom)
    monkeypatch.setattr(store, "update_hints", boom)
    items, _ = crawler(store, site(), FakeLLM()).crawl("https://ex.org/events", EVENT_SPEC, opts())
    assert len(items) == 3
