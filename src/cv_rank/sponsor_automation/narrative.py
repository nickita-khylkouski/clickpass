from __future__ import annotations

import json
import re
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

DEFAULT_MODEL = "gpt-4.1-mini"
FINAL_DOCUMENT_PROMPT_FILE = "final_document_editor.md"

SECTION_ORDER: tuple[str, ...] = (
    "executive",
    "funnel",
    "hiring",
    "sales_bd",
    "market_research",
    "marketing",
    "adoption",
    "recommendations",
)

SECTION_TITLES: dict[str, str] = {
    "executive": "Executive Summary",
    "funnel": "Funnel",
    "hiring": "Hiring Insights",
    "sales_bd": "Sales/BD Insights",
    "market_research": "Market Research Insights",
    "marketing": "Marketing Insights",
    "adoption": "Sponsor Tech Adoption",
    "recommendations": "Recommendations (next 30/60 days)",
}

SECTION_PROMPT_FILES: dict[str, str] = {
    "executive": "section_executive.md",
    "funnel": "section_funnel.md",
    "hiring": "section_hiring.md",
    "sales_bd": "section_sales_bd.md",
    "market_research": "section_market_research.md",
    "marketing": "section_marketing.md",
    "adoption": "section_adoption.md",
    "recommendations": "section_recommendations.md",
}

_SECTION_ALIASES: dict[str, str] = {
    "executive": "executive",
    "executivesummary": "executive",
    "summary": "executive",
    "funnel": "funnel",
    "hiring": "hiring",
    "hiringinsights": "hiring",
    "sales": "sales_bd",
    "salesbd": "sales_bd",
    "businessdevelopment": "sales_bd",
    "marketresearch": "market_research",
    "marketresearchinsights": "market_research",
    "marketing": "marketing",
    "marketinginsights": "marketing",
    "adoption": "adoption",
    "sponsortechadoption": "adoption",
    "recommendations": "recommendations",
    "next3060days": "recommendations",
}

_SECTION_KEYWORD_MAP: tuple[tuple[str, str], ...] = (
    ("sectione", "adoption"),
    ("sectionb", "hiring"),
    ("sectionc", "hiring"),
    ("sectiond", "sales_bd"),
    ("sectionf", "marketing"),
    ("sectionh", "market_research"),
    ("sectioni", "recommendations"),
    ("funnel", "funnel"),
)

_REPO_ROOT = Path(__file__).resolve().parents[3]
DEFAULT_PROMPT_DIR = _REPO_ROOT / "prompts" / "sponsor"

_EDITOR_PROMPT = """You are editing one sponsor-facing markdown section.

Hard constraints:
- Keep all facts and numbers consistent with CLAIMS_JSON.
- Remove duplicate or near-duplicate bullets.
- Improve readability and flow.
- Do not add new claims.
- This section will be assembled into one final sponsor document, so reduce overlap with executive-level points unless the section adds a new interpretation.
- Preserve useful numerical density; do not over-compress into generic bullets.
- Preserve section subheadings when present.
- Do not mention methodology, calculations, extraction process, confidence, source, claim labels, or metadata terms.
- Remove low-value tautology lines (for example: `0/N multi-tool`, `N/N external base URL`, or coverage-only filler).
- Prefer plain English over internal analytics jargon.
- Ensure each bullet includes a clear "what this means" interpretation, not just a raw metric.
- Return markdown for this section only.
"""


def _compact_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", value.lower())


def normalize_section_name(section: str) -> str:
    key = _compact_key(section)
    if key in _SECTION_ALIASES:
        return _SECTION_ALIASES[key]

    for token, mapped in _SECTION_KEYWORD_MAP:
        if token in key:
            return mapped

    raise ValueError(f"Unrecognized section name: {section!r}")


def section_prompt_path(section: str, *, prompt_dir: Path | None = None) -> Path:
    canonical = normalize_section_name(section)
    prompt_root = prompt_dir or DEFAULT_PROMPT_DIR
    return prompt_root / SECTION_PROMPT_FILES[canonical]


def load_section_prompt(section: str, *, prompt_dir: Path | None = None) -> str:
    path = section_prompt_path(section, prompt_dir=prompt_dir)
    if not path.exists():
        raise FileNotFoundError(f"Prompt template not found for section {section!r}: {path}")
    return path.read_text(encoding="utf-8").strip()


