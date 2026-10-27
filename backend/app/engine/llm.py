"""
Minimal, provider-agnostic LLM client.

Supported providers
-------------------
* ``openai``   – any OpenAI-compatible ``/chat/completions`` endpoint
                 (OpenAI, Z.ai, OpenRouter, Ollama, vLLM, ...). This is the
                 default because it needs nothing but a base URL + key.
* ``anthropic``– native Anthropic Messages API (Claude).
* ``none``     – no key configured; callers fall back to their local
                 deterministic generators so the app keeps working offline.

Every helper returns parsed JSON when possible, otherwise raw text.
"""
from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional

import httpx

from ..config import ANTHROPIC_KEY, LLM_API_KEY, LLM_BASE_URL, LLM_MODEL, LLM_PROVIDER, VISION_MODEL


class LLMUnavailable(RuntimeError):
    """No provider configured, or the provider rejected the request."""


def configured() -> bool:
    if LLM_PROVIDER == "none":
        return False
    if LLM_PROVIDER == "anthropic":
        return bool(ANTHROPIC_KEY)
    return bool(LLM_API_KEY)


# ---------------------------------------------------------------------------
# JSON extraction (models love to wrap JSON in ``` fences)
# ---------------------------------------------------------------------------
def extract_json(text: str) -> Dict[str, Any]:
    if not text:
        return {}
    cleaned = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(cleaned)
    except Exception:
        pass
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            return json.loads(cleaned[start : end + 1])
        except Exception:
            return {}
    return {}


# ---------------------------------------------------------------------------
# Core request
# ---------------------------------------------------------------------------
def _post(url: str, *, headers: Dict[str, str], payload: Dict[str, Any], timeout: float = 90.0) -> Dict[str, Any]:
    try:
        resp = httpx.post(url, json=payload, headers=headers, timeout=timeout)
    except httpx.HTTPError as exc:
        raise LLMUnavailable(f"LLM request failed: {exc}") from exc
    if resp.status_code >= 400:
        raise LLMUnavailable(f"LLM HTTP {resp.status_code}: {resp.text[:300]}")
    return resp.json()


def complete(
    system: str,
    user: str,
    *,
    json_mode: bool = True,
    temperature: float = 0.4,
    max_tokens: int = 2200,
    model: Optional[str] = None,
) -> str:
    """Return the raw text completion from the configured provider."""
    if not configured():
        raise LLMUnavailable("No LLM provider configured (set WV_LLM_API_KEY).")

    if LLM_PROVIDER == "anthropic":
        data = _post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            payload={
                "model": model or LLM_MODEL,
                "max_tokens": max_tokens,
                "temperature": temperature,
                "system": system,
                "messages": [{"role": "user", "content": user}],
            },
        )
        return "".join(b.get("text", "") for b in data.get("content", []))

    payload: Dict[str, Any] = {
        "model": model or LLM_MODEL,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
    }
    if json_mode:
        payload["response_format"] = {"type": "json_object"}

    headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
    try:
        data = _post(f"{LLM_BASE_URL.rstrip('/')}/chat/completions", headers=headers, payload=payload)
    except LLMUnavailable:
        # some compatible endpoints reject response_format – retry without it
        payload.pop("response_format", None)
        data = _post(f"{LLM_BASE_URL.rstrip('/')}/chat/completions", headers=headers, payload=payload)

    return data["choices"][0]["message"]["content"]


def complete_json(system: str, user: str, **kwargs: Any) -> Dict[str, Any]:
    """Completion that is expected to be JSON; never raises on parse failure."""
    try:
        return extract_json(complete(system, user, json_mode=True, **kwargs))
    except LLMUnavailable:
        raise
    except Exception:
        return {}


