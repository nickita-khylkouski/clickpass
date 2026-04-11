from __future__ import annotations

from cv_rank_v2.ingest.models import Applicant

from .sections import application_section, basic_section, contact_section, evidence_section, history_section


def format_profile(applicant: Applicant, *, include_extra_fields: bool = True) -> str:
    sections = [
        basic_section(applicant),
        contact_section(applicant),
        application_section(applicant),
        evidence_section(applicant),
        history_section(applicant) if include_extra_fields else None,
    ]
    return "\n\n".join(section for section in sections if section)

