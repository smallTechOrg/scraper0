"""v2 sites & actions (spec/api-v2.md) end-to-end over HTTP: onboarding, learn/lock/contract, reads with freshness and
the shared store, per-tenant AI verdicts, lookups, idempotent writes. Offline: fake fetcher, fake connectors, fake AI."""
import json
import re
import threading
import time
from datetime import datetime, timedelta

import pytest
from fastapi.testclient import TestClient

from scraper0.config import get_settings
from scraper0.core.dates import IST, to_iso
from scraper0.core.llm import LLMResult
from scraper0.core.render import RenderResult
from scraper0.db.models import Job, SiteItem, WriteRecord
from scraper0.db.session import session_scope

H = {"X-Requested-With": "scraper0"}
ADMIN = ("root@example.com", "correct horse battery")


# ------------------------------------------------------------------ fixtures / helpers

@pytest.fixture
def settings(monkeypatch):
    from pydantic import SecretStr
    st = get_settings()
    monkeypatch.setattr(st, "bootstrap_admin_email", ADMIN[0])
    monkeypatch.setattr(st, "bootstrap_admin_password", SecretStr(ADMIN[1]))
    monkeypatch.setattr(st, "bootstrap_legacy_tenant", "")
    return st


@pytest.fixture
def app(settings):
    from scraper0.api import routes_public
    from scraper0.api.app import create_app
    from scraper0.bootstrap import seed
    routes_public._rl.clear()
    seed()
    return create_app(run_startup=False)


@pytest.fixture
def thread_mode(settings, monkeypatch):
    monkeypatch.setattr(settings, "job_mode", "thread")


