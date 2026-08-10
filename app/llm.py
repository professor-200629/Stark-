"""
LLM access with graceful degradation.

Primary path: Groq (OpenAI-compatible), JSON-mode, with tool/format error
recovery — Groq's smaller models occasionally emit prose around the JSON, so we
salvage the first well-formed object rather than failing the request.

Fallback path: a deterministic synthesiser that composes the same JSON structure
directly from recalled memory. This keeps the demo fully functional with zero
credentials and makes the memory contribution unambiguous: whatever appears in
the brief came out of memory, not out of a model's priors.
"""

from __future__ import annotations

import json
import re
from typing import Any

import httpx

from .config import settings

_JSON_OBJECT_RE = re.compile(r"\{.*\}", re.S)


class LLMUnavailable(RuntimeError):
    pass


def available() -> bool:
    return settings.llm_enabled


def complete_json(
    system: str,
    user: str,
    *,
    temperature: float = 0.2,
    max_tokens: int = 1600,
    retries: int = 2,
) -> dict[str, Any]:
    """Call the LLM and return a parsed JSON object. Raises LLMUnavailable."""
    if not settings.llm_enabled:
        raise LLMUnavailable("no GROQ_API_KEY configured")

    payload = {
        "model": settings.groq_model,
        "messages": [
            {"role": "system", "content": system},
            {"role": "user", "content": user},
        ],
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    headers = {"Authorization": f"Bearer {settings.groq_api_key}"}

    last_error: Exception | None = None
    for attempt in range(retries + 1):
        try:
            with httpx.Client(timeout=60.0) as client:
                response = client.post(
                    f"{settings.groq_base_url}/chat/completions",
                    json=payload,
                    headers=headers,
                )
            if response.status_code == 429:
                last_error = LLMUnavailable("rate limited by provider")
                continue
            response.raise_for_status()
            content = response.json()["choices"][0]["message"]["content"]
            return _parse_json(content)
        except Exception as exc:  # noqa: BLE001 - we intentionally fall back
            last_error = exc
            # Some models ignore response_format; drop it and retry as plain text.
            payload.pop("response_format", None)
    raise LLMUnavailable(str(last_error))


def _parse_json(content: str) -> dict[str, Any]:
    content = (content or "").strip()
    try:
        return json.loads(content)
    except json.JSONDecodeError:
        pass
    # strip markdown fences
    fenced = re.sub(r"^```(?:json)?|```$", "", content, flags=re.M).strip()
    try:
        return json.loads(fenced)
    except json.JSONDecodeError:
        pass
    match = _JSON_OBJECT_RE.search(fenced)
    if match:
        return json.loads(match.group(0))
    raise LLMUnavailable("model did not return parseable JSON")
