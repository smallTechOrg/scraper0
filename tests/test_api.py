"""End-to-end over HTTP (FastAPI TestClient): auth, tenancy, AI metering, jobs, sources, items."""
import json
import re

import pytest
from fastapi.testclient import TestClient

from scraper0.config import get_settings
from scraper0.core.llm import LLMResult

H = {"X-Requested-With": "scraper0"}
ADMIN = ("root@example.com", "correct horse battery")


@pytest.fixture
def settings(monkeypatch):
    st = get_settings()
    monkeypatch.setattr(st, "bootstrap_admin_email", ADMIN[0])
    from pydantic import SecretStr
    monkeypatch.setattr(st, "bootstrap_admin_password", SecretStr(ADMIN[1]))
    from pydantic import SecretStr as S
    monkeypatch.setattr(st, "internal_token", S("internal-secret-token"))
    return st


@pytest.fixture
def app(settings):
    from scraper0.api.app import create_app
    from scraper0.api import routes_public
    from scraper0.bootstrap import seed
    routes_public._rl.clear()          # rate-limit buckets are process-global
    seed()
    return create_app(run_startup=False)


def login(app, email=ADMIN[0], pw=ADMIN[1]) -> TestClient:
    c = TestClient(app)
    r = c.post("/v1/auth/login", json={"email": email, "password": pw})
    assert r.status_code == 200, r.text
    return c


class FakeLLM:
    """Stands in for llm.complete: answers EXTRACT prompts from a table, counts tokens."""
    def __init__(self, answers=None):
        self.calls, self.answers = [], answers or {}

    def __call__(self, cfg, prompt, image=None, mime=None, **kw):
        self.calls.append((cfg.name, "vision" if image else "text"))
        if image is not None:
            text = json.dumps([{"name": "Poster Walk", "start": "2026-04-01T07:00", "address": "Lalbagh"}])
        else:
            m = re.search(r"Page URL: (\S+)", prompt)
            text = self.answers.get(m.group(1) if m else "", json.dumps({"found": False, "items": []}))
        return LLMResult(text, 1000, 200, cfg.model)


@pytest.fixture
def fake_llm(monkeypatch):
    f = FakeLLM()
    monkeypatch.setattr("scraper0.core.llm.complete", f)
    return f


class FakeFetcher:
    from conftest import no_cookie as cookie, request_via_get as request  # noqa: E402
    def __init__(self, pages): self.pages = pages
    def get(self, url):
        p = self.pages.get(url)
        return (p, "text/html") if p else None


def event_page(name, date):
    return ("<html><head><script type='application/ld+json'>" + json.dumps(
        {"@type": "Event", "name": name, "startDate": date, "location": {"@type": "Place", "name": "Cubbon Park"}})
        + "</script></head><body><h1>" + name + "</h1></body></html>")


@pytest.fixture
def site(monkeypatch):
    pages = {"https://ex.org/events": '<html><body><a href="/events/a">a</a><a href="/events/b">b</a></body></html>',
             "https://ex.org/events/a": event_page("Lake Cleanup", "2026-12-14T07:00:00+05:30"),
             "https://ex.org/events/b": event_page("Tree Planting", "2026-12-21T07:00:00+05:30")}
    monkeypatch.setattr("scraper0.jobs.execute._fetcher", lambda: FakeFetcher(pages))
    return pages


def onboard(app, admin: TestClient, slug="acme", budget=5.0):
    """Access request → admin approves → invitee accepts → logged-in tenant admin client."""
    pub = TestClient(app)
    ar = pub.post("/v1/access-requests", json={"name": "Ann", "email": f"ann@{slug}.org", "organisation": slug})
    assert ar.status_code == 201
    r = admin.post(f"/v1/admin/access-requests/{ar.json()['data']['id']}/approve",
                   json={"slug": slug, "monthly_ai_budget_usd": budget}, headers=H)
    assert r.status_code == 200, r.text
    token = r.json()["data"]["invite_url"].rsplit("/", 1)[-1]
    c = TestClient(app)
    r = c.post("/v1/auth/accept-invite", json={"token": token, "name": "Ann", "password": "ann-password-123"})
    assert r.status_code == 200, r.text
    return c, r.json()["data"]["user"]