def vision_describe(image_bytes: bytes, prompt: str, *, mime: str = "image/jpeg") -> str:
    """Describe a frame with a vision-capable model (Claude or multimodal chat)."""
    import base64

    b64 = base64.b64encode(image_bytes).decode()
    if LLM_PROVIDER == "anthropic" or (not LLM_API_KEY and ANTHROPIC_KEY):
        data = _post(
            "https://api.anthropic.com/v1/messages",
            headers={
                "x-api-key": ANTHROPIC_KEY,
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
            payload={
                "model": VISION_MODEL or "claude-3-5-sonnet-latest",
                "max_tokens": 700,
                "messages": [
                    {
                        "role": "user",
                        "content": [
                            {"type": "image", "source": {"type": "base64", "media_type": mime, "data": b64}},
                            {"type": "text", "text": prompt},
                        ],
                    }
                ],
            },
        )
        return "".join(b.get("text", "") for b in data.get("content", []))

    data = _post(
        f"{LLM_BASE_URL.rstrip('/')}/chat/completions",
        headers={"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"},
        payload={
            "model": VISION_MODEL or LLM_MODEL,
            "max_tokens": 700,
            "messages": [
                {
                    "role": "user",
                    "content": [
                        {"type": "text", "text": prompt},
                        {"type": "image_url", "image_url": {"url": f"data:{mime};base64,{b64}"}},
                    ],
                }
            ],
        },
    )
    return data["choices"][0]["message"]["content"]


def provider_label() -> str:
    if not configured():
        return "local-fallback"
    return LLM_PROVIDER if LLM_PROVIDER != "openai" else LLM_BASE_URL


# ---------------------------------------------------------------------------
# Feature-facing helpers
#
# Every feature (Clip Maker metadata, Summarizer, Note Taker, Video-to-Text)
# calls these instead of ``complete`` directly, because they never raise: when
# no key is configured or the provider fails they return ``{"_fallback": True}``
# so the caller can switch to its deterministic local generator.
# ---------------------------------------------------------------------------
def llm_json(system: str, user: str, **kwargs: Any) -> Dict[str, Any]:
    """JSON completion that degrades to ``{"_fallback": True}`` offline."""
    if not configured():
        return {"_fallback": True}
    try:
        data = complete_json(system, user, **kwargs)
    except (LLMUnavailable, httpx.HTTPError):
        return {"_fallback": True}
    if not data:
        return {"_fallback": True}
    return data


def llm_text(system: str, user: str, **kwargs: Any) -> str:
    """Plain-text completion; empty string when no provider is configured."""
    if not configured():
        return ""
    try:
        return complete(system, user, json_mode=False, **kwargs).strip()
    except (LLMUnavailable, httpx.HTTPError):
        return ""


def vision_json(system: str, user: str, images: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """Multi-frame vision completion.

    ``images`` is a list of ``{"t": seconds, "b64": jpeg}``. Returns ``None``
    when no provider is configured so callers fall back to local frame
    analysis instead of failing the whole job.
    """
    if not configured() or not images:
        return None

    if LLM_PROVIDER == "anthropic":
        content: List[Dict[str, Any]] = []
        for img in images:
            content.append({"type": "text", "text": f"[frame at {img['t']}s]"})
            content.append({"type": "image", "source": {"type": "base64",
                                                          "media_type": "image/jpeg",
                                                          "data": img["b64"]}})
        content.append({"type": "text", "text": user})
        try:
            data = _post(
                "https://api.anthropic.com/v1/messages",
                headers={"x-api-key": ANTHROPIC_KEY,
                         "anthropic-version": "2023-06-01",
                         "content-type": "application/json"},
                payload={"model": VISION_MODEL or LLM_MODEL, "max_tokens": 1400,
                         "temperature": 0.3, "system": system,
                         "messages": [{"role": "user", "content": content}]},
                timeout=120.0,
            )
            text = "".join(b.get("text", "") for b in data.get("content", []))
        except LLMUnavailable:
            return None
        return extract_json(text) or None

    parts: List[Dict[str, Any]] = [{"type": "text", "text": user}]
    for img in images:
        parts.append({"type": "text", "text": f"[frame at {img['t']}s]"})
        parts.append({"type": "image_url",
                      "image_url": {"url": f"data:image/jpeg;base64,{img['b64']}"}})
    payload: Dict[str, Any] = {
        "model": VISION_MODEL or LLM_MODEL,
        "temperature": 0.3,
        "max_tokens": 1400,
        "response_format": {"type": "json_object"},
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": parts}],
    }
    headers = {"Authorization": f"Bearer {LLM_API_KEY}", "Content-Type": "application/json"}
    try:
        data = _post(f"{LLM_BASE_URL.rstrip('/')}/chat/completions",
                     headers=headers, payload=payload, timeout=120.0)
    except LLMUnavailable:
        payload.pop("response_format", None)
        try:
            data = _post(f"{LLM_BASE_URL.rstrip('/')}/chat/completions",
                         headers=headers, payload=payload, timeout=120.0)
        except LLMUnavailable:
            return None
    return extract_json(data["choices"][0]["message"]["content"]) or None

