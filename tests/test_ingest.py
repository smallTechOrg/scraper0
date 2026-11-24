import io
import json
from datetime import datetime, timezone

from PIL import Image

from scraper0.events.ingest import IngestInput, IngestOptions, ingest, sniff

NOW = datetime(2026, 3, 1, tzinfo=timezone.utc)


def png(size=(3000, 2000)):
    b = io.BytesIO()
    Image.new("RGBA", size, (200, 30, 30, 255)).save(b, "PNG")
    return b.getvalue()


POSTER = json.dumps([{"name": "Lalbagh Tree Walk", "start": "2026-03-14T07:00", "end": "2026-03-14T09:00",
                      "address": "Lalbagh West Gate", "organisation": "Green BLR", "description": "Bring water",
                      "contact": "98450 00000", "price": "Free", "is_online": False}])


def run(inputs=(), text=None, llm=lambda p: "[]", vision=lambda p, b, m: "[]"):
    return ingest(list(inputs), text, llm, vision, IngestOptions(portal="ADMIN", now=NOW))


def test_image_poster_to_event_and_image_is_normalised():
    seen = {}
    def vision(prompt, data, mime):
        im = Image.open(io.BytesIO(data))
        seen.update(size=im.size, mime=mime, fmt=im.format)
        return "```json\n" + POSTER + "\n```"
    r = run([IngestInput("flyer.png", png())], vision=vision)
    [e] = r["events"]
    assert max(seen["size"]) <= 1600 and seen["fmt"] == "JPEG" and seen["mime"] == "image/jpeg"
    assert e.name == "Lalbagh Tree Walk" and e.start_time == "2026-03-14T01:30:00.000Z"
    assert e.price == "Free" and "98450 00000" in e.description and e.extraction == "image"
    assert e.confidence <= 0.8 and e.warnings == []


def test_filename_cannot_spoof_type():
    r = run([IngestInput("evil.png", b"<html>not an image</html>")])
    assert r["report"][0]["error"] == "unsupported file type" and r["events"] == []


def test_missing_fields_are_flagged_not_dropped():
    def vision(*_):
        return json.dumps([{"name": "Mystery Meetup"}])
    [e] = run([IngestInput("a.png", png((50, 50)))], vision=vision)["events"]
    assert {"no_date", "no_address", "low_confidence"} <= set(e.warnings)


def test_past_event_flagged():
    def vision(*_):
        return json.dumps([{"name": "Old", "start": "2025-01-01T10:00", "address": "X"}])
    [e] = run([IngestInput("a.png", png((50, 50)))], vision=vision)["events"]
    assert "past" in e.warnings


def test_bad_model_output_is_reported_not_fatal():
    r = run([IngestInput("a.png", png((50, 50))), IngestInput("b.csv", b"name,date\nWalk,2026-04-01\n")],
            vision=lambda *_: "sorry I can't")
    assert r["report"][0]["error"] and r["report"][1]["events"] == 1


def test_csv_with_aliases_semicolons_and_split_time():
    csv_ = "Event Name;Date;Time;Venue;Organizer;Category;Lat;Lng\nBeach Cleanup;04/05/2026;7:00 am;Marina;NGO;BEACH_CLEANUP;13.05;80.28\n;;;;;;;\n"
    [e] = run([IngestInput("x.csv", csv_.encode())])["events"]
    assert e.name == "Beach Cleanup" and e.type == "BEACH_CLEANUP"
    assert e.start_time == "2026-05-04T01:30:00.000Z"          # 4 May (dd/mm) 07:00 IST
    assert (e.latitude, e.longitude) == (13.05, 80.28) and e.address == "Marina"


def test_csv_bad_date_warns():
    [e] = run([IngestInput("x.csv", b"title,start\nWalk,someday\n")])["events"]
    assert any(w.startswith("unparsable_date") for w in e.warnings) and "no_date" in e.warnings


def test_json_events_wrapper():
    body = json.dumps({"events": [{"title": "A", "start": "2026-04-01T10:00:00+05:30", "location": "Park"}]})
    [e] = run([IngestInput("e.json", body.encode())])["events"]
    assert e.name == "A" and e.start_time == "2026-04-01T04:30:00.000Z"


def test_ics_upload_and_sniff():
    ics = b"BEGIN:VCALENDAR\nBEGIN:VEVENT\nSUMMARY:Walk\nDTSTART:20260401T040000Z\nLOCATION:Park\nEND:VEVENT\nEND:VCALENDAR"
    assert sniff(ics) == "text/calendar"
    [e] = run([IngestInput("cal.ics", ics)])["events"]
    assert e.name == "Walk" and e.link.startswith("upload:cal.ics#")


def test_pasted_text_multiple_events_and_blank_pdf():
    from pypdf import PdfWriter
    w = PdfWriter()
    w.add_blank_page(100, 100)
    b = io.BytesIO()
    w.write(b)
    def llm(p):
        return json.dumps([{"name": "A", "start": "2026-04-01T09:00", "address": "X"},
                                    {"name": "B", "start": "2026-04-02T09:00", "address": "Y"}])
    r = run([IngestInput("scan.pdf", b.getvalue())], text="two events...", llm=llm)
    assert [e.name for e in r["events"]] == ["A", "B"]
    assert "scanned" in r["report"][0]["error"]
