"""
Supabase enrichment: pull profile data from all directory DB tables.

Queries LinkedIn, GitHub, event, and organization tables in batches,
then resolves organization IDs to human-readable names.

Ported from hackathon-elo/pipeline_v2/rank.py ``enrich_all``.
"""

import logging
import time

from cv_rank.utils import supa_get, parse_linkedin_username, parse_github_username

logger = logging.getLogger("cv_rank")


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def enrich_from_supabase(people: list[dict], config: dict) -> list[dict]:
    """Enrich *people* from all Supabase directory tables.

    Pulls from:
      LinkedIn: profile, analytics, positions, education, publications,
                certifications, projects, volunteer
      GitHub:   profile (bio), analytics, repositories (with LLM ratings)
      Events:   event_applicants (Q&A answers, approval history)
      Orgs:     linkedin_organization (resolve company/school names)
      X/Twitter: x table (if available)

    Parameters
    ----------
    people:
        List of person dicts (mutated in place and returned).
    config:
        Full configuration dict.  Supabase credentials are read from
        ``config["enrichment"]["supabase"]["url"]`` and ``["key"]``.

    Returns
    -------
    list[dict]
        The same *people* list, enriched with additional fields.
    """
    supa_cfg = config["enrichment"]["supabase"]
    url = supa_cfg["url"]
    key = supa_cfg["key"]
    batch_size = supa_cfg.get("batch_size", 50)

    if not url or not key:
        logger.warning("Supabase URL or key not configured -- skipping DB enrichment")
        return people

    logger.info("ENRICHING FROM DIRECTORY DB")

    # Build lookup maps: username/email -> person indices
    li_map: dict[str, list[int]] = {}
    for i, p in enumerate(people):
        u = parse_linkedin_username(p.get("linkedin_url"))
        if u:
            li_map.setdefault(u, []).append(i)
            p["_li_username"] = u

    gh_map: dict[str, list[int]] = {}
    for i, p in enumerate(people):
        u = parse_github_username(p.get("github_url"))
        if u:
            gh_map.setdefault(u, []).append(i)
            p["_gh_username"] = u

    email_map: dict[str, list[int]] = {}
    for i, p in enumerate(people):
        e = p.get("email", "").lower().strip()
        if e:
            email_map.setdefault(e, []).append(i)

    # Phase 1: LinkedIn profile, analytics, positions, education, etc.
    all_org_ids = _enrich_linkedin(people, url, key, batch_size, li_map)

    # Phase 2-3: Resolve org IDs to names, then apply to positions/education/volunteer
    org_names = _resolve_org_names(all_org_ids, url, key, batch_size)
    _apply_org_names(people, org_names)

    # Phase 4: GitHub profile, analytics, repositories
    _enrich_github_db(people, url, key, batch_size, gh_map)

    # Phase 5: Event history, Q&A answers, company/title from applications
    _enrich_events(people, url, key, batch_size, email_map)

    # Phase 6: X/Twitter (best-effort)
    _enrich_x_handles(people, url, key, email_map, li_map, batch_size)

    # Phase 7: Extract company/role from LinkedIn positions if not already set
    _extract_company_role(people)

    _print_enrichment_summary(people)

    return people


# ---------------------------------------------------------------------------
# Shared helpers
# ---------------------------------------------------------------------------

def _safe_in_filter(values: list[str]) -> str:
    """Build a PostgREST ``in.(...)`` value string, skipping dangerous chars.

    Filters out values containing double-quotes, parentheses, or commas
    that would break the ``in.("v1","v2")`` syntax.
    """
    safe = [v for v in values if v and '"' not in v and '(' not in v and ')' not in v and ',' not in v]
    return ",".join(f'"{v}"' for v in safe)


# ---------------------------------------------------------------------------
# Phase helpers (extracted from enrich_from_supabase)
# ---------------------------------------------------------------------------

