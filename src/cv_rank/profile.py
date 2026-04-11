"""
Profile formatting for LLM ranking prompts.

Merges all fields from both pipeline_v2/rank.py::format_profile_for_ranking
and pipeline_v2/shared.py::format_profile into one canonical function.
Every section is conditional -- only rendered when the person dict has data.
"""

from __future__ import annotations

import json
from typing import Any


def _parse_skill(s: Any) -> str:
    """Parse a skill entry that may be a raw string or JSON-encoded object."""
    if isinstance(s, str) and s.startswith("{"):
        try:
            return json.loads(s).get("skill", s)
        except Exception:
            return s
    return str(s)


def _clean_date(d: Any) -> str:
    """Trim ISO timestamps to YYYY-MM."""
    d = str(d) if d else ""
    if "T" in d:
        return d[:7]
    return d


# ---------------------------------------------------------------------------
# Section builders — each returns str | None.
# Sections are rendered in order; None means "skip this section".
# ---------------------------------------------------------------------------


def _section_basic_info(p: dict, include_raw_csv: bool = False) -> str:
    """Always present: name, title, company, self-description, etc."""
    lines: list[str] = [f"Name: {p.get('name', '?')}"]
    role = p.get("title") or p.get("role")
    if role:
        lines.append(f"Title: {role}")
    if p.get("company"):
        lines.append(f"Company: {p['company']}")
    if p.get("current_job"):
        lines.append(f"Current Role: {p['current_job']}")
    if p.get("looking_for_job"):
        lines.append(f"Looking for Job: {p['looking_for_job']}")
    if p.get("self_description"):
        lines.append(f"Self-Description: {p['self_description']}")
    if p.get("ai_project"):
        lines.append(f"AI Project: {p['ai_project']}")
    if p.get("education") and isinstance(p["education"], str):
        lines.append(f"Education (self-reported): {p['education']}")

    if include_raw_csv:
        raw = p.get("_raw_csv", {})
        # Skip fields already rendered canonically plus noisy workflow/status fields.
        shown = {
            "name",
            "first_name",
            "last_name",
            "email",
            "company",
            "title",
            "role",
            "linkedin",
            "linkedin_url",
            "github",
            "github_url",
            "x",
            "x_handle",
            "twitter",
            "twitter_url",
            "looking_for_job",
            "self_description",
            "ai_project",
            "applied_at",
            "cv_recommended",
            "partner_approved",
        }
        for k, v in raw.items():
            if k.lower().strip() not in shown and v and len(str(v)) > 2:
                lines.append(f"{k}: {str(v)[:200]}")

    return "BASIC INFO\n" + "\n".join(lines)


def _section_linkedin(p: dict) -> str | None:
    lines: list[str] = []
    if p.get("linkedin_url"):
        lines.append(f"LinkedIn: {p['linkedin_url']}")
    else:
        lines.append("LinkedIn: NOT PROVIDED")
    if p.get("linkedin_headline"):
        lines.append(f"Headline: {p['linkedin_headline']}")
    if p.get("linkedin_bio"):
        lines.append(f"Bio: {p['linkedin_bio'][:300]}")

    if p.get("linkedin_skills"):
        skills = p["linkedin_skills"]
        if isinstance(skills, list):
            skill_strs = [_parse_skill(s) for s in skills[:10]]
            lines.append(f"Skills: {', '.join(skill_strs)}")
        else:
            lines.append(f"Skills: {skills}")

    if p.get("li_follower_count"):
        try:
            lines.append(f"Followers: {int(p['li_follower_count']):,}")
        except (ValueError, TypeError):
            lines.append(f"Followers: {p['li_follower_count']}")
    if p.get("li_connection_count"):
        try:
            lines.append(f"Connections: {int(p['li_connection_count']):,}")
        except (ValueError, TypeError):
            lines.append(f"Connections: {p['li_connection_count']}")
    if p.get("li_is_creator"):
        lines.append("LinkedIn Creator: Yes")
    if p.get("li_is_premium"):
        lines.append("LinkedIn Premium: Yes")
    if p.get("li_country"):
        lines.append(f"Country: {p['li_country']}")
    if p.get("li_awards"):
        lines.append(f"Awards: {json.dumps(p['li_awards'][:3])}")
    if p.get("li_patents"):
        lines.append(f"Patents: {json.dumps(p['li_patents'][:3])}")
    return ("LINKEDIN\n" + "\n".join(lines)) if lines else None


