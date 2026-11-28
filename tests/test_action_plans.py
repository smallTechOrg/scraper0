"""Action plans (core/action_plans.py): lookups and writes run from data in a browser. The unmarked tests need no
browser; the `browser` ones drive a real Chromium against tests/fake_portal.py (run with `-m browser`)."""
import copy
import json

import pytest
from fake_portal import PASSWORD, USER, Portal, report_plan, track_plan
from fastapi.testclient import TestClient

from scraper0 import site_plans
from scraper0.config import get_settings
from scraper0.core import action_plans as ap
from scraper0.core.plans import PlanError, validate as validate_any

H = {"X-Requested-With": "scraper0"}
ADMIN = ("root@example.com", "correct horse battery")
AUTH = {"username": USER, "password": PASSWORD}
REPORT = {"category": "Roads", "sub_category": "pothole", "description": "Deep pothole near the lake",
          "latitude": 12.97, "longitude": 77.59}


# ------------------------------------------------------------------ helpers

class MemState:
    def __init__(self):
        self.d = {}

    def get(self, k):
        return self.d.get(k)

    def set(self, k, v):
        self.d[k] = v

    def delete(self, k):
        self.d.pop(k, None)


class FakeAI:
    """AIClient-like: scripted captcha readings, with the counters the runner accounts from."""
    def __init__(self, answers):
        self.answers, self.calls, self.last_usage = list(answers), 0, {}

    def vision(self, prompt, image, mime="image/png"):
        self.calls += 1
        self.last_usage = {"input_tokens": 100, "output_tokens": 5}
        return self.answers.pop(0) if self.answers else ""


def ai_solver(image, ai):
    """A solver whose non-AI route always fails, so every attempt falls back to the AI."""
    assert image[:4] == b"\x89PNG"
    return ai.vision("read", image, "image/png")


@pytest.fixture
def portal():
    p = Portal()
    yield p
    p.close()


@pytest.fixture(scope="module")
def renderer():
    from scraper0.core.render import Renderer
    return Renderer(headless=True, allow=lambda url: True)        # the fake portal is on 127.0.0.1


def local_download(url):
    return ap.download_to_temp(url, allow=lambda u: True)


def run(plan, renderer, input, auth=None, **kw):
    kw.setdefault("solver", ai_solver)
    kw.setdefault("download", local_download)
    return ap.run_action_plan(plan, renderer, input, auth, timeout_s=60, **kw)


# ------------------------------------------------------------------ no browser: schema, inputs, output

def test_shipped_bbmp_plans_are_valid_and_declare_their_ai():
    track = ap.validate(site_plans.load("smartoneblr-com", "track_issue"))
    report = validate_any(site_plans.load("smartoneblr-com", "report_issue"))     # plans.validate routes by kind
    assert not ap.uses_ai(track) and ap.uses_ai(report)                           # only the login captcha may use AI
    assert [s["id"] for s in report["steps"] if s.get("commit")] == ["submit"]
    assert report["verify"] == {"action": "track_issue", "input": {"tracking_id": "{output.tracking_id}"}}
    ins, outs = ap.schemas(report)
    assert ins["required"] == ["input", "auth"] and ins["properties"]["input"]["required"] == \
        ["category", "sub_category", "latitude", "longitude"]
    assert outs["properties"]["tracking_id"] == {"type": "integer"}
    assert ap.schemas(track)[1]["properties"]["status"] == {"enum": ["CLOSED", "OPEN"]}
    assert any("COMMIT" in line for line in ap.describe(report))


def _bad(change):
    p = report_plan("https://x.test")
    change(p)
    return p


@pytest.mark.parametrize("change,msg", [
    (lambda p: p.update(mode="read"), "mode must be"),
    (lambda p: p["steps"][-4].pop("commit"), "exactly one top-level step"),
    (lambda p: p["steps"][2].update(commit=True), "exactly one top-level step"),
    (lambda p: p["steps"][-4].update(retry={"attempts": 2}), "without retry"),
    (lambda p: p["steps"][1]["login"][3].pop("uses_ai"), "must declare"),
    (lambda p: p["steps"][2].update(uses_ai=True), "may use AI"),
    (lambda p: p["steps"][2].update(target={"xpath": "//a"}), "exactly one of"),
    (lambda p: p["steps"][2].update(do="hover"), "do must be"),
    (lambda p: p["steps"].append({"id": "x", "type": "code"}), "unknown step type"),
    (lambda p: p["steps"].append(dict(p["steps"][0])), "unique string id"),
    (lambda p: p.pop("output"), "output must map"),
    (lambda p: p["inputs"]["category"].update(pattern="("), "bad pattern"),
    (lambda p: p["steps"][-1]["fields"]["complaint_id"].update(regex="("), "bad regex"),
])
def test_validation_rejects(change, msg):
    with pytest.raises(PlanError, match=msg):
        ap.validate(_bad(change))