def _enrich_linkedin(
    people: list[dict],
    url: str,
    key: str,
    batch_size: int,
    li_map: dict[str, list[int]],
) -> set[str]:
    """Query all LinkedIn tables and enrich people in-place.

    Returns the set of organization IDs found in positions, education,
    and volunteer records (for later batch resolution).
    """
    logger.info("  LinkedIn: %d usernames to look up", len(li_map))

    all_org_ids: set[str] = set()

    li_usernames = list(li_map.keys())
    for batch_start in range(0, len(li_usernames), batch_size):
        batch = li_usernames[batch_start:batch_start + batch_size]
        unames = ",".join(f'"{u}"' for u in batch)

        # 1. LinkedIn profile
        rows = supa_get(url, key, "linkedin", {
            "username": f"in.({unames})",
            "select": "username,headline,summary,skills,follower_count,connection_count,"
                      "country,is_creator,is_premium,awards,patents",
        })
        for r in rows:
            for idx in li_map.get(r["username"].lower(), []):
                p = people[idx]
                p["linkedin_headline"] = r.get("headline") or ""
                p["linkedin_bio"] = r.get("summary") or ""
                p["linkedin_skills"] = r.get("skills") or []
                p["li_follower_count"] = r.get("follower_count")
                p["li_connection_count"] = r.get("connection_count")
                p["li_country"] = r.get("country")
                p["li_is_creator"] = r.get("is_creator")
                p["li_is_premium"] = r.get("is_premium")
                p["li_awards"] = r.get("awards")
                p["li_patents"] = r.get("patents")

        # 2. LinkedIn analytics
        rows = supa_get(url, key, "linkedin_analytics", {
            "linkedin_username": f"in.({unames})",
            "select": "linkedin_username,is_founder,is_decision_maker,is_in_big_tech,"
                      "is_student,years_experience,education_level,top_school,"
                      "technical_school,employment_category,notable_skills,"
                      "notable_achievements",
        })
        for r in rows:
            for idx in li_map.get(r["linkedin_username"].lower(), []):
                p = people[idx]
                for k in [
                    "is_founder", "is_decision_maker", "is_in_big_tech", "is_student",
                    "years_experience", "education_level", "top_school", "technical_school",
                    "employment_category",
                ]:
                    if r.get(k) is not None:
                        p[k] = r[k]
                p["notable_skills_linkedin"] = r.get("notable_skills") or []
                p["notable_achievements"] = r.get("notable_achievements") or []

        # 3. Work history (positions) -- uses organization_id, not company_name
        rows = supa_get(url, key, "linkedin_position", {
            "linkedin_username": f"in.({unames})",
            "select": "linkedin_username,title,description,from_date,to_date,"
                      "organization_id,current_position,duration_years",
            "order": "from_date.desc.nullsfirst",
            "limit": "500",
        })
        positions_by_user: dict[str, list[dict]] = {}
        for r in rows:
            u = r["linkedin_username"].lower()
            positions_by_user.setdefault(u, []).append(r)
            if r.get("organization_id"):
                all_org_ids.add(r["organization_id"])
        for u, positions in positions_by_user.items():
            for idx in li_map.get(u, []):
                people[idx]["_raw_positions"] = positions[:8]

        # 4. Education -- uses organization_id, not school_name
        rows = supa_get(url, key, "linkedin_education", {
            "linkedin_username": f"in.({unames})",
            "select": "linkedin_username,degree,from_date,to_date,"
                      "organization_id,education_level",
            "limit": "500",
        })
        edu_by_user: dict[str, list[dict]] = {}
        for r in rows:
            u = r["linkedin_username"].lower()
            edu_by_user.setdefault(u, []).append(r)
            if r.get("organization_id"):
                all_org_ids.add(r["organization_id"])
        for u, edu in edu_by_user.items():
            for idx in li_map.get(u, []):
                people[idx]["_raw_education"] = edu[:6]

        # 5. Publications
        rows = supa_get(url, key, "linkedin_publication", {
            "linkedin_username": f"in.({unames})",
            "select": "linkedin_username,name,description,publisher,published_date",
            "limit": "200",
        })
        pubs_by_user: dict[str, list[dict]] = {}
        for r in rows:
            u = r["linkedin_username"].lower()
            pubs_by_user.setdefault(u, []).append(r)
        for u, pubs in pubs_by_user.items():
            for idx in li_map.get(u, []):
                people[idx]["publications"] = pubs[:5]

        # 6. Certifications
        rows = supa_get(url, key, "linkedin_certification", {
            "linkedin_username": f"in.({unames})",
            "select": "linkedin_username,name,authority,issued_date",
            "limit": "300",
        })
        certs_by_user: dict[str, list[dict]] = {}
        for r in rows:
            u = r["linkedin_username"].lower()
            certs_by_user.setdefault(u, []).append(r)
        for u, certs in certs_by_user.items():
            for idx in li_map.get(u, []):
                people[idx]["certifications"] = certs[:5]

        # 7. Projects (high value -- rich self-written descriptions)
        rows = supa_get(url, key, "linkedin_project", {
            "linkedin_username": f"in.({unames})",
            "select": "linkedin_username,title,description,from_date,to_date",
            "limit": "300",
        })
        projects_by_user: dict[str, list[dict]] = {}
        for r in rows:
            u = r["linkedin_username"].lower()
            projects_by_user.setdefault(u, []).append(r)
        for u, projects in projects_by_user.items():
            for idx in li_map.get(u, []):
                people[idx]["linkedin_projects"] = projects[:5]

        # 8. Volunteer experience
        rows = supa_get(url, key, "linkedin_volunteer_experience", {
            "linkedin_username": f"in.({unames})",
            "select": "linkedin_username,role,organization_id,from_date,to_date",
            "limit": "200",
        })
        vol_by_user: dict[str, list[dict]] = {}
        for r in rows:
            u = r["linkedin_username"].lower()
            vol_by_user.setdefault(u, []).append(r)
            if r.get("organization_id"):
                all_org_ids.add(r["organization_id"])
        for u, vol in vol_by_user.items():
            for idx in li_map.get(u, []):
                people[idx]["_raw_volunteer"] = vol[:3]

        batch_num = batch_start // batch_size + 1
        total_batches = (len(li_usernames) + batch_size - 1) // batch_size
        if total_batches <= 10 or batch_num % 5 == 0 or batch_num == total_batches:
            logger.info(
                "    LinkedIn batch %d/%d: %d/%d",
                batch_num, total_batches,
                min(batch_start + batch_size, len(li_usernames)),
                len(li_usernames),
            )
        time.sleep(0.1)

    li_enriched = sum(1 for p in people if p.get("linkedin_headline"))
    if people:
        logger.info(
            "  LinkedIn: %d/%d enriched (%d%%)",
            li_enriched, len(people),
            li_enriched * 100 // len(people),
        )

    return all_org_ids