def add_shared_groq(admin):
    r = admin.post("/v1/admin/ai/providers", headers=H, json={
        "name": "Groq", "kind": "groq", "model": "llama", "api_key": "gsk_secret_value_1234",
        "price_in_per_mtok": 1.0, "price_out_per_mtok": 2.0})
    assert r.status_code == 201, r.text
    return r.json()["data"]


def onboard_ex(c: TestClient) -> dict:
    """Onboard https://ex.org, learn fetch_events from /events, lock version 1 → the learn job."""
    assert c.post("/v2/sites", json={"base_url": "https://ex.org"}, headers=H).status_code in (200, 201)
    r = c.post("/v2/sites/ex-org/actions", headers=H, json={"name": "fetch_events", "kind": "read", "entity": "event",
                                                           "seed_urls": ["https://ex.org/events"]})
    assert r.status_code == 201, r.text
    r = c.post("/v2/sites/ex-org/actions/fetch_events/learn", headers=H)
    assert r.status_code == 202, r.text
    job = c.get(f"/v1/jobs/{r.json()['data']['job_id']}").json()["data"]
    assert job["status"] == "succeeded", job
    assert c.post("/v2/sites/ex-org/actions/fetch_events/versions/1/lock", headers=H).status_code == 200
    return job


def refresh_ex(c: TestClient) -> dict:
    r = c.post("/v2/sites/ex-org/actions/fetch_events/refresh", headers=H)
    assert r.status_code == 202, r.text
    return c.get(f"/v1/jobs/{r.json()['data']['job_id']}").json()["data"]


# ------------------------------------------------------------------ public / auth

def test_health_and_meta(app):
    c = TestClient(app)
    assert c.get("/api/health").json()["status"] == "ok"
    assert "event" in c.get("/v1/meta").json()["data"]["entities"]


def test_login_me_csrf_logout(app):
    c = TestClient(app)
    assert c.post("/v1/auth/login", json={"email": ADMIN[0], "password": "nope"}).status_code == 401
    c = login(app)
    me = c.get("/v1/auth/me").json()["data"]
    assert me["user"]["is_platform_admin"] and me["tenants"] == []
    assert c.post("/v1/admin/tenants", json={"slug": "x1"}).json()["error"]["code"] == "csrf"
    assert c.post("/v1/auth/logout").status_code == 204
    assert c.get("/v1/auth/me").status_code == 401


def test_onboarding_flow_and_roles(app):
    admin = login(app)
    ann, user = onboard(app, admin)
    t = ann.get("/v1/tenant").json()["data"]
    assert t["slug"] == "acme" and t["monthly_ai_budget_usd"] == 5.0
    inv = ann.post("/v1/members/invite", json={"email": "bob@acme.org", "role": "member"}, headers=H).json()["data"]
    bob = TestClient(app)
    bob.post("/v1/auth/accept-invite", json={"token": inv["invite_url"].rsplit("/", 1)[-1], "name": "Bob",
                                             "password": "bob-password-123"})
    assert bob.post("/v1/api-keys", json={"name": "x"}, headers=H).status_code == 403          # member
    assert bob.get("/v1/admin/tenants").status_code == 403
    assert ann.delete(f"/v1/members/{user['id']}", headers=H).json()["error"]["code"] == "last_admin"
    assert TestClient(app).post("/v1/auth/accept-invite", json={"token": "bogus", "name": "x",
                                                               "password": "whatever-123"}).status_code == 400


