"""Gemini LLM wrapper supporting structured JSON output and Google Search grounding."""

from __future__ import annotations

import json
import logging
import warnings
from typing import Any, TypeVar

from pydantic import BaseModel

warnings.filterwarnings("ignore", category=SyntaxWarning, module="google.genai")

from google import genai
from google.genai import types

logger = logging.getLogger(__name__)
T = TypeVar("T", bound=BaseModel)


class GeminiClient:
    def __init__(self, api_key: str, default_model: str = "gemini-3.8-flash") -> None:
        self.api_key = api_key
        self.default_model = default_model
        self._client: genai.Client | None = None
        if api_key:
            self._client = genai.Client(api_key=api_key)

    @property
    def available(self) -> bool:
        return self._client is not None

    def generate_structured(
        self,
        prompt: str,
        schema: type[T],
        system_instruction: str | None = None,
        temperature: float = 0.2,
        model: str | None = None,
    ) -> T:
        if not self._client:
            raise RuntimeError("GEMINI_API_KEY is not configured.")

        config = types.GenerateContentConfig(
            temperature=temperature,
            response_mime_type="application/json",
            response_schema=schema,
            system_instruction=system_instruction,
        )
        resp = self._client.models.generate_content(
            model=model or self.default_model,
            contents=prompt,
            config=config,
        )
        if resp.parsed is not None and isinstance(resp.parsed, schema):
            return resp.parsed
        raw_text = (resp.text or "{}").strip()
        return schema.model_validate_json(raw_text)

    def generate_grounded_json(
        self,
        prompt: str,
        system_instruction: str | None = None,
        temperature: float = 0.2,
        model: str | None = None,
    ) -> Any:
        """Uses Google Search grounding tool and extracts JSON block from grounded response."""
        if not self._client:
            raise RuntimeError("GEMINI_API_KEY is not configured.")

        full_prompt = (
            f"{prompt}\n\n"
            "Return ONLY valid JSON (either a JSON array or JSON object) with no markdown prose outside a ```json fence."
        )
        config = types.GenerateContentConfig(
            temperature=temperature,
            tools=[types.Tool(google_search=types.GoogleSearch())],
            system_instruction=system_instruction,
        )
        resp = self._client.models.generate_content(
            model=model or self.default_model,
            contents=full_prompt,
            config=config,
        )
        text = (resp.text or "").strip()
        return extract_json_payload(text)


def extract_json_payload(text: str) -> Any:
    if "```json" in text:
        text = text.split("```json", 1)[1].split("```", 1)[0].strip()
    elif "```" in text:
        text = text.split("```", 1)[1].split("```", 1)[0].strip()
    # Locate outer JSON brackets/braces if extra text remains
    for start_char, end_char in [("[", "]"), ("{", "}")]:
        start_idx = text.find(start_char)
        end_idx = text.rfind(end_char)
        if start_idx != -1 and end_idx != -1 and end_idx > start_idx:
            candidate = text[start_idx : end_idx + 1]
            try:
                return json.loads(candidate)
            except json.JSONDecodeError:
                continue
    return json.loads(text)