def _resolve_org_names(
    all_org_ids: set[str],
    url: str,
    key: str,
    batch_size: int,
) -> dict[str, str]:
    """Query linkedin_organization table to map org IDs to names."""
    org_names: dict[str, str] = {}
    if not all_org_ids:
        return org_names

    logger.info("  Resolving %d organization names...", len(all_org_ids))
    org_id_list = list(all_org_ids)
    for batch_start in range(0, len(org_id_list), batch_size):
        batch = org_id_list[batch_start:batch_start + batch_size]
        ids = ",".join(f'"{oid}"' for oid in batch)
        rows = supa_get(url, key, "linkedin_organization", {
            "id": f"in.({ids})",
            "select": "id,name,industry",
        })
        for r in rows:
            org_names[r["id"]] = r.get("name") or "?"
        time.sleep(0.05)
    logger.info("    Resolved %d/%d orgs", len(org_names), len(all_org_ids))

    return org_names


def _apply_org_names(people: list[dict], org_names: dict[str, str]) -> None:
    """Replace _raw_positions/_raw_education/_raw_volunteer with resolved names."""
    for p in people:
        # Positions -> positions_with_companies
        raw_pos = p.pop("_raw_positions", [])
        if raw_pos:
            p["positions_with_companies"] = []
            for pos in raw_pos:
                company = org_names.get(pos.get("organization_id"), "")
                p["positions_with_companies"].append({
                    "title": pos.get("title", ""),
                    "company_name": company,
                    "description": pos.get("description", ""),
                    "from_date": pos.get("from_date", ""),
                    "to_date": pos.get("to_date", ""),
                    "current_position": pos.get("current_position", False),
                })

        # Education -> education_with_schools
        raw_edu = p.pop("_raw_education", [])
        if raw_edu:
            p["education_with_schools"] = []
            for edu in raw_edu:
                school = org_names.get(edu.get("organization_id"), "")
                degree_raw = edu.get("degree", "")
                p["education_with_schools"].append({
                    "degree": degree_raw,
                    "school_name": school,
                    "education_level": edu.get("education_level", ""),
                    "from_date": edu.get("from_date", ""),
                    "to_date": edu.get("to_date", ""),
                })

        # Volunteer -> volunteer_experience
        raw_vol = p.pop("_raw_volunteer", [])
        if raw_vol:
            p["volunteer_experience"] = []
            for vol in raw_vol:
                org = org_names.get(vol.get("organization_id"), "")
                p["volunteer_experience"].append({
                    "role": vol.get("role", ""),
                    "organization": org,
                })