def test_api_keys_and_isolation(app, site, fake_llm):
    admin = login(app)
    ann, _ = onboard(app, admin, "acme")
    zed, _ = onboard(app, admin, "zed")
    key = ann.post("/v1/api-keys", json={"name": "ci"}, headers=H).json()["data"]
    assert key["key"].startswith("s0_")
    k = TestClient(app, headers={"X-API-Key": key["key"]})
    onboard_ex(ann)
    job = refresh_ex(ann)
    assert job["status"] == "succeeded" and job["site_id"] == "ex-org" and job["action"] == "fetch_events"
    assert zed.get(f"/v1/jobs/{job['id']}").status_code == 404                  # other tenant can't see the job
    assert k.post("/v2/sites/ex-org/actions/fetch_events/refresh").status_code == 403     # service keys can't
    assert k.get("/v1/items").json()["total"] == 2
    shared = zed.get("/v1/items").json()                                         # …but site items are shared
    assert shared["total"] == 2 and {i["site_id"] for i in shared["data"]} == {"ex-org"}
    ann.delete(f"/v1/api-keys/{key['id']}", headers=H)
    assert k.get("/v1/jobs").json()["error"]["code"] == "invalid_api_key"


# ------------------------------------------------------------------ AI providers, metering, budget

def test_shared_provider_metering_budget_and_byo(app, fake_llm):
    admin = login(app)
    add_shared_groq(admin)
    ann, _ = onboard(app, admin, budget=0.004)
    pv = ann.get("/v1/ai/providers").json()["data"]
    assert pv["active"]["scope"] == "shared" and pv["active"]["key_hint"] == "…1234"
    assert "gsk_secret" not in json.dumps(pv)
    tid = ann.get("/v1/tenant").json()["data"]["id"]
    from scraper0.services.ai import AIClient, BudgetExceeded
    ai = AIClient(tid)
    ai.text("hi")                                         # 1000 in @ $1 + 200 out @ $2 = $0.0014
    ai.text("hi")
    ai.text("hi")                                         # spend now $0.0042 ≥ budget 0.004
    with pytest.raises(BudgetExceeded):
        ai.text("hi")
    u = ann.get("/v1/ai/usage").json()["data"]
    assert u["by_purpose"][0]["calls"] == 3 and abs(u["total_usd"] - 0.0042) < 1e-9
    # BYO key: takes over and is not limited by the shared budget
    r = ann.post("/v1/ai/providers", headers=H, json={"name": "Mine", "kind": "anthropic", "model": "claude-haiku-5-5",
                                                       "api_key": "sk-ant-mine-9999"})
    assert r.status_code == 201
    assert ann.get("/v1/ai/providers").json()["data"]["active"]["name"] == "Mine"
    ai.text("hi")
    assert fake_llm.calls[-1][0] == "Mine"
    assert admin.get("/v1/admin/usage").json()["data"]["by_tenant"][0]["slug"] == "acme"


def test_provider_validation(app):
    admin = login(app)
    bad = admin.post("/v1/admin/ai/providers", headers=H, json={"name": "x", "kind": "openai_compat", "model": "m",
                                                                "api_key": "k", "base_url": "http://insecure"})
    assert bad.status_code == 422
    assert admin.post("/v1/admin/ai/providers", headers=H, json={"name": "x", "kind": "nope", "model": "m",
                                                                 "api_key": "k"}).status_code == 422


# ------------------------------------------------------------------ jobs, change feed, SSE

def test_refresh_items_and_change_feed(app, site, fake_llm):
    admin = login(app)
    ann, _ = onboard(app, admin)
    onboard_ex(ann)
    j = refresh_ex(ann)
    assert j["kind"] == "site_refresh" and j["status"] == "succeeded"
    feed = ann.get("/v1/items/changes?entity=event").json()
    assert sorted(c["change"] for c in feed["data"]) == ["created", "created"]
    cursor = feed["next_cursor"]
    assert refresh_ex(ann)["status"] == "succeeded"
    assert ann.get(f"/v1/items/changes?cursor={cursor}").json()["data"] == []        # nothing new
    site["https://ex.org/events/b"] = event_page("Tree Planting", "2026-12-28T07:00:00+05:30")   # date moved
    refresh_ex(ann)
    ch = ann.get(f"/v1/items/changes?cursor={cursor}").json()["data"]
    assert len(ch) == 1 and ch[0]["change"] == "updated" and ch[0]["item"]["data"]["name"] == "Tree Planting"
    assert ann.get("/v1/items?q=lake").json()["total"] == 1


