from __future__ import annotations

from collections.abc import Iterable

from cv_rank_v2.ingest.models import Applicant


def _section(title: str, lines: Iterable[str]) -> str | None:
    body = [line for line in lines if line]
    return f"{title}\n" + "\n".join(body) if body else None


def _format_boolish(value: str) -> str:
    text = value.strip().lower()
    if not text:
        return ""
    if text in {"1", "true", "yes", "y"}:
        return "Yes"
    if text in {"0", "false", "no", "n"}:
        return "No"
    return value


def basic_section(applicant: Applicant) -> str:
    lines = [
        f"Applicant ID: {applicant.applicant_id}",
        f"Name: {applicant.name}",
    ]
    if applicant.company:
        lines.append(f"Company: {applicant.company}")
    if applicant.role:
        lines.append(f"Role: {applicant.role}")
    if applicant.location:
        lines.append(f"Location: {applicant.location}")
    if applicant.years_experience is not None:
        lines.append(f"Years Experience: {applicant.years_experience:g}")
    if applicant.education_level:
        lines.append(f"Education Level: {applicant.education_level}")
    if applicant.employment_category:
        lines.append(f"Employment Category: {applicant.employment_category}")
    if applicant.looking_for_job:
        lines.append(f"Looking For Job: {_format_boolish(applicant.looking_for_job)}")
    return "BASIC INFO\n" + "\n".join(lines)


def contact_section(applicant: Applicant) -> str | None:
    lines = []
    if applicant.email:
        lines.append(f"Email: {applicant.email}")
    if applicant.linkedin_url:
        lines.append(f"LinkedIn: {applicant.linkedin_url}")
    if applicant.github_url:
        lines.append(f"GitHub: {applicant.github_url}")
    if applicant.x_handle:
        lines.append(f"X: @{applicant.x_handle.lstrip('@')}")
    return _section("CONTACT", lines)


def application_section(applicant: Applicant) -> str | None:
    lines = []
    if applicant.self_description:
        lines.append(f"Self Description: {applicant.self_description}")
    if applicant.ai_project:
        lines.append(f"AI Project: {applicant.ai_project}")
    if applicant.notable_achievements:
        lines.append("Notable Achievements: " + "; ".join(applicant.notable_achievements))
    if applicant.publications_count is not None:
        lines.append(f"Publications Count: {applicant.publications_count}")
    if applicant.certifications_count is not None:
        lines.append(f"Certifications Count: {applicant.certifications_count}")
    if applicant.total_cv_events is not None:
        lines.append(f"Total CV Events: {applicant.total_cv_events}")
    if applicant.hackathon_submissions is not None:
        lines.append(f"Hackathon Submissions: {applicant.hackathon_submissions}")
    return _section("APPLICATION", lines)


def evidence_section(applicant: Applicant) -> str | None:
    lines = []
    if applicant.work_history:
        lines.append(f"Work History ({len(applicant.work_history)}):")
        for item in applicant.work_history[:5]:
            line = f"  - {item.title} @ {item.company}"
            if item.start or item.end:
                line += f" ({item.start or '?' } - {item.end or 'Present'})"
            if item.current:
                line += " [current]"
            if item.description:
                line += f": {item.description}"
            lines.append(line)
    if applicant.education_history:
        lines.append(f"Education ({len(applicant.education_history)}):")
        for item in applicant.education_history[:4]:
            line = f"  - {item.degree} {item.field_of_study} @ {item.school}".strip()
            if item.start or item.end:
                line += f" ({item.start or '?'} - {item.end or '?'})"
            if item.level:
                line += f" [{item.level}]"
            lines.append(line)
    if applicant.projects:
        lines.append(f"Projects ({len(applicant.projects)}):")
        for item in applicant.projects[:4]:
            line = f"  - {item.title}"
            if item.description:
                line += f": {item.description}"
            lines.append(line)
    if applicant.publications:
        lines.append(f"Publications ({len(applicant.publications)}):")
        for item in applicant.publications[:3]:
            line = f"  - {item.title}"
            if item.publisher:
                line += f" ({item.publisher})"
            lines.append(line)
    if applicant.certifications:
        lines.append(f"Certifications ({len(applicant.certifications)}):")
        for item in applicant.certifications[:3]:
            line = f"  - {item.name}"
            if item.authority:
                line += f" ({item.authority})"
            lines.append(line)
    return _section("EVIDENCE", lines)


def history_section(applicant: Applicant) -> str | None:
    lines = []
    if applicant.event_history:
        lines.append(f"Event History ({len(applicant.event_history)}):")
        for item in applicant.event_history[:5]:
            line = f"  - {item.name}"
            if item.date:
                line += f" ({item.date})"
            if item.status:
                line += f" [{item.status}]"
            lines.append(line)
    if applicant.extra_fields:
        lines.append("Extra Fields:")
        for key, value in applicant.extra_fields[:10]:
            lines.append(f"  - {key}: {value}")
    return _section("HISTORY", lines)

