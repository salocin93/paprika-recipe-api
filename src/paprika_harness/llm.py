"""Thin client for OpenAI-compatible chat endpoints (local or hosted).

Works with LM Studio, Ollama, vLLM, llama.cpp server, Gemini's OpenAI
endpoint, etc. Prefers structured output (``json_schema``); if the server
rejects that, falls back to plain prompting and lenient JSON extraction.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from typing import Any

from openai import BadRequestError, OpenAI

from .config import LLMConfig

log = logging.getLogger(__name__)


class LLMError(RuntimeError):
    """The model could not produce a valid response."""


def extract_json(text: str) -> dict[str, Any]:
    """Parse the first JSON object in ``text`` (tolerates code fences / chatter)."""
    text = text.strip()
    fence = re.search(r"```(?:json)?\s*(\{.*?\})\s*```", text, re.S)
    if fence:
        text = fence.group(1)
    try:
        data = json.loads(text)
    except json.JSONDecodeError:
        start, end = text.find("{"), text.rfind("}")
        if start == -1 or end <= start:
            raise LLMError(f"no JSON object in response: {text[:200]!r}")
        try:
            data = json.loads(text[start : end + 1])
        except json.JSONDecodeError as exc:
            raise LLMError(f"invalid JSON in response: {exc}") from exc
    if not isinstance(data, dict):
        raise LLMError("response JSON is not an object")
    return data


class RateLimiter:
    """Spaces calls at least ``60 / rpm`` seconds apart (thread-safe)."""

    def __init__(self, rpm: float | None):
        self.interval = 60.0 / rpm if rpm else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self) -> None:
        if not self.interval:
            return
        with self._lock:
            now = time.monotonic()
            start = max(now, self._next)
            self._next = start + self.interval
        if start > now:
            time.sleep(start - now)


class LLMClient:
    def __init__(
        self, config: LLMConfig, *, timeout: float = 60, max_retries: int = 6, max_rpm: float | None = None
    ):
        config.require()
        self.config = config
        self.limiter = RateLimiter(max_rpm if max_rpm is not None else config.max_rpm)
        self.client = OpenAI(
            base_url=config.base_url,
            api_key=config.api_key or "not-needed",
            timeout=timeout,
            max_retries=max_retries,  # SDK backs off on 429 / 5xx
        )
        self._structured = True  # flipped off if the server rejects json_schema

    @property
    def model(self) -> str:
        return self.config.model or ""

    def complete_json(
        self, system: str, user: str, schema: dict[str, Any], *, name: str = "result"
    ) -> dict[str, Any]:
        messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
        kwargs: dict[str, Any] = {"model": self.model, "messages": messages, "temperature": 0}

        if self._structured:
            try:
                self.limiter.wait()
                resp = self.client.chat.completions.create(
                    **kwargs,
                    response_format={
                        "type": "json_schema",
                        "json_schema": {"name": name, "schema": schema, "strict": True},
                    },
                )
                return extract_json(resp.choices[0].message.content or "")
            except BadRequestError as exc:
                log.warning("Server rejected structured output (%s); falling back to prompt-only JSON", exc)
                self._structured = False

        messages[0]["content"] = (
            f"{system}\n\nRespond with ONLY a JSON object matching this schema, no other text:\n"
            f"{json.dumps(schema)}"
        )
        self.limiter.wait()
        resp = self.client.chat.completions.create(**kwargs)
        return extract_json(resp.choices[0].message.content or "")
