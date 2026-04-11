from __future__ import annotations

import hashlib
import json
import re
from dataclasses import replace
from collections.abc import Mapping, Sequence

from .models import (
    Applicant,
    AnswerEntry,
    CertificationEntry,
    EducationEntry,
    EventEntry,
    ProjectEntry,
    PublicationEntry,
    ValidationMessage,
    WorkEntry,
)

_HEADER_NORMALIZER = re.compile(r"[^a-z0-9]+")

_ALIASES: dict[str, tuple[str, ...]] = {
    "name": ("name", "full name", "full_name", "fullname"),
    "first_name": ("first name", "first_name", "firstname", "fname"),
    "last_name": ("last name", "last_name", "lastname", "lname", "surname"),
    "email": ("email", "e-mail", "email address", "email_address"),
    "linkedin_url": ("linkedin", "linkedin url", "linkedin_url", "linkedin link", "linkedin profile"),
    "github_url": ("github", "github url", "github_url", "github link", "github profile"),
    "x_handle": ("x", "x handle", "x_handle", "twitter", "twitter handle", "twitter_handle"),
    "company": ("company", "organisation", "organization", "org"),
    "role": ("role", "title", "job title", "position"),
    "location": ("location", "city", "country"),
    "self_description": ("self description", "self_description", "bio", "about", "description"),
    "ai_project": ("ai project", "ai_project", "project", "built"),
    "looking_for_job": ("looking for job", "looking_for_job"),
    "years_experience": ("years experience", "years_experience", "experience"),
    "education_level": ("education level", "education_level", "education"),
    "employment_category": ("employment category", "employment_category"),
    "total_cv_events": ("total cv events", "total_cv_events"),
    "hackathon_submissions": ("hackathon submissions", "hackathon_submissions"),
    "publications": ("publications",),
    "certifications": ("certifications",),
    "notable_achievements": ("notable achievements", "notable_achievements", "achievements"),
    "work_history": ("work history", "work_history", "positions", "positions_with_companies"),
    "education_history": ("education history", "education_history", "education_with_schools"),
    "projects": ("projects", "linkedin projects"),
    "publications_detail": ("publications detail", "publications_detail"),
    "certifications_detail": ("certifications detail", "certifications_detail"),
    "event_history": ("event history", "event_history", "cv event history"),
    "application_answers": ("application answers", "application_answers", "event qa answers"),
}

_CANONICAL_FIELDS = tuple(_ALIASES)
_MISSING = object()


def canonicalize_header(value: str) -> str:
    return _HEADER_NORMALIZER.sub(" ", value.strip().lower()).strip()


def build_header_map(headers: Sequence[str]) -> dict[str, str]:
    normalized = {header: canonicalize_header(header) for header in headers}
    mapping: dict[str, str] = {}
    for canonical, aliases in _ALIASES.items():
        for header, normalized_header in normalized.items():
            if normalized_header in aliases:
                mapping[canonical] = header
                break
    return mapping


def _get(row: Mapping[str, str], header_map: Mapping[str, str], canonical: str) -> str:
    header = header_map.get(canonical)
    if header is None:
        return ""
    value = row.get(header, "")
    return value.strip() if isinstance(value, str) else str(value).strip()


def _clean_text(value: object) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value.strip()
    return str(value).strip()


def _parse_int(value: object) -> int | None:
    text = _clean_text(value)
    if not text:
        return None
    try:
        return int(float(text.replace(",", "")))
    except (TypeError, ValueError):
        return None


def _parse_float(value: object) -> float | None:
    text = _clean_text(value)
    if not text:
        return None
    try:
        return float(text.replace(",", ""))
    except (TypeError, ValueError):
        return None


def _parse_bool(value: object) -> bool | None:
    text = _clean_text(value).lower()
    if not text:
        return None
    if text in {"1", "true", "t", "yes", "y"}:
        return True
    if text in {"0", "false", "f", "no", "n"}:
        return False
    return None


