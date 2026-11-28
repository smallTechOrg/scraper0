"""Recipe plans (core/plans.py) and the plan learner (core/plan_learn.py), against offline fake sites."""
import json
from urllib.parse import parse_qsl, urlsplit

import pytest
from conftest import json_calls

from scraper0.core import plan_learn, plans
from scraper0.core.entities import EVENT_SPEC
from scraper0.core.fetch import HTTPResult
from scraper0.core.render import CapturedRequest

TODAY = plans.today_ist()


class FakeHTTP:
    """routes: {(METHOD, url without query): handler(query: dict, body: str|None, headers: dict) → (status, text[, headers])}."""

    def __init__(self, routes, cookies=None):
        self.routes, self.calls, self._cookies = routes, [], dict(cookies or {})

    def request(self, method, url, headers=None, data=None):
        p = urlsplit(url)
        key = (method.upper(), f"{p.scheme}://{p.netloc}{p.path}")
        self.calls.append((method.upper(), url, dict(headers or {}), data))
        h = self.routes.get(key)
        if h is None:
            return HTTPResult(404, "not found", "text/plain")
        out = h(dict(parse_qsl(p.query)), data, {k.lower(): v for k, v in (headers or {}).items()})
        if out is None:
            return None
        status, text, *rest = out
        return HTTPResult(status, text, "application/json" if text[:1] in "{[" else "text/html", rest[0] if rest else {})

    def cookie(self, name):
        return self._cookies.get(name)


def ev(i, day="2099-12-%02d"):
    return {"id": f"e{i}", "title": f"Event {i}", "date": day % (i % 28 + 1), "venue": f"Hall {i}"}


def page_plan(**paginate):
    return {"plan_version": 1, "steps": [{"id": "list", "type": "request", "url": "https://api.x/events",
                                          "query": {"offset": "{page.value}"}, "paginate": paginate,
                                          "items": {"json": "data"}}],
            "mapping": {"name": "title", "start_time": "date", "address": "venue"}}


def offset_api(total=25, size=10, ignore=False):
    allev = [ev(i) for i in range(total)]

    def h(q, body, headers):
        off = 0 if ignore else int(q.get("offset", 0))
        return 200, json.dumps({"total": total, "data": allev[off:off + size]})
    return FakeHTTP({("GET", "https://api.x/events"): h})


# ------------------------------------------------------------------ schema

@pytest.mark.parametrize("plan,msg", [
    ({"plan_version": 2, "steps": []}, "plan_version"),
    ({"plan_version": 1, "steps": [{"id": "a", "type": "teleport"}]}, "unknown step type"),
    ({"plan_version": 1, "steps": [{"id": "a", "type": "browser"}]}, "action plan"),
    ({"plan_version": 1, "steps": [{"id": "a", "type": "request", "url": "u"},
                                   {"id": "b", "type": "ai", "over": "a", "fields": ["name"]}]}, "uses_ai"),
    ({"plan_version": 1, "steps": [{"id": "a", "type": "request", "url": "u", "uses_ai": True}]}, "only ai, captcha"),
    ({"plan_version": 1, "steps": [{"id": "a", "type": "request", "url": "u", "on_failure": {"goto": "a"}}]}, "later step"),
    ({"plan_version": 1, "steps": [{"id": "a", "type": "request", "url": "u", "retry": {"attempts": 9}}]}, "retry"),
    ({"plan_version": 1, "steps": [{"id": "a", "type": "request", "url": "u", "method": "DELETE"}]}, "GET or POST"),
])
def test_invalid_plans_are_rejected(plan, msg):
    with pytest.raises(plans.PlanError, match=msg):
        plans.validate(plan)


def test_templating_is_substitution_only():
    ctx = {"page": {"value": 30}, "input": {"q": "x"}}
    assert plans.render({"from": "{page.value}", "q": "s={input.q}&p={page.value}"}, ctx) == {"from": 30, "q": "s=x&p=30"}
    assert plans.render("{__import__('os')}", ctx) == "{__import__('os')}"        # not a placeholder: left as is
    with pytest.raises(plans.MissingValue):
        plans.render("{input.nope}", ctx)