def login(app) -> TestClient:
    c = TestClient(app)
    assert c.post("/v1/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]}).status_code == 200
    return c


def onboard(app, admin, slug):
    ar = TestClient(app).post("/v1/access-requests", json={"name": "A", "email": f"a@{slug}.org", "organisation": slug})
    r = admin.post(f"/v1/admin/access-requests/{ar.json()['data']['id']}/approve", json={"slug": slug}, headers=H)
    token = r.json()["data"]["invite_url"].rsplit("/", 1)[-1]
    c = TestClient(app)
    assert c.post("/v1/auth/accept-invite", json={"token": token, "name": "A", "password": "pw-123456789"}).status_code == 200
    key = c.post("/v1/api-keys", json={"name": "k"}, headers=H).json()["data"]["key"]
    return c, TestClient(app, headers={"X-API-Key": key})


@pytest.fixture
def tenants(app):
    admin = login(app)
    acme, acme_key = onboard(app, admin, "acme")
    zed, zed_key = onboard(app, admin, "zed")
    return {"admin": admin, "acme": acme, "acme_key": acme_key, "zed": zed, "zed_key": zed_key}


def add_shared_ai(admin):
    r = admin.post("/v1/admin/ai/providers", headers=H, json={"name": "Groq", "kind": "groq", "model": "llama",
                                                               "api_key": "gsk_secret_value_1234"})
    assert r.status_code == 201, r.text


def day(n: int, hh: int = 7, mm: int = 0) -> str:
    d = datetime.now(IST).replace(hour=hh, minute=mm, second=0, microsecond=0) + timedelta(days=n)
    return to_iso(d)


def ymd(n: int) -> str:
    return (datetime.now(IST) + timedelta(days=n)).date().isoformat()


def ev(i, name, start, online=False, desc="Bring water."):
    return {"name": name, "image": f"https://cdn.x/{i}.jpg", "portal": "TEAMEVEREST", "start_time": start,
            "end_time": None, "address": "Online" if online else f"Park {i}, Bengaluru",
            "link": f"https://www.teameverest.ngo/events/{i}", "organisation": "Team Everest", "type": "OTHER",
            "description": desc, "is_online": online}


class FakeConnector:
    """Stands in for a connector action: returns `events`, raises, or blocks until released."""
    def __init__(self, events=None):
        self.events, self.calls, self.fail, self.gate = events or [], 0, None, None

    def __call__(self, data, ctx):
        from scraper0.connectors.base import ConnectorError
        self.calls += 1
        if self.gate is not None:
            assert self.gate.wait(10)
        if self.fail:
            raise ConnectorError(self.fail)
        return {"events": [dict(e) for e in self.events]}


@pytest.fixture
def everest(monkeypatch):
    from scraper0.connectors.base import get
    f = FakeConnector([ev(1, "Lake Cleanup", day(1)), ev(2, "Tree Planting", day(3)), ev(3, "Waste Webinar", day(2), True),
                       ev(4, "Old Drive", day(-3)), ev(5, "Dawn Walk", day(0, 0, 30))])
    monkeypatch.setitem(get("TEAMEVEREST").actions, "FETCH_EVENTS", f)
    return f


def read(c, site="teameverest-ngo", action="fetch_events", **body):
    return c.post(f"/v2/sites/{site}/actions/{action}/read", json=body)


def wait_jobs():
    for _ in range(200):
        with session_scope() as s:
            if not s.query(Job).filter(Job.status.in_(["queued", "running"])).count():
                return
        time.sleep(0.05)
    raise AssertionError("jobs still running")


# ------------------------------------------------------------------ sites

def test_site_id_derivation():
    from scraper0.services.sites import parse_base_url
    assert parse_base_url("https://www.teameverest.ngo/events?x=1") == ("teameverest-ngo", "https://www.teameverest.ngo",
                                                                        "teameverest.ngo")
    assert parse_base_url("https://karmana.in/anything")[0] == "karmana-in"
    assert parse_base_url("http://Sub.Example.co.uk:8080/")[0] == "sub-example-co-uk-8080"


def test_create_site_reuses_existing_and_seeded_actions(app, tenants):
    acme, zed = tenants["acme"], tenants["zed"]
    r = acme.post("/v2/sites", json={"base_url": "https://karmana.in/anything"}, headers=H)
    assert r.status_code == 201 and r.json()["existing"] is False
    site = r.json()["data"]
    assert site["site_id"] == "karmana-in" and site["base_url"] == "https://karmana.in" and site["actions"] == []
    assert site["created_by_tenant"] == "acme"
    r = zed.post("/v2/sites", json={"base_url": "http://www.karmana.in/"}, headers=H)       # another tenant: same site
    assert r.status_code == 200 and r.json()["existing"] is True and r.json()["data"]["site_id"] == "karmana-in"
    assert acme.post("/v2/sites", json={"base_url": "ftp://x"}, headers=H).json()["error"]["code"] == "invalid_input"
    assert tenants["acme_key"].post("/v2/sites", json={"base_url": "https://a.org"}).status_code == 403   # not an admin

    from scraper0.bootstrap import seed
    seed()                                                                                  # idempotent
    sites = {s["site_id"]: s for s in acme.get("/v2/sites").json()["data"]}
    assert {"teameverest-ngo", "mybharat-gov-in", "ivolunteer-in", "smartoneblr-com", "karmana-in"} <= set(sites)
    assert sites["smartoneblr-com"]["actions"] == [{"name": "report_issue", "kind": "write", "status": "locked"},
                                                   {"name": "track_issue", "kind": "read", "status": "locked"}]
    assert [s["site_id"] for s in acme.get("/v2/sites?q=bharat").json()["data"]] == ["mybharat-gov-in"]
    a = acme.get("/v2/sites/teameverest-ngo/actions/fetch_events").json()["data"]
    assert a["implementation"] == "code" and a["locked_version"] == 1 and a["status"] == "locked"
    [v] = acme.get("/v2/sites/teameverest-ngo/actions/fetch_events/versions").json()["data"]
    assert v["status"] == "locked" and v["implementation_snapshot"]["connector"] == "TEAMEVEREST"
    c = acme.get("/v2/sites/smartoneblr-com/actions/report_issue/contract").json()["data"]
    assert c["kind"] == "write" and c["endpoint"].endswith("/v2/sites/smartoneblr-com/actions/report_issue/write")
    assert "Idempotency-Key" in c["curl"] and "$SCRAPER0_API_KEY" in c["curl"]


def test_error_codes(app, tenants):
    acme = tenants["acme_key"]
    assert read(acme, site="nope-org").json()["error"]["code"] == "site_not_onboarded"
    assert read(acme, action="nope").json()["error"]["code"] == "action_not_available"
    assert tenants["acme"].get("/v2/sites/nope-org").status_code == 404
    tenants["acme"].post("/v2/sites", json={"base_url": "https://acts.org"}, headers=H)
    r = tenants["acme"].post("/v2/sites/acts-org/actions", json={"name": "fetch_events", "kind": "read"}, headers=H)
    assert r.status_code == 201 and r.json()["data"]["status"] == "draft"
    assert tenants["acme"].post("/v2/sites/acts-org/actions", json={"name": "fetch_events"}, headers=H).json()[
        "error"]["code"] == "action_exists"
    assert tenants["acme"].post("/v2/sites/acts-org/actions", json={"name": "x", "seed_urls": ["https://other.org/"]},
                                headers=H).json()["error"]["code"] == "invalid_input"
    r = read(acme, site="acts-org")                                                         # no locked version yet
    assert r.status_code == 404 and r.json()["error"]["code"] == "action_not_available"
    assert tenants["acme"].get("/v2/sites/acts-org/actions/fetch_events/contract").json()["error"]["code"] == \
        "action_not_available"
    assert read(acme, options={"from": "26-11-2026"}).json()["error"]["code"] == "invalid_input"
    assert read(acme, timeout_s=999).json()["error"]["code"] == "invalid_input"
    assert read(acme, site="smartoneblr-com", action="report_issue").json()["error"]["code"] == "invalid_input"
    assert TestClient(app).get("/v2/sites").json()["error"]["code"] == "unauthenticated"


# ------------------------------------------------------------------ learned JSON-API site: learn → lock → contract → read

def activity(i, name, n, time_, addr, online=False, desc=None):
    return {"id": f"act-{i:04d}", "name": name, "description": desc or f"<p>Join {name}</p>", "date": ymd(n),
            "start_time": time_, "media_urls": [f"/files/{i}.webp"],
            "location": {"address": addr, "latitude": 12.9, "longitude": 77.6, "is_online": online}}


SHELL = '<html><head><title>Acts</title></head><body><div id="root"></div><script src="/m.js"></script></body></html>'


class ActsSite:
    """A React site: the HTML is a shell; the page calls /api/activities."""
    def __init__(self):
        self.acts = [activity(1, "Ulsoor Lake Cleanup", 2, "07:00", "Ulsoor Lake"),
                     activity(2, "Cubbon Tree Walk", 4, "16:45", "Cubbon Park"),
                     activity(3, "Waste Policy Webinar", 5, "18:00", "Online", online=True)]
        self.renders = 0

    def fetcher(self):
        site = self

        class F:
            render_hosts = set()

            def get(self, url):
                if url in ("https://acts.org", "https://acts.org/"):
                    return SHELL, "text/html"
                if url == "https://acts.org/api/activities":
                    return json.dumps(site.acts), "application/json"
                return None

            def render(self, url):
                site.renders += 1
                cards = "".join(f'<a href="/activities?activity_id={a["id"]}">{a["name"]}</a>' for a in site.acts)
                return RenderResult(html=f"<html><body>{cards}</body></html>", final_url=url,
                                    json_responses=[("https://acts.org/api/activities", json.dumps(site.acts))])
        return F()


@pytest.fixture
def acts(monkeypatch):
    site = ActsSite()
    monkeypatch.setattr("scraper0.jobs.execute._fetcher", site.fetcher)
    return site


def test_learn_lock_contract_and_read_from_snapshot(app, tenants, acts):
    acme, key = tenants["acme"], tenants["acme_key"]
    acme.post("/v2/sites", json={"base_url": "https://acts.org"}, headers=H)
    acme.post("/v2/sites/acts-org/actions", json={"name": "fetch_events", "kind": "read", "entity": "event"}, headers=H)
    r = acme.post("/v2/sites/acts-org/actions/fetch_events/learn", headers=H)
    assert r.status_code == 202 and r.json()["data"]["version"] == 1
    job = acme.get(f"/v1/jobs/{r.json()['data']['job_id']}").json()["data"]
    assert job["status"] == "succeeded" and job["result"]["preview"]["count"] == 3        # online kept while learning
    [v] = acme.get("/v2/sites/acts-org/actions/fetch_events/versions").json()["data"]
    assert v["status"] == "draft" and v["preview"]["count"] == 3 and len(v["preview"]["items"]) == 3
    assert v["preview"]["items"][0]["category"] is None and v["preview"]["items"][0]["name"] == "Ulsoor Lake Cleanup"
    snap = v["implementation_snapshot"]
    assert snap["strategy"] == "json_api" and snap["recipes"][0]["rules"]["api_url"] == "https://acts.org/api/activities"
    assert acts.renders == 2           # the page, plus its /activities list page once (looking for detail links)

    c = acme.post("/v2/sites/acts-org/actions/fetch_events/versions/1/lock", headers=H).json()["data"]
    assert c["version"] == 1 and c["endpoint"] == "http://testserver/v2/sites/acts-org/actions/fetch_events/read"
    assert '"X-API-Key: $SCRAPER0_API_KEY"' in c["curl"] and '"freshness_hours": 48' in c["curl"]
    assert acme.get("/v2/sites/acts-org/actions/fetch_events").json()["data"]["status"] == "locked"

    # the live recipe is relearned/broken later: the locked version keeps working from its snapshot
    from scraper0.core.store import default_store
    for rec in default_store().list_recipes("acts.org"):
        default_store().save_recipe(rec["domain"], rec["path_pattern"], rec["entity"], "json_api",
                                    {"api_url": "https://acts.org/api/gone", "mapping": {}}, rec["sample_url"])
    d = read(key, site="acts-org").json()["data"]
    assert d["complete"] is True and d["refresh_job_id"] and d["health"] == "ok" and d["version"] == 1
    assert [i["data"]["name"] for i in d["items"]] == ["Ulsoor Lake Cleanup", "Cubbon Tree Walk"]   # online dropped
    assert acts.renders == 2                                                                 # no browser on refresh
    it = d["items"][1]
    assert re.fullmatch(r"itm_[0-9a-f]{24}", it["id"])
    assert set(it["data"]) == {"name", "start_time", "end_time", "address", "link", "organisation", "image", "category",
                               "description", "latitude", "longitude", "is_online", "price", "source_url"}
    assert it["data"]["link"] == "https://acts.org/activities?activity_id=act-0002" and it["data"]["category"] is None

    # second version: lock retires the first
    acme.post("/v2/sites/acts-org/actions/fetch_events/learn", headers=H)
    acme.post("/v2/sites/acts-org/actions/fetch_events/versions/2/lock", headers=H)
    vs = {v["version"]: v["status"] for v in acme.get("/v2/sites/acts-org/actions/fetch_events/versions").json()["data"]}
    assert vs == {1: "retired", 2: "locked"}


# ------------------------------------------------------------------ reads on a code action: freshness, store, filtering

def test_read_freshness_stable_ids_and_updates(app, tenants, everest):
    key = tenants["acme_key"]
    d = read(key).json()["data"]
    assert everest.calls == 1 and d["complete"] and d["refresh_job_id"] and d["scraped_at"]
    names = [i["data"]["name"] for i in d["items"]]
    assert names == ["Dawn Walk", "Lake Cleanup", "Tree Planting"]          # earlier today kept; past + online dropped
    ids = {i["data"]["name"]: (i["id"], i["updated_at"]) for i in d["items"]}

    d2 = read(key).json()["data"]                                           # fresh: no scraping at all
    assert everest.calls == 1 and d2["refresh_job_id"] is None and d2["items"] == d["items"]
    d3 = read(tenants["zed_key"]).json()["data"]                            # other tenant: same shared items
    assert everest.calls == 1 and [i["id"] for i in d3["items"]] == [i["id"] for i in d["items"]]

    everest.events[1]["description"] = "Now with saplings."                 # Tree Planting changes
    everest.events[0]["image"] = "https://cdn.x/1-new.jpg"                  # image-only change is not an update
    time.sleep(0.01)
    d4 = read(key, freshness_hours=0).json()["data"]
    assert everest.calls == 2 and d4["refresh_job_id"]
    got = {i["data"]["name"]: (i["id"], i["updated_at"]) for i in d4["items"]}
    assert got["Lake Cleanup"] == ids["Lake Cleanup"] and got["Dawn Walk"] == ids["Dawn Walk"]
    assert got["Tree Planting"][0] == ids["Tree Planting"][0] and got["Tree Planting"][1] != ids["Tree Planting"][1]
    with session_scope() as s:                                              # old events are not stored at all
        assert {i.data["name"] for i in s.query(SiteItem)} == {"Dawn Walk", "Lake Cleanup", "Tree Planting", "Waste Webinar"}

    d5 = read(key, options={"include_online": True, "from": ymd(2)}).json()["data"]
    assert [i["data"]["name"] for i in d5["items"]] == ["Waste Webinar", "Tree Planting"]
    assert d5["items"][0]["data"]["is_online"] is True

    everest.events = everest.events[:1]                                     # item gone from the site → not returned
    assert [i["data"]["name"] for i in read(key, freshness_hours=0).json()["data"]["items"]] == ["Lake Cleanup"]


def test_read_timeout_then_next_read_has_items_single_refresh(app, tenants, everest, thread_mode):
    everest.gate = threading.Event()
    key, zed = tenants["acme_key"], tenants["zed_key"]
    d = read(key, timeout_s=0.3).json()["data"]
    assert d["complete"] is False and d["items"] == [] and d["refresh_job_id"] and d["scraped_at"] is None
    d2 = read(zed, timeout_s=0.2).json()["data"]                            # another reader joins the same refresh
    assert d2["refresh_job_id"] == d["refresh_job_id"] and d2["complete"] is False
    everest.gate.set()
    wait_jobs()
    assert everest.calls == 1
    d3 = read(key).json()["data"]
    assert d3["complete"] is True and d3["refresh_job_id"] is None and len(d3["items"]) == 3


def test_broken_after_three_failures_keeps_serving(app, tenants, everest):
    acme, key = tenants["acme"], tenants["acme_key"]
    assert len(read(key).json()["data"]["items"]) == 3
    everest.fail = "portal is down"
    for n in range(3):
        d = read(key, freshness_hours=0).json()["data"]
        assert len(d["items"]) == 3 and d["complete"] is True
        assert d["health"] == ("broken" if n == 2 else "ok")
    a = acme.get("/v2/sites/teameverest-ngo/actions/fetch_events").json()["data"]
    assert a["status"] == "broken" and a["consecutive_failures"] == 3 and a["locked_version"] == 1
    calls = everest.calls
    d = read(key, freshness_hours=0).json()["data"]                         # broken: no hammering, stored items served
    assert everest.calls == calls and d["refresh_job_id"] is None and d["health"] == "broken" and len(d["items"]) == 3

    everest.fail = None                                                     # code fixed → repair → lock
    r = acme.post("/v2/sites/teameverest-ngo/actions/fetch_events/repair", headers=H).json()["data"]
    assert r["version"] == 2
    acme.post("/v2/sites/teameverest-ngo/actions/fetch_events/versions/2/lock", headers=H)
    a = acme.get("/v2/sites/teameverest-ngo/actions/fetch_events").json()["data"]
    assert a["status"] == "locked" and a["consecutive_failures"] == 0 and a["locked_version"] == 2
    assert read(key).json()["data"]["health"] == "ok"


def test_zero_items_after_items_counts_as_failure(app, tenants, everest):
    key = tenants["acme_key"]
    read(key)
    everest.events = []
    d = read(key, freshness_hours=0).json()["data"]
    assert len(d["items"]) == 3                                            # stored items untouched
    a = tenants["acme"].get("/v2/sites/teameverest-ngo/actions/fetch_events").json()["data"]
    assert a["consecutive_failures"] == 1


# ------------------------------------------------------------------ AI filter + categories, cached per tenant

class FakeAI:
    def __init__(self):
        self.prompts, self.fail = [], False

    def __call__(self, cfg, prompt, image=None, mime=None, **kw):
        self.prompts.append(prompt)
        if self.fail:
            raise RuntimeError("provider exploded")
        cats = re.search(r"Categories \(pick exactly one, verbatim\): (.*)", prompt)
        out = []
        for m in re.finditer(r"^\[(\d+)\] ([^|]*)\|", prompt, re.M):
            name = m.group(2).strip()
            v = {"index": int(m.group(1)), "select": "Webinar" not in name and "Skip" not in name}
            if cats:
                v["category"] = "TREES" if "Tree" in name else "CLEANUP" if "Clean" in name else "whatever"
            out.append(v)
        return LLMResult(json.dumps(out), 100, 20, cfg.model)


@pytest.fixture
def fake_ai(monkeypatch):
    f = FakeAI()
    monkeypatch.setattr("scraper0.core.llm.complete", f)
    return f


def test_ai_filter_categories_and_verdict_cache(app, tenants, everest, fake_ai):
    add_shared_ai(tenants["admin"])
    words = ["Alpha", "Bravo", "Charlie", "Delta", "Echo", "Foxtrot", "Golf", "Hotel"]
    everest.events += [ev(10 + i, f"Skip {w} Meetup", day(4 + i)) for i, w in enumerate(words)]
    key, zed = tenants["acme_key"], tenants["zed_key"]
    opts = {"event_filter": "in-person civic", "categories": ["CLEANUP", "TREES", "OTHER"], "include_online": True}
    d = read(key, options=opts).json()["data"]
    assert len(fake_ai.prompts) == 2                                               # 12 items, 8 per call
    assert "CLEANUP, TREES, OTHER" in fake_ai.prompts[0] and "CLEANLINESS_DRIVE" not in fake_ai.prompts[0]
    assert "in-person civic" in fake_ai.prompts[0]
    got = {i["data"]["name"]: i["data"]["category"] for i in d["items"]}
    assert got == {"Dawn Walk": "OTHER", "Lake Cleanup": "CLEANUP", "Tree Planting": "TREES"} and "ai_error" not in d

    assert read(key, options=opts).json()["data"]["items"] == d["items"]
    assert len(fake_ai.prompts) == 2                                               # cached: 0 AI calls
    read(key, options={**opts, "categories": ["CLEANUP", "OTHER"]})
    assert len(fake_ai.prompts) == 4                                               # different options → new calls
    read(zed, options=opts)
    assert len(fake_ai.prompts) == 6                                               # verdicts are per tenant

    d = read(key, options={"event_filter": "in-person civic"}).json()["data"]     # no categories → null
    assert {i["data"]["category"] for i in d["items"]} == {None} and len(d["items"]) == 3
    with session_scope() as s:
        from scraper0.db.models import AIUsage
        assert s.query(AIUsage).filter(AIUsage.purpose == "filter").count() == len(fake_ai.prompts)


def test_ai_unavailable_keeps_everything(app, tenants, everest, fake_ai):
    key = tenants["acme_key"]
    opts = {"event_filter": "x", "categories": ["A", "B"]}
    d = read(key, options=opts).json()["data"]                                    # no provider configured at all
    assert "AI provider" in d["ai_error"] and len(d["items"]) == 3
    assert {i["data"]["category"] for i in d["items"]} == {None}
    add_shared_ai(tenants["admin"])
    fake_ai.fail = True
    d = read(key, options=opts).json()["data"]
    assert "provider exploded" in d["ai_error"] and len(d["items"]) == 3
    fake_ai.fail = False                                                          # failures were not cached
    d = read(key, options=opts).json()["data"]
    assert "ai_error" not in d and {i["data"]["category"] for i in d["items"]} == {"B"}   # no OTHER → last category


# ------------------------------------------------------------------ lookup reads

@pytest.fixture
def track(monkeypatch):
    from scraper0.connectors.base import ConnectorError, get
    state = {"calls": 0, "gate": None}

    def fake(data, ctx):
        state["calls"] += 1
        if state["gate"] is not None:
            assert state["gate"].wait(10)
        if data.get("tracking_id") == "0":
            raise ConnectorError("Complaint details not found. Invalid ID?")
        return {"status": "OPEN", "meta_data": {"id": data["tracking_id"]}}
    monkeypatch.setitem(get("SMARTONEBLR").actions, "TRACK_ISSUE", fake)
    return state


def test_lookup_read(app, tenants, track, settings, monkeypatch):
    key = tenants["acme_key"]
    r = read(key, site="smartoneblr-com", action="track_issue", input={"tracking_id": "21025835"})
    assert r.status_code == 200, r.text
    assert r.json()["data"] == {"site_id": "smartoneblr-com", "action": "track_issue", "version": 1,
                                "output": {"status": "OPEN", "meta_data": {"id": "21025835"}}}
    r = read(key, site="smartoneblr-com", action="track_issue", input={"tracking_id": "0"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "action_failed"
    assert read(key, site="smartoneblr-com", action="track_issue").json()["error"]["code"] == "invalid_input"

    monkeypatch.setattr(settings, "job_mode", "thread")
    track["gate"] = threading.Event()
    r = read(key, site="smartoneblr-com", action="track_issue", input={"tracking_id": "5"}, timeout_s=0.2)
    assert r.status_code == 202 and r.json()["data"]["job_id"]
    track["gate"].set()
    wait_jobs()
    job = key.get(f"/v1/jobs/{r.json()['data']['job_id']}").json()["data"]
    assert job["status"] == "succeeded" and job["result"]["data"]["status"] == "OPEN"


# ------------------------------------------------------------------ writes

class FakeReport:
    def __init__(self):
        self.calls, self.mode, self.gate = 0, "ok", None

    def __call__(self, data, ctx):
        from scraper0.connectors.base import ConnectorError
        self.calls += 1
        assert ctx.auth == {"username": "9999", "password": "hunter2"}
        if self.mode == "fail_before":
            raise ConnectorError("Re-login failed: bad captcha")
        ctx.progress("Submitting complaint", {})
        if self.gate is not None:
            assert self.gate.wait(10)
        if self.mode == "fail_after":
            raise ConnectorError("Timeout while submitting complaint")
        return {"tracking_id": 21025835 + self.calls}


@pytest.fixture
def report(monkeypatch):
    from scraper0.connectors.base import get
    f = FakeReport()
    monkeypatch.setitem(get("SMARTONEBLR").actions, "REPORT_ISSUE", f)
    return f


INPUT = {"category": "Garbage", "sub_category": "Garbage dump", "description": "pile", "latitude": 12.9, "longitude": 77.6}
AUTH = {"username": "9999", "password": "hunter2"}


def write(c, key=None, **body):
    body = {"input": INPUT, "auth": AUTH, **body}
    return c.post("/v2/sites/smartoneblr-com/actions/report_issue/write", json=body,
                  headers={"Idempotency-Key": key} if key else {})


def test_write_idempotency_replay_force_and_isolation(app, tenants, report):
    key, zed = tenants["acme_key"], tenants["zed_key"]
    r = write(key, "hl-issue-1")
    assert r.status_code == 200, r.text
    d = r.json()["data"]
    assert d["status"] == "succeeded" and d["output"] == {"tracking_id": 21025836} and d["replayed"] is False
    assert d["idempotency_key"] == "hl-issue-1" and "hunter2" not in r.text
    r = write(key, "hl-issue-1")
    assert r.json()["data"]["replayed"] is True and r.json()["data"]["output"] == d["output"] and report.calls == 1
    assert write(key, "hl-issue-1", force=True).json()["data"]["replayed"] is False and report.calls == 2
    assert write(zed, "hl-issue-1").json()["data"]["replayed"] is False and report.calls == 3   # per tenant

    a = write(key).json()["data"]                                            # default key: hash of the input
    b = write(key).json()["data"]
    assert a["idempotency_key"] == b["idempotency_key"] and b["replayed"] is True and report.calls == 4
    c = write(key, input={**INPUT, "description": "another"}).json()["data"]
    assert c["idempotency_key"] != a["idempotency_key"] and report.calls == 5

    with session_scope() as s:
        raw = json.dumps([j.input for j in s.query(Job).all()]) + json.dumps([w.output for w in s.query(WriteRecord)])
    assert "hunter2" not in raw and "auth_enc" in raw
    job = key.get(f"/v1/jobs/{d['job_id']}").json()["data"]
    assert job["input"]["auth"] == "***"
    assert tenants["acme_key"].post("/v2/sites/teameverest-ngo/actions/fetch_events/write",
                                    json={"input": {}}).json()["error"]["code"] == "invalid_input"


def test_write_failed_reruns_but_outcome_unknown_is_sticky(app, tenants, report):
    key = tenants["acme_key"]
    report.mode = "fail_before"
    r = write(key, "k1")
    assert r.status_code == 422 and r.json()["error"]["code"] == "action_failed" and "captcha" in r.text
    report.mode = "ok"
    assert write(key, "k1").status_code == 200 and report.calls == 2          # definitive failure: same key re-runs

    report.mode = "fail_after"                                                # failed after the submit step began
    r = write(key, "k2")
    assert r.status_code == 409 and r.json()["error"]["code"] == "outcome_unknown" and r.json()["error"]["job_id"]
    report.mode = "ok"
    assert write(key, "k2").status_code == 409 and report.calls == 3          # never retried automatically
    assert write(key, "k2", force=True).status_code == 200 and report.calls == 4


def test_write_timeout_is_outcome_unknown_and_running_replays(app, tenants, report, thread_mode):
    key = tenants["acme_key"]
    report.gate = threading.Event()
    r = write(key, "slow", timeout_s=0.3)
    assert r.status_code == 409 and r.json()["error"]["code"] == "outcome_unknown"
    assert write(key, "slow").status_code == 409 and report.calls == 1
    report.gate.set()
    wait_jobs()                                                               # it did finish: the record learns that
    r = write(key, "slow")
    assert r.status_code == 200 and r.json()["data"]["replayed"] is True and report.calls == 1

    report.gate = threading.Event()
    out = {}
    t = threading.Thread(target=lambda: out.setdefault("r", write(TestClient(app, headers=dict(key.headers)), "conc",
                                                                  timeout_s=10)))
    t.start()
    for _ in range(100):
        if report.calls == 2:
            break
        time.sleep(0.05)
    r = write(key, "conc")                                                    # same key while the first runs
    assert r.status_code == 202 and r.json()["data"]["status"] == "running" and r.json()["data"]["replayed"] is True
    report.gate.set()
    t.join(10)
    assert out["r"].status_code == 200 and out["r"].json()["data"]["replayed"] is False and report.calls == 2


# ------------------------------------------------------------------ image copies

def _png(color) -> bytes:
    import io

    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (2400, 1200), color).save(out, "PNG")
    return out.getvalue()


def test_expiring_images_are_copied_and_served(app, tenants, everest, settings, monkeypatch):
    """Signed image URLs (Airtable, S3) expire within hours: stored items point at our own permanent copy."""
    from scraper0.services import media
    monkeypatch.setattr(settings, "copy_images", True)
    imgs = {f"https://cdn.x/{i}.jpg": _png((i * 40, 10, 10)) for i in range(1, 6)}
    calls = []

    def fake_download(url):
        calls.append(url)
        base = url.split("?")[0]
        return (imgs[base], "image/png") if base in imgs else None
    monkeypatch.setattr(media, "download", fake_download)
    for e in everest.events:
        e["image"] += "?sig=abc&expires=1"
    everest.events[1]["image"] = "https://cdn.x/missing.jpg"         # can't be copied: keeps its URL

    d = read(tenants["acme_key"]).json()["data"]
    by_name = {i["data"]["name"]: i["data"] for i in d["items"]}
    lake = by_name["Lake Cleanup"]["image"]
    assert lake.startswith(settings.public_base_url + "/v2/media/") and "sig=" not in lake
    assert by_name["Tree Planting"]["image"] == "https://cdn.x/missing.jpg"

    r = TestClient(app).get(lake[len(settings.public_base_url):])          # public: no login, no key
    assert r.status_code == 200 and r.headers["content-type"] == "image/jpeg" and "immutable" in r.headers["cache-control"]
    from PIL import Image
    import io
    assert max(Image.open(io.BytesIO(r.content)).size) == media.MAX_SIDE   # shrunk from 2400 px

    # the next scrape gets a new signature for the same image: the stored copy is reused, nothing downloaded
    n = len(calls)
    for e in everest.events:
        e["image"] = e["image"].replace("sig=abc", "sig=def")
    from scraper0.services.site_runs import store_items
    store_items("teameverest-ngo", "fetch_events", 1, "j", [dict(e) for e in everest.events if e["name"] != "Old Drive"])
    assert [c for c in calls[n:] if "missing" not in c] == []
    assert TestClient(app).get("/v2/media/" + "0" * 40).status_code == 404


def test_repair_relearns_from_the_live_site(app, tenants, acts):
    """Learn/repair must look at the site again, not replay the stored recipe."""
    acme = tenants["acme"]
    acme.post("/v2/sites", json={"base_url": "https://acts.org"}, headers=H)
    acme.post("/v2/sites/acts-org/actions", json={"name": "fetch_events", "kind": "read", "entity": "event"}, headers=H)
    acme.post("/v2/sites/acts-org/actions/fetch_events/learn", headers=H)
    first = acts.renders
    r = acme.post("/v2/sites/acts-org/actions/fetch_events/repair", headers=H)
    assert r.status_code == 202
    job = acme.get(f"/v1/jobs/{r.json()['data']['job_id']}").json()["data"]
    assert job["status"] == "succeeded" and job["result"]["preview"]["count"] == 3
    assert acts.renders > first                                       # rendered again: really re-learned