def final_document_prompt_path(*, prompt_dir: Path | None = None) -> Path:
    prompt_root = prompt_dir or DEFAULT_PROMPT_DIR
    return prompt_root / FINAL_DOCUMENT_PROMPT_FILE


def load_final_document_prompt(*, prompt_dir: Path | None = None) -> str:
    path = final_document_prompt_path(prompt_dir=prompt_dir)
    if not path.exists():
        raise FileNotFoundError(f"Final document prompt template not found: {path}")
    return path.read_text(encoding="utf-8").strip()


def _claim_to_dict(claim: Any) -> dict[str, Any]:
    if isinstance(claim, Mapping):
        return dict(claim)
    if is_dataclass(claim):
        return asdict(claim)
    if hasattr(claim, "__dict__"):
        return dict(vars(claim))
    raise TypeError(f"Unsupported claim type: {type(claim)!r}")


def map_claims_by_section(
    claims: Mapping[str, Sequence[Any]] | Sequence[Any],
    *,
    strict: bool = True,
) -> dict[str, list[dict[str, Any]]]:
    mapped: dict[str, list[dict[str, Any]]] = {section: [] for section in SECTION_ORDER}

    if isinstance(claims, Mapping):
        items: list[tuple[str, Any]] = []
        for section_key, rows in claims.items():
            for row in rows:
                items.append((section_key, row))
    else:
        items = []
        for row in claims:
            data = _claim_to_dict(row)
            section_key = (
                data.get("narrative_section")
                or data.get("section")
                or data.get("section_name")
                or data.get("topic")
            )
            if section_key is None:
                if strict:
                    raise ValueError(f"Claim missing section key: {data}")
                continue
            items.append((str(section_key), row))

    for section_key, row in items:
        data = _claim_to_dict(row)
        try:
            canonical = normalize_section_name(str(section_key))
        except ValueError:
            if strict:
                raise
            continue
        data["section"] = canonical
        mapped[canonical].append(data)

    return mapped


def _format_generation_payload(section: str, claims: Sequence[Mapping[str, Any]]) -> str:
    payload = {
        "section_key": section,
        "section_title": SECTION_TITLES[section],
        "claims_json": [dict(claim) for claim in claims],
    }
    return (
        "Write the section from this payload. Use only claims_json.\n\n"
        f"{json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True)}"
    )


def _extract_response_text(response: Any) -> str:
    text = getattr(response, "output_text", None)
    if isinstance(text, str) and text.strip():
        return text.strip()

    if hasattr(response, "model_dump"):
        data = response.model_dump()
    elif isinstance(response, Mapping):
        data = dict(response)
    else:
        data = {}

    output = data.get("output", [])
    parts: list[str] = []
    for item in output:
        for content in item.get("content", []):
            if content.get("type") in {"output_text", "text"}:
                block = content.get("text")
                if isinstance(block, str) and block.strip():
                    parts.append(block.strip())
    if parts:
        return "\n\n".join(parts).strip()

    raise ValueError("Unable to extract text from OpenAI response.")


def _responses_create(
    client: Any,
    *,
    model: str,
    system_prompt: str,
    user_prompt: str,
    temperature: float = 0.0,
    max_output_tokens: int = 1800,
) -> str:
    response = client.responses.create(
        model=model,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
        input=[
            {"role": "system", "content": [{"type": "input_text", "text": system_prompt}]},
            {"role": "user", "content": [{"type": "input_text", "text": user_prompt}]},
        ],
    )
    return _extract_response_text(response)


def _ensure_heading(section: str, markdown: str) -> str:
    text = markdown.strip()
    heading = f"## {SECTION_TITLES[section]}"
    if not text.startswith("## "):
        return f"{heading}\n\n{text}".strip()
    return text


def generate_section_markdown(
    client: Any,
    *,
    section: str,
    claims: Sequence[Mapping[str, Any] | Any],
    model: str = DEFAULT_MODEL,
    prompt_dir: Path | None = None,
    temperature: float = 0.0,
    max_output_tokens: int = 1800,
    run_editor_pass: bool = False,
    editor_model: str | None = None,
) -> str:
    canonical_section = normalize_section_name(section)
    claim_rows = [_claim_to_dict(claim) for claim in claims]
    if not claim_rows:
        return f"## {SECTION_TITLES[canonical_section]}\n\n- No validated claims were provided for this section."

    prompt = load_section_prompt(canonical_section, prompt_dir=prompt_dir)
    payload = _format_generation_payload(canonical_section, claim_rows)
    draft = _responses_create(
        client,
        model=model,
        system_prompt=prompt,
        user_prompt=payload,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
    )
    draft = _ensure_heading(canonical_section, draft)

    if run_editor_pass:
        draft = edit_section_markdown(
            client,
            section=canonical_section,
            draft_markdown=draft,
            claims=claim_rows,
            model=editor_model or model,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
        )
    return draft


