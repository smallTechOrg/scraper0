"""Generic learner mechanisms, each against an offline fake site that reproduces the pattern:
values that are not visible text, templates shared across URL patterns, paging to the reported total, unstable
order, links that are not anchors, organiser names, finding the listing from a home page, placeholder images."""
import base64
import json
import random
import re
from urllib.parse import quote

import pytest
from test_plans import FakeHTTP, ev

from scraper0.core import CrawlOptions, Crawler, Store, plan_learn, plans
from scraper0.core import json_extract as jx
from scraper0.core.crawler import listing_candidates
from scraper0.core.entities import EVENT_SPEC
from scraper0.core.fetch import HTTPResult
from scraper0.core.llm_extract import _parse_json
from scraper0.core.recipes import apply_recipe, learn_recipe, share_rules, template_signature, template_similarity
from scraper0.core.render import CapturedRequest, RenderResult


def b64(s: str) -> str:
    return base64.b64encode(s.encode()).decode()


# ------------------------------------------------------------------ 1. values beyond visible text

def builder_page(title, date, venue, about, img, extra=""):
    """A site-builder detail page: bound spans (one key only base64-encoded), the venue inside url-encoded JSON
    props, the description only in inline JSON, the picture only in og:image."""
    props = quote(json.dumps({"venue": {"name": venue, "zoom": 12}}))
    data = json.dumps({"props": {"pageProps": {"event": {"about": about, "id": 7}}}})
    return f"""<html><head><title>Events | Org</title><meta property="og:image" content="{img}">
    <script id="__NEXT_DATA__" type="application/json">{data}</script></head>
    <body><nav><a href="/">Home</a><a href="/events">Events</a></nav><div class="row">
    <p><span class="bind" data-bind="page.Header Text" data-value="{b64(title)}">{title}</span></p>
    <p><span class="bind" data-bind="" data-bind-encoded="{b64('page.Event Date')}">{date}</span></p>
    <div class="map-widget" data-props="{props}"></div>{extra}
    <p>Come along and bring a friend, everyone is welcome at our community gatherings all year round.</p>
    </div></body></html>"""


PAGES = {
    "a": ("Lake Cleanup", "14 March 2099 | 7:00 am", "Ulsoor Lake, Bengaluru", "Clean the lake shore with us and learn about wetlands.", "https://cdn.b.x/p/a.jpg"),
    "b": ("Tree Planting Day", "21 March 2099 | 8:30 am", "Cubbon Park, Bengaluru", "Plant native saplings along the park's north edge.", "https://cdn.b.x/p/b.jpg"),
    "c": ("Beach Plog", "28 March 2099 | 6:00 am", "Marina Beach, Chennai", "Jog and pick litter along two kilometres of beach.", "https://cdn.b.x/p/c.jpg"),
    "d": ("Seed Ball Workshop", "2 April 2099 | 4:00 pm", "Lalbagh, Bengaluru", "Make seed balls for the monsoon with local gardeners.", "https://cdn.b.x/p/d.jpg"),
}


def answer(slug):
    t, d, v, about, img = PAGES[slug]
    day, hh, mm, ap = re.match(r"(\d+) \w+ 2099 \| (\d+):(\d+) (am|pm)", d).groups()
    month = "03" if "March" in d else "04"
    hour = int(hh) + (12 if ap == "pm" and int(hh) != 12 else 0)
    return {"found": True, "items": [{
        "name": {"value": t, "source": t},
        "start_time": {"value": f"2099-{month}-{int(day):02d}T{hour:02d}:{mm}", "source": d},
        "address": {"value": v, "source": v},
        "description": {"value": about, "source": about},
        "image": {"value": img, "source": img}}]}