def _enrich_github_db(
    people: list[dict],
    url: str,
    key: str,
    batch_size: int,
    gh_map: dict[str, list[int]],
) -> None:
    """Query GitHub profile, analytics, and repository tables."""
    logger.info("  GitHub: %d usernames to look up", len(gh_map))

    gh_usernames = list(gh_map.keys())
    for batch_start in range(0, len(gh_usernames), batch_size):
        batch = gh_usernames[batch_start:batch_start + batch_size]
        unames = ",".join(f'"{u}"' for u in batch)

        # 1. GitHub profile (bio, external_contributions)
        rows = supa_get(url, key, "github", {
            "username": f"in.({unames})",
            "select": "username,bio,external_contributions",
        })
        for r in rows:
            for idx in gh_map.get(r["username"].lower(), []):
                p = people[idx]
                if r.get("bio"):
                    p["github_bio"] = r["bio"]
                if r.get("external_contributions"):
                    p["github_external_contributions"] = r["external_contributions"]

        # 2. GitHub analytics
        rows = supa_get(url, key, "github_analytics", {
            "github_username": f"in.({unames})",
            "select": "github_username,total_score,implementation_score,"
                      "difficulty_score,language_control_score,"
                      "average_commits_per_day,num_oss_repos_contributed_to,"
                      "best_languages,notable_skills,total_code_by_language",
        })
        for r in rows:
            for idx in gh_map.get(r["github_username"].lower(), []):
                p = people[idx]
                p["github_total_score"] = r.get("total_score")
                p["github_implementation_score"] = r.get("implementation_score")
                p["github_difficulty_score"] = r.get("difficulty_score")
                p["github_language_control_score"] = r.get("language_control_score")
                p["github_best_languages"] = r.get("best_languages") or []
                p["github_notable_skills"] = r.get("notable_skills") or []
                p["github_avg_commits_per_day"] = r.get("average_commits_per_day")
                p["github_oss_repos_contributed"] = r.get("num_oss_repos_contributed_to")
                p["github_code_by_language"] = r.get("total_code_by_language") or {}

        # 3. GitHub repos with LLM ratings (top by stars, non-fork)
        rows = supa_get(url, key, "github_repository", {
            "github_username": f"in.({unames})",
            "select": "github_username,name,stargazer_count,fork_count,commit_count,"
                      "is_fork,description,implementation_rating,difficulty_rating,"
                      "skills,tools,topics,languages",
            "is_fork": "eq.false",
            "order": "stargazer_count.desc",
            "limit": "500",
        })
        repos_by_user: dict[str, list[dict]] = {}
        for r in rows:
            u = r["github_username"].lower()
            repos_by_user.setdefault(u, []).append(r)
        for u, repos in repos_by_user.items():
            for idx in gh_map.get(u, []):
                p = people[idx]
                p["gh_api_top_repos"] = [
                    {
                        "name": r["name"],
                        "stars": r.get("stargazer_count") or 0,
                        "forks": r.get("fork_count") or 0,
                        "description": r.get("description") or "",
                        "commits": r.get("commit_count") or 0,
                        "implementation_rating": r.get("implementation_rating"),
                        "difficulty_rating": r.get("difficulty_rating"),
                        "skills": r.get("skills") or [],
                        "tools": r.get("tools") or [],
                        "languages": r.get("languages") or {},
                    }
                    for r in repos[:5]
                ]
                p["gh_api_stars"] = sum(r.get("stargazer_count") or 0 for r in repos)
                p["gh_api_repos"] = len(repos)

        time.sleep(0.1)

    gh_enriched = sum(
        1 for p in people
        if p.get("github_total_score") or p.get("gh_api_stars")
    )
    if people:
        logger.info(
            "  GitHub DB: %d/%d enriched (%d%%)",
            gh_enriched, len(people),
            gh_enriched * 100 // len(people),
        )