# ------------------------------------------------------------------ paging

def test_offset_paging_stops_at_total():
    f = offset_api()
    items, st = plans.run_plan(page_plan(mode="offset", start=0, step=10, stop={"total_path": "total"}), f, EVENT_SPEC, "https://x/")
    assert len(items) == 25 and st.pages == 3 and st.ai_calls == 0


def test_page_mode_fewer_than_and_max_pages_and_ignored_param():
    f = offset_api(total=100)
    items, st = plans.run_plan(page_plan(mode="offset", start=0, step=10, max_pages=2), f, EVENT_SPEC, "https://x/")
    assert len(items) == 20 and st.pages == 2                                   # max_pages cap
    items, st = plans.run_plan(page_plan(mode="offset", start=0, step=10, stop={"fewer_than": 10}),
                               offset_api(total=15), EVENT_SPEC, "https://x/")
    assert len(items) == 15 and st.pages == 2                                   # short page ends it
    items, st = plans.run_plan(page_plan(mode="offset", start=0, step=10), offset_api(ignore=True), EVENT_SPEC, "https://x/")
    assert len(items) == 10 and st.pages == 1                                   # same page again: param ignored, stop


def test_cursor_paging():
    pages = {"": ("c2", [ev(1), ev(2)]), "c2": ("c3", [ev(3)]), "c3": (None, [ev(4)])}

    def h(q, body, headers):
        nxt, data = pages[q.get("cursor", "")]
        return 200, json.dumps({"next": nxt, "data": data})
    plan = {"plan_version": 1, "steps": [{"id": "list", "type": "request", "url": "https://api.x/events",
                                          "query": {"cursor": "{page.value}"},
                                          "paginate": {"mode": "cursor", "start": "", "cursor_path": "next"},
                                          "items": {"json": "data"}}],
            "mapping": {"name": "title", "start_time": "date"}}
    items, st = plans.run_plan(plan, FakeHTTP({("GET", "https://api.x/events"): h}), EVENT_SPEC, "https://x/")
    assert [i["name"] for i in items] == ["Event 1", "Event 2", "Event 3", "Event 4"] and st.pages == 3


# ------------------------------------------------------------------ for_each, capture, lookups

def detail_site(bad=()):
    listing = "<html><body>" + "".join(f'<a href="/e/{i}">x</a>' for i in range(3)) + "</body></html>"
    routes = {("GET", "https://s.x/events"): lambda q, b, h: (200, listing)}
    for i in range(3):
        html = f"<html><body><h1>Talk {i}</h1><time>2099-12-0{i + 1} 10:00</time></body></html>"
        routes[("GET", f"https://s.x/e/{i}")] = (lambda html: (lambda q, b, h: (200, html)))(html) if i not in bad \
            else (lambda q, b, h: (500, "boom"))
    return FakeHTTP(routes)


def detail_plan(optional):
    rules = {"name": [{"sel": "h1"}], "start_time": [{"sel": "time"}]}
    return {"plan_version": 1, "steps": [
        {"id": "list", "type": "request", "url": "https://s.x/events", "items": {"links": {"patterns": ["/e/*"]}}},
        {"id": "detail", "type": "for_each", "over": "list", "optional": optional, "retry": {"attempts": 1},
         "request": {"url": "{item.url}"}, "extract": {"selectors": [{"path_pattern": "/e/*", "rules": rules}]}}]}


def test_for_each_reads_detail_pages_and_optional_keeps_failures():
    items, st = plans.run_plan(detail_plan(False), detail_site(bad={1}), EVENT_SPEC, "https://s.x/events")
    assert sorted(i["name"] for i in items) == ["Talk 0", "Talk 2"] and st.detail_failures == 1
    _, st = plans.run_plan(detail_plan(True), detail_site(bad={1}), EVENT_SPEC, "https://s.x/events")
    assert st.detail_failures == 1                         # kept as a record, but it has no fields → not an event


