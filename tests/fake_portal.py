"""A small local "complaints portal" for action-plan tests: login behind a captcha (checked client-side, like the
real BBMP portal), a complaint form in an iframe with a sub-category list that fills in asynchronously, a
confirmation popup, an acknowledgement page, and a status page. Knobs on the Portal object break it on purpose."""
from __future__ import annotations

import io
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

CAPTCHA = "AB12"
USER, PASSWORD = "9000000001", "s3cret-Pass"

HOME = """<html><body>
<div id="top">%(top)s</div>
<a href="javascript:void(0)" onclick="openForm()">File complaint</a>
<div id="content"></div>
<div id="popup" style="display:none"><button id="popup_ok" onclick="ack()">OK</button></div>
<div class="modal-dialog modal-sm" id="login" style="display:none"><div class="modal-body">
  <input id="username" name="username"><input id="password" name="password" type="password">
  <img id="cap" src="/captcha.png?n=0" width="120" height="40">
  <i class="fa-refresh" onclick="refreshCap()" style="display:inline-block;width:16px;height:16px">r</i>
  <input name="captcha" placeholder="Enter captcha">
  <button onclick="signIn()">Sign In</button><span id="err"></span>
</div></div>
<script>
window.NO_POPUP = %(no_popup)s;
let capN = 0, lastId = null;
function openLogin(){ document.getElementById('login').style.display = 'block'; }
function refreshCap(){ capN++; document.getElementById('cap').src = '/captcha.png?n=' + capN; fetch('/refreshed'); }
async function signIn(){
  const c = document.querySelector('input[name=captcha]').value;
  if (c !== '%(captcha)s') { document.getElementById('err').textContent = 'bad captcha'; return; }
  const r = await fetch('/login', {method: 'POST', body: JSON.stringify({
      u: document.getElementById('username').value, p: document.getElementById('password').value})});
  const t = await r.text();
  if (t.includes('loginid:')) location.reload(); else document.getElementById('err').textContent = 'bad credentials';
}
function openForm(){ document.getElementById('content').innerHTML =
  '<iframe id="formframe" src="/form" width="700" height="500"></iframe>'; }
function showPopup(id){ lastId = id; if (!window.NO_POPUP) document.getElementById('popup').style.display = 'block'; }
function ack(){ document.getElementById('popup').style.display = 'none';
  document.getElementById('content').innerHTML = '<h5 class="mainHeading">Complaint Registration Acknowledgement</h5>' +
    '<p class="ack">Your Complaint ID ' + lastId + ' was registered on 28/11/2026 10:30</p>'; }
</script></body></html>"""

FORM = """<html><body><form id="f" onsubmit="return false">
<select id="typeId" onchange="loadSubs()"><option value="">--Select--</option>
  <option value="1">Roads</option><option value="2">Garbage</option></select>
<select id="subTypeId"><option value="">--Select--</option></select>
<label for="d">Description</label><textarea id="d"></textarea>
<input type="file" id="photo">
<input type="checkbox" id="useloc"><input id="addr" placeholder="Address Search">
%(submit)s
</form><script>
function loadSubs(){ setTimeout(() => { document.getElementById('subTypeId').innerHTML =
  '<option value="">--Select--</option><option value="11">Pothole</option><option value="12">Street Light</option>'; }, 300); }
let entered = false;
document.getElementById('addr').addEventListener('keydown', e => { if (e.key === 'Enter') entered = true; });
async function send(){
  const g = id => document.getElementById(id);
  const f = g('photo').files[0];
  const r = await fetch('/submit', {method: 'POST', body: JSON.stringify({category: g('typeId').value,
      sub: g('subTypeId').value, description: g('d').value, addr: g('addr').value, loc: g('useloc').checked,
      entered: entered, photo_bytes: f ? f.size : 0})});
  const j = await r.json();
  parent.showPopup(j.id);
}
</script></body></html>"""

TRACK = """<html><body>
<input id="no" placeholder="Complaint number" onkeydown="if (event.key === 'Enter') search()">%(button)s
<div id="res"></div>
<script>
async function search(){
  const no = document.getElementById('no').value;
  const j = await (await fetch('/status?no=' + encodeURIComponent(no))).json();
  if (!j.found) { document.getElementById('res').innerHTML = '<p>No such complaint</p>'; return; }
  const row = (k, v) => '<div class="form-group"><label>' + k + '</label><div class="col-sm-6">' + v + '</div></div>';
  document.getElementById('res').innerHTML =
    '<div class="panel panel-primary">' + row('Complaint No', no) + '</div>' +
    '<div class="panel panel-primary">' + row('Grievance Status', j.status) + row('Staff Remarks', 'On it') +
    row('Assigned To', 'Ravi') + row('Contact Details', '9999') + '</div>';
}
</script></body></html>"""