def _split_items(value: object) -> tuple[str, ...]:
    text = _clean_text(value)
    if not text:
        return ()
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, list):
            items = tuple(_clean_text(item) for item in parsed)
            return tuple(item for item in items if item)
    parts = re.split(r"[;,|]\s*", text)
    items = tuple(_clean_text(part) for part in parts)
    return tuple(item for item in items if item)


def _parse_jsonish(value: object) -> object | None:
    text = _clean_text(value)
    if not text:
        return None
    if text.startswith("{") or text.startswith("["):
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return None
    return None


def _parse_work_entries(value: object) -> tuple[WorkEntry, ...]:
    parsed = _parse_jsonish(value)
    if isinstance(parsed, dict):
        parsed = [parsed]
    if isinstance(parsed, list):
        entries = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            entries.append(
                WorkEntry(
                    title=_clean_text(item.get("title") or item.get("role") or item.get("position") or item.get("name")),
                    company=_clean_text(item.get("company_name") or item.get("company") or item.get("organization")),
                    start=_clean_text(item.get("from_date") or item.get("start_date") or item.get("started_on")),
                    end=_clean_text(item.get("to_date") or item.get("end_date") or item.get("ended_on")),
                    current=bool(item.get("is_current") or item.get("current")),
                    description=_clean_text(item.get("description")),
                )
            )
        return tuple(entry for entry in entries if entry.title or entry.company)
    text = _clean_text(value)
    if not text:
        return ()
    return (WorkEntry(title=text, company=""),)


def _parse_education_entries(value: object) -> tuple[EducationEntry, ...]:
    parsed = _parse_jsonish(value)
    if isinstance(parsed, dict):
        parsed = [parsed]
    if isinstance(parsed, list):
        entries = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            entries.append(
                EducationEntry(
                    school=_clean_text(item.get("school_name") or item.get("school") or item.get("institution")),
                    degree=_clean_text(item.get("degree") or item.get("degree_name")),
                    field_of_study=_clean_text(item.get("field_of_study") or item.get("major")),
                    start=_clean_text(item.get("from_date") or item.get("start_date") or item.get("started_on")),
                    end=_clean_text(item.get("to_date") or item.get("end_date") or item.get("ended_on")),
                    level=_clean_text(item.get("education_level") or item.get("level")),
                )
            )
        return tuple(entry for entry in entries if entry.school or entry.degree or entry.field_of_study)
    text = _clean_text(value)
    if not text:
        return ()
    return (EducationEntry(school=text),)


def _parse_projects(value: object) -> tuple[ProjectEntry, ...]:
    parsed = _parse_jsonish(value)
    if isinstance(parsed, dict):
        parsed = [parsed]
    if isinstance(parsed, list):
        entries = []
        for item in parsed:
            if isinstance(item, dict):
                title = _clean_text(item.get("title") or item.get("name"))
                desc = _clean_text(item.get("description"))
                if title or desc:
                    entries.append(ProjectEntry(title=title or desc, description=desc if title else ""))
            else:
                text = _clean_text(item)
                if text:
                    entries.append(ProjectEntry(title=text))
        return tuple(entries)
    text = _clean_text(value)
    if not text:
        return ()
    return (ProjectEntry(title=text),)


def _parse_publications(value: object) -> tuple[PublicationEntry, ...]:
    parsed = _parse_jsonish(value)
    if isinstance(parsed, dict):
        parsed = [parsed]
    if isinstance(parsed, list):
        entries = []
        for item in parsed:
            if isinstance(item, dict):
                title = _clean_text(item.get("name") or item.get("title"))
                publisher = _clean_text(item.get("publisher") or item.get("venue"))
                if title or publisher:
                    entries.append(PublicationEntry(title=title or publisher, publisher=publisher if title else ""))
            else:
                text = _clean_text(item)
                if text:
                    entries.append(PublicationEntry(title=text))
        return tuple(entries)
    text = _clean_text(value)
    if not text:
        return ()
    return (PublicationEntry(title=text),)