def test_capture_from_json_regex_header_and_cookie():
    seen = {}

    def token(q, b, h):
        return 200, json.dumps({"auth": {"token": "T1"}, "nonce": "N9"}), {"x-csrf": "C3"}

    def data(q, b, h):
        seen.update(h)
        return 200, json.dumps({"data": [ev(1), ev(2)]})
    plan = {"plan_version": 1, "steps": [
        {"id": "tok", "type": "request", "url": "https://api.x/token",
         "capture": [{"name": "t", "from": "json", "path": "auth.token"}, {"name": "n", "from": "regex", "pattern": '"nonce": "(\\w+)"'},
                     {"name": "c", "from": "header", "header": "X-CSRF"}, {"name": "s", "from": "cookie", "cookie": "sid"}]},
        {"id": "list", "type": "request", "url": "https://api.x/events", "items": {"json": "data"},
         "headers": {"authorization": "Bearer {captures.t}", "x-nonce": "{captures.n}", "x-csrf": "{captures.c}",
                     "x-sid": "{captures.s}"}}],
        "mapping": {"name": "title", "start_time": "date"}}
    f = FakeHTTP({("GET", "https://api.x/token"): token, ("GET", "https://api.x/events"): data}, cookies={"sid": "S7"})
    items, _ = plans.run_plan(plan, f, EVENT_SPEC, "https://x/")
    assert len(items) == 2
    assert seen["authorization"] == "Bearer T1" and seen["x-nonce"] == "N9" and seen["x-csrf"] == "C3" and seen["x-sid"] == "S7"


def test_lookup_joins_names():
    plan = {"plan_version": 1, "steps": [
        {"id": "list", "type": "request", "url": "https://api.x/events", "items": {"json": "data"}},
        {"id": "orgs", "type": "lookup", "request": {"url": "https://api.x/orgs"}, "items": {"json": ""},
         "field": "organisation", "key": "org", "id_key": "id", "value": "name"}],
        "mapping": {"name": "title", "start_time": "date"}}
    f = FakeHTTP({("GET", "https://api.x/events"): lambda q, b, h: (200, json.dumps({"data": [dict(ev(1), org="o1")]})),
                  ("GET", "https://api.x/orgs"): lambda q, b, h: (200, json.dumps([{"id": "o1", "name": "Lake Friends"}]))})
    [item], _ = plans.run_plan(plan, f, EVENT_SPEC, "https://x/")
    assert item["organisation"] == "Lake Friends"


# ------------------------------------------------------------------ failure handling

def simple(url="https://api.x/events", **extra):
    return {"id": "list", "type": "request", "url": url, "items": {"json": "data"}, **extra}


def run(steps, f, **plan):
    return plans.run_plan({"plan_version": 1, "steps": steps, "mapping": {"name": "title", "start_time": "date"}, **plan},
                          f, EVENT_SPEC, "https://x/")


def ok(events):
    return lambda q, b, h: (200, json.dumps({"data": events}))


def test_any_of_falls_back_and_records_the_winner():
    f = FakeHTTP({("GET", "https://api.x/v2/events"): ok([ev(1)])})
    items, st = run([{"id": "list", "type": "any_of", "retry": {"attempts": 1},
                      "alternatives": [simple("https://api.x/v1/events"), simple("https://api.x/v2/events")]}], f)
    assert len(items) == 1 and st.alternatives == {"list": 1}


def test_retry_on_5xx_then_success():
    n = {"calls": 0}

    def flaky(q, b, h):
        n["calls"] += 1
        return (503, "busy") if n["calls"] < 3 else (200, json.dumps({"data": [ev(1)]}))
    items, st = run([simple(retry={"attempts": 3, "backoff_s": 0})], FakeHTTP({("GET", "https://api.x/events"): flaky}))
    assert len(items) == 1 and st.retries == {"list": 2}
    with pytest.raises(plans.PlanError, match="HTTP 404"):                     # not transient: no retry
        run([simple(url="https://api.x/missing", retry={"attempts": 3, "backoff_s": 0})], FakeHTTP({}))


