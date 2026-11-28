"""Recipes as files: export locked versions, lose the database, import them back."""
import json

import pytest
from fastapi.testclient import TestClient
from test_v2 import H, acts, app, login, onboard, read, settings, tenants  # noqa: F401 - fixtures

from scraper0.db import session as db_session
from scraper0.db.models import Base
from scraper0.services import recipe_export as rx


def learn_and_lock(c):
    c.post("/v2/sites", json={"base_url": "https://acts.org"}, headers=H)
    c.post("/v2/sites/acts-org/actions", json={"name": "fetch_events", "kind": "read", "entity": "event"}, headers=H)
    assert c.post("/v2/sites/acts-org/actions/fetch_events/learn", headers=H).status_code == 202
    assert c.post("/v2/sites/acts-org/actions/fetch_events/versions/1/lock", headers=H).status_code == 200


def core(docs):
    return sorted((rx.content_hash(d), d["site"]["site_id"], d["action"]["name"]) for d in docs)


def snapshot(root):
    return {p.relative_to(root).as_posix(): (p.read_text(), p.stat().st_mtime_ns) for p in root.rglob("*.json")}


def test_export_is_deterministic_and_idempotent(app, tenants, acts, tmp_path):  # noqa: F811
    learn_and_lock(tenants["acme"])
    r = rx.export_dir(tmp_path)
    assert r["written"] == 6 and r["unchanged"] == 0            # 5 seeded code actions + the learned one
    assert "acts-org/fetch_events.json" in r["files"] and "smartoneblr-com/report_issue.json" in r["files"]
    before = snapshot(tmp_path)
    r2 = rx.export_dir(tmp_path)
    assert r2["written"] == 0 and r2["unchanged"] == 6 and snapshot(tmp_path) == before      # files untouched

    text = (tmp_path / "acts-org" / "fetch_events.json").read_text()
    doc = json.loads(text)
    assert text == rx.dumps(doc) and text.endswith("}\n")       # sorted keys, stable layout
    assert doc["format"] == rx.FORMAT and doc["version"] == 1 and doc["locked_by"] == "a@acme.org"
    assert doc["site"] == {"site_id": "acts-org", "base_url": "https://acts.org", "name": "acts.org"}
    assert doc["action"]["mode"] == "list" and doc["action"]["seed_urls"] == ["https://acts.org"]
    impl = doc["implementation"]
    assert impl["implementation"] == "learned" and impl["plans"][0]["plan"]["steps"]
    assert "learn" not in impl and "check_run" not in impl and "preview" not in doc     # no run state
    code = json.loads((tmp_path / "teameverest-ngo" / "fetch_events.json").read_text())
    assert code["implementation"] == {"implementation": "code", "connector": "TEAMEVEREST", "action": "FETCH_EVENTS"}

    index = json.loads((tmp_path / rx.INDEX).read_text())
    assert [e["file"] for e in index["recipes"]] == sorted(r["files"])
    assert all(len(e["sha256"]) == 64 for e in index["recipes"])


def test_round_trip_after_losing_the_database(app, tenants, acts, tmp_path, fresh_db):  # noqa: F811
    learn_and_lock(tenants["acme"])
    before = rx.export_all()
    rx.export_dir(tmp_path)

    Base.metadata.drop_all(fresh_db)                              # the database is gone
    Base.metadata.create_all(fresh_db)
    db_session.set_engine(fresh_db)
    assert rx.export_all() == []

    r = rx.import_path(tmp_path, lock=True)
    assert r == {"created": 6, "updated": 0, "unchanged": 0, "errors": []}
    after = rx.export_all()
    assert core(after) == core(before)
    assert [d["implementation"] for d in after] == [d["implementation"] for d in before]    # identical plans
    assert rx.import_path(tmp_path, lock=True) == {"created": 0, "updated": 0, "unchanged": 6, "errors": []}

    # the imported plan runs: a new tenant reads the site with no learning (and no browser)
    from scraper0.bootstrap import seed
    seed()
    renders = acts.renders
    _, key = onboard(app, login(app), "newco")
    d = read(key, site="acts-org").json()["data"]
    assert d["version"] == 1 and len(d["items"]) == 2 and acts.renders == renders


def test_import_adds_a_draft_and_lock_is_explicit(app, tenants, acts, tmp_path):  # noqa: F811
    learn_and_lock(tenants["acme"])
    rx.export_dir(tmp_path)
    f = tmp_path / "acts-org" / "fetch_events.json"
    doc = json.loads(f.read_text())
    doc["implementation"]["plans"][0]["plan"]["link_template"] = "https://acts.org/a/{id}"
    f.write_text(rx.dumps(doc))

    assert rx.import_path(tmp_path)["updated"] == 1               # differs from the locked version → draft v2
    vs = tenants["acme"].get("/v2/sites/acts-org/actions/fetch_events/versions").json()["data"]
    assert [(v["version"], v["status"]) for v in vs] == [(2, "draft"), (1, "locked")]
    assert rx.import_path(tmp_path, lock=True)["updated"] == 1    # still differs from what is locked → v3, locked
    vs = tenants["acme"].get("/v2/sites/acts-org/actions/fetch_events/versions").json()["data"]
    assert [(v["version"], v["status"]) for v in vs] == [(3, "locked"), (2, "draft"), (1, "retired")]
    assert rx.import_path(tmp_path, lock=True)["unchanged"] == 6


