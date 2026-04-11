"""OpenAI Responses API wrapper for sponsor automation section generation."""

from __future__ import annotations

import os
from typing import Any, Callable

from openai import OpenAI

from cv_rank.sponsor_automation.cache import read_cache, write_cache
from cv_rank.sponsor_automation.schemas import (
    SchemaValidationError,
    parse_and_validate_section_output,
    validate_section_output,
)

DEFAULT_MODEL = "gpt-4.1-mini"


class OpenAIClientConfigError(RuntimeError):
    """Raised when OpenAI client configuration is missing or invalid."""


def get_openai_client() -> OpenAI:
    """Build a sync OpenAI client using OPENAI_API_KEY from environment."""
    api_key = os.environ.get("OPENAI_API_KEY")
    if not api_key:
        raise OpenAIClientConfigError("Set OPENAI_API_KEY environment variable")
    return OpenAI(api_key=api_key, timeout=60.0)


def _build_prompt(system_prompt: str, user_prompt: str) -> str:
    return f"[SYSTEM]\n{system_prompt}\n\n[USER]\n{user_prompt}"


def _response_item_to_dict(item: Any) -> dict[str, Any] | None:
    if isinstance(item, dict):
        return item
    model_dump = getattr(item, "model_dump", None)
    if callable(model_dump):
        dumped = model_dump()
        if isinstance(dumped, dict):
            return dumped
    return None


def extract_response_text(response: Any) -> str:
    """Extract plain text from an OpenAI Responses API object."""
    output_text = getattr(response, "output_text", None)
    if isinstance(output_text, str) and output_text.strip():
        return output_text.strip()

    chunks: list[str] = []
    output_items = getattr(response, "output", None)
    if isinstance(output_items, list):
        for item in output_items:
            item_dict = _response_item_to_dict(item)
            if not item_dict:
                continue
            content_items = item_dict.get("content")
            if not isinstance(content_items, list):
                continue
            for content in content_items:
                content_dict = _response_item_to_dict(content)
                if not content_dict:
                    continue
                if content_dict.get("type") not in {"output_text", "text"}:
                    continue
                text = content_dict.get("text")
                if isinstance(text, str) and text.strip():
                    chunks.append(text.strip())

    if chunks:
        return "\n".join(chunks)

    raise SchemaValidationError("OpenAI response did not contain textual output")


def generate_section_output(
    *,
    system_prompt: str,
    user_prompt: str,
    claims: Any,
    model: str = DEFAULT_MODEL,
    max_output_tokens: int | None = None,
    temperature: float | None = None,
    force_refresh: bool = False,
    validator: Callable[[Any], dict[str, Any]] = validate_section_output,
) -> dict[str, Any]:
    """Generate structured section output via OpenAI Responses API.

    Cache key is deterministic from prompt+claims hash and stored on disk under
    /Users/nickita/cv-rank/.cache/sponsor_automation.
    """
    prompt = _build_prompt(system_prompt=system_prompt, user_prompt=user_prompt)
    cache_key, cached_payload = read_cache(prompt=prompt, claims=claims)

    if not force_refresh and cached_payload:
        cached_output = cached_payload.get("section_output")
        if isinstance(cached_output, dict):
            return validator(cached_output)

    client = get_openai_client()
    try:
        request: dict[str, Any] = {
            "model": model,
            "input": [
                {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
                {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]},
            ],
        }
        if max_output_tokens is not None:
            request["max_output_tokens"] = max_output_tokens
        if temperature is not None:
            request["temperature"] = temperature

        response = client.responses.create(**request)
        output_text = extract_response_text(response)
        parsed_output = parse_and_validate_section_output(output_text)
        validated_output = validator(parsed_output)
    finally:
        client.close()

    write_cache(
        prompt=prompt,
        claims=claims,
        payload={
            "expected_cache_key": cache_key,
            "model": model,
            "response_id": getattr(response, "id", None),
            "section_output": validated_output,
            "raw_output_text": output_text,
        },
    )

    return validated_output


__all__ = [
    "DEFAULT_MODEL",
    "OpenAIClientConfigError",
    "extract_response_text",
    "generate_section_output",
    "get_openai_client",
]