def _section_linkedin_analytics(p: dict) -> str | None:
    lines: list[str] = []
    for field, label in [
        ("is_founder", "Founder"),
        ("is_decision_maker", "Decision Maker"),
        ("is_in_big_tech", "Big Tech"),
        ("is_student", "Student"),
    ]:
        v = p.get(field)
        if v is True:
            lines.append(f"{label}: Yes")
        elif v is False:
            lines.append(f"{label}: No")
    if p.get("years_experience"):
        try:
            lines.append(f"Years Experience: {float(p['years_experience']):.1f}")
        except (ValueError, TypeError):
            lines.append(f"Years Experience: {p['years_experience']}")
    if p.get("education_level"):
        lines.append(f"Education Level: {p['education_level']}")
    if p.get("top_school") is True:
        lines.append("Top School: Yes")
    if p.get("technical_school") is True:
        lines.append("Technical School: Yes")
    if p.get("employment_category"):
        lines.append(f"Employment Category: {p['employment_category']}")
    if p.get("notable_skills_linkedin") and isinstance(
        p["notable_skills_linkedin"], list
    ):
        lines.append(
            f"Notable Skills: {'; '.join(str(s)[:80] for s in p['notable_skills_linkedin'][:5])}"
        )
    if p.get("notable_achievements") and isinstance(
        p["notable_achievements"], list
    ):
        lines.append(
            f"Achievements: {'; '.join(str(a)[:100] for a in p['notable_achievements'][:5])}"
        )
    return ("LINKEDIN ANALYTICS\n" + "\n".join(lines)) if lines else None


def _section_github(p: dict) -> str | None:
    lines: list[str] = []
    if p.get("github_url"):
        lines.append(f"GitHub: {p['github_url']}")
    else:
        lines.append("GitHub: NOT PROVIDED")
    if p.get("github_bio"):
        lines.append(f"Bio: {p['github_bio']}")
    for field, label in [
        ("gh_api_stars", "Total Stars"),
        ("gh_api_followers", "Followers"),
        ("gh_api_repos", "Public Repos"),
        ("gh_api_commits_year", "Commits (last year)"),
        ("gh_api_prs_year", "PRs (last year)"),
        ("gh_api_issues_year", "Issues (last year)"),
        ("gh_api_private_contributions", "Private Contributions (last year)"),
    ]:
        val = p.get(field)
        if val is not None:
            lines.append(
                f"{label}: {val:,}" if isinstance(val, int) else f"{label}: {val}"
            )
    if p.get("gh_account_created"):
        # Show account age in years
        created = p["gh_account_created"][:10]  # "2015-03-21T..."
        lines.append(f"Account Created: {created}")

    if p.get("github_total_score"):
        lines.append(f"DB Quality Score: {p['github_total_score']}/5")
    if p.get("github_implementation_score"):
        lines.append(f"Implementation Score: {p['github_implementation_score']}/5")
    if p.get("github_difficulty_score"):
        lines.append(f"Difficulty Score: {p['github_difficulty_score']}/5")
    if p.get("github_language_control_score"):
        lines.append(f"Language Control Score: {p['github_language_control_score']}/5")

    if p.get("github_best_languages") and isinstance(
        p["github_best_languages"], list
    ):
        lines.append(f"Best Languages: {', '.join(p['github_best_languages'])}")
    if p.get("github_notable_skills") and isinstance(
        p["github_notable_skills"], list
    ):
        lines.append(
            f"GitHub Skills: {'; '.join(str(s)[:60] for s in p['github_notable_skills'][:3])}"
        )

    if p.get("github_oss_repos_contributed"):
        lines.append(
            f"OSS Repos Contributed: {p['github_oss_repos_contributed']}"
        )
    if p.get("github_avg_commits_per_day"):
        lines.append(f"Avg Commits/Day: {p['github_avg_commits_per_day']:.1f}")

    if p.get("github_external_contributions"):
        contribs = p["github_external_contributions"]
        if isinstance(contribs, list) and contribs:
            lines.append(
                f"External OSS Contributions: {', '.join(str(c)[:50] for c in contribs[:3])}"
            )

    if p.get("github_code_by_language"):
        langs = p["github_code_by_language"]
        if isinstance(langs, dict):
            top_langs = sorted(langs.items(), key=lambda x: x[1], reverse=True)[
                :5
            ]
            lines.append(
                f"Code Volume: {', '.join(f'{lang}: {vol:,} bytes' for lang, vol in top_langs)}"
            )

    return ("GITHUB\n" + "\n".join(lines)) if lines else None