@pytest.mark.parametrize("change, error", [
    (lambda d: d["implementation"]["plans"][0]["plan"]["steps"].append({"id": "x", "type": "teleport"}), "plans[0]"),
    (lambda d: d["implementation"]["plans"][0]["plan"]["steps"][0].setdefault("headers", {}).update(Cookie="s=1"), "credential"),
    (lambda d: d["site"].update(site_id="other-org"), "does not match"),
    (lambda d: d.update(format="nope"), "not a scraper0.recipe/1"),
    (lambda d: d.update(implementation={"implementation": "code", "connector": "NOPE", "action": "X"}), "unknown connector"),
])
def test_invalid_recipes_are_rejected_without_stopping_the_rest(app, tenants, acts, tmp_path, change, error):  # noqa: F811
    learn_and_lock(tenants["acme"])
    rx.export_dir(tmp_path)
    f = tmp_path / "acts-org" / "fetch_events.json"
    doc = json.loads(f.read_text())
    change(doc)
    f.write_text(rx.dumps(doc))
    (tmp_path / "acts-org" / "broken.json").write_text("{not json")
    r = rx.import_path(tmp_path, lock=True)
    assert r["unchanged"] == 5 and r["created"] == 0 and r["updated"] == 0
    errors = {e["file"]: e["error"] for e in r["errors"]}
    assert error in errors["acts-org/fetch_events.json"] and "invalid JSON" in errors["acts-org/broken.json"]
    vs = tenants["acme"].get("/v2/sites/acts-org/actions/fetch_events/versions").json()["data"]
    assert len(vs) == 1                                           # nothing was added


def test_no_secrets_in_exports(app, tenants, acts, tmp_path, settings):  # noqa: F811
    from sqlalchemy import select

    from scraper0.db.models import ActionVersion
    from scraper0.db.session import session_scope
    learn_and_lock(tenants["acme"])
    api_key = tenants["acme"].post("/v1/api-keys", json={"name": "k2"}, headers=H).json()["data"]["key"]
    with session_scope() as s:                                    # a plan that (wrongly) kept credential headers
        v = s.scalars(select(ActionVersion).where(ActionVersion.status == "locked")).all()[-1]
        snap = json.loads(json.dumps(v.implementation_snapshot))
        snap["plans"][0]["plan"]["steps"][0]["headers"] = {"Authorization": "Bearer tok-123", "cookie": "sid=abc",
                                                           "X-Api-Key": "k-999", "accept": "application/json"}
        v.implementation_snapshot = snap
    rx.export_dir(tmp_path)
    blob = "".join(p.read_text() for p in tmp_path.rglob("*.json"))
    for secret in ("tok-123", "sid=abc", "k-999", api_key, settings.secret_key.get_secret_value(),
                   "correct horse battery", "password_hash"):
        assert secret not in blob
    assert '"accept": "application/json"' in blob                 # ordinary headers stay


def test_export_api_and_lock_hook(app, tenants, acts, tmp_path, settings, monkeypatch):  # noqa: F811
    monkeypatch.setattr(settings, "recipe_export_dir", str(tmp_path / "repo"))
    acme, zed, admin = tenants["acme"], tenants["zed_key"], tenants["admin"]
    learn_and_lock(acme)                                          # locking writes the file
    f = tmp_path / "repo" / "acts-org" / "fetch_events.json"
    doc = json.loads(f.read_text())
    assert doc["version"] == 1 and json.loads((tmp_path / "repo" / rx.INDEX).read_text())["recipes"][0]["action"] == "fetch_events"

    assert acme.get("/v2/sites/acts-org/actions/fetch_events/export").json()["data"] == doc
    assert zed.get("/v2/sites/acts-org/actions/fetch_events/export").status_code == 403          # service key: no
    assert acme.get("/v2/sites/acts-org/actions/nope/export").json()["error"]["code"] == "action_not_available"
    assert acme.get("/v2/export").status_code == 403                                             # platform admin only
    b = admin.get("/v2/export").json()["data"]
    assert b["format"] == rx.BUNDLE_FORMAT and len(b["recipes"]) == 6

    bundle = tmp_path / "bundle.json"                             # a bundle file imports like a directory
    bundle.write_text(json.dumps(b))
    assert rx.import_path(bundle)["unchanged"] == 6
    assert TestClient(app).get("/v2/export").status_code == 401


def test_cli_export_and_import(app, tenants, acts, tmp_path, capsys, monkeypatch):  # noqa: F811
    from scraper0 import cli
    monkeypatch.setattr("scraper0.bootstrap.migrate", lambda: None)
    learn_and_lock(tenants["acme"])
    cli.main(["export-recipes", str(tmp_path)])
    assert "6 written, 0 unchanged" in capsys.readouterr().out
    cli.main(["import-recipes", str(tmp_path), "--lock"])
    assert "0 created, 0 updated, 6 unchanged, 0 failed" in capsys.readouterr().out
    (tmp_path / "acts-org" / "fetch_events.json").write_text("{}")
    with pytest.raises(SystemExit):
        cli.main(["import-recipes", str(tmp_path)])
    assert "acts-org/fetch_events.json" in capsys.readouterr().err