def test_expect_catches_silent_breakage():
    f = FakeHTTP({("GET", "https://api.x/events"): ok([])})
    with pytest.raises(plans.PlanError, match="expect failed: 0 item"):
        run([simple(expect={"min_items": 1})], f)
    f = FakeHTTP({("GET", "https://api.x/events"): ok([{"id": 1, "title": "No date", "date": None}] * 3)})
    with pytest.raises(plans.PlanError, match="have start_time"):
        run([simple(expect={"coverage": {"fields": ["start_time"], "min_ratio": 0.5}})], f)


def test_on_failure_goto_runs_the_fallback_branch():
    f = FakeHTTP({("GET", "https://api.x/backup"): ok([ev(1), ev(2)])})
    items, st = run([simple(retry={"attempts": 1}, on_failure={"goto": "backup"}),
                     dict(simple("https://api.x/backup"), id="backup", fallback_only=True)], f, output="list")
    assert len(items) == 2 and st.outcomes == {"list": "failed", "backup": "fallback"}
    items, st = run([simple("https://api.x/backup"), dict(simple("https://api.x/backup"), id="backup", fallback_only=True)],
                    f, output="list")
    assert st.outcomes["backup"] == "skipped"                                   # not jumped to: never runs


def test_when_conditions():
    f = FakeHTTP({("GET", "https://api.x/token"): lambda q, b, h: (200, json.dumps({"mode": "new"})),
                  ("GET", "https://api.x/new"): ok([ev(1)]), ("GET", "https://api.x/old"): ok([ev(2), ev(3)])})
    items, st = run([{"id": "tok", "type": "request", "url": "https://api.x/token",
                      "capture": [{"name": "mode", "from": "json", "path": "mode"}]},
                     dict(simple("https://api.x/old"), id="old", when={"captured": "mode", "op": "equals", "value": "old"}),
                     dict(simple("https://api.x/new"), id="new", when={"captured": "mode", "op": "equals", "value": "new"})], f)
    assert [i["name"] for i in items] == ["Event 1"] and st.outcomes["old"] == "skipped"


def test_navigate_fallback_when_the_direct_url_moved():
    home = '<html><body><a href="/whats-on">Upcoming Events</a><a href="/about">About</a></body></html>'
    listing = '<html><body><a href="/e/0">x</a></body></html>'
    detail = "<html><body><h1>Talk</h1><time>2099-12-01 10:00</time></body></html>"
    f = FakeHTTP({("GET", "https://s.x/"): lambda q, b, h: (200, home),
                  ("GET", "https://s.x/whats-on"): lambda q, b, h: (200, listing),
                  ("GET", "https://s.x/e/0"): lambda q, b, h: (200, detail)})
    layouts = [{"path_pattern": "/e/*", "rules": {"name": [{"sel": "h1"}], "start_time": [{"sel": "time"}]}}]
    plan = plans.from_selectors(layouts, "https://s.x/events", patterns=["/e/*"],
                                navigate=[{"link": {"text": ["Events", "Upcoming events"], "href_contains": ["/events"]}}])
    items, st = plans.run_plan(plan, f, EVENT_SPEC, "https://s.x/events")      # /events now 404s
    assert [i["name"] for i in items] == ["Talk"]
    assert st.entry == "navigate" and st.entry_changed_to == "https://s.x/whats-on"


def test_learn_navigate_records_the_home_page_link():
    home = '<html><body><a href="/events">Events</a><a href="https://www.s.x/events/">All events</a></body></html>'
    f = FakeHTTP({("GET", "https://s.x/"): lambda q, b, h: (200, home)})
    assert plans.learn_navigate(f, "https://s.x/events") == [{"link": {"text": ["Events", "All events"],
                                                                       "href_contains": ["/events"]}}]
    assert plans.learn_navigate(f, "https://s.x/") == []


# ------------------------------------------------------------------ AI only in declared steps