class BuilderLLM:
    def __init__(self, empty=()):
        self.calls, self.urls, self.empty = 0, [], set(empty)

    def __call__(self, prompt):
        self.calls += 1
        url = re.search(r"Page URL: (\S+)", prompt).group(1)
        self.urls.append(url)
        slug = url.rstrip("/").split("/")[-1]
        if slug not in PAGES or slug in self.empty:
            return json.dumps({"found": False, "items": []})
        return json.dumps(answer(slug))


def test_rules_find_values_in_attributes_encoded_props_inline_json_and_meta():
    html = builder_page(*PAGES["a"])
    item = {"name": "Lake Cleanup", "start_time": "2099-03-14T01:30:00.000Z", "address": "Ulsoor Lake, Bengaluru",
            "description": PAGES["a"][3], "image": PAGES["a"][4]}
    rules = learn_recipe(html, "https://b.x/events/in-person/a", EVENT_SPEC, item,
                         {"name": "Lake Cleanup", "start_time": "14 March 2099 | 7:00 am"})
    assert rules["name"][0] == {"sel": 'span[data-bind="page.Header Text"]', "attr": "text"}
    assert not any("data-value" in r["sel"] for r in rules["name"])                 # an attribute repeating the value is no key
    assert rules["start_time"][0]["sel"] == f'span[data-bind-encoded="{b64("page.Event Date")}"]'   # the encoded binding key
    assert rules["address"][0] == {"sel": "div.map-widget", "attr": "data-props", "decode": ["url", {"json": "venue.name"}]}
    assert rules["description"][0]["decode"] == [{"json": "props.pageProps.event.about"}]
    assert rules["description"][0]["sel"] in ("#__NEXT_DATA__", 'script[type="application/json"]')
    assert rules["image"][0] == {"sel": 'meta[property="og:image"]', "attr": "content"}

    other, _ = apply_recipe(rules, builder_page(*PAGES["b"]), "https://b.x/events/virtual/b", EVENT_SPEC)
    assert other == {"name": "Tree Planting Day", "start_time": "2099-03-21T03:00:00.000Z", "address": "Cubbon Park, Bengaluru",
                     "description": PAGES["b"][3], "image": "https://cdn.b.x/p/b.jpg"}


def test_decode_chain_base64_html_and_json_inside_script_assignment():
    from bs4 import BeautifulSoup

    from scraper0.core.recipes import apply_rule
    soup = BeautifulSoup(f"""<div id="x" data-a="{b64('Town Hall, Pune')}" data-b="Caf&amp;eacute; Vista"></div>
        <script id="s">window.__STATE__ = {{"e": {{"when": "2099-05-01T10:00"}}}};</script>""", "html.parser")
    assert apply_rule({"sel": "#x", "attr": "data-a", "decode": ["base64"]}, soup) == "Town Hall, Pune"
    assert apply_rule({"sel": "#x", "attr": "data-b", "decode": ["html"]}, soup) == "Café Vista"
    assert apply_rule({"sel": "#s", "attr": "text", "decode": [{"json": "e.when"}]}, soup) == "2099-05-01T10:00"
    assert apply_rule({"sel": "#x", "attr": "data-b", "decode": ["base64"]}, soup) is None        # not base64: no guess


def test_summarised_text_is_located_by_its_quote_and_layouts_share_keyed_rules():
    """The AI shortens a description: the rule is the element its quote starts, not a failed exact match. And a
    layout that never got a description rule borrows the keyed one a sibling layout learned."""
    html = """<html><body><h1 class="t">River Walk</h1><time class="d">2099-06-01 09:00</time>
        <div data-slot="about">Walk the river with a naturalist. Bring water and a hat. We end at the old bridge by noon.</div>
        </body></html>"""
    rules = learn_recipe(html, "https://r.x/e/1", EVENT_SPEC,
                         {"name": "River Walk", "start_time": "2099-06-01T03:30:00.000Z", "description": "A guided river walk."},
                         {"name": "River Walk", "start_time": "2099-06-01 09:00", "description": "Walk the river with a naturalist."})
    assert rules["description"][0] == {"sel": 'div[data-slot="about"]', "attr": "text"}
    shared = share_rules([{"path_pattern": "/e/*", "rules": rules},
                          {"path_pattern": "/v/*", "rules": {"name": [{"sel": "h1.t", "attr": "text"}]}}])
    assert shared[1]["rules"]["description"] == [{"sel": 'div[data-slot="about"]', "attr": "text"}]
    assert "start_time" not in shared[1]["rules"]             # time.d names nothing: positional rules are not shared