def _enrich_events(
    people: list[dict],
    url: str,
    key: str,
    batch_size: int,
    email_map: dict[str, list[int]],
) -> None:
    """Query event_applicants once for history, Q&A answers, and company/title."""
    logger.info("  Events: looking up %d emails", len(email_map))
    emails_list = list(email_map.keys())
    event_enriched = 0

    for batch_start in range(0, len(emails_list), batch_size):
        batch = emails_list[batch_start:batch_start + batch_size]
        emails_str = ",".join(f'"{e}"' for e in batch)

        # Single query fetches all fields needed for both event history and company/title
        rows = supa_get(url, key, "event_applicants", {
            "email": f"in.({emails_str})",
            "select": "email,event_name,status,event_specific_data,company,job_title",
            "limit": "1000",
        })

        # Group by email
        events_by_email: dict[str, list[dict]] = {}
        for r in rows:
            e = r.get("email", "").lower()
            events_by_email.setdefault(e, []).append(r)

        for e, events in events_by_email.items():
            for idx in email_map.get(e, []):
                p = people[idx]
                p["event_history"] = []
                all_qa: dict[str, str] = {}
                for ev in events:
                    p["event_history"].append({
                        "event_name": ev.get("event_name", ""),
                        "status": ev.get("status", ""),
                    })

                    # Extract Q&A answers from event_specific_data
                    esd = ev.get("event_specific_data") or {}
                    for q, a in esd.items():
                        if isinstance(a, str) and len(a) > 5:
                            ql = q.lower()
                            if any(skip in ql for skip in [
                                "api_id", "first_name", "last_name",
                                "email", "phone", "linkedin", "github",
                            ]):
                                continue
                            all_qa[q] = a

                    # Company/title from approved applications (fill if missing)
                    if ev.get("status") == "approved":
                        if ev.get("company") and not p.get("company"):
                            p["company"] = ev["company"]
                        if ev.get("job_title") and not p.get("title"):
                            p["title"] = ev["job_title"]

                if all_qa:
                    p["event_qa_answers"] = all_qa
                    event_enriched += 1

                p["total_events_applied"] = len(events)
                p["events_approved"] = sum(
                    1 for ev in events if ev.get("status") == "approved"
                )

        time.sleep(0.1)

    logger.info("  Events: %d/%d have Q&A answers", event_enriched, len(people))


# ---------------------------------------------------------------------------
# Load applicants by event name
# ---------------------------------------------------------------------------

# Fields in event_specific_data that are identity/PII, not Q&A answers.
_ESD_SKIP_FIELDS = frozenset([
    "api_id", "first_name", "last_name", "email", "phone", "linkedin", "github",
])


def load_from_supabase(event_name: str, url: str, key: str) -> list[dict]:
    """Load applicants for *event_name* from the ``event_applicants`` table.

    Returns a list of person dicts in the same shape as ``csv_io.load_csv``,
    so the rest of the pipeline works identically regardless of source.
    """
    logger.info("Loading applicants for event %r from Supabase ...", event_name)

    rows = supa_get(url, key, "event_applicants", {
        "event_name": f"eq.{event_name}",
        "select": "email,event_name,status,event_specific_data,company,job_title",
        "limit": "5000",
    })

    if not rows:
        logger.warning("No applicants found for event %r", event_name)
        return []

    seen_emails: set[str] = set()
    people: list[dict] = []

    for row in rows:
        esd = row.get("event_specific_data") or {}
        first = (esd.get("first_name") or "").strip()
        last = (esd.get("last_name") or "").strip()
        name = f"{first} {last}".strip()
        if not name:
            continue

        email = (row.get("email") or "").strip().lower()
        if email:
            if email in seen_emails:
                logger.debug("Skipping duplicate email: %s (%s)", email, name)
                continue
            seen_emails.add(email)

        # Build Q&A text from event_specific_data (same skip logic as _enrich_events)
        qa_parts: list[str] = []
        for q, a in esd.items():
            if isinstance(a, str) and len(a) > 5:
                if q.lower() not in _ESD_SKIP_FIELDS and not any(
                    skip in q.lower() for skip in _ESD_SKIP_FIELDS
                ):
                    qa_parts.append(a)

        person: dict = {
            "name": name,
            "first_name": first,
            "last_name": last,
            "email": email,
            "linkedin_url": (esd.get("linkedin") or "").strip(),
            "github_url": (esd.get("github") or "").strip(),
            "company": (row.get("company") or "").strip(),
            "role": (row.get("job_title") or "").strip(),
            "self_description": " | ".join(qa_parts) if qa_parts else "",
            "x_handle": "",
            "ai_project": "",
            "looking_for_job": "",
            "_raw_csv": esd,
            "_source": "supabase",
            "_event_status": row.get("status", ""),
        }
        people.append(person)

    logger.info("Loaded %d applicants for %r from Supabase", len(people), event_name)
    return people


# ---------------------------------------------------------------------------
# Existing helpers (unchanged)
# ---------------------------------------------------------------------------