class CountingAI:
    def __init__(self):
        self.calls, self.last_usage = 0, {}

    def text_fn(self, purpose):
        def call(prompt):
            self.calls += 1
            self.last_usage = {"input_tokens": 100, "output_tokens": 20}
            return json.dumps({"found": True, "items": [{"name": {"value": "Talk", "source": "Talk"},
                                                         "address": {"value": "Hall 9", "source": "Hall 9"}}]})
        return call


def test_plans_without_ai_steps_never_get_an_ai_handle():
    ai = CountingAI()
    _, st = run([simple()], FakeHTTP({("GET", "https://api.x/events"): ok([ev(1)])}))
    r = plans.Runner(FakeHTTP({}), ai=None)
    assert r._ai is None and st.ai_calls == 0
    plan = {"plan_version": 1, "steps": [simple()], "mapping": {"name": "title", "start_time": "date"}}
    items, st = plans.run_plan(plan, FakeHTTP({("GET", "https://api.x/events"): ok([ev(1)])}), EVENT_SPEC, "https://x/", ai=ai)
    assert items and ai.calls == 0 and st.ai_calls == 0                        # dropped by run_plan


def test_ai_step_is_the_only_place_ai_runs_and_is_accounted():
    ai = CountingAI()
    listing = '<html><body><a href="/e/0">x</a><a href="/e/1">y</a></body></html>'
    page0 = "<html><body><h1>Talk</h1><time>2099-12-01 10:00</time><p>Hall 9</p>" + "<p>words</p>" * 20 + "</body></html>"
    page1 = "<html><body><h1>Talk</h1><time>2099-12-02 10:00</time><address>Main Hall</address></body></html>"
    f = FakeHTTP({("GET", "https://s.x/events"): lambda q, b, h: (200, listing),
                  ("GET", "https://s.x/e/0"): lambda q, b, h: (200, page0),
                  ("GET", "https://s.x/e/1"): lambda q, b, h: (200, page1)})
    rules = {"name": [{"sel": "h1"}], "start_time": [{"sel": "time"}], "address": [{"sel": "address"}]}
    plan = {"plan_version": 1, "steps": [
        {"id": "list", "type": "request", "url": "https://s.x/events", "items": {"links": {"patterns": ["/e/*"]}}},
        {"id": "detail", "type": "for_each", "over": "list", "request": {"url": "{item.url}"},
         "extract": {"selectors": [{"path_pattern": "/e/*", "rules": rules}]}},
        {"id": "fill", "type": "ai", "uses_ai": True, "over": "list", "fields": ["address"]}]}
    items, st = plans.run_plan(plan, f, EVENT_SPEC, "https://s.x/events", ai=ai)
    assert sorted(i["address"] for i in items) == ["Hall 9", "Main Hall"]
    assert ai.calls == 1                                   # only the page whose address the selectors missed
    assert st.ai_calls == 1 and st.ai_tokens == 120 and st.ai == {"fill": {"calls": 1, "tokens": 120}}
    with pytest.raises(plans.PlanError, match="no AI client"):
        plans.run_plan(plan, f, EVENT_SPEC, "https://s.x/events")


# ------------------------------------------------------------------ learning search APIs

def mybharat_like():
    """POST JSON, page numbers as strings, a total, and a date filter that really filters."""
    allev = [{"_source": {"event_name": f"Drive {i}", "event_from_date": "2099-12-%02d" % (i % 27 + 1),
                          "event_from_time": "10:00:00", "address": f"Ward {i}", "image_path": f"img{i}.jpg",
                          "slug": f"drive-{i}"}} for i in range(30)]

    def h(q, body, headers):
        b = json.loads(body)
        assert headers.get("content-type", "").startswith("application/json")
        page, limit = int(b["page"]), b["limit"]
        gt = b["query"]["bool"]["must"][1]["range"]["event_from_date"]["gt"]
        pool = allev if gt == TODAY else allev[:3]
        return 200, json.dumps({"total": len(pool), "records": pool[(page - 1) * limit: page * limit]})
    body = {"query": {"bool": {"must": [{"term": {"type": "vo"}}, {"range": {"event_from_date": {"gt": TODAY}}}]}},
            "page": "1", "limit": 12}
    f = FakeHTTP({("POST", "https://search.mb.x/events"): h})
    first = h({}, json.dumps(body), {"content-type": "application/json"})[1]
    call = CapturedRequest("POST", "https://search.mb.x/events", {"content-type": "application/json", "origin": "https://mb.x"},
                           json.dumps(body), 200, "application/json", first)
    return f, call


