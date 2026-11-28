import json

from scraper0.connectors import _events_common as common

from .conftest import FakeAI


def test_bind_kwargs_skips_loopback_and_none():
    assert common.bind_kwargs("10.1.2.3") == {"interface": "10.1.2.3"}
    assert common.bind_kwargs("127.0.0.1") == {}
    assert common.bind_kwargs(None) == {}


def test_strip_utm_params():
    assert common.strip_utm_params("https://x.in/p?a=1&utm_source=wa&utm_medium=x") == "https://x.in/p?a=1"
    assert common.strip_utm_params("https://x.in/p?utm_medium=x") == "https://x.in/p"
    assert common.strip_utm_params("https://x.in/p") == "https://x.in/p"


def test_single_prompt_is_filled_from_legacy_template():
    ai = FakeAI(['{"select_event": true, "type": "BEACH_CLEANUP"}'])
    r = common.classify_single(ai, "PAGE " * 5000, "beach cleanups", "environment")
    assert r == {"select_event": True, "type": "BEACH_CLEANUP"}
    p = ai.text_prompts[0]
    assert "Event filter: beach cleanups" in p and "Category filter: environment" in p
    assert "CLEANLINESS_DRIVE, BEACH_CLEANUP" in p
    assert "{event_content}" not in p
    assert p.count("PAGE") <= common.SINGLE_TEXT_CAP // 5 + 1   # page text capped at 6000 chars


def test_single_tolerates_fences_and_prose_and_fixes_unknown_type():
    ai = FakeAI(['```json\n{"select_event": true, "type": "PARTY"}\n```',
                 'Sure! Here it is: {"select_event": false, "type": "WORKSHOP"} hope that helps'])
    assert common.classify_single(ai, "x", "f", "") == {"select_event": True, "type": "OTHER"}
    assert common.classify_single(ai, "x", "f", "") == {"select_event": False, "type": "WORKSHOP"}


def test_single_falls_back_to_reject_on_bad_json_or_ai_error():
    default = {"select_event": False, "type": "OTHER"}
    assert common.classify_single(FakeAI(["not json at all"]), "x", "f", "") == default
    assert common.classify_single(FakeAI(["[1, 2]"]), "x", "f", "") == default
    assert common.classify_single(FakeAI(RuntimeError("budget")), "x", "f", "") == default


def test_batch_prompt_numbering_and_cap():
    ai = FakeAI(['[{"index": 0, "select_event": true, "type": "VOLUNTEERING"}]'])
    common.classify_batch(ai, ["a" * 3000, "b"], "f", "c")
    p = ai.text_prompts[0]
    assert "--- Event 0 ---\n" + "a" * 1500 + "\n\n--- Event 1 ---\nb" in p
    assert "a" * 1501 not in p


def test_batch_fallbacks():
    rej = [{"index": 0, "select_event": False, "type": "OTHER"}, {"index": 1, "select_event": False, "type": "OTHER"}]
    assert common.classify_batch(FakeAI(["garbage"]), ["a", "b"], "f", "") == rej
    assert common.classify_batch(FakeAI(['{"index": 0}']), ["a", "b"], "f", "") == rej
    assert common.classify_batch(FakeAI(TimeoutError("x")), ["a", "b"], "f", "") == rej


def test_filter_in_batches_maps_by_index_across_batches():
    items = [f"ev{i}" for i in range(10)]   # 2 batches: 8 + 2

    def reply(prompt):
        if "--- Event 7 ---" in prompt:   # first batch: select 1 and 7, reply out of order, one junk entry
            return json.dumps([{"index": 7, "select_event": True, "type": "WORKSHOP"}, "junk",
                               {"index": "1", "select_event": True, "type": "NOPE"},
                               {"index": 2, "select_event": "false"}, {"select_event": True}])
        return '[{"index": 1, "select_event": true, "type": "TREEPLANTATION"}]'   # second batch: ev9

    progress = []
    out = common.filter_in_batches(FakeAI(reply), items, lambda s: s, "f", "", progress=lambda m, d: progress.append(d))
    assert out == [("ev1", "OTHER"), ("ev7", "WORKSHOP"), ("ev9", "TREEPLANTATION")]
    assert [d["done"] for d in progress] == [8, 10]