class BuilderSite:
    """/events lists detail pages under two URL patterns that share one template, plus look-alike pages
    without an event."""
    from conftest import no_cookie as cookie, request_via_get as request  # noqa: E402

    def __init__(self, extra_empty=0):
        links = [f"/events/in-person/{s}" for s in "ab"] + [f"/events/virtual/{s}" for s in "cd"]
        self.pages = {"https://b.x/events": "<html><body>" + "".join(f'<a href="{u}">x</a>' for u in links)
                      + "".join(f'<a href="/events/info/n{i}">i</a>' for i in range(extra_empty)) + "</body></html>"}
        for u in links:
            self.pages["https://b.x" + u] = builder_page(*PAGES[u[-1]])
        for i in range(extra_empty):          # same chrome, no bound slots
            self.pages[f"https://b.x/events/info/n{i}"] = (
                "<html><body><nav><a href='/'>Home</a></nav><div class='row'><p class='about-us'>We are a small "
                f"volunteer group number {i} and we meet every week to plan what comes next for the town.</p></div></body></html>")
        self.calls = []

    def get(self, url):
        self.calls.append(url)
        p = self.pages.get(url)
        return (p, "text/html") if p is not None else None


def test_one_template_under_two_url_patterns_costs_one_ai_call_even_with_parallel_workers():
    llm, site = BuilderLLM(), BuilderSite()
    items, stats = Crawler(Store(), site, llm).crawl(
        "https://b.x/events", EVENT_SPEC, CrawlOptions(workers=6, link_hint=re.compile(r"/events/")))
    assert sorted(i["name"] for i in items) == sorted(p[0] for p in PAGES.values())
    assert llm.calls == 1 and stats.by_method == {"none": 1, "llm": 1, "recipe": 3}
    a, b = (template_signature(site.pages[f"https://b.x/events/{p}"]) for p in ("in-person/a", "virtual/c"))
    assert template_similarity(a, b) == 1.0


def test_a_template_the_ai_found_empty_twice_is_not_asked_again_and_one_dateless_page_is_not_the_template():
    llm, site = BuilderLLM(empty={"a"}), BuilderSite(extra_empty=6)      # event "a" has no date this time
    items, stats = Crawler(Store(), site, llm).crawl(
        "https://b.x/events", EVENT_SPEC, CrawlOptions(workers=4, link_hint=re.compile(r"/events/")))
    assert sorted(i["name"] for i in items) == ["Beach Plog", "Seed Ball Workshop", "Tree Planting Day"]
    info = [u for u in llm.urls if "/info/" in u]
    assert len(info) == 2 and stats.by_method.get("skipped") == 4          # six look-alikes, two questions
    assert llm.calls == 4                                                   # a (empty), b (learned), 2 × info


def test_ai_budget_caps_a_crawl():
    llm, site = BuilderLLM(), BuilderSite(extra_empty=6)
    _, stats = Crawler(Store(), site, llm).crawl(
        "https://b.x/events", EVENT_SPEC, CrawlOptions(workers=1, link_hint=re.compile(r"/events/"), max_llm_calls=2))
    assert llm.calls == 2 and stats.llm_calls == 2


def test_unbalanced_json_from_a_model_is_repaired():
    raw = '{"found": true, "items": [{"name": {"value": "A [b] walk", "source": "A"}, "price": {"value": "Free"}]}'
    assert _parse_json(raw)["items"][0]["price"] == {"value": "Free"}


