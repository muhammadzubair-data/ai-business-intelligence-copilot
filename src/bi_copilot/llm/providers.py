"""Provider-flexible LLM access.

The Copilot works fully offline (deterministic planner + templated narrative).
Setting BI_COPILOT_LLM_PROVIDER=anthropic or openai adds an LLM for question
understanding and narrative writing. Any OpenAI-compatible endpoint works via
OPENAI_BASE_URL (for example a local model server).

The LLM never produces numbers the user sees without verification: plans are
validated against the catalog, SQL is guarded, and narratives are checked for
ungrounded figures (see analytics/grounding.py).
"""
from __future__ import annotations

import json
import os
import re
from typing import Optional

import numpy as np
import requests

from ..config import settings


class LLMError(RuntimeError):
    pass


class LLM:
    name = "base"

    def complete(self, system: str, user: str, max_tokens: int = 1200, temperature: float = 0.0) -> str:
        raise NotImplementedError

    def complete_json(self, system: str, user: str, max_tokens: int = 1200) -> dict:
        text = self.complete(system + "\nRespond with a single JSON object only. No prose, no markdown fences.",
                             user, max_tokens=max_tokens)
        return parse_json(text)


def parse_json(text: str) -> dict:
    cleaned = re.sub(r"```(?:json)?", "", text).strip()
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start < 0 or end < 0:
        raise LLMError("No JSON object in model output.")
    try:
        return json.loads(cleaned[start:end + 1])
    except json.JSONDecodeError as e:
        raise LLMError(f"Invalid JSON from model: {e}") from e


class AnthropicLLM(LLM):
    name = "anthropic"

    def __init__(self, model: str | None = None, api_key: str | None = None):
        self.model = model or settings.anthropic_model
        self.api_key = api_key or os.getenv("ANTHROPIC_API_KEY")
        if not self.api_key:
            raise LLMError("ANTHROPIC_API_KEY is not set.")

    def complete(self, system, user, max_tokens=1200, temperature=0.0):
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=60, headers={
            "x-api-key": self.api_key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
            json={"model": self.model, "max_tokens": max_tokens, "temperature": temperature, "system": system,
                  "messages": [{"role": "user", "content": user}]})
        if r.status_code != 200:
            raise LLMError(f"Anthropic API error {r.status_code}: {r.text[:300]}")
        return "".join(b.get("text", "") for b in r.json().get("content", []) if b.get("type") == "text")


class OpenAICompatibleLLM(LLM):
    name = "openai"

    def __init__(self, model: str | None = None, api_key: str | None = None, base_url: str | None = None):
        self.model = model or settings.openai_model
        self.api_key = api_key or os.getenv("OPENAI_API_KEY", "")
        self.base_url = (base_url or settings.openai_base_url).rstrip("/")
        if not self.api_key and "api.openai.com" in self.base_url:
            raise LLMError("OPENAI_API_KEY is not set.")

    def complete(self, system, user, max_tokens=1200, temperature=0.0):
        r = requests.post(f"{self.base_url}/chat/completions", timeout=60,
                          headers={"Authorization": f"Bearer {self.api_key}", "Content-Type": "application/json"},
                          json={"model": self.model, "max_tokens": max_tokens, "temperature": temperature,
                                "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]})
        if r.status_code != 200:
            raise LLMError(f"OpenAI-compatible API error {r.status_code}: {r.text[:300]}")
        return r.json()["choices"][0]["message"]["content"]


def get_llm(provider: str | None = None) -> Optional[LLM]:
    """Return a configured LLM, or None for offline mode."""
    provider = (provider or settings.llm_provider).lower()
    try:
        if provider == "anthropic":
            return AnthropicLLM()
        if provider in ("openai", "openai_compatible"):
            return OpenAICompatibleLLM()
    except LLMError:
        return None
    return None


def get_embedder():
    """Optional dense embeddings (OpenAI-compatible /embeddings). Returns a callable or None."""
    model = os.getenv("BI_COPILOT_EMBEDDING_MODEL")
    if not model:
        return None
    base = settings.openai_base_url.rstrip("/")
    key = os.getenv("OPENAI_API_KEY", "")

    def embed(texts: list[str]) -> np.ndarray:
        r = requests.post(f"{base}/embeddings", timeout=60, headers={"Authorization": f"Bearer {key}"},
                          json={"model": model, "input": texts})
        r.raise_for_status()
        return np.array([d["embedding"] for d in r.json()["data"]], dtype=float)

    return embed