def _section_repositories(p: dict) -> str | None:
    repos = p.get("top_repos_evaluated") or p.get("gh_api_top_repos") or []
    if not repos:
        return None
    lines = [f"Top Repositories ({len(repos)}):"]
    for r in repos[:5]:
        if not isinstance(r, dict):
            continue
        name = r.get("name", "?")
        stars = r.get("stars") or r.get("stargazerCount", "?")
        desc = (r.get("description") or "")[:100]
        line = f"  - {name} ({stars} stars): {desc}"

        ratings: list[str] = []
        if r.get("implementation_rating"):
            ratings.append(f"impl={r['implementation_rating']}")
        if r.get("difficulty_rating"):
            ratings.append(f"diff={r['difficulty_rating']}")
        if r.get("notable_skills"):
            ratings.append(f"notable_skills: {r['notable_skills']}")
        if ratings:
            line += f" [{', '.join(ratings)}]"

        repo_skills = r.get("skills") or []
        if repo_skills:
            line += (
                f"\n    Skills: {', '.join(str(s)[:40] for s in repo_skills[:3])}"
            )

        lines.append(line)
    return "REPOSITORIES\n" + "\n".join(lines)


def _section_work_history(p: dict) -> str | None:
    positions = (
        p.get("positions_with_companies") or p.get("job_history") or []
    )
    if not positions:
        return None
    lines = [f"Work History ({len(positions)} positions):"]
    for pos in positions[:5]:
        if not isinstance(pos, dict):
            continue
        title = pos.get("title") or "?"
        company = (
            pos.get("company_name") or pos.get("company") or "?"
        )
        started = _clean_date(
            pos.get("from_date")
            or pos.get("started_on")
            or pos.get("start_date")
            or ""
        )
        ended = _clean_date(
            pos.get("to_date")
            or pos.get("ended_on")
            or pos.get("end_date")
            or ""
        ) or "Present"
        desc = (pos.get("description") or "")[:100]
        line = f"  - {title} @ {company}"
        if started:
            line += f" ({started} - {ended})"
        if pos.get("is_current"):
            line += " [current]"
        if desc:
            line += f": {desc}"
        lines.append(line)
    return "WORK HISTORY\n" + "\n".join(lines)


def _section_education(p: dict) -> str | None:
    edu = (
        p.get("education_with_schools")
        or p.get("detailed_education")
        or []
    )
    if not edu or not isinstance(edu, list):
        return None
    lines = [f"Education ({len(edu)} entries):"]
    for e in edu[:4]:
        if not isinstance(e, dict):
            continue
        school = e.get("school_name") or e.get("school") or "?"
        degree = e.get("degree") or e.get("degree_name") or ""
        field_of_study = e.get("field_of_study") or ""
        started = _clean_date(e.get("from_date") or e.get("started_on") or "")
        ended = _clean_date(e.get("to_date") or e.get("ended_on") or "")
        line = f"  - {degree} {field_of_study} @ {school}".strip()
        if started or ended:
            line += f" ({started} - {ended or '?'})"
        if e.get("education_level"):
            line += f" [{e['education_level']}]"
        lines.append(line)
    return "EDUCATION\n" + "\n".join(lines)


def _section_projects(p: dict) -> str | None:
    """LinkedIn projects — high-value self-written content."""
    projects = p.get("linkedin_projects") or []
    if not projects:
        return None
    lines = [f"Projects ({len(projects)}):"]
    for proj in projects[:4]:
        if not isinstance(proj, dict):
            continue
        title = proj.get("title", "?")
        desc = (proj.get("description", "") or "")[:150]
        line = f"  - {title}"
        if desc:
            line += f": {desc}"
        lines.append(line)
    return "PROJECTS\n" + "\n".join(lines)


def _section_certifications(p: dict) -> str | None:
    certs = p.get("certifications") or []
    if not certs:
        return None
    lines = [f"Certifications ({len(certs)}):"]
    for c in certs[:4]:
        if isinstance(c, dict):
            name = c.get("name", "?")[:100]
            auth = c.get("authority", "")
            line = f"  - {name}"
            if auth:
                line += f" ({auth})"
            lines.append(line)
        else:
            lines.append(f"  - {str(c)[:100]}")
    return "CERTIFICATIONS\n" + "\n".join(lines)


def _section_publications(p: dict) -> str | None:
    pubs = p.get("publications_detail") or p.get("publications") or []
    if not pubs:
        return None
    lines = [f"Publications ({len(pubs)}):"]
    for pub in pubs[:3]:
        if isinstance(pub, dict):
            line = f"  - {(pub.get('name') or '?')[:120]}"
            if pub.get("publisher"):
                line += f" ({pub['publisher']})"
            lines.append(line)
        else:
            lines.append(f"  - {str(pub)[:120]}")
    return "PUBLICATIONS\n" + "\n".join(lines)