# ------------------------------------------------------------------ 2. paging to the total, caps, truncation

def paged_plan():
    return {"plan_version": 1, "steps": [{"id": "list", "type": "request", "url": "https://api.x/events",
                                          "query": {"offset": "{page.value}"},
                                          "paginate": {"mode": "offset", "start": 0, "step": 10, "stop": {"total_path": "total"}},
                                          "items": {"json": "data"}}],
            "mapping": {"name": "title", "start_time": "date", "address": "venue"}}


def big_api(total, size=10):
    allev = [dict(ev(i), id=f"e{i}", title=f"Event {i}") for i in range(total)]

    def h(q, body, headers):
        off = int(q.get("offset", 0))
        return 200, json.dumps({"total": total, "data": allev[off:off + size]})
    return FakeHTTP({("GET", "https://api.x/events"): h})


def test_paging_runs_to_the_reported_total_not_to_a_page_count():
    items, st = plans.run_plan(paged_plan(), big_api(450), EVENT_SPEC, "https://x/")
    assert len(items) == 450 and st.pages == 45 and st.truncated == []


def test_a_safety_cap_ends_the_run_and_says_so(monkeypatch):
    monkeypatch.setattr(plans, "caps", lambda: (300, 3, 5000))
    items, st = plans.run_plan(paged_plan(), big_api(450), EVENT_SPEC, "https://x/")
    assert len(items) == 30 and st.truncated == [{"step": "list", "cap": "max_pages", "items": 30, "total": 450}]
    monkeypatch.setattr(plans, "caps", lambda: (300, 200, 25))
    items, st = plans.run_plan(paged_plan(), big_api(450), EVENT_SPEC, "https://x/")
    assert len(items) == 25 and st.truncated[0]["cap"] == "max_items" and st.as_dict()["truncated"]
    monkeypatch.setattr(plans, "caps", lambda: (4, 200, 5000))
    items, st = plans.run_plan(paged_plan(), big_api(450), EVENT_SPEC, "https://x/")
    assert len(items) == 40 and st.truncated[0]["cap"] == "max_requests"