def test_learns_post_json_search_api_with_paging_date_and_image_prefix():
    f, call = mybharat_like()
    imgs = ["https://cdn.mb.x/events/img0.jpg", "https://cdn.mb.x/events/img1.jpg"]
    plan, items, _ = plan_learn.learn_plan(EVENT_SPEC, [call], "https://mb.x/", f, [], imgs)
    st = plan["steps"][0]
    assert st["method"] == "POST" and st["items"] == {"json": "records.*._source"}
    assert st["paginate"]["mode"] == "page" and st["paginate"]["type"] == "str" and st["paginate"]["stop"]["total_path"] == "total"
    assert st["body"]["json"]["page"] == "{page.value}"
    assert st["body"]["json"]["query"]["bool"]["must"][1]["range"]["event_from_date"]["gt"] == "{today}"
    assert plan["mapping"]["image"] == {"template": "https://cdn.mb.x/events/{image_path}"}
    assert len(items) == 30 and items[0]["image"].startswith("https://cdn.mb.x/events/")
    f.calls.clear()
    again, run_ = plans.run_plan(plan, f, EVENT_SPEC, "https://mb.x/")
    assert len(again) == 30 and run_.pages == 3 and run_.ai_calls == 0      # cold run: no browser, no AI


def test_learns_form_encoded_offset_api():
    opps = [{"Title": f"Teach {i}", "StartDate": "2099-12-%02d" % (i % 27 + 1), "Location": f"School {i}",
             "SID": f"s{i}"} for i in range(20)]

    def h(q, body, headers):
        form = dict(parse_qsl(body))
        off = int(form["currentRows"])
        return 200, json.dumps({"total": len(opps), "opportunities": opps[off:off + 8]})
    f = FakeHTTP({("POST", "https://iv.x/search"): h})
    body = "parameters%5Bkeyword%5D=&currentRows=0&isSearch=true"
    call = CapturedRequest("POST", "https://iv.x/search", {"content-type": "application/x-www-form-urlencoded; charset=UTF-8",
                                                          "x-requested-with": "XMLHttpRequest"},
                           body, 200, "application/json", h({}, body, {})[1])
    plan, items, _ = plan_learn.learn_plan(EVENT_SPEC, [call], "https://iv.x/search", f, [], [])
    st = plan["steps"][0]
    assert st["body"]["form"]["currentRows"] == "{page.value}" and st["paginate"]["mode"] == "offset"
    assert st["paginate"]["step"] == 8 and st["headers"]["x-requested-with"] == "XMLHttpRequest"
    assert len(items) == 20


def test_learns_a_token_capture_when_the_replay_needs_it():
    data = [ev(i) for i in range(4)]

    def api(q, body, headers):
        return (200, json.dumps({"data": data})) if headers.get("authorization") == "Bearer tok-123" else (401, "{}")
    f = FakeHTTP({("GET", "https://api.t.x/session"): lambda q, b, h: (200, json.dumps({"session": {"jwt": "tok-123"}})),
                  ("GET", "https://api.t.x/events"): api})
    sess = CapturedRequest("GET", "https://api.t.x/session", {}, None, 200, "application/json",
                           json.dumps({"session": {"jwt": "tok-123"}}))
    call = CapturedRequest("GET", "https://api.t.x/events", {"accept": "application/json"}, None, 200, "application/json",
                           json.dumps({"data": data}), secret_headers=["authorization"])
    call.raw_secret_values = {"authorization": "Bearer tok-123"}               # what the browser sent (learning only)
    plan, items, _ = plan_learn.learn_plan(EVENT_SPEC, [sess, call], "https://t.x/", f, [], [])
    tok, lst = plan["steps"][0], plan["steps"][1]
    assert tok["capture"] == [{"name": "token", "from": "json", "path": "session.jwt"}]
    assert lst["headers"]["authorization"] == "Bearer {captures.token}" and "tok-123" not in json.dumps(plan)
    assert len(items) == 4


