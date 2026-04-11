"""Simple JSON schema checks for sponsor automation section outputs."""

from __future__ import annotations

import json
import re
from typing import Any

ALLOWED_CONFIDENCE = frozenset({"high", "medium", "low"})


class SchemaValidationError(ValueError):
    """Raised when LLM output does not match the expected section schema."""


def _require_dict(value: Any, *, name: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise SchemaValidationError(f"{name} must be an object")
    return value


def _require_nonempty_str(value: Any, *, name: str) -> str:
    if not isinstance(value, str):
        raise SchemaValidationError(f"{name} must be a string")
    stripped = value.strip()
    if not stripped:
        raise SchemaValidationError(f"{name} must be non-empty")
    return stripped


def _extract_json_text(raw_text: str) -> str:
    text = raw_text.strip()
    text = re.sub(r"^```(?:json)?\s*\n?", "", text)
    text = re.sub(r"\n?```\s*$", "", text)
    return text.strip()


def parse_json_object(raw_text: str) -> dict[str, Any]:
    """Parse a JSON object from model output text."""
    cleaned = _extract_json_text(raw_text)
    try:
        parsed = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise SchemaValidationError(f"Model output is not valid JSON: {exc}") from exc
    return _require_dict(parsed, name="section_output")


def validate_section_claim(claim: Any, *, index: int = 0) -> dict[str, Any]:
    """Validate one claim record."""
    item = _require_dict(claim, name=f"claims[{index}]")
    statement = item.get("statement", item.get("claim"))
    source = item.get("source")
    confidence = item.get("confidence")

    statement_value = _require_nonempty_str(statement, name=f"claims[{index}].statement")
    source_value = _require_nonempty_str(source, name=f"claims[{index}].source")
    confidence_value = _require_nonempty_str(confidence, name=f"claims[{index}].confidence").lower()

    if confidence_value not in ALLOWED_CONFIDENCE:
        allowed = ", ".join(sorted(ALLOWED_CONFIDENCE))
        raise SchemaValidationError(f"claims[{index}].confidence must be one of: {allowed}")

    normalized = dict(item)
    normalized["statement"] = statement_value
    normalized["source"] = source_value
    normalized["confidence"] = confidence_value
    return normalized


def validate_section_output(section_output: Any) -> dict[str, Any]:
    """Validate section output JSON with lightweight deterministic checks.

    Required fields:
    - section_id: non-empty string
    - summary_markdown: non-empty string
    - claims: list[object]
    """
    root = _require_dict(section_output, name="section_output")

    section_id = _require_nonempty_str(root.get("section_id"), name="section_id")
    summary_markdown = _require_nonempty_str(root.get("summary_markdown"), name="summary_markdown")
    claims = root.get("claims")
    if not isinstance(claims, list):
        raise SchemaValidationError("claims must be a list")

    normalized_claims = [validate_section_claim(item, index=idx) for idx, item in enumerate(claims)]

    normalized = dict(root)
    normalized["section_id"] = section_id
    normalized["summary_markdown"] = summary_markdown
    normalized["claims"] = normalized_claims

    question_ids = root.get("question_ids")
    if question_ids is not None:
        if not isinstance(question_ids, list):
            raise SchemaValidationError("question_ids must be a list when present")
        normalized["question_ids"] = [
            _require_nonempty_str(question_id, name=f"question_ids[{idx}]")
            for idx, question_id in enumerate(question_ids)
        ]

    csv_rows = root.get("csv_rows")
    if csv_rows is not None:
        if not isinstance(csv_rows, list) or any(not isinstance(row, dict) for row in csv_rows):
            raise SchemaValidationError("csv_rows must be a list of objects when present")

    return normalized


def parse_and_validate_section_output(raw_text: str) -> dict[str, Any]:
    """Parse model output text and validate section schema."""
    parsed = parse_json_object(raw_text)
    return validate_section_output(parsed)