def search_api(total=130, max_limit=50, tiebreak=True, shuffle=True, seed=3):
    """POST search: `limit` above max_limit is a 400; ties on the date sort come back in a random order unless
    the sort carries an id tiebreaker (when the API honours one)."""
    rng = random.Random(seed)
    allev = [{"_source": {"id": 1000 + i, "event_name": f"Drive {i}", "event_from_date": "2099-12-%02d" % (i // 40 + 1),
                          "event_start_time": "07:30:00", "event_address": f"Ward {i}", "org_name": f"Club {i % 7}",
                          "eventorganizer": [{"name": f"Person {i}", "phone_no": 99}],
                          "event_link": f"https://mb.x/pages/event_detail?event_name=drive-{i}&key={i}9{i}"}}
             for i in range(total)]

    def h(q, body, headers):
        b = json.loads(body)
        if b["limit"] > max_limit:
            return 400, json.dumps({"message": [f"limit must not be greater than {max_limit}"]})
        keys = [next(iter(s)) for s in b.get("sort") or []]
        rows = list(allev)
        if shuffle and not (tiebreak and "id" in keys):
            rng.shuffle(rows)
            rows.sort(key=lambda r: r["_source"]["event_from_date"])       # stable only on the date
        page, limit = int(b["page"]), b["limit"]
        return 201, json.dumps({"total": total, "records": rows[(page - 1) * limit: page * limit]})
    body = {"query": {"bool": {"must": [{"term": {"type": "vo"}}]}}, "sort": [{"event_from_date": {"order": "asc"}}],
            "page": 1, "limit": 12}
    f = FakeHTTP({("POST", "https://search.mb.x/events"): h})
    call = CapturedRequest("POST", "https://search.mb.x/events", {"content-type": "application/json"}, json.dumps(body),
                           201, "application/json", h({}, json.dumps(body), {})[1])
    return f, call


def test_page_size_is_raised_as_far_as_the_api_allows_and_ties_get_a_confirmed_tiebreaker():
    f, call = search_api()
    plan, items, _ = plan_learn.learn_plan(EVENT_SPEC, [call], "https://mb.x/pages/events", f, [], [])
    st = plan["steps"][0]
    body = st["body"]["json"]
    assert body["limit"] == 50 and st["paginate"]["stop"] == {"fewer_than": 50, "total_path": "total"}
    assert "max_pages" not in st["paginate"]                                       # the total ends it, not a page count
    assert body["sort"] == [{"event_from_date": {"order": "asc"}}, {"id": {"order": "asc"}}]
    assert "unstable_order" not in st["paginate"]
    assert len({i["name"] for i in items}) == 130                                  # every item, exactly once
    again, run = plans.run_plan(plan, f, EVENT_SPEC, "https://mb.x/pages/events")
    assert len(again) == 130 and run.pages == 3 and run.paging == {} and run.ai_calls == 0


def test_unstable_order_without_a_usable_tiebreaker_is_flagged_and_swept_until_everything_was_seen():
    f, call = search_api(total=60, tiebreak=False)
    plan, items, _ = plan_learn.learn_plan(EVENT_SPEC, [call], "https://mb.x/pages/events", f, [], [])
    pg = plan["steps"][0]["paginate"]
    assert pg["unstable_order"] is True and plan["steps"][0]["body"]["json"]["sort"] == [{"event_from_date": {"order": "asc"}}]
    again, run = plans.run_plan(plan, f, EVENT_SPEC, "https://mb.x/pages/events")
    seen = run.paging["list"]
    assert seen["total"] == 60 and seen["sweeps"] >= 2 and seen["unique"] == len(again) >= 55
    assert len({i["name"] for i in again}) == len(again)                           # repeats between pages are dropped


# ------------------------------------------------------------------ 4/5. links and organiser names from the items

def test_items_own_url_time_sibling_and_organiser_are_mapped_without_ai():
    f, call = search_api(shuffle=False)
    plan, items, _ = plan_learn.learn_plan(EVENT_SPEC, [call], "https://mb.x/pages/events", f, [], [])
    m = plan["mapping"]
    assert m["name"] == "event_name"                                   # not "…organizer.0.name", not "org_name"
    assert m["start_time"] == ["event_from_date", "event_start_time"]  # the time lives in a sibling key
    assert m["organisation"] == {"first_of": ["org_name", "eventorganizer.0.name"]}
    assert plan["link_template"] == "{event_link}"
    assert items[0]["link"].startswith("https://mb.x/pages/event_detail?event_name=drive-")
    assert items[0]["organisation"].startswith("Club ") and items[0]["start_time"].endswith("T02:00:00.000Z")


def test_who_fields_from_abbreviations_nested_objects_and_whose_name_it_is():
    rows = [{"title": f"T{i}", "date": "2099-01-0%d" % (i + 1), "org_name": f"NGO {i}", "category": {"name": "Health"}}
            for i in range(3)]
    m = jx.heuristic_mapping(EVENT_SPEC, rows)
    assert m["organisation"] == "org_name" and m["name"] == "title"
    rows = [{"event": {"title": f"T{i}"}, "date": "2099-01-0%d" % (i + 1), "organizer": {"name": f"NGO {i}", "id": i},
             "venue": {"name": f"Hall {i}"}} for i in range(3)]
    m = jx.heuristic_mapping(EVENT_SPEC, rows)
    assert m["name"] == "event.title" and m["organisation"] == "organizer.name" and m["address"] == "venue.name"


def test_link_field_must_be_a_distinct_same_site_page_per_item():
    rows = [{"id": i, "detail_url": f"/happenings/{i}", "site": "https://s.x", "logo": f"https://s.x/i/{i}.png",
             "ticket_url": f"https://tickets.other/{i}"} for i in range(5)]
    assert jx.item_link_field(rows, "https://s.x/") == "detail_url"
    assert jx.item_link_field([dict(r, detail_url="/happenings") for r in rows], "https://s.x/") is None


class CardSite:
    """A JavaScript listing whose cards are not anchors. The item pages exist at /happenings/{slug}."""
    def __init__(self, routes_in_script=False, pages_up=True, clickable=True):
        self.acts = [{"id": f"h{i}", "slug": f"walk-{i}", "name": f"Heritage Walk {i}", "date": "2099-07-%02d" % (i + 1),
                      "venue": f"Gate {i}"} for i in range(4)]
        self.routes_in_script, self.pages_up, self.clickable = routes_in_script, pages_up, clickable
        self.opened, self.render_hosts = [], set()

    def get(self, url):
        r = self.request("GET", url)
        return (r.text, r.content_type) if r.status == 200 else None

    def request(self, method, url, headers=None, data=None):
        if url == "https://c.x/":
            return HTTPResult(200, '<html><body><div id="app"></div><script src="/a.js"></script></body></html>', "text/html")
        if url == "https://c.x/api/happenings":
            return HTTPResult(200, json.dumps(self.acts), "application/json")
        m = re.fullmatch(r"https://c\.x/happenings/(walk-\d)", url)
        if m and self.pages_up:
            name = next(a["name"] for a in self.acts if a["slug"] == m.group(1))
            return HTTPResult(200, f"<html><body><h1>{name}</h1><p>{'All about it. ' * 30}</p></body></html>", "text/html")
        return HTTPResult(404, "nope", "text/html")

    def cookie(self, name):
        return None

    def render(self, url):
        script = ("<script>window.__routes = " + json.dumps({a["id"]: f"/happenings/{a['slug']}" for a in self.acts})
                  + ";</script>") if self.routes_in_script else ""
        cards = "".join(f'<div class="card" onclick="go()">{a["name"]}</div>' for a in self.acts)
        return RenderResult(html=f"<html><body>{cards}{script}</body></html>", final_url=url, requests=[
            CapturedRequest("GET", "https://c.x/api/happenings", {}, None, 200, "application/json", json.dumps(self.acts))])

    def open_items(self, url, texts):
        self.opened.append(list(texts))
        if not self.clickable:
            return []
        by_name = {a["name"]: a for a in self.acts}
        return [{"text": t, "url": f"https://c.x/happenings/{by_name[t]['slug']}?ref=card", "canonical":
                 f"https://c.x/happenings/{by_name[t]['slug']}"} for t in texts if t in by_name]


def no_llm(prompt):
    raise AssertionError("no AI needed here")


def test_links_that_are_not_anchors_are_learned_by_opening_two_items():
    site = CardSite()
    items, stats = Crawler(Store(), site, no_llm).crawl("https://c.x/", EVENT_SPEC, CrawlOptions(workers=1))
    assert stats.plan["link_template"] == "https://c.x/happenings/{slug}"        # the canonical URL, not ?ref=card
    assert site.opened == [["Heritage Walk 0", "Heritage Walk 1", "Heritage Walk 2"]]
    assert [i["link"] for i in items][:2] == ["https://c.x/happenings/walk-0", "https://c.x/happenings/walk-1"]


def test_links_in_the_pages_scripts_are_found_without_clicking():
    site = CardSite(routes_in_script=True)
    _, stats = Crawler(Store(), site, no_llm).crawl("https://c.x/", EVENT_SPEC, CrawlOptions(workers=1))
    assert stats.plan["link_template"] == "https://c.x/happenings/{slug}" and site.opened == []


def test_a_link_template_whose_pages_do_not_resolve_is_not_kept():
    site = CardSite(routes_in_script=True, pages_up=False, clickable=False)
    items, stats = Crawler(Store(), site, no_llm).crawl("https://c.x/", EVENT_SPEC, CrawlOptions(workers=1))
    assert "link_template" not in stats.plan and len(items) == 4


# ------------------------------------------------------------------ 6. the listing page, from a home page

class HomeSite:
    """The home page shows a 4-item widget feed; "Events" in the navigation leads to the full, paged listing."""
    def __init__(self):
        self.widget = [dict(ev(i), id=f"w{i}") for i in range(4)]
        self.all = [dict(ev(i), id=f"e{i}") for i in range(60)]
        self.render_hosts, self.rendered = set(), []

    def _home(self):
        return ('<html><body><nav><a href="/about">About</a><a href="/mega-events">Mega Events</a>'
                '<a href="/events">Events</a><a href="/events?tab=past">Past</a></nav><div id="app"></div>'
                '<script src="/a.js"></script></body></html>')

    def request(self, method, url, headers=None, data=None):
        from urllib.parse import parse_qsl, urlsplit
        p = urlsplit(url)
        q = dict(parse_qsl(p.query))
        if p.path in ("/", "/events", "/mega-events", "/about"):
            return HTTPResult(200, self._home(), "text/html")
        if p.path == "/api/featured":
            return HTTPResult(200, json.dumps({"data": self.widget}), "application/json")
        if p.path == "/api/events":
            off = int(q.get("offset", 0))
            return HTTPResult(200, json.dumps({"total": 60, "data": self.all[off:off + 20]}), "application/json")
        return HTTPResult(404, "", "text/plain")

    def get(self, url):
        r = self.request("GET", url)
        return (r.text, r.content_type) if r.status == 200 else None

    def cookie(self, name):
        return None

    def render(self, url):
        self.rendered.append(url)
        api = {"https://h.x/": "https://h.x/api/featured", "https://h.x/events": "https://h.x/api/events?offset=0&limit=20"}.get(url)
        calls = [CapturedRequest("GET", api, {}, None, 200, "application/json", self.request("GET", api).text)] if api else []
        return RenderResult(html=self._home(), final_url=url, requests=calls)


def test_listing_candidates_rank_the_entitys_own_name_and_exact_matches_first():
    html = ('<a href="/about">About</a><a href="/mega-events">Mega Events</a><a href="/search">Advanced Search</a>'
            '<a href="/events">Events</a><a href="/events?tab=past">Past events</a><a href="/volunteer">Volunteer</a>'
            '<a href="https://other.x/events">Events elsewhere</a>')
    assert listing_candidates(html, "https://h.x/", EVENT_SPEC) == [
        "https://h.x/events", "https://h.x/mega-events", "https://h.x/search", "https://h.x/volunteer"]


def test_a_home_page_widget_feed_gives_way_to_the_sites_full_listing():
    site = HomeSite()
    items, stats = Crawler(Store(), site, no_llm).crawl("https://h.x/", EVENT_SPEC, CrawlOptions(workers=1))
    assert len(items) == 60 and stats.entry == "https://h.x/events"
    st = stats.plan["steps"][0]
    assert st["url"] == "https://h.x/api/events" and st["paginate"]["mode"] == "offset"
    assert site.rendered == ["https://h.x/", "https://h.x/events"]             # the exact match first, then stop
    nav = plans.learn_navigate(site, stats.entry)
    assert nav == [{"link": {"text": ["Events"], "href_contains": ["/events"]}}]


def test_a_seed_that_is_the_listing_is_not_second_guessed():
    site = HomeSite()
    items, stats = Crawler(Store(), site, no_llm).crawl("https://h.x/events", EVENT_SPEC, CrawlOptions(workers=1))
    assert len(items) == 60 and stats.entry is None and site.rendered == ["https://h.x/events"]


# ------------------------------------------------------------------ 7. placeholder images

def image_api(detail_og):
    """10 opportunities: 3 with their own picture, 7 with the site's stand-in. `detail_og(i)` = each item page's
    og:image."""
    stand_in = "https://cdn.i.x/images/Square%20logo_green.png"
    rows = [{"Title": f"Teach {i}", "StartDate": "2099-12-%02d" % (i + 1), "Location": f"School {i}",
             "ImageURL": f"https://cdn.i.x/opps/{i}.jpg" if i < 3 else stand_in, "OccurrenceUrl": f"/opportunity/o{i}"}
            for i in range(10)]

    def page(i):
        return lambda q, b, h: (200, f'<html><head><meta property="og:image" content="{detail_og(i)}"></head><body>x</body></html>')
    routes = {("POST", "https://i.x/search/list"): lambda q, b, h: (200, json.dumps({"listing": rows}))}
    routes.update({("GET", f"https://i.x/opportunity/o{i}"): page(i) for i in range(10)})
    f = FakeHTTP(routes)
    call = CapturedRequest("POST", "https://i.x/search/list", {"content-type": "application/x-www-form-urlencoded"},
                           "blockId=1", 200, "application/json", json.dumps({"listing": rows}))
    return f, call, stand_in


def test_a_stand_in_image_is_no_image_and_pages_showing_only_it_are_not_fetched():
    f, call, stand_in = image_api(lambda i: "https://cdn.i.x/images/Square%20logo_green.png")
    plan, items, _ = plan_learn.learn_plan(EVENT_SPEC, [call], "https://i.x/search", f, [], [])
    assert plan["placeholders"] == {"image": [stand_in]} and [s["id"] for s in plan["steps"]] == ["list"]
    assert [bool(i.get("image")) for i in items] == [True] * 3 + [False] * 7
    f.calls.clear()
    plans.run_plan(plan, f, EVENT_SPEC, "https://i.x/search")
    assert len(f.calls) == 1                                           # refresh: the list call, nothing else


def test_item_pages_with_real_pictures_are_read_only_for_items_that_lack_one():
    f, call, _ = image_api(lambda i: f"https://cdn.i.x/full/{i}.jpg")
    plan, items, _ = plan_learn.learn_plan(EVENT_SPEC, [call], "https://i.x/search", f, [], [])
    step = plan["steps"][-1]
    assert step["id"] == "detail_image" and step["missing"] == ["image"] and step["request"]["url"] == "https://i.x{item.OccurrenceUrl}"
    f.calls.clear()
    again, run = plans.run_plan(plan, f, EVENT_SPEC, "https://i.x/search")
    assert [i["image"] for i in again] == [f"https://cdn.i.x/opps/{i}.jpg" for i in range(3)] + \
        [f"https://cdn.i.x/full/{i}.jpg" for i in range(3, 10)]
    assert len(f.calls) == 1 + 7 and run.ai_calls == 0                 # the three with a picture cost no request


def test_one_image_on_most_items_of_a_run_is_dropped_even_if_learning_never_saw_it():
    items = [{"name": f"E{i}", "image": "https://x/og-share.jpg" if i < 4 else f"https://x/{i}.jpg"} for i in range(6)]
    out = jx.drop_placeholder_images(items)
    assert [i["image"] for i in out] == [None] * 4 + ["https://x/4.jpg", "https://x/5.jpg"]
    assert jx.placeholder_images(["https://x/img/no-image.png", "https://x/a.jpg"]) == ["https://x/img/no-image.png"]


@pytest.mark.parametrize("src,chrome", [("/img/logo.png", True), ("/media/posters/run.jpg", False), ("/i/sprite.svg", True)])
def test_site_chrome_is_never_offered_to_the_ai_as_an_items_picture(src, chrome):
    from scraper0.core.llm_extract import page_text
    html = f'<html><body><header><img src="/h/banner-top.jpg"></header><main><img src="{src}"></main></body></html>'
    images = page_text(html)[1]
    assert "/h/banner-top.jpg" not in images and ((src not in images) is chrome)