def test_lookup_plans_cannot_commit():
    p = track_plan("https://x.test")
    p["steps"][0]["commit"] = True
    with pytest.raises(PlanError, match="can't have a commit"):
        ap.validate(p)


def test_inputs_are_checked_before_anything_runs():
    p = report_plan("https://x.test")
    inp, auth = ap.check_inputs(p, dict(REPORT, extra="ignored"), AUTH)
    assert inp["latitude"] == "12.97" and inp["description"].startswith("Deep") and "extra" not in inp
    assert ap.check_inputs(p, {k: v for k, v in REPORT.items() if k != "description"}, AUTH)[0]["description"] == ""
    with pytest.raises(PlanError) as e:
        ap.check_inputs(p, {"category": 5, "media_url": "ftp://x/y.png"}, {"username": "u"})
    msg = str(e.value)
    for part in ("category: not a valid string", "sub_category: required", "media_url: must be an http(s) URL",
                 "latitude: required", "auth.password: required"):
        assert part in msg
    with pytest.raises(PlanError, match="tracking_id: invalid value"):
        ap.check_inputs(track_plan("https://x.test"), {"tracking_id": "1'); drop"}, None)
    assert ap.check_inputs(track_plan("https://x.test"), {"tracking_id": 21025835}, None)[0] == {"tracking_id": "21025835"}


def test_output_mapping_first_of_and_messages():
    spec = track_plan("https://x.test")["output"]
    ctx = {"vars": {"staff": {"Grievance Status": " Closed ", "Staff Remarks": "Done", "Assigned To": "Ravi"}}}
    assert ap.build_output(spec, ctx) == {"status": "CLOSED", "meta_data": {"remarks": "Done", "staff_name": "Ravi",
                                                                            "mobile_number": None}}
    ctx["vars"]["staff"] = {"Grievance Status": "Pending"}
    with pytest.raises(PlanError, match="Unknown grievance status: pending"):
        ap.build_output(spec, ctx)
    ctx["vars"]["staff"] = {}
    with pytest.raises(PlanError, match="status was not found"):
        ap.build_output(spec, ctx)
    assert ap.build_output({"id": {"from": "vars.a.id", "type": "int"}}, {"vars": {"a": {"id": " 5001 "}}}) == {"id": 5001}


def test_download_for_uploads_refuses_private_addresses():
    with pytest.raises(PlanError, match="public http"):
        ap.download_to_temp("http://127.0.0.1:9/photo.png")
    with pytest.raises(PlanError, match="public http"):
        ap.download_to_temp("file:///etc/passwd")


def test_glob():
    assert ap.glob_re("**polyfill.io**").search("https://cdn.polyfill.io/v3/x.js")
    assert ap.glob_re("**/dwr/call/*.dwr").search("https://s.com/a/dwr/call/Login.dwr")
    assert not ap.glob_re("**/dwr/call/*.dwr").search("https://s.com/a/dwr/call/x/Login.dwr")


# ------------------------------------------------------------------ v2 wiring (no browser: the runner is faked)

@pytest.fixture
def settings(monkeypatch):
    from pydantic import SecretStr
    st = get_settings()
    monkeypatch.setattr(st, "bootstrap_admin_email", ADMIN[0])
    monkeypatch.setattr(st, "bootstrap_admin_password", SecretStr(ADMIN[1]))
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
def clients(app):
    admin = TestClient(app)
    assert admin.post("/v1/auth/login", json={"email": ADMIN[0], "password": ADMIN[1]}).status_code == 200
    ar = TestClient(app).post("/v1/access-requests", json={"name": "A", "email": "a@acme.org", "organisation": "acme"})
    r = admin.post(f"/v1/admin/access-requests/{ar.json()['data']['id']}/approve", json={"slug": "acme"}, headers=H)
    token = r.json()["data"]["invite_url"].rsplit("/", 1)[-1]
    user = TestClient(app)
    assert user.post("/v1/auth/accept-invite", json={"token": token, "name": "A", "password": "pw-123456789"}).status_code == 200
    key = user.post("/v1/api-keys", json={"name": "k"}, headers=H).json()["data"]["key"]
    return user, TestClient(app, headers={"X-API-Key": key})


