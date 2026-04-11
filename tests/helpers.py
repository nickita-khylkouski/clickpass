"""Shared test helpers for cv-rank."""

from __future__ import annotations

from typing import Any


def make_person(name: str, *, full: bool = False, **kwargs: Any) -> dict[str, Any]:
    """Create a person dict for testing.

    Args:
        name: Person name (required).
        full: If True, populate all standard CSV fields with empty defaults.
              Needed by tests that exercise code touching many person keys.
        **kwargs: Override any field.
    """
    person: dict[str, Any] = {"name": name}

    if "email" not in kwargs:
        slug = name.lower().replace(" ", "")
        person["email"] = f"{slug}@test.com"

    if full:
        defaults = {
            "first_name": name.split()[0] if name else "",
            "last_name": name.split()[-1] if name else "",
            "linkedin_url": "", "github_url": "", "company": "", "role": "",
            "location": "", "x_handle": "", "self_description": "", "ai_project": "",
            "years_experience": "", "is_founder": "", "is_big_tech": "", "is_student": "",
            "top_school": "", "education_level": "", "employment_category": "",
            "looking_for_job": "", "linkedin_headline": "", "linkedin_followers": "",
            "linkedin_connections": "", "github_bio": "", "github_stars": "",
            "github_followers": "", "github_repos": "", "github_commits_year": "",
            "publications": "", "certifications": "", "notable_achievements": "",
            "hackathon_submissions": "", "total_cv_events": "", "_raw_csv": {},
        }
        for k, v in defaults.items():
            if k not in kwargs:
                person[k] = v

    person.update(kwargs)
    return person