def _section_volunteer(p: dict) -> str | None:
    vol = p.get("volunteer_experience") or []
    if not vol:
        return None
    lines = [f"Volunteer ({len(vol)}):"]
    for v in vol[:2]:
        if isinstance(v, dict):
            lines.append(
                f"  - {v.get('role', '?')} @ {v.get('organization', '?')}"
            )
    return "VOLUNTEER\n" + "\n".join(lines)


def _section_x_twitter(p: dict) -> str | None:
    """X/Twitter profile data.

    Fields come from two sources: supabase.py queries the `x` table
    (x_handle, x_name, x_bio, x_follower_count) and github_api.py
    fetches twitter_username from the GitHub REST API.
    """
    lines: list[str] = []
    if p.get("x_handle"):
        handle = str(p["x_handle"]).strip()
        if handle.startswith("http://") or handle.startswith("https://"):
            lines.append(f"X/Twitter: {handle}")
        else:
            lines.append(f"X/Twitter: @{handle.lstrip('@')}")
    if p.get("x_name"):
        lines.append(f"Display Name: {p['x_name']}")
    if p.get("x_bio"):
        lines.append(f"Bio: {p['x_bio'][:200]}")
    if p.get("x_follower_count"):
        try:
            lines.append(f"Followers: {int(p['x_follower_count']):,}")
        except (ValueError, TypeError):
            lines.append(f"Followers: {p['x_follower_count']}")
    return ("X / TWITTER\n" + "\n".join(lines)) if lines else None


def _section_event_history(p: dict) -> str | None:
    """Supabase event history + application Q&A answers."""
    events = p.get("event_history") or []
    qa = p.get("event_qa_answers") or {}
    if not events and not qa:
        return None
    lines: list[str] = []
    if p.get("total_events_applied"):
        parts = [f"Events Applied: {p['total_events_applied']}"]
        if p.get("checkin_count"):
            parts.append(f"Checked In: {p['checkin_count']}")
            rate = p.get("checkin_rate")
            if rate is not None:
                parts.append(f"Rate: {rate:.0%}")
        lines.append(" | ".join(parts))
    if events:
        event_names = [
            e.get("event_name", "?")[:50]
            for e in events[:5]
            if isinstance(e, dict)
        ]
        if event_names:
            lines.append(f"Events: {'; '.join(event_names)}")
    if qa:
        lines.append("Application Answers:")
        for q, a in list(qa.items())[:5]:
            q_short = q[:60] if len(q) > 60 else q
            a_short = str(a)[:200] if len(str(a)) > 200 else str(a)
            lines.append(f"  Q: {q_short}")
            lines.append(f"  A: {a_short}")
    return ("EVENT HISTORY\n" + "\n".join(lines)) if lines else None


def _section_community(p: dict) -> str | None:
    """CV community events (shared.py legacy — previous_event_scores from person store)."""
    lines: list[str] = []
    if p.get("total_cv_events"):
        lines.append(f"CV Events Attended: {p['total_cv_events']}")
    if p.get("applied_to_opus_46"):
        status = p.get("opus_46_status") or "applied"
        lines.append(f"Applied to Opus 4.6 Hackathon: {status}")
    if p.get("cv_event_history"):
        cv_events = p["cv_event_history"]
        if isinstance(cv_events, list) and cv_events:
            lines.append(
                f"Event History: {', '.join(str(e.get('event_name', '?'))[:40] for e in cv_events[:5] if isinstance(e, dict))}"
            )
    return ("COMMUNITY & HACKATHONS\n" + "\n".join(lines)) if lines else None


