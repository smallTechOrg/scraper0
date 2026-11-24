import json
import os
import sys

import pytest

from scraper0.connectors.base import ConnectorError
from scraper0.connectors.bbmp import login as bbmp_login
from scraper0.connectors.bbmp import report, track

from .conftest import FakeState

AUTH = {"username": "9000000000", "password": "pw"}
DATA = {"category": "Road Engineering", "sub_category": "Potholes", "description": "Big pothole",
        "media_url": "https://cdn.example.org/p.jpg", "latitude": "12.97", "longitude": "77.59"}


# --------------------------------------------------------------------------- validation

@pytest.mark.parametrize("data, msg", [
    ({"sub_category": "x"}, "category: Missing data for required field."),
    ({"category": "x"}, "sub_category: Missing data for required field."),
    ({"category": 1, "sub_category": "x"}, "category: Not a valid string."),
    ({"category": "x", "sub_category": "y", "description": 5}, "description: Not a valid string."),
    ({"category": "x", "sub_category": "y", "media_url": "/etc/passwd"}, "media_url must be an http(s) URL"),
    ({"category": "x", "sub_category": "y", "media_url": "file:///tmp/a.jpg"}, "media_url must be an http(s) URL"),
])
def test_report_validation_errors(make_ctx, data, msg):
    with pytest.raises(ConnectorError, match=msg.replace("(", r"\(").replace(")", r"\)")):
        report.report_issue(data, make_ctx(auth=AUTH))


@pytest.mark.parametrize("auth", [None, {}, {"username": "u"}, {"password": "p"}, {"username": "", "password": "p"}])
def test_report_requires_auth(make_ctx, auth):
    with pytest.raises(ConnectorError, match="Missing auth"):
        report.report_issue(dict(DATA), make_ctx(auth=auth))


def test_report_accepts_numeric_coordinates():
    params, user, pw = report.validate_report({"category": "a", "sub_category": "b", "latitude": 12.5,
                                               "longitude": 77}, AUTH)
    assert (params["latitude"], params["longitude"], params["media_url"]) == ("12.5", "77", None)
    assert (user, pw) == ("9000000000", "pw")


@pytest.mark.parametrize("data, msg", [({}, "Missing tracking_id in action_data"),
                                       ({"tracking_id": ""}, "Missing tracking_id"),
                                       ({"tracking_id": "12'); x"}, "Invalid tracking_id"),
                                       ({"tracking_id": True}, "Not a valid string")])
def test_track_validation(make_ctx, data, msg):
    with pytest.raises(ConnectorError, match=msg):
        track.track_issue(data, make_ctx())


# --------------------------------------------------------------------------- report flow (browser mocked)

def _fake_browser(calls, result=(True, {"complaint_id": "21025835", "timestamp": "24/11/2026 10:00"}), new_state=None):
    def fake(params, media_path, mobile, password, ai, storage_state, save_state, progress):
        calls.append({"params": params, "media_path": media_path, "media_exists": bool(media_path and
                      os.path.exists(media_path)), "mobile": mobile, "password": password,
                      "storage_state": storage_state})
        if new_state is not None:
            save_state(new_state)
        return result
    return fake


def _fake_download(tmp_path):
    def fake(url):
        p = tmp_path / "img.jpg"
        p.write_bytes(b"\xff\xd8jpeg")
        return str(p)
    return fake


def test_report_reuses_and_saves_storage_state(make_ctx, monkeypatch, tmp_path):
    stored = {"cookies": [{"name": "JSESSIONID", "value": "old"}], "origins": []}
    state = FakeState({"storage_state:9000000000": json.dumps(stored)})
    new = {"cookies": [{"name": "JSESSIONID", "value": "new"}], "origins": []}
    calls = []
    monkeypatch.setattr(report, "submit_complaint_in_browser", _fake_browser(calls, new_state=new))
    monkeypatch.setattr(report, "download_media", _fake_download(tmp_path))

    ctx = make_ctx(auth=AUTH, state=state)
    assert report.report_issue(dict(DATA), ctx) == {"tracking_id": 21025835}

    assert calls[0]["storage_state"] == stored
    assert calls[0]["media_exists"] and calls[0]["mobile"] == "9000000000"
    assert state.gets == ["storage_state:9000000000"]
    assert json.loads(state.data["storage_state:9000000000"]) == new
    assert not os.path.exists(calls[0]["media_path"])   # temp file removed
    assert not (tmp_path / "bbmp_auth.json").exists() and not os.path.exists("bbmp_auth.json")
    assert any(m == "Complaint registered" for m, _ in ctx.progress_log)


def test_report_without_state_and_media(make_ctx, monkeypatch):
    calls = []
    monkeypatch.setattr(report, "submit_complaint_in_browser", _fake_browser(calls))
    state = FakeState({"storage_state:9000000000": "{corrupt"})
    data = {k: v for k, v in DATA.items() if k != "media_url"}
    assert report.report_issue(data, make_ctx(auth=AUTH, state=state)) == {"tracking_id": 21025835}
    assert calls[0]["storage_state"] is None and calls[0]["media_path"] is None
    assert state.sets == []


@pytest.mark.parametrize("msg", ["Re-login failed: Invalid mobile number or password",
                                 "Timeout while submitting complaint", "Error: boom"])
def test_report_failures_keep_legacy_messages(make_ctx, monkeypatch, msg):
    monkeypatch.setattr(report, "submit_complaint_in_browser", _fake_browser([], result=(False, msg)))
    with pytest.raises(ConnectorError) as e:
        report.report_issue({"category": "a", "sub_category": "b"}, make_ctx(auth=AUTH))
    assert str(e.value) == msg