def edit_section_markdown(
    client: Any,
    *,
    section: str,
    draft_markdown: str,
    claims: Sequence[Mapping[str, Any] | Any],
    model: str = DEFAULT_MODEL,
    temperature: float = 0.0,
    max_output_tokens: int = 1800,
) -> str:
    canonical_section = normalize_section_name(section)
    claim_rows = [_claim_to_dict(claim) for claim in claims]
    user_prompt = json.dumps(
        {
            "section_key": canonical_section,
            "section_title": SECTION_TITLES[canonical_section],
            "claims_json": claim_rows,
            "draft_markdown": draft_markdown,
        },
        ensure_ascii=True,
        indent=2,
        sort_keys=True,
    )
    edited = _responses_create(
        client,
        model=model,
        system_prompt=_EDITOR_PROMPT,
        user_prompt=user_prompt,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
    )
    return _ensure_heading(canonical_section, edited)


def generate_narrative_sections(
    client: Any,
    claims: Mapping[str, Sequence[Any]] | Sequence[Any],
    *,
    sections: Sequence[str] = SECTION_ORDER,
    model: str = DEFAULT_MODEL,
    prompt_dir: Path | None = None,
    temperature: float = 0.0,
    max_output_tokens: int = 1800,
    run_editor_pass: bool = False,
    editor_model: str | None = None,
    strict_mapping: bool = True,
) -> dict[str, str]:
    claims_by_section = map_claims_by_section(claims, strict=strict_mapping)
    rendered: dict[str, str] = {}
    for section in sections:
        canonical = normalize_section_name(section)
        rendered[canonical] = generate_section_markdown(
            client,
            section=canonical,
            claims=claims_by_section.get(canonical, []),
            model=model,
            prompt_dir=prompt_dir,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            run_editor_pass=run_editor_pass,
            editor_model=editor_model,
        )
    return rendered


def render_report_markdown(
    section_markdown: Mapping[str, str],
    *,
    section_order: Sequence[str] = SECTION_ORDER,
) -> str:
    blocks: list[str] = []
    for section in section_order:
        canonical = normalize_section_name(section)
        block = section_markdown.get(canonical, "").strip()
        if block:
            blocks.append(block)
    return "\n\n".join(blocks).strip() + "\n"


def synthesize_final_document(
    client: Any,
    *,
    draft_markdown: str,
    claims: Sequence[Mapping[str, Any] | Any],
    event_name: str | None = None,
    model: str = DEFAULT_MODEL,
    prompt_dir: Path | None = None,
    temperature: float = 0.0,
    max_output_tokens: int = 2200,
    max_words: int = 1400,
) -> str:
    claim_rows = [_claim_to_dict(claim) for claim in claims]
    system_prompt = load_final_document_prompt(prompt_dir=prompt_dir)
    user_prompt = json.dumps(
        {
            "event_name": event_name or "",
            "max_words": max_words,
            "claims_json": claim_rows,
            "draft_markdown": draft_markdown,
        },
        ensure_ascii=True,
        indent=2,
        sort_keys=True,
    )
    text = _responses_create(
        client,
        model=model,
        system_prompt=system_prompt,
        user_prompt=user_prompt,
        temperature=temperature,
        max_output_tokens=max_output_tokens,
    )
    return text.strip() + "\n"


__all__ = [
    "DEFAULT_MODEL",
    "DEFAULT_PROMPT_DIR",
    "SECTION_ORDER",
    "SECTION_TITLES",
    "edit_section_markdown",
    "final_document_prompt_path",
    "generate_narrative_sections",
    "generate_section_markdown",
    "load_final_document_prompt",
    "load_section_prompt",
    "map_claims_by_section",
    "normalize_section_name",
    "render_report_markdown",
    "section_prompt_path",
    "synthesize_final_document",
]