def _enrich_x_handles(
    people: list[dict],
    url: str,
    key: str,
    email_map: dict[str, list[int]],
    li_map: dict[str, list[int]],
    batch_size: int,
) -> None:
    """Best-effort enrichment from ``x`` table (X/Twitter handles).

    Attempts to query the table; silently skips if the table does not exist
    or returns an error.
    """
    # Try querying by linkedin_username linkage (most common identity link)
    li_usernames = list(li_map.keys())
    x_enriched = 0

    for batch_start in range(0, len(li_usernames), batch_size):
        batch = li_usernames[batch_start:batch_start + batch_size]
        unames = _safe_in_filter(batch)
        if not unames:
            continue

        rows = supa_get(url, key, "x", {
            "linkedin_username": f"in.({unames})",
            "select": "linkedin_username,username,name,bio,follower_count",
        })

        # Empty batch is normal — not all users have X data.
        if not rows:
            continue

        for r in rows:
            li_u = (r.get("linkedin_username") or "").lower()
            for idx in li_map.get(li_u, []):
                p = people[idx]
                p["x_handle"] = r.get("username") or ""
                p["x_name"] = r.get("name") or ""
                p["x_bio"] = r.get("bio") or ""
                p["x_follower_count"] = r.get("follower_count")
                x_enriched += 1

        time.sleep(0.05)

    if x_enriched:
        logger.info("  X/Twitter: %d enriched", x_enriched)


def _extract_company_role(people: list[dict]) -> None:
    """Fill company/role from LinkedIn positions if not already set."""
    filled = 0
    for p in people:
        positions = p.get("positions_with_companies")
        if not positions:
            continue
        # Find current position, or fall back to most recent
        current = None
        for pos in positions:
            if pos.get("current_position"):
                current = pos
                break
        if not current:
            current = positions[0]
        if not p.get("company") and current.get("company_name"):
            p["company"] = current["company_name"]
        if not p.get("role") and current.get("title"):
            p["role"] = current["title"]
        if p.get("company") or p.get("role"):
            filled += 1
    logger.info("  Company/role extracted from positions: %d/%d", filled, len(people))


def _print_enrichment_summary(people: list[dict]) -> None:
    """Print the enrichment summary box."""
    total = len(people)
    if total == 0:
        logger.info("  No people to summarize.")
        return

    has_li = sum(1 for p in people if p.get("linkedin_headline"))
    has_gh = sum(1 for p in people if p.get("github_total_score") or p.get("gh_api_stars"))
    has_pos = sum(1 for p in people if p.get("positions_with_companies"))
    has_edu = sum(1 for p in people if p.get("education_with_schools"))
    has_proj = sum(1 for p in people if p.get("linkedin_projects"))
    has_cert = sum(1 for p in people if p.get("certifications"))
    has_pubs = sum(1 for p in people if p.get("publications"))
    has_events = sum(1 for p in people if p.get("event_history"))
    has_x = sum(1 for p in people if p.get("x_handle"))
    has_any = sum(
        1 for p in people
        if p.get("linkedin_headline") or p.get("gh_api_stars") or p.get("github_total_score")
    )

    def _pct(n: int) -> int:
        return n * 100 // total

    lines = [
        "",
        "  +=== Enrichment Summary ===========================+",
        f"  |  LinkedIn profiles:    {has_li:4d}/{total} ({_pct(has_li):3d}%)     |",
        f"  |  Work history:         {has_pos:4d}/{total} ({_pct(has_pos):3d}%)     |",
        f"  |  Education:            {has_edu:4d}/{total} ({_pct(has_edu):3d}%)     |",
        f"  |  Projects:             {has_proj:4d}/{total} ({_pct(has_proj):3d}%)     |",
        f"  |  Certifications:       {has_cert:4d}/{total} ({_pct(has_cert):3d}%)     |",
        f"  |  Publications:         {has_pubs:4d}/{total} ({_pct(has_pubs):3d}%)     |",
        f"  |  GitHub profiles:      {has_gh:4d}/{total} ({_pct(has_gh):3d}%)     |",
        f"  |  Event history (DB):   {has_events:4d}/{total} ({_pct(has_events):3d}%)     |",
        f"  |  X/Twitter:            {has_x:4d}/{total} ({_pct(has_x):3d}%)     |",
        f"  |  ANY data:             {has_any:4d}/{total} ({_pct(has_any):3d}%)     |",
        "  +==================================================+",
    ]
    for line in lines:
        logger.info(line)