def _png() -> bytes:
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (120, 40), (250, 250, 250)).save(out, "PNG")
    return out.getvalue()


class Portal:
    def __init__(self):
        self.logins = 0                 # successful /login calls
        self.refreshes = 0              # captcha refresh clicks
        self.submissions: list[dict] = []
        self.status = {"777": "Closed"}
        self.sessions: set[str] = set()
        self.submit_button = True       # False: the form has no Submit button (fails before the commit step)
        self.popup = True               # False: the confirmation never shows (fails after the commit step)
        self.search_button = True       # False: the status page has no button, only Enter works
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), self._handler())
        self.base = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()

    def _handler(self):
        portal = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):  # quiet
                pass

            def _send(self, body, ctype="text/html", headers=None):
                data = body if isinstance(body, bytes) else body.encode()
                self.send_response(200)
                self.send_header("Content-Type", ctype)
                self.send_header("Content-Length", str(len(data)))
                for k, v in (headers or {}).items():
                    self.send_header(k, v)
                self.end_headers()
                self.wfile.write(data)

            def _logged_in(self):
                cookie = self.headers.get("Cookie") or ""
                return any(f"sid={s}" in cookie for s in portal.sessions)

            def do_GET(self):
                u = urlsplit(self.path)
                if u.path == "/home":
                    top = "Welcome, citizen" if self._logged_in() else \
                        '<a href="javascript:void(0)" onclick="openLogin()">Login/Register</a>'
                    self._send(HOME % {"top": top, "no_popup": "false" if portal.popup else "true", "captcha": CAPTCHA})
                elif u.path == "/form":
                    btn = '<button type="button" onclick="send()">Submit</button>' if portal.submit_button else ""
                    self._send(FORM % {"submit": btn})
                elif u.path == "/track":
                    self._send(TRACK % {"button": '<button onclick="search()">Go</button>' if portal.search_button else ""})
                elif u.path == "/status":
                    no = (parse_qs(u.query).get("no") or [""])[0]
                    st = portal.status.get(no)
                    self._send(json.dumps({"found": st is not None, "status": st}), "application/json")
                elif u.path in ("/captcha.png", "/photo.png"):
                    self._send(_png(), "image/png")
                elif u.path == "/refreshed":
                    portal.refreshes += 1
                    self._send("ok", "text/plain")
                else:
                    self.send_error(404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path == "/login":
                    if body.get("u") == USER and body.get("p") == PASSWORD:
                        portal.logins += 1
                        sid = f"s{portal.logins}"
                        portal.sessions.add(sid)
                        self._send("ok loginid:1", "text/plain", {"Set-Cookie": f"sid={sid}; Path=/"})
                    else:
                        self._send("denied", "text/plain")
                elif self.path == "/submit":
                    if not self._logged_in():
                        self.send_error(403)
                        return
                    cid = str(5001 + len(portal.submissions))
                    portal.submissions.append(body)
                    portal.status[cid] = "Registered"
                    self._send(json.dumps({"id": cid}), "application/json")
                else:
                    self.send_error(404)
        return H


def track_plan(base: str) -> dict:
    return {"plan_version": 1, "kind": "action", "mode": "lookup", "timeout_ms": 4000,
            "inputs": {"tracking_id": {"type": "string", "required": True, "coerce": True, "pattern": "[A-Za-z0-9-]+"}},
            "steps": [
                {"id": "open", "type": "browser", "do": "goto", "url": f"{base}/track"},
                {"id": "number", "type": "browser", "do": "fill", "value": "{input.tracking_id}",
                 "target": [{"css": "#complainantNo"}, {"placeholder": "Complaint number"}], "timeout_ms": 3000},
                {"id": "search", "type": "any_of", "alternatives": [
                    {"type": "browser", "do": "click", "target": {"role": "button", "name": "Go"}, "timeout_ms": 800},
                    {"type": "browser", "do": "press", "key": "Enter", "target": {"placeholder": "Complaint number"}}]},
                {"id": "result", "type": "browser", "do": "wait_for", "message": "Complaint details not found. Invalid ID?",
                 "target": {"css": "div.panel.panel-primary div.col-sm-6:has-text('{input.tracking_id}')"},
                 "timeout_ms": 2500},
                {"id": "staff", "type": "extract", "labels": {"container": "div.panel.panel-primary", "index": 1,
                                                              "min_containers": 2, "group": "div.form-group"}}],
            "output": {
                "status": {"from": "vars.staff.Grievance Status", "required": True,
                           "map": {"registered": "OPEN", "closed": "CLOSED"},
                           "unknown_message": "Unknown grievance status: {value}"},
                "meta_data": {"object": {
                    "remarks": {"from": "vars.staff.Staff Remarks"},
                    "staff_name": {"first_of": ["vars.staff.Staff Name", "vars.staff.Assigned To"], "omit_if_missing": True},
                    "mobile_number": {"from": "vars.staff.Contact Details"}}}}}


def report_plan(base: str, verify: str | None = None) -> dict:
    modal = {"css": ".modal-dialog.modal-sm", "has": "#username"}
    frame = {"frame_url": "/form"}
    plan = {
        "plan_version": 1, "kind": "action", "mode": "write", "timeout_ms": 4000,
        "inputs": {"category": {"type": "string", "required": True}, "sub_category": {"type": "string", "required": True},
                   "description": {"type": "string", "default": ""}, "media_url": {"type": "string", "format": "url"},
                   "latitude": {"type": "string", "coerce": True, "required": True},
                   "longitude": {"type": "string", "coerce": True, "required": True}},
        "auth": {"required": True, "fields": ["username", "password"]},
        "block": ["**polyfill.io**"],
        "steps": [
            {"id": "open", "type": "browser", "do": "goto", "url": f"{base}/home"},
            {"id": "session", "type": "session", "key": "{auth.username}",
             "logged_in": {"visible": {"text": "Welcome"}, "unless": {"visible": {"text": "Login/Register"}},
                           "timeout_ms": 3000},
             "login": [
                 {"type": "browser", "do": "click", "target": {"text": "Login/Register"}},
                 {"type": "browser", "do": "fill", "target": {"css": "#username", "in": [modal]}, "value": "{auth.username}"},
                 {"type": "browser", "do": "fill", "target": {"css": "#password", "in": [modal]}, "value": "{auth.password}"},
                 {"type": "captcha", "uses_ai": True, "attempts": 3, "message": "Captcha solving failed after retries",
                  "image": {"any": [{"css": "img"}], "in": [modal]},
                  "into": {"any": [{"css": "input[name='WordVerification']"}, {"css": "input[placeholder*='captcha' i]"}],
                           "in": [modal, {"css": ".modal-body"}]},
                  "refresh": {"css": "i.fa-refresh", "in": [modal]}, "refresh_settle_ms": 150,
                  "submit": [{"type": "browser", "do": "click",
                              "target": {"role": "button", "name": "Sign In", "in": [modal]}}],
                  "watch": {"url": "**/login", "timeout_ms": 1200, "fail_unless_contains": "loginid:",
                            "fail_message": "Invalid mobile number or password"},
                  "success": {"visible": {"text": "Welcome"}, "timeout_ms": 3000}}]},
            {"id": "open_form", "type": "browser", "do": "click", "target": {"text": "File complaint"}, "no_wait_after": True},
            {"id": "category", "type": "browser", "do": "select", "in": [frame], "target": {"css": "select#typeId"},
             "label": "{input.category}", "ignore_case": False},
            {"id": "sub_category", "type": "browser", "do": "select", "in": [frame],
             "target": [{"css": "select#subCategoryId"}, {"css": "select#subTypeId"}], "label": "{input.sub_category}"},
            {"id": "description", "type": "browser", "do": "fill", "in": [frame], "value": "{input.description}",
             "target": [{"css": "textarea#nope"}, {"label": "Description"}]},
            {"id": "photo", "type": "browser", "do": "upload", "in": [frame], "when": {"input": "media_url"},
             "target": {"css": "input[type=file]"}, "file": {"url": "{input.media_url}"}},
            {"id": "use_location", "type": "browser", "do": "check", "in": [frame], "target": {"css": "input[type='checkbox']"}},
            {"id": "address", "type": "browser", "do": "fill", "in": [frame],
             "target": {"placeholder": "Address Search"}, "value": "{input.latitude}, {input.longitude}"},
            {"id": "address_enter", "type": "browser", "do": "press", "key": "Enter"},
            {"id": "submit", "type": "browser", "do": "click", "in": [frame], "commit": True, "note": "Submitting complaint",
             "target": [{"css": "button:has-text('Submit')"}], "timeout_ms": 2500},
            {"id": "confirm", "type": "browser", "do": "click", "target": {"css": "#popup_ok"}, "timeout_ms": 2500},
            {"id": "acknowledged", "type": "browser", "do": "wait_for",
             "target": {"css": "h5.mainHeading:has-text('Complaint Registration Acknowledgement')"}},
            {"id": "ack", "type": "extract", "target": {"css": "p.ack"}, "fields": {
                "complaint_id": {"regex": r"Complaint ID\s*(\d+)", "required": True, "message": "Could not extract Complaint ID"},
                "timestamp": {"regex": r"on\s*(\d{2}/\d{2}/\d{4}\s+\d{2}:\d{2})", "required": True}}}],
        "output": {"tracking_id": {"from": "vars.ack.complaint_id", "type": "int", "required": True}}}
    if verify:
        plan["verify"] = {"action": verify, "input": {"tracking_id": "{output.tracking_id}"}}
    return plan