def onboard_plans(user, base, verify="track"):
    """Onboard the site and its two plan actions, locked. → site_id"""
    site_id = user.post("/v2/sites", json={"base_url": base}, headers=H).json()["data"]["site_id"]
    r = user.post(f"/v2/sites/{site_id}/actions", json={"name": "track", "plan": track_plan(base)}, headers=H)
    assert r.status_code == 201 and r.json()["data"]["implementation"] == "plan" and r.json()["data"]["kind"] == "read"
    r = user.post(f"/v2/sites/{site_id}/actions", json={"name": "report", "plan": report_plan(base, verify)}, headers=H)
    assert r.status_code == 201 and r.json()["data"]["kind"] == "write" and r.json()["data"]["status"] == "draft"
    for name in ("track", "report"):
        assert user.post(f"/v2/sites/{site_id}/actions/{name}/versions/1/lock", headers=H).status_code == 200
    return site_id


def test_seeded_bbmp_plan_actions_are_drafts_next_to_the_code_actions(app, clients):
    user, key = clients
    acts = {a["name"]: a for a in user.get("/v2/sites/smartoneblr-com").json()["data"]["actions"]}
    assert acts["track_issue"]["implementation"] == "code" and acts["track_issue"]["status"] == "locked"
    assert acts["track_issue_plan"]["implementation"] == "plan" and acts["track_issue_plan"]["status"] == "draft"
    assert acts["report_issue_plan"]["kind"] == "write" and acts["report_issue_plan"]["locked_version"] is None
    [v] = user.get("/v2/sites/smartoneblr-com/actions/report_issue_plan/versions").json()["data"]
    assert v["status"] == "draft" and v["implementation_snapshot"]["plan"]["mode"] == "write"
    # a draft is not callable: nothing reaches the portal until an admin locks it
    r = key.post("/v2/sites/smartoneblr-com/actions/report_issue_plan/write", json={"input": REPORT, "auth": AUTH})
    assert r.status_code == 404 and r.json()["error"]["code"] == "action_not_available"
    from scraper0.services.sites import seed_plan_actions
    assert seed_plan_actions() == 0                                          # idempotent