def test_sse_stream_and_cancel(app, site, fake_llm, monkeypatch):
    admin = login(app)
    ann, _ = onboard(app, admin)
    onboard_ex(ann)
    j = refresh_ex(ann)
    body = ann.get(f"/v1/jobs/{j['id']}/events").text
    assert "event: progress" in body and "event: done" in body and '"succeeded"' in body
    monkeypatch.setattr(get_settings(), "job_mode", "cloud_tasks")
    monkeypatch.setattr("scraper0.jobs.cloud_tasks.enqueue", lambda job_id: None)       # stays queued
    q = ann.post("/v2/sites/ex-org/actions/fetch_events/refresh", headers=H)
    assert q.status_code == 202
    qid = q.json()["data"]["job_id"]
    assert ann.get(f"/v1/jobs/{qid}").json()["data"]["status"] == "queued"
    again = ann.post("/v2/sites/ex-org/actions/fetch_events/refresh", headers=H).json()["data"]
    assert again == {"job_id": qid, "joined": True, "visible": True}                    # joins, never doubles
    c = ann.post(f"/v1/jobs/{qid}/cancel", headers=H).json()["data"]
    assert c["status"] == "cancelled"


def test_ad_hoc_jobs_and_uploads_are_gone(app):
    admin = login(app)
    ann, _ = onboard(app, admin)
    for b in ({"kind": "crawl", "input": {"urls": ["https://x.org"]}}, {"kind": "connector",
              "input": {"connector": "TEAMEVEREST", "action": "FETCH_EVENTS"}}, {"kind": "ingest", "input": {}},
              {"kind": "nope"}, {}):
        r = ann.post("/v1/jobs", json=b, headers=H)
        assert r.status_code == 422 and "/v2/sites" in r.json()["error"]["message"], b
    assert ann.post("/v1/jobs/ingest", headers=H, data={"text": "x"}).status_code in (404, 405)


def test_refresh_now_rules(app, site, fake_llm):
    admin = login(app)
    ann, _ = onboard(app, admin)
    assert ann.post("/v2/sites/ex-org/actions/fetch_events/refresh", headers=H).status_code == 404   # not onboarded
    ann.post("/v2/sites", json={"base_url": "https://ex.org"}, headers=H)
    ann.post("/v2/sites/ex-org/actions", headers=H, json={"name": "fetch_events", "kind": "read", "entity": "event",
                                                         "seed_urls": ["https://ex.org/events"]})
    r = ann.post("/v2/sites/ex-org/actions/fetch_events/refresh", headers=H)
    assert r.status_code == 404 and r.json()["error"]["code"] == "action_not_available"          # nothing locked
    assert ann.post("/v2/sites/smartoneblr-com/actions/track_issue/refresh", headers=H).status_code == 422  # lookup


# ------------------------------------------------------------------ custom entities

def test_custom_entity_versioning(app):
    admin = login(app)
    ann, _ = onboard(app, admin, "acme")
    zed, _ = onboard(app, admin, "zed")
    spec = {"description": "a job post", "fields": [{"name": "title", "required": True}, {"name": "salary", "type": "number"}]}
    assert ann.put("/v1/entities/job", json=spec, headers=H).json()["data"]["version"] == 1
    assert ann.put("/v1/entities/job", json=spec, headers=H).json()["data"]["version"] == 2
    assert ann.put("/v1/entities/event", json=spec, headers=H).status_code == 422
    assert [e["name"] for e in zed.get("/v1/entities").json()["data"]] == ["event"]


