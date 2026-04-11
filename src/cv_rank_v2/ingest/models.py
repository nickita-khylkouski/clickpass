from __future__ import annotations

from dataclasses import dataclass, field
from typing import Literal


ValidationLevel = Literal["error", "warning", "info"]


@dataclass(frozen=True, slots=True)
class ValidationMessage:
    level: ValidationLevel
    code: str
    message: str
    row_number: int | None = None
    field: str | None = None


@dataclass(frozen=True, slots=True)
class ValidationEnvelope:
    source: str
    row_count: int
    accepted_count: int
    skipped_count: int
    messages: tuple[ValidationMessage, ...] = ()

    @property
    def valid(self) -> bool:
        return not any(message.level == "error" for message in self.messages)


@dataclass(frozen=True, slots=True)
class WorkEntry:
    title: str
    company: str
    start: str = ""
    end: str = ""
    current: bool = False
    description: str = ""


@dataclass(frozen=True, slots=True)
class EducationEntry:
    school: str
    degree: str = ""
    field_of_study: str = ""
    start: str = ""
    end: str = ""
    level: str = ""


@dataclass(frozen=True, slots=True)
class ProjectEntry:
    title: str
    description: str = ""


@dataclass(frozen=True, slots=True)
class PublicationEntry:
    title: str
    publisher: str = ""


@dataclass(frozen=True, slots=True)
class CertificationEntry:
    name: str
    authority: str = ""


@dataclass(frozen=True, slots=True)
class EventEntry:
    name: str
    date: str = ""
    status: str = ""


@dataclass(frozen=True, slots=True)
class AnswerEntry:
    question: str
    answer: str


@dataclass(frozen=True, slots=True)
class Applicant:
    applicant_id: str
    name: str
    first_name: str = ""
    last_name: str = ""
    email: str = ""
    company: str = ""
    role: str = ""
    location: str = ""
    linkedin_url: str = ""
    github_url: str = ""
    x_handle: str = ""
    self_description: str = ""
    ai_project: str = ""
    looking_for_job: str = ""
    years_experience: float | None = None
    education_level: str = ""
    employment_category: str = ""
    total_cv_events: int | None = None
    hackathon_submissions: int | None = None
    publications_count: int | None = None
    certifications_count: int | None = None
    notable_achievements: tuple[str, ...] = ()
    work_history: tuple[WorkEntry, ...] = ()
    education_history: tuple[EducationEntry, ...] = ()
    projects: tuple[ProjectEntry, ...] = ()
    publications: tuple[PublicationEntry, ...] = ()
    certifications: tuple[CertificationEntry, ...] = ()
    event_history: tuple[EventEntry, ...] = ()
    application_answers: tuple[AnswerEntry, ...] = ()
    extra_fields: tuple[tuple[str, str], ...] = ()
    source_name: str = "csv"
    source_row: tuple[tuple[str, str], ...] = ()

    def extra_field_map(self) -> dict[str, str]:
        return dict(self.extra_fields)


@dataclass(frozen=True, slots=True)
class IngestResult:
    applicants: tuple[Applicant, ...] = ()
    validation: ValidationEnvelope = field(
        default_factory=lambda: ValidationEnvelope(
            source="csv",
            row_count=0,
            accepted_count=0,
            skipped_count=0,
        )
    )

