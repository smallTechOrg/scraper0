"""Provider-agnostic LLM calls over plain HTTP (no SDKs): Groq and any OpenAI-compatible API, plus
Anthropic. Text and vision. Retries on 429/5xx with Retry-After, and per-provider pacing.
Returns token counts so callers can meter cost.
"""
from __future__ import annotations

import base64
import threading
import time
from dataclasses import dataclass

import httpx
import structlog

log = structlog.get_logger(__name__)

DEFAULT_BASE = {
    "groq": "https://api.groq.com/openai/v1",
    "anthropic": "https://api.anthropic.com/v1",
}
# Groq free/on-demand tiers allow ~30 requests/min: pace to stay under it.
DEFAULT_MIN_INTERVAL = {"groq": 2.1}
KINDS = ("groq", "anthropic", "openai_compat")


class LLMError(RuntimeError):
    pass


@dataclass
class ProviderConfig:
    kind: str
    model: str
    api_key: str
    base_url: str | None = None
    vision_model: str | None = None
    price_in_per_mtok: float = 0.0
    price_out_per_mtok: float = 0.0
    id: str | None = None
    name: str = ""
    byo: bool = False

    def base(self) -> str:
        if self.base_url:
            return self.base_url.rstrip("/")
        if self.kind in DEFAULT_BASE:
            return DEFAULT_BASE[self.kind]
        raise LLMError("openai_compat providers need a base_url")

    def cost(self, tin: int, tout: int) -> float:
        return (tin * self.price_in_per_mtok + tout * self.price_out_per_mtok) / 1_000_000


@dataclass
class LLMResult:
    text: str
    input_tokens: int
    output_tokens: int
    model: str


_pace_lock = threading.Lock()
_last_call: dict[str, float] = {}


def _pace(p: ProviderConfig):
    gap = DEFAULT_MIN_INTERVAL.get(p.kind, 0.0)
    if not gap:
        return
    key = f"{p.kind}:{p.id or p.api_key[-6:]}"
    with _pace_lock:
        nowt = time.monotonic()
        slot = max(nowt, _last_call.get(key, 0) + gap)
        _last_call[key] = slot
    if slot > nowt:
        time.sleep(slot - nowt)


def complete(p: ProviderConfig, prompt: str, image: bytes | None = None, mime: str = "image/jpeg",
             max_tokens: int = 2000, timeout: float = 90.0, retries: int = 4,
             client: httpx.Client | None = None) -> LLMResult:
    if p.kind not in KINDS:
        raise LLMError(f"unknown provider kind {p.kind!r}")
    model = (p.vision_model or p.model) if image is not None else p.model
    if p.kind == "anthropic":
        url = f"{p.base()}/messages"
        headers = {"x-api-key": p.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"}
        content: list | str = prompt
        if image is not None:
            content = [{"type": "image", "source": {"type": "base64", "media_type": mime,
                                                    "data": base64.b64encode(image).decode()}},
                       {"type": "text", "text": prompt}]
        body = {"model": model, "max_tokens": max_tokens, "temperature": 0,
                "messages": [{"role": "user", "content": content}]}
    else:
        url = f"{p.base()}/chat/completions"
        headers = {"authorization": f"Bearer {p.api_key}", "content-type": "application/json"}
        content = prompt
        if image is not None:
            content = [{"type": "text", "text": prompt},
                       {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{base64.b64encode(image).decode()}"}}]
        body = {"model": model, "max_tokens": max_tokens, "temperature": 0,
                "messages": [{"role": "user", "content": content}]}

    own = client is None
    client = client or httpx.Client(timeout=timeout)
    try:
        last_err = "no attempt"
        for attempt in range(retries + 1):
            _pace(p)
            try:
                r = client.post(url, headers=headers, json=body)
            except httpx.HTTPError as e:
                last_err = f"{type(e).__name__}: {e}"
                time.sleep(min(2 ** attempt, 20))
                continue
            if r.status_code == 200:
                return _parse(p.kind, r.json(), model)
            if r.status_code in (429, 500, 502, 503, 529) and attempt < retries:
                wait = _retry_after(r, default=min(2 ** (attempt + 1), 30))
                log.warning("llm_retry", kind=p.kind, status=r.status_code, wait_s=wait, attempt=attempt + 1)
                time.sleep(wait)
                continue
            last_err = f"HTTP {r.status_code}: {r.text[:300]}"
            break
        raise LLMError(last_err)
    finally:
        if own:
            client.close()


def _retry_after(r: httpx.Response, default: float) -> float:
    try:
        return min(float(r.headers.get("retry-after", "")), 60.0)
    except ValueError:
        import re
        m = re.search(r"try again in (\d+(?:\.\d+)?)s", r.text, re.I)
        return float(m.group(1)) + 0.5 if m else default


def _parse(kind: str, data: dict, model: str) -> LLMResult:
    if kind == "anthropic":
        text = "".join(b.get("text", "") for b in data.get("content", []) if b.get("type") == "text")
        u = data.get("usage", {})
        return LLMResult(text, int(u.get("input_tokens", 0)), int(u.get("output_tokens", 0)), model)
    choice = (data.get("choices") or [{}])[0]
    text = (choice.get("message") or {}).get("content") or ""
    u = data.get("usage") or {}
    return LLMResult(text, int(u.get("prompt_tokens", 0)), int(u.get("completion_tokens", 0)), model)