def _section_judging(p: dict) -> str | None:
    """Hackathon judging scores — supports both aggregated and per-criteria formats."""
    judging = p.get("judging_scores_received") or []
    if not judging:
        return None
    lines: list[str] = []
    # Aggregated format (has 'pct') vs legacy per-criteria
    if isinstance(judging[0], dict) and "pct" in judging[0]:
        lines.append(f"Hackathon Judging ({len(judging)} events):")
        for j in judging[:5]:
            event = j.get("event", "?")
            team = j.get("team", "?")
            pct = j.get("pct", 0)
            finalist = " FINALIST" if j.get("finalist_rec") else ""
            lines.append(
                f"  - {event} (team: {team}): {pct:.0f}%{finalist}"
            )
            for c in j.get("criteria", [])[:4]:
                lines.append(
                    f"    {c['name']} (w={c['weight']}): {c['score']}/10"
                )
    else:
        avg = sum(
            s.get("score", 0) for s in judging if isinstance(s, dict)
        ) / max(len(judging), 1)
        lines.append(
            f"Hackathon Judging: {len(judging)} scores, avg {avg:.1f}/10"
        )
        for s in judging[:5]:
            if isinstance(s, dict):
                lines.append(
                    f"  - {s.get('criteria', '?')} ({s.get('event', '?')}): {s.get('score', 0)}/10"
                )
    return ("JUDGING SCORES\n" + "\n".join(lines)) if lines else None


def _section_hackathon_submissions(p: dict) -> str | None:
    subs = p.get("hackathon_submissions") or []
    if not subs:
        return None
    lines = [f"Hackathon Submissions ({len(subs)}):"]
    for s in subs[:5]:
        if not isinstance(s, dict):
            continue
        if s.get("event"):
            line = f"  - {s.get('team', '?')} @ {s.get('event', '?')}"
            if s.get("event_date"):
                line += f" ({s['event_date']})"
            if s.get("placement"):
                line += f" -> {s['placement']}"
            lines.append(line)
        elif s.get("project_name"):
            lines.append(
                f"  - {s.get('project_name', '?')}: {(s.get('description') or '')[:80]}"
            )
    placements = p.get("hackathon_placements") or []
    if placements:
        lines.append(f"  Placements: {', '.join(placements)}")
    return "HACKATHON SUBMISSIONS\n" + "\n".join(lines)


def _section_submission_details(p: dict) -> str | None:
    details = p.get("submission_details") or []
    if not details:
        return None
    shown: set[str] = set()
    detail_lines: list[str] = []
    for d in details:
        if not isinstance(d, dict):
            continue
        key = f"{d.get('event', '')}/{d.get('team', '')}"
        if (
            d.get("field") in ("Project Description", "Project Title")
            and key not in shown
        ):
            shown.add(key)
            val = (d.get("value", "") or "")[:200]
            detail_lines.append(
                f"  - [{d.get('event', '?')[:30]}] {d.get('field', '?')}: {val}"
            )
    if not detail_lines:
        return None
    return "SUBMISSION DETAILS\n" + "\n".join(detail_lines[:5])


def _section_prior_events(p: dict) -> str | None:
    """Previous event performance scores (shared.py legacy)."""
    prev = p.get("previous_event_scores")
    if not prev or not isinstance(prev, dict):
        return None
    lines = ["Previous Event Performance:"]
    for ev_name, ev_scores in prev.items():
        short_name = ev_name.replace("_", " ").title()
        score_parts: list[str] = []
        for k, val in ev_scores.items():
            if isinstance(val, (int, float)):
                score_parts.append(f"{k}={val}")
            elif isinstance(val, str) and val:
                score_parts.append(f"{k}={val}")
        if score_parts:
            lines.append(f"  - {short_name}: {', '.join(score_parts)}")
    return ("PRIOR EVENTS\n" + "\n".join(lines)) if len(lines) > 1 else None


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

# Ordered list of section builders. _section_basic_info has a special
# signature (include_raw_csv) so it's called separately.
_SECTION_BUILDERS = [
    _section_linkedin,
    _section_linkedin_analytics,
    _section_github,
    _section_repositories,
    _section_x_twitter,
    _section_work_history,
    _section_education,
    _section_projects,
    _section_certifications,
    _section_publications,
    _section_volunteer,
    _section_event_history,
    _section_community,
    _section_judging,
    _section_hackathon_submissions,
    _section_submission_details,
    _section_prior_events,
]


def format_profile(person: dict, include_raw_csv: bool = False) -> str:
    """Turn a multi-field person dict into readable text for the LLM.

    Parameters
    ----------
    person:
        The applicant profile dictionary. Field names follow the conventions
        used by the hackathon-elo pipeline (DB + CSV enrichment).
    include_raw_csv:
        When True, append any extra CSV columns the LLM has not already seen
        into the BASIC INFO section.

    Returns
    -------
    str
        Human-readable profile text with labelled sections separated by blank
        lines. Only sections with data are included.
    """
    sections: list[str] = [_section_basic_info(person, include_raw_csv)]

    for builder in _SECTION_BUILDERS:
        text = builder(person)
        if text:
            sections.append(text)

    return "\n\n".join(sections)