def _parse_certifications(value: object) -> tuple[CertificationEntry, ...]:
    parsed = _parse_jsonish(value)
    if isinstance(parsed, dict):
        parsed = [parsed]
    if isinstance(parsed, list):
        entries = []
        for item in parsed:
            if isinstance(item, dict):
                name = _clean_text(item.get("name") or item.get("title"))
                authority = _clean_text(item.get("authority") or item.get("issuer"))
                if name or authority:
                    entries.append(CertificationEntry(name=name or authority, authority=authority if name else ""))
            else:
                text = _clean_text(item)
                if text:
                    entries.append(CertificationEntry(name=text))
        return tuple(entries)
    text = _clean_text(value)
    if not text:
        return ()
    return (CertificationEntry(name=text),)


def _parse_events(value: object) -> tuple[EventEntry, ...]:
    parsed = _parse_jsonish(value)
    if isinstance(parsed, dict):
        parsed = [parsed]
    if isinstance(parsed, list):
        entries = []
        for item in parsed:
            if isinstance(item, dict):
                name = _clean_text(item.get("event_name") or item.get("name") or item.get("event"))
                date = _clean_text(item.get("event_date") or item.get("date"))
                status = _clean_text(item.get("status") or item.get("placement"))
                if name or date or status:
                    entries.append(EventEntry(name=name or date or status, date=date, status=status))
            else:
                text = _clean_text(item)
                if text:
                    entries.append(EventEntry(name=text))
        return tuple(entries)
    text = _clean_text(value)
    if not text:
        return ()
    return (EventEntry(name=text),)


def _parse_answers(value: object) -> tuple[AnswerEntry, ...]:
    parsed = _parse_jsonish(value)
    if isinstance(parsed, dict):
        parsed = [parsed]
    if isinstance(parsed, list):
        entries = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            question = _clean_text(item.get("question") or item.get("q"))
            answer = _clean_text(item.get("answer") or item.get("a"))
            if question or answer:
                entries.append(AnswerEntry(question=question or "Question", answer=answer))
        return tuple(entries)
    text = _clean_text(value)
    if not text:
        return ()
    return (AnswerEntry(question="Answer", answer=text),)


def _normalize_url(value: object, *, kind: str) -> str:
    text = _clean_text(value)
    if not text:
        return ""
    if kind == "linkedin":
        lower = text.lower()
        if "linkedin.com/in/" in lower:
            return text if text.startswith("http") else f"https://{text}".rstrip("/")
        if "linkedin.com" in lower:
            return ""
        slug = text.lstrip("/").rstrip("/")
        return f"https://www.linkedin.com/in/{slug}" if slug else ""
    if kind == "github":
        lower = text.lower()
        if "github.com/" in lower:
            return text if text.startswith("http") else f"https://{text}".rstrip("/")
        if text.startswith("@"):
            text = text[1:]
        return f"https://github.com/{text}" if text else ""
    return text.rstrip("/")


def _stable_name(first_name: str, last_name: str, email: str, name: str) -> str:
    if name:
        return name
    combined = " ".join(part for part in (first_name, last_name) if part)
    if combined:
        return combined
    return email


def _build_fingerprint(applicant: Applicant) -> str:
    payload = {
        "email": applicant.email.casefold(),
        "github_url": applicant.github_url,
        "linkedin_url": applicant.linkedin_url,
        "location": applicant.location.casefold(),
        "name": applicant.name.casefold(),
        "role": applicant.role.casefold(),
        "company": applicant.company.casefold(),
        "source_name": applicant.source_name,
        "extra_fields": applicant.extra_fields,
        "source_row": applicant.source_row,
    }
    blob = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=True)
    return f"app_{hashlib.sha1(blob.encode('utf-8')).hexdigest()[:12]}"


def build_applicant_id(applicant: Applicant) -> str:
    if applicant.email:
        key = f"email:{applicant.email.casefold()}"
    elif applicant.linkedin_url:
        key = f"linkedin:{applicant.linkedin_url}"
    elif applicant.github_url:
        key = f"github:{applicant.github_url}"
    else:
        key = _build_fingerprint(applicant)
    digest = hashlib.sha1(key.encode("utf-8")).hexdigest()[:12]
    return f"app_{digest}"