def test_sources_and_tick(app, site, fake_llm, monkeypatch):
    admin = login(app)
    ann, _ = onboard(app, admin)
    r = ann.post("/v1/sources", headers=H, json={"name": "x", "site_id": "ex-org", "interval_minutes": 60})
    assert r.status_code == 404 and r.json()["error"]["code"] == "site_not_onboarded"
    onboard_ex(ann)
    bad = ann.post("/v1/sources", headers=H, json={"name": "x", "site_id": "ex-org", "interval_minutes": 5})
    assert bad.status_code == 422
    lookup = ann.post("/v1/sources", headers=H, json={"name": "x", "site_id": "smartoneblr-com",
                                                      "action": "track_issue", "interval_minutes": 60})
    assert lookup.status_code == 422                                              # only list actions
    src = ann.post("/v1/sources", headers=H, json={"name": "Ex events", "site_id": "ex-org", "interval_minutes": 60})
    assert src.status_code == 201, src.text
    assert src.json()["data"]["action"] == "fetch_events"
    internal = TestClient(app)
    assert internal.post("/internal/tick").status_code == 401
    t = internal.post("/internal/tick", headers={"Authorization": "Bearer internal-secret-token"}).json()["data"]
    assert t["due"] == 1 and len(t["started"]) == 1
    s = ann.get("/v1/sources").json()["data"][0]
    assert s["last_job"]["status"] == "succeeded" and s["last_job"]["kind"] == "site_refresh" and s["next_run_at"]
    t2 = internal.post("/internal/tick", headers={"Authorization": "Bearer internal-secret-token"}).json()["data"]
    assert t2["due"] == 0                                                         # not due again for an hour
    assert ann.get("/v1/items").json()["data"][0]["job_id"] == s["last_job"]["id"]
    run = ann.post(f"/v1/sources/{s['id']}/run", headers=H)
    assert run.status_code == 202 and run.json()["data"]["kind"] == "site_refresh"
    assert ann.patch(f"/v1/sources/{s['id']}", headers=H, json={"enabled": False}).json()["data"]["enabled"] is False


def test_internal_run_requires_token(app):
    assert TestClient(app).post("/internal/jobs/x/run").status_code == 401


def test_ai_unavailable_is_reported_not_silent(app, site):
    """No provider configured: learning from structured data still succeeds, and the job says AI was unavailable."""
    admin = login(app)
    ann, _ = onboard(app, admin)
    site["https://ex.org/events/c"] = "<html><body><h2>Plain page</h2><p>" + "words " * 40 + "</p></body></html>"
    site["https://ex.org/events"] = site["https://ex.org/events"].replace("</body>", '<a href="/events/c">c</a></body>')
    j = onboard_ex(ann)
    assert j["result"]["preview"]["count"] == 2
    assert any("no AI provider" in e for e in j["result"]["ai"]["errors"])


def test_listing_events_without_urls_do_not_overwrite_each_other(app):
    from scraper0.services.items import batch_keys
    evs = [{"name": "A", "start_time": "2099-12-01T04:30:00.000Z", "link": "https://ex.org/events"},
           {"name": "B", "start_time": "2099-12-02T04:30:00.000Z", "link": "https://ex.org/events"}]
    keys = batch_keys(evs)
    assert len(set(keys)) == 2 and keys == batch_keys(evs)                   # distinct, and stable across runs


def test_change_password(app):
    c = login(app)
    assert c.post("/v1/auth/password", json={"current_password": "wrong", "new_password": "x" * 12},
                  headers=H).status_code == 401
    assert c.post("/v1/auth/password", json={"current_password": ADMIN[1], "new_password": "short"},
                  headers=H).status_code == 422
    other = login(app)
    assert c.post("/v1/auth/password", json={"current_password": ADMIN[1], "new_password": "brand-new-password"},
                  headers=H).status_code == 200
    assert other.get("/v1/auth/me").status_code == 401                            # other sessions ended
    assert c.get("/v1/auth/me").status_code == 200
    login(app, pw="brand-new-password")


def test_sse_accepts_tenant_query_param(app, site, fake_llm):
    admin = login(app)
    ann, _ = onboard(app, admin, "acme")
    zed, _ = onboard(app, admin, "zed")
    tid = zed.get("/v1/tenant").json()["data"]["id"]
    onboard_ex(zed)
    j = refresh_ex(zed)
    # platform admin (no memberships) streams another tenant's job via ?tenant= (EventSource can't set headers)
    assert "event: done" in admin.get(f"/v1/jobs/{j['id']}/events?tenant={tid}").text
    assert ann.get(f"/v1/jobs/{j['id']}/events?tenant={tid}").status_code == 403      # not a member