def test_plan_action_api_versions_contract_and_input_errors(app, clients):
    user, key = clients
    base = "https://portal.example.org"
    site_id = onboard_plans(user, base)
    assert key.post(f"/v2/sites/{site_id}/actions", json={"name": "x", "plan": track_plan(base)}).status_code == 403
    bad = track_plan(base)
    bad["steps"][0]["do"] = "hover"
    r = user.post(f"/v2/sites/{site_id}/actions", json={"name": "bad", "plan": bad}, headers=H)
    assert r.status_code == 422 and "do must be" in r.json()["error"]["message"]
    assert user.post(f"/v2/sites/{site_id}/actions", json={"name": "w", "kind": "write"}, headers=H).status_code == 422

    c = user.get(f"/v2/sites/{site_id}/actions/report/contract").json()["data"]
    assert c["kind"] == "write" and c["endpoint"].endswith(f"/v2/sites/{site_id}/actions/report/write")
    assert "Idempotency-Key" in c["curl"] and '"username": "<username>"' in c["curl"] and '"category": "<category>"' in c["curl"]
    assert c["input_schema"]["properties"]["input"]["required"] == ["category", "sub_category", "latitude", "longitude"]

    # a new draft version (an edited plan); the contract follows whichever version is locked
    edited = track_plan(base)
    edited["inputs"]["tracking_id"]["pattern"] = "[0-9]+"
    r = user.post(f"/v2/sites/{site_id}/actions/track/versions", json={"plan": edited}, headers=H)
    assert r.status_code == 201 and r.json()["data"]["version"] == 2 and r.json()["data"]["status"] == "draft"
    assert user.post(f"/v2/sites/{site_id}/actions/track/versions", json={"plan": report_plan(base)}, headers=H).status_code == 422
    assert user.post(f"/v2/sites/{site_id}/actions/track/versions/2/lock", headers=H).status_code == 200
    assert user.get(f"/v2/sites/{site_id}/actions/track").json()["data"]["input_schema"]["properties"]["input"][
        "properties"]["tracking_id"]["pattern"] == "[0-9]+"

    # bad input never reaches a browser: 422 invalid_input, no job
    r = key.post(f"/v2/sites/{site_id}/actions/track/read", json={"input": {"tracking_id": "ab-1"}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "invalid_input"
    r = key.post(f"/v2/sites/{site_id}/actions/report/write", json={"input": {"category": "Roads"}, "auth": AUTH})
    assert r.status_code == 422 and "sub_category: required" in r.json()["error"]["message"]
    assert user.post(f"/v2/sites/{site_id}/actions/track/learn", headers=H).status_code == 422       # plans aren't learned


def test_write_semantics_around_the_commit_step(app, clients, monkeypatch):
    """before commit → action_failed (same key may retry); at/after commit → outcome_unknown (sticky)."""
    from scraper0.services import site_runs
    user, key = clients
    site_id = onboard_plans(user, "https://portal.example.org", verify=None)
    calls = []

    def fake(mode):
        def run_action(site, plan, tenant_id, data, auth, rep, ai, on_commit=None, timeout_s=0):
            calls.append((mode, data["category"], sorted(auth)))
            if mode == "before":
                raise ap.ActionError("step category: option 'Roads' not found", committed=False)
            on_commit()
            if mode == "after":
                raise ap.ActionError("step confirm: no element matched", committed=True)
            return {"tracking_id": 5001}, {"committed": True, "ai_calls": 0}
        return run_action
    url = f"/v2/sites/{site_id}/actions/report/write"
    body = {"input": REPORT, "auth": AUTH}

    monkeypatch.setattr(site_runs, "run_action", fake("before"))
    r = key.post(url, json=body, headers={"Idempotency-Key": "k1"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "action_failed" and "not found" in r.json()["error"]["message"]
    monkeypatch.setattr(site_runs, "run_action", fake("ok"))
    r = key.post(url, json=body, headers={"Idempotency-Key": "k1"})                 # nothing was submitted: retry runs
    assert r.status_code == 200 and r.json()["data"]["output"] == {"tracking_id": 5001} and r.json()["data"]["replayed"] is False
    assert key.post(url, json=body, headers={"Idempotency-Key": "k1"}).json()["data"]["replayed"] is True
    assert len(calls) == 2

    monkeypatch.setattr(site_runs, "run_action", fake("after"))
    r = key.post(url, json=body, headers={"Idempotency-Key": "k2"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "outcome_unknown"
    monkeypatch.setattr(site_runs, "run_action", fake("ok"))
    assert key.post(url, json=body, headers={"Idempotency-Key": "k2"}).status_code == 409    # never retried by itself
    assert len(calls) == 3
    r = key.post(url, json={**body, "force": True}, headers={"Idempotency-Key": "k2"})       # explicit override
    assert r.status_code == 200 and len(calls) == 4
    job = user.get(f"/v1/jobs/{r.json()['data']['job_id']}").json()["data"]
    assert job["result"]["write"]["submit_started"] is True and job["result"]["plan_run"]["committed"] is True
    assert PASSWORD not in json.dumps(job)                                           # auth is never echoed or stored clear


# ------------------------------------------------------------------ browser: the runner against the fake portal

@pytest.mark.browser
def test_lookup_plan_reads_status_with_target_and_step_alternatives(portal, renderer):
    plan = track_plan(portal.base)
    out, stats = run(plan, renderer, {"tracking_id": "777"})
    assert out == {"status": "CLOSED", "meta_data": {"remarks": "On it", "staff_name": "Ravi", "mobile_number": "9999"}}
    assert stats["targets"] == {"number": 1}                    # #complainantNo is gone: the placeholder matched
    assert stats["alternatives"] == {"search": 0} and stats["ai_calls"] == 0 and stats["committed"] is False

    portal.search_button = False                                # the button is gone: pressing Enter still works
    out, stats = run(plan, renderer, {"tracking_id": "777"})
    assert out["status"] == "CLOSED" and stats["alternatives"] == {"search": 1}

    with pytest.raises(ap.ActionError, match="Complaint details not found") as e:
        run(plan, renderer, {"tracking_id": "404"})
    assert e.value.committed is False and e.value.stats["failed_step"] == "result"


@pytest.mark.browser
def test_write_plan_logs_in_through_the_captcha_fills_the_form_and_reads_the_id(portal, renderer):
    plan, state = report_plan(portal.base), MemState()
    ai = FakeAI(["", "ZZZZ", "AB12"])            # unreadable → refresh; wrong → site rejects it → refresh; right
    committed = []
    out, stats = run(plan, renderer, dict(REPORT, media_url=f"{portal.base}/photo.png"), AUTH, ai=ai, state=state,
                     on_commit=lambda: committed.append(len(portal.submissions)))
    assert out == {"tracking_id": 5001}
    [sent] = portal.submissions
    assert sent["category"] == "1" and sent["sub"] == "11"                    # "pothole" matched "Pothole" (async list)
    assert sent["description"] == REPORT["description"] and sent["addr"] == "12.97, 77.59"
    assert sent["loc"] is True and sent["entered"] is True and sent["photo_bytes"] > 0      # file downloaded and attached
    assert committed == [0]                                                   # told before the click, not after

    cap = "session.login.3"
    assert stats["captcha"] == {cap: {"attempts": 3, "ai_calls": 3}} and portal.refreshes == 2
    assert stats["ai_calls"] == 3 and stats["ai_tokens"] == 315 and stats["ai_by_step"] == {cap: {"calls": 3, "tokens": 315}}
    assert stats["session"] == {"reused": False, "logged_in": True} and portal.logins == 1
    assert stats["targets"] == {"sub_category": 1, "description": 1}          # first alternatives missed
    assert stats["committed"] is True and stats["outcomes"]["submit"] == "ok" and stats["outcomes"]["photo"] == "ok"
    assert PASSWORD not in json.dumps(stats) and PASSWORD not in json.dumps(out)

    # second run, same tenant state: the stored session is reused — no login, no captcha, no AI
    ai2 = FakeAI(["never used"])
    out, stats = run(plan, renderer, REPORT, AUTH, ai=ai2, state=state)
    assert out == {"tracking_id": 5002} and stats["session"] == {"reused": True, "logged_in": False}
    assert portal.logins == 1 and ai2.calls == 0 and stats["ai_calls"] == 0 and stats["captcha"] == {}
    assert stats["outcomes"]["photo"] == "skipped" and portal.submissions[1]["photo_bytes"] == 0    # when: no media_url
    [stored] = state.d.values()
    assert "cookies" in json.loads(stored)                                    # (DBStateStore encrypts this at rest)


@pytest.mark.browser
def test_wrong_password_fails_before_commit_and_never_leaks_it(portal, renderer):
    ai = FakeAI(["AB12"])
    with pytest.raises(ap.ActionError, match="Invalid mobile number or password") as e:
        run(report_plan(portal.base), renderer, REPORT, {"username": USER, "password": "wrong-pw-123"}, ai=ai, state=MemState())
    assert e.value.committed is False and portal.submissions == [] and ai.calls == 1
    assert "wrong-pw-123" not in str(e.value) and "wrong-pw-123" not in json.dumps(e.value.stats)

    ai = FakeAI(["", "", ""])
    with pytest.raises(ap.ActionError, match="Captcha solving failed after retries") as e:
        run(report_plan(portal.base), renderer, REPORT, AUTH, ai=ai, state=MemState())
    assert e.value.committed is False and e.value.stats["captcha"]["session.login.3"]["attempts"] == 3


@pytest.mark.browser
def test_commit_boundary_before_and_after(portal, renderer):
    plan, state = report_plan(portal.base), MemState()
    portal.submit_button = False                                 # the submit control is missing: nothing was clicked
    with pytest.raises(ap.ActionError, match="step submit") as e:
        run(plan, renderer, REPORT, AUTH, ai=FakeAI(["AB12"]), state=state)
    assert e.value.committed is False and portal.submissions == [] and e.value.stats["failed_step"] == "submit"

    portal.submit_button = True
    bad = copy.deepcopy(plan)
    bad["steps"][3]["label"] = "Parks"                           # category that does not exist: fails well before commit
    with pytest.raises(ap.ActionError, match="option 'Parks' not found. Available: \\['Roads', 'Garbage'\\]") as e:
        run(bad, renderer, REPORT, AUTH, state=state)
    assert e.value.committed is False and e.value.stats["failed_step"] == "category" and portal.submissions == []

    portal.popup = False                                         # submitted, but the confirmation never shows
    with pytest.raises(ap.ActionError, match="step confirm") as e:
        run(plan, renderer, REPORT, AUTH, state=state)
    assert e.value.committed is True and len(portal.submissions) == 1        # it did go through: outcome unknown


@pytest.mark.browser
def test_session_guard_blocks_non_public_urls(portal):
    from scraper0.core.render import Renderer
    strict = Renderer(headless=True)                             # the real SSRF guard: 127.0.0.1 is refused
    with pytest.raises(ap.ActionError, match="not public"):
        ap.run_action_plan(track_plan(portal.base), strict, {"tracking_id": "777"}, timeout_s=30)


# ------------------------------------------------------------------ browser: through the v2 API

@pytest.fixture
def live(monkeypatch, renderer):
    from scraper0.connectors.bbmp import captcha
    from scraper0.services import site_runs
    monkeypatch.setattr(site_runs, "_action_renderer", lambda: renderer)
    monkeypatch.setattr(captcha, "solve_captcha", lambda image, ai, mode=None: "AB12")     # the non-AI solver reads it
    real = ap.download_to_temp
    monkeypatch.setattr(ap, "download_to_temp", lambda url, **kw: real(url, **{**kw, "allow": lambda u: True}))


@pytest.mark.browser
def test_v2_plan_lookup_and_verified_idempotent_write(app, clients, portal, live):
    user, key = clients
    site_id = onboard_plans(user, portal.base)
    r = key.post(f"/v2/sites/{site_id}/actions/track/read", json={"input": {"tracking_id": 777}})
    assert r.status_code == 200 and r.json()["data"]["output"]["status"] == "CLOSED" and r.json()["data"]["version"] == 1
    r = key.post(f"/v2/sites/{site_id}/actions/track/read", json={"input": {"tracking_id": "404"}})
    assert r.status_code == 422 and r.json()["error"]["code"] == "action_failed"

    url = f"/v2/sites/{site_id}/actions/report/write"
    body = {"input": dict(REPORT, media_url=f"{portal.base}/photo.png"), "auth": AUTH}
    r = key.post(url, json=body, headers={"Idempotency-Key": "issue-1"})
    assert r.status_code == 200 and r.json()["data"]["output"] == {"tracking_id": 5001}
    job = user.get(f"/v1/jobs/{r.json()['data']['job_id']}").json()["data"]
    w = job["result"]["write"]
    assert w["verified"] is True and w["verify"]["action"] == "track" and w["verify"]["output"]["status"] == "OPEN"
    assert job["result"]["plan_run"]["committed"] is True and job["result"]["plan_run"]["ai_calls"] == 0
    assert job["result"]["ai"]["calls"] == 0 and job["site_id"] == site_id and job["action"] == "report"
    assert PASSWORD not in json.dumps(job)

    again = key.post(url, json=body, headers={"Idempotency-Key": "issue-1"})          # same key: replayed, not re-filed
    assert again.json()["data"]["replayed"] is True and len(portal.submissions) == 1

    portal.popup = False                                                             # submitted, confirmation lost
    r = key.post(url, json=body, headers={"Idempotency-Key": "issue-2"})
    assert r.status_code == 409 and r.json()["error"]["code"] == "outcome_unknown" and len(portal.submissions) == 2
    assert key.post(url, json=body, headers={"Idempotency-Key": "issue-2"}).status_code == 409
    assert len(portal.submissions) == 2                                              # not filed again

    portal.popup, portal.submit_button = True, False                                 # no submit control: nothing sent
    r = key.post(url, json=body, headers={"Idempotency-Key": "issue-3"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "action_failed" and len(portal.submissions) == 2
    portal.submit_button = True
    r = key.post(url, json=body, headers={"Idempotency-Key": "issue-3"})             # so the same key may retry
    assert r.status_code == 200 and r.json()["data"]["output"] == {"tracking_id": 5003}

    # the tenant's session was stored encrypted and reused: one portal login across all these runs
    assert portal.logins == 1
    from scraper0.db.models import ConnectorState
    from scraper0.db.session import session_scope
    with session_scope() as s:
        [row] = s.query(ConnectorState).all()
        assert row.connector == f"site:{site_id}" and "cookies" not in row.value_enc and USER not in row.key