def test_parse_acknowledgement():
    ack = "Your complaint has been registered with Complaint ID 21025835 on 24/11/2026 10:05. Thank you."
    assert report.parse_acknowledgement(ack) == {"complaint_id": "21025835", "timestamp": "24/11/2026 10:05"}
    with pytest.raises(Exception, match="Complaint ID"):
        report.parse_acknowledgement("nothing")


def test_download_media_blocks_private_hosts(monkeypatch):
    import scraper0.core.fetch as fetch
    monkeypatch.setattr(fetch, "is_public_url", lambda u: False)
    with pytest.raises(ConnectorError, match="public"):
        report.download_media("http://169.254.169.254/latest/meta-data")


def test_download_media_follows_redirects_with_guard(monkeypatch):
    import httpx

    import scraper0.core.fetch as fetch
    checked = []
    monkeypatch.setattr(fetch, "is_public_url", lambda u: checked.append(u) or "internal" not in u)

    def handler(request):
        if request.url.host == "cdn.example.org":
            return httpx.Response(302, headers={"location": "http://internal.local/x.png"})
        return httpx.Response(200, content=b"x")
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(transport=httpx.MockTransport(handler), **kw))
    with pytest.raises(ConnectorError, match="public"):
        report.download_media("https://cdn.example.org/a.png")
    assert checked == ["https://cdn.example.org/a.png", "http://internal.local/x.png"]


def test_download_media_writes_temp_file(monkeypatch):
    import httpx

    import scraper0.core.fetch as fetch
    monkeypatch.setattr(fetch, "is_public_url", lambda u: True)
    real_client = httpx.Client
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(
        transport=httpx.MockTransport(lambda r: httpx.Response(200, content=b"PNGDATA")), **kw))
    path = report.download_media("https://cdn.example.org/a.PNG?sig=1")
    try:
        assert path.endswith(".png") and open(path, "rb").read() == b"PNGDATA"
    finally:
        os.unlink(path)
    monkeypatch.setattr(httpx, "Client", lambda **kw: real_client(
        transport=httpx.MockTransport(lambda r: httpx.Response(404)), **kw))
    with pytest.raises(ConnectorError, match="status: 404"):
        report.download_media("https://cdn.example.org/a.png")


def test_state_helpers():
    st = FakeState()
    bbmp_login.save_storage_state(st, "u1", {"cookies": []})
    assert st.data == {"storage_state:u1": '{"cookies": []}'}
    assert bbmp_login.load_storage_state(st, "u1") == {"cookies": []}
    assert bbmp_login.load_storage_state(st, "nobody") is None


# --------------------------------------------------------------------------- track

def _track_html(status="Registered", remarks="Work in progress", staff="Ward Engineer", contact="080-0000000"):
    def group(label, value):
        return f'<div class="form-group"><label>{label}</label><div class="col-sm-6">{value}</div></div>'
    return ('<html><body><div class="panel panel-primary">' + group("Complaint ID", "21025835") + "</div>"
            '<div class="panel panel-primary">' + group("Grievance\xa0Status", status) + group("Staff Remarks", remarks)
            + group("Staff Name", staff) + group("Contact Details", contact) + "</div></body></html>")


@pytest.mark.parametrize("raw, status", [("Registered", "OPEN"), (" CLOSED ", "CLOSED")])
def test_track_payload(make_ctx, monkeypatch, raw, status):
    seen = []
    monkeypatch.setattr(track, "fetch_status_html", lambda tid: seen.append(tid) or _track_html(status=raw))
    out = track.track_issue({"tracking_id": 21025835}, make_ctx())
    assert seen == ["21025835"]
    assert out == {"status": status, "meta_data": {"remarks": "Work in progress", "staff_name": "Ward Engineer",
                                                   "mobile_number": "080-0000000"}}


def test_track_errors(make_ctx, monkeypatch):
    monkeypatch.setattr(track, "fetch_status_html", lambda tid: _track_html(status="Reopened"))
    with pytest.raises(ConnectorError, match="Unknown grievance status: reopened"):
        track.track_issue({"tracking_id": "1"}, make_ctx())

    monkeypatch.setattr(track, "fetch_status_html", lambda tid: "<html><div class='panel panel-primary'></div></html>")
    with pytest.raises(ConnectorError, match=r"Complaint details not found. Invalid ID\?"):
        track.track_issue({"tracking_id": "1"}, make_ctx())

    def timeout(tid):
        raise track.FetchTimeout()
    monkeypatch.setattr(track, "fetch_status_html", timeout)
    with pytest.raises(ConnectorError, match="Request timed out while fetching complaint details."):
        track.track_issue({"tracking_id": "1"}, make_ctx())

    def boom(tid):
        raise RuntimeError("browser missing")
    monkeypatch.setattr(track, "fetch_status_html", boom)
    with pytest.raises(ConnectorError, match="Unexpected error: browser missing"):
        track.track_issue({"tracking_id": "1"}, make_ctx())


# --------------------------------------------------------------------------- registry / lazy imports

def test_registry_and_lazy_imports():
    from scraper0.connectors import base
    names = {c.name: c for c in base.all_connectors()}
    assert set(names["SMARTONEBLR"].actions) == {"REPORT_ISSUE", "TRACK_ISSUE"}
    assert names["SMARTONEBLR"].needs_browser and names["SMARTONEBLR"].source == "GOV_ISSUE_PORTAL"
    for n in ("TEAMEVEREST", "MYBHARATGOVIN", "IVOLUNTEERIN"):
        assert names[n].source == "EVENT_PORTAL" and set(names[n].actions) == {"FETCH_EVENTS"}
    assert "easyocr" not in sys.modules
    assert "playwright.sync_api" not in sys.modules
