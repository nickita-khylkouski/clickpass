from __future__ import annotations

import json
import os
import random
from collections.abc import Mapping
from pathlib import Path
from typing import TypeVar

from dotenv import load_dotenv

from cv_rank.enrichment.platform_db import load_from_platform_db
from cv_rank.enrichment.supabase import load_from_supabase

from .models import IngestResult, ValidationEnvelope, ValidationMessage
from .normalize import build_header_map, normalize_row


T = TypeVar("T")


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _canonicalize_person(person: Mapping[str, object], source_name: str) -> dict[str, object]:
    answers = person.get("_raw_csv")
    application_answers = ""
    if isinstance(answers, Mapping):
        payload = [
            {"question": str(question), "answer": str(answer).strip()}
            for question, answer in answers.items()
            if str(answer).strip()
        ]
        if payload:
            application_answers = json.dumps(payload, ensure_ascii=True)

    return {
        "name": person.get("name", ""),
        "first_name": person.get("first_name", ""),
        "last_name": person.get("last_name", ""),
        "email": person.get("email", ""),
        "linkedin_url": person.get("linkedin_url", ""),
        "github_url": person.get("github_url", ""),
        "x_handle": person.get("x_handle", ""),
        "self_description": person.get("self_description", ""),
        "ai_project": person.get("ai_project", ""),
        "looking_for_job": person.get("looking_for_job", ""),
        "company": person.get("company", ""),
        "role": person.get("role", ""),
        "location": person.get("location", ""),
        "application_answers": application_answers,
        "_source": source_name,
    }


def _apply_sample(items: list[T], sample_size: int | None, sample_seed: int) -> tuple[list[T], ValidationMessage | None]:
    if sample_size is None or sample_size <= 0 or sample_size >= len(items):
        return items, None
    rng = random.Random(sample_seed)
    selected_indices = sorted(rng.sample(range(len(items)), sample_size))
    sampled = [items[index] for index in selected_indices]
    return (
        sampled,
        ValidationMessage(
            level="info",
            code="sampled_event_applicants",
            message=f"Selected {sample_size} applicants deterministically from {len(items)} event applicants (seed={sample_seed}).",
        ),
    )


def load_event_applicants(
    event_name: str,
    *,
    platform_dsn: str = "",
    supabase_url: str = "",
    supabase_key: str = "",
    sample_size: int | None = None,
    sample_seed: int = 0,
) -> IngestResult:
    load_dotenv(_repo_root() / ".env", override=False)

    resolved_platform_dsn = platform_dsn or os.environ.get("PLATFORM_DATABASE_URL", "")
    resolved_supabase_url = supabase_url or os.environ.get("SUPABASE_URL", "")
    resolved_supabase_key = supabase_key or os.environ.get("SUPABASE_KEY", "")

    raw_people: list[dict[str, object]] = []
    source_label = ""
    messages: list[ValidationMessage] = []

    if resolved_platform_dsn:
        raw_people = list(load_from_platform_db(event_name, resolved_platform_dsn))
        if raw_people:
            source_label = "platform_db"
            messages.append(
                ValidationMessage(
                    level="info",
                    code="platform_db_loaded",
                    message=f"Loaded {len(raw_people)} applicants for {event_name!r} from Platform DB.",
                )
            )

    if not raw_people and resolved_supabase_url and resolved_supabase_key:
        raw_people = list(load_from_supabase(event_name, resolved_supabase_url, resolved_supabase_key))
        if raw_people:
            source_label = "supabase"
            messages.append(
                ValidationMessage(
                    level="info",
                    code="supabase_loaded",
                    message=f"Loaded {len(raw_people)} applicants for {event_name!r} from Supabase.",
                )
            )

    if not raw_people:
        if not resolved_platform_dsn and not (resolved_supabase_url and resolved_supabase_key):
            messages.append(
                ValidationMessage(
                    level="error",
                    code="missing_event_credentials",
                    message="Event loading requires PLATFORM_DATABASE_URL or SUPABASE_URL/SUPABASE_KEY.",
                )
            )
        else:
            messages.append(
                ValidationMessage(
                    level="error",
                    code="event_not_found",
                    message=f"No applicants found for event {event_name!r}.",
                )
            )
        validation = ValidationEnvelope(
            source=f"event:{event_name}",
            row_count=0,
            accepted_count=0,
            skipped_count=0,
            messages=tuple(messages),
        )
        return IngestResult(applicants=(), validation=validation)

    canonical_rows = [_canonicalize_person(person, source_label) for person in raw_people]
    canonical_rows, sample_message = _apply_sample(canonical_rows, sample_size, sample_seed)
    if sample_message is not None:
        messages.append(sample_message)

    headers = sorted({key for row in canonical_rows for key in row})
    header_map = build_header_map(headers)
    applicants = []
    seen_ids: set[str] = set()
    skipped = 0

    for row_number, row in enumerate(canonical_rows, start=1):
        applicant, row_messages = normalize_row(
            row,
            header_map,
            source_name=f"{source_label}:{event_name}",
            row_number=row_number,
        )
        messages.extend(row_messages)
        if applicant is None:
            skipped += 1
            continue
        if applicant.applicant_id in seen_ids:
            skipped += 1
            messages.append(
                ValidationMessage(
                    level="warning",
                    code="duplicate_applicant",
                    message="Duplicate applicant_id encountered; later event row skipped.",
                    row_number=row_number,
                )
            )
            continue
        seen_ids.add(applicant.applicant_id)
        applicants.append(applicant)

    validation = ValidationEnvelope(
        source=f"{source_label}:{event_name}" if source_label else f"event:{event_name}",
        row_count=len(canonical_rows),
        accepted_count=len(applicants),
        skipped_count=skipped,
        messages=tuple(messages),
    )
    return IngestResult(applicants=tuple(applicants), validation=validation)