def normalize_row(
    row: Mapping[str, str],
    header_map: Mapping[str, str],
    *,
    source_name: str = "csv",
    row_number: int | None = None,
) -> tuple[Applicant | None, tuple[ValidationMessage, ...]]:
    clean_row = {
        key: value.strip() if isinstance(value, str) else _clean_text(value)
        for key, value in row.items()
        if key and _clean_text(value)
    }

    name = _get(clean_row, header_map, "name")
    first_name = _get(clean_row, header_map, "first_name")
    last_name = _get(clean_row, header_map, "last_name")
    email = _get(clean_row, header_map, "email").casefold()

    messages: list[ValidationMessage] = []
    if not name and first_name and last_name:
        name = f"{first_name} {last_name}"
        messages.append(
            ValidationMessage(
                level="info",
                code="synthesized_name",
                message="Synthesized display name from first_name + last_name.",
                row_number=row_number,
                field="name",
            )
        )
    elif not name and email:
        name = email
        messages.append(
            ValidationMessage(
                level="warning",
                code="synthesized_name_from_email",
                message="Synthesized display name from email because no name columns were present.",
                row_number=row_number,
                field="name",
            )
        )

    if not name:
        messages.append(
            ValidationMessage(
                level="error",
                code="missing_identity",
                message="Row is missing a usable name, first_name/last_name, and email.",
                row_number=row_number,
            )
        )
        return None, tuple(messages)

    extra_keys = set(header_map.values())
    applicant = Applicant(
        applicant_id="",
        name=name,
        first_name=first_name or (name.split(None, 1)[0] if " " in name else name),
        last_name=last_name or (name.split(None, 1)[1] if " " in name else ""),
        email=email,
        company=_get(clean_row, header_map, "company"),
        role=_get(clean_row, header_map, "role"),
        location=_get(clean_row, header_map, "location"),
        linkedin_url=_normalize_url(_get(clean_row, header_map, "linkedin_url"), kind="linkedin"),
        github_url=_normalize_url(_get(clean_row, header_map, "github_url"), kind="github"),
        x_handle=_clean_text(_get(clean_row, header_map, "x_handle")).lstrip("@"),
        self_description=_get(clean_row, header_map, "self_description"),
        ai_project=_get(clean_row, header_map, "ai_project"),
        looking_for_job=_get(clean_row, header_map, "looking_for_job"),
        years_experience=_parse_float(_get(clean_row, header_map, "years_experience")),
        education_level=_get(clean_row, header_map, "education_level"),
        employment_category=_get(clean_row, header_map, "employment_category"),
        total_cv_events=_parse_int(_get(clean_row, header_map, "total_cv_events")),
        hackathon_submissions=_parse_int(_get(clean_row, header_map, "hackathon_submissions")),
        publications_count=_parse_int(_get(clean_row, header_map, "publications")),
        certifications_count=_parse_int(_get(clean_row, header_map, "certifications")),
        notable_achievements=_split_items(_get(clean_row, header_map, "notable_achievements")),
        work_history=_parse_work_entries(_get(clean_row, header_map, "work_history")),
        education_history=_parse_education_entries(_get(clean_row, header_map, "education_history")),
        projects=_parse_projects(_get(clean_row, header_map, "projects")),
        publications=_parse_publications(_get(clean_row, header_map, "publications_detail")),
        certifications=_parse_certifications(_get(clean_row, header_map, "certifications_detail")),
        event_history=_parse_events(_get(clean_row, header_map, "event_history")),
        application_answers=_parse_answers(_get(clean_row, header_map, "application_answers")),
        extra_fields=tuple(
            sorted(
                (key, value)
                for key, value in clean_row.items()
                if key not in extra_keys and value
            )
        ),
        source_name=source_name,
        source_row=tuple(sorted(clean_row.items())),
    )
    applicant = replace(applicant, applicant_id=build_applicant_id(applicant))
    return applicant, tuple(messages)
