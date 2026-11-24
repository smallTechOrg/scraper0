"""Captcha solving for the SmartOne BLR login.

Default: vision LLM (ctx.ai.vision) on a Pillow-preprocessed image. EasyOCR is an optional extra
(`uv sync --extra ocr`); it is imported lazily, its Reader cached, and when it can't be imported we
fall back to the LLM. Mode comes from get_settings().captcha_solver: "llm" | "easyocr" | "auto".
"""
from __future__ import annotations

import io
import threading

import structlog

from scraper0.connectors.base import AI

log = structlog.get_logger(__name__)

THRESHOLD = 150
SCALE = 2

VISION_PROMPT = (
    "This image is a website login captcha. Read the characters shown in it exactly, preserving "
    "upper/lower case. Reply with only the characters, no spaces, no punctuation, no explanation."
)

_reader = None
_reader_lock = threading.Lock()


def preprocess(image_bytes: bytes) -> bytes:
    """Grayscale → 2× upscale → binary threshold (legacy cv2 pipeline, in Pillow). Returns PNG bytes."""
    from PIL import Image

    with Image.open(io.BytesIO(image_bytes)) as im:
        gray = im.convert("L")
        gray = gray.resize((gray.width * SCALE, gray.height * SCALE), Image.Resampling.LANCZOS)
        bw = gray.point(lambda p: 255 if p > THRESHOLD else 0)
        out = io.BytesIO()
        bw.save(out, format="PNG")
        return out.getvalue()


def clean(text: str | None) -> str:
    return "".join(c for c in (text or "") if c.isascii() and c.isalnum())


def _easyocr_reader():
    """Cached EasyOCR Reader; raises ImportError when the optional extra isn't installed."""
    global _reader
    if _reader is None:
        with _reader_lock:
            if _reader is None:
                import easyocr  # noqa: PLC0415 — optional, heavy (PyTorch)
                _reader = easyocr.Reader(["en"], gpu=False, verbose=False, quantize=True)
    return _reader


def easyocr_available() -> bool:
    import importlib.util
    try:
        return importlib.util.find_spec("easyocr") is not None
    except (ImportError, ValueError):
        return False


def _solve_easyocr(png: bytes) -> str:
    import numpy as np  # comes with easyocr
    from PIL import Image

    reader = _easyocr_reader()
    with Image.open(io.BytesIO(png)) as im:
        arr = np.array(im.convert("L"))
    return clean("".join(reader.readtext(arr, detail=0)))


def _solve_llm(png: bytes, ai: AI) -> str:
    return clean(ai.vision(VISION_PROMPT, png, "image/png"))


def _mode() -> str:
    try:
        from scraper0.config import get_settings
        return get_settings().captcha_solver
    except Exception:  # noqa: BLE001
        return "llm"


def solve_captcha(image_bytes: bytes, ai: AI, mode: str | None = None) -> str:
    """Return the captcha's alphanumeric characters, or "" when it can't be read (caller refreshes)."""
    mode = mode or _mode()
    try:
        png = preprocess(image_bytes)
    except Exception as e:  # noqa: BLE001
        log.warning("captcha_preprocess_failed", error=str(e))
        return ""

    use_easyocr = mode == "easyocr" or (mode == "auto" and easyocr_available())
    if use_easyocr:
        try:
            text = _solve_easyocr(png)
            log.info("captcha_solved", solver="easyocr", length=len(text))
            return text
        except ImportError:
            log.warning("captcha_easyocr_unavailable_fallback_llm")
        except Exception as e:  # noqa: BLE001
            log.warning("captcha_easyocr_failed_fallback_llm", error=str(e))

    try:
        text = _solve_llm(png, ai)
        log.info("captcha_solved", solver="llm", length=len(text))
        return text
    except Exception as e:  # noqa: BLE001 — legacy: OCR error → "" → refresh captcha and retry
        log.warning("captcha_llm_failed", error=str(e), error_type=type(e).__name__)
        return ""
