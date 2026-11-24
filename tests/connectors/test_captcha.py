import builtins
import io
import sys

from PIL import Image

from scraper0.connectors.bbmp import captcha

from .conftest import FakeAI


def _captcha_png(w=60, h=20) -> bytes:
    im = Image.new("RGB", (w, h), (200, 200, 200))
    for x in range(10, 50):
        im.putpixel((x, 10), (20, 20, 20))
    buf = io.BytesIO()
    im.save(buf, format="PNG")
    return buf.getvalue()


def test_preprocess_grayscale_upscale_threshold():
    out = captcha.preprocess(_captcha_png())
    im = Image.open(io.BytesIO(out))
    assert im.format == "PNG" and im.mode == "L" and im.size == (120, 40)
    lo, hi = im.getextrema()
    assert (lo, hi) == (0, 255) and len(im.histogram()) == 256 and sum(im.histogram()[1:255]) == 0


def test_llm_solver_filters_to_alphanumerics():
    ai = FakeAI(vision_replies=["  The captcha is: `aB3 x9`! "])
    assert captcha.solve_captcha(_captcha_png(), ai, mode="llm") == "ThecaptchaisaB3x9"
    prompt, image, mime = ai.vision_calls[0]
    assert mime == "image/png" and "only the characters" in prompt
    assert Image.open(io.BytesIO(image)).size == (120, 40)   # the preprocessed image is sent


def test_llm_error_or_bad_image_returns_empty():
    assert captcha.solve_captcha(_captcha_png(), FakeAI(vision_replies=RuntimeError("x")), mode="llm") == ""
    assert captcha.solve_captcha(b"not an image", FakeAI(), mode="llm") == ""


def _block_easyocr(monkeypatch):
    real_import = builtins.__import__

    def fake_import(name, *a, **k):
        if name == "easyocr":
            raise ImportError("No module named 'easyocr'")
        return real_import(name, *a, **k)
    monkeypatch.setattr(builtins, "__import__", fake_import)
    monkeypatch.setattr(captcha, "_reader", None)


def test_easyocr_mode_falls_back_to_llm_when_not_importable(monkeypatch):
    _block_easyocr(monkeypatch)
    ai = FakeAI(vision_replies=["X7k2"])
    assert captcha.solve_captcha(_captcha_png(), ai, mode="easyocr") == "X7k2"
    assert len(ai.vision_calls) == 1


def test_auto_mode_uses_llm_without_easyocr(monkeypatch):
    monkeypatch.setattr(captcha, "easyocr_available", lambda: False)
    ai = FakeAI(vision_replies=["ab12"])
    assert captcha.solve_captcha(_captcha_png(), ai, mode="auto") == "ab12"


def test_easyocr_used_and_reader_cached(monkeypatch):
    created = []

    class FakeReader:
        def __init__(self, *a, **k):
            created.append(1)

        def readtext(self, arr, detail=0):
            return ["Q9", " z-1"]

    import types
    monkeypatch.setitem(sys.modules, "easyocr", types.SimpleNamespace(Reader=FakeReader))
    monkeypatch.setitem(sys.modules, "numpy", types.SimpleNamespace(array=lambda im: im))   # numpy ships with easyocr
    monkeypatch.setattr(captcha, "_reader", None)
    monkeypatch.setattr(captcha, "easyocr_available", lambda: True)
    ai = FakeAI()
    assert captcha.solve_captcha(_captcha_png(), ai, mode="auto") == "Q9z1"
    assert captcha.solve_captcha(_captcha_png(), ai, mode="easyocr") == "Q9z1"
    assert created == [1] and ai.vision_calls == []


def test_mode_from_settings(monkeypatch):
    from scraper0 import config
    monkeypatch.setenv("SCRAPER0_CAPTCHA_SOLVER", "llm")
    config.get_settings.cache_clear()
    try:
        assert captcha._mode() == "llm"
    finally:
        config.get_settings.cache_clear()