def test_unreplayable_call_is_not_learned():
    f = FakeHTTP({("GET", "https://api.t.x/events"): lambda q, b, h: (401, "{}")})
    call = CapturedRequest("GET", "https://api.t.x/events", {}, None, 200, "application/json",
                           json.dumps({"data": [ev(1), ev(2)]}), secret_headers=["authorization"])
    with pytest.raises(plan_learn.LearnFailed, match="without the browser fails.*authorization"):
        plan_learn.learn_plan(EVENT_SPEC, [call], "https://t.x/", f, [], [])


def test_get_api_offset_in_query_is_confirmed():
    f = offset_api(total=25)
    first = json.dumps({"total": 25, "data": [ev(i) for i in range(10)]})
    calls = json_calls([("https://api.x/events?offset=0&limit=10", first)])
    plan, items, _ = plan_learn.learn_plan(EVENT_SPEC, calls, "https://x/", f, [], [])
    st = plan["steps"][0]
    assert st["query"]["offset"] == "{page.value}" and st["paginate"]["mode"] == "offset" and len(items) == 25


def test_json_encoded_as_a_string_and_201_responses():
    """iVolunteer-style double encoding ("listing": "[{...}]") and APIs answering 201."""
    opps = [{"Title": f"Teach {i}", "StartDate": "2099-12-0%d" % (i + 1), "Location": f"School {i}"} for i in range(3)]
    body = json.dumps({"total": 3, "listing": json.dumps(opps)})
    f = FakeHTTP({("POST", "https://iv.x/first"): lambda q, b, h: (201, body)})
    call = CapturedRequest("POST", "https://iv.x/first", {"content-type": "application/x-www-form-urlencoded"},
                           "blockId=1", 201, "text/html", body)
    plan, items, _ = plan_learn.learn_plan(EVENT_SPEC, [call], "https://iv.x/search", f, [], [])
    assert plan["steps"][0]["items"] == {"json": "listing"} and plan["mapping"]["start_time"] == "StartDate"
    assert [i["name"] for i in items] == ["Teach 0", "Teach 1", "Teach 2"]


def test_list_member_fields_rank_below_direct_keys():
    from scraper0.core import json_extract as jx
    rows = [{"event_name": f"Drive {i}", "eventorganizer": [{"name": f"Org {i}"}], "event_from_date": "2099-12-01"}
            for i in range(3)]
    m = jx.heuristic_mapping(EVENT_SPEC, rows)
    assert m["name"] == {"first_of": ["event_name", "eventorganizer.0.name"]} or m["name"] == "event_name"


def test_paging_on_an_api_with_unstable_order_is_learned_and_marked():
    import random
    rng = random.Random(7)
    allev = [ev(i) for i in range(40)]

    def h(q, body, headers):
        page = int(json.loads(body)["page"])
        pool = allev[(page - 1) * 10: page * 10 + 6]          # ties: each call returns 10 of 16 candidates
        return 200, json.dumps({"total": 40, "data": rng.sample(pool, 10)})
    f = FakeHTTP({("POST", "https://api.u.x/s"): h})
    body = json.dumps({"page": 1, "size": 10})
    call = CapturedRequest("POST", "https://api.u.x/s", {"content-type": "application/json"}, body, 200,
                           "application/json", h({}, body, {})[1])
    plan, items, _ = plan_learn.learn_plan(EVENT_SPEC, [call], "https://u.x/", f, [], [])
    pg = plan["steps"][0]["paginate"]
    assert pg["mode"] == "page" and pg.get("unstable_order") is True and len(items) >= 30
