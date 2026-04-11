"""
Platform DB enrichment: pull behavioral data from Cerebral Valley platform PostgreSQL.

Queries event history, hackathon submissions, and judging scores.
Does NOT pull approval status (avoids incumbency bias).
"""

import logging
import os
from collections import defaultdict

from cv_rank.utils import parse_github_username, parse_linkedin_username

logger = logging.getLogger("cv_rank")

_GENERIC_EMAIL_DOMAINS = frozenset({
    "gmail.com", "yahoo.com", "hotmail.com", "outlook.com",
    "icloud.com", "protonmail.com", "proton.me", "aol.com",
    "live.com", "me.com", "mail.com", "ymail.com",
    "fastmail.com", "hey.com", "pm.me", "tutanota.com",
})


def enrich_from_platform_db(people: list[dict], config: dict) -> list[dict]:
    """Enrich *people* from the CV platform PostgreSQL database.

    Pulls:
      - Total CV events attended (count only, no approval status)
      - Event names list
      - Hackathon submissions (team name, event, placement)
      - Judging scores (per-criteria with weights)
      - User bio/location as fallbacks

    Mutates people in-place and returns the same list.
    """
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        logger.warning("psycopg2 not installed — skipping platform DB enrichment. "
                       "Install with: pip install psycopg2-binary")
        return people

    db_cfg = config.get("enrichment", {}).get("platform_db", {})
    dsn = db_cfg.get("dsn") or os.environ.get("PLATFORM_DATABASE_URL", "")
    batch_size = db_cfg.get("batch_size", 100)

    if not dsn or dsn.startswith("${"):
        logger.info("Platform DB DSN not configured — skipping")
        return people

    # Build email → person indices map
    email_map: dict[str, list[int]] = {}
    for i, p in enumerate(people):
        e = (p.get("email") or "").lower().strip()
        if e:
            email_map.setdefault(e, []).append(i)

    if not email_map:
        logger.warning("No emails to look up in platform DB")
        return people

    emails = list(email_map.keys())
    logger.info("Platform DB: looking up %d emails in %d batches",
                len(emails), (len(emails) + batch_size - 1) // batch_size)

    try:
        conn = psycopg2.connect(dsn)
    except Exception as exc:
        logger.warning("Platform DB connection failed: %s", exc)
        return people

    try:
        _enrich_profile_basics(conn, people, emails, email_map, batch_size)
        _enrich_event_history(conn, people, emails, email_map, batch_size)
        _enrich_hackathon_data(conn, people, emails, email_map, batch_size)

        # Fallback: match unenriched people by name / LinkedIn / GitHub
        unenriched = [i for i, p in enumerate(people)
                      if not p.get("total_cv_events") and p.get("email")]
        if unenriched:
            resolved = _fallback_match(conn, people, unenriched, email_map, batch_size)
            if resolved:
                logger.info("Platform DB: fallback matched %d extra people by name/profile", resolved)
    except Exception as exc:
        logger.warning("Platform DB enrichment error: %s", exc)
    finally:
        conn.close()

    enriched = sum(1 for p in people if p.get("total_cv_events"))
    logger.info("Platform DB: enriched %d / %d people", enriched, len(people))
    return people


def _enrich_profile_basics(conn, people, emails, email_map, batch_size):
    """Pull bio and location from UserProfile as fallbacks."""
    query = """
        SELECT LOWER(email) as email, description, location
        FROM "UserProfile"
        WHERE LOWER(email) = ANY(%s)
        AND (description IS NOT NULL AND description != ''
             OR location IS NOT NULL AND location != '')
    """
    with conn.cursor(cursor_factory=_dict_cursor(conn)) as cur:
        for batch in _batched(emails, batch_size):
            cur.execute(query, (batch,))
            for row in cur.fetchall():
                for idx in email_map.get(row["email"], []):
                    p = people[idx]
                    if not p.get("self_description") and row.get("description"):
                        p["self_description"] = row["description"]
                    if not p.get("location") and row.get("location"):
                        p["location"] = row["location"]


def _enrich_event_history(conn, people, emails, email_map, batch_size):
    """Pull event attendance count, names, and check-in history (no approval status)."""
    query = """
        SELECT LOWER(up.email) as email, e.title as event_name,
               a."checkedIn" as checked_in
        FROM "UserProfile" up
        JOIN "EventApplicant" a ON a."userId" = up."userId"
        JOIN "PlatformEvent" e ON a."eventId" = e.id
        WHERE LOWER(up.email) = ANY(%s)
        ORDER BY a."createdAt" DESC
    """
    with conn.cursor(cursor_factory=_dict_cursor(conn)) as cur:
        for batch in _batched(emails, batch_size):
            cur.execute(query, (batch,))
            # Group by email — store full row data for check-in stats
            by_email: dict[str, list[dict]] = defaultdict(list)
            for row in cur.fetchall():
                by_email[row["email"]].append(row)

            for email, rows in by_email.items():
                for idx in email_map.get(email, []):
                    p = people[idx]
                    event_names = [r["event_name"] for r in rows]
                    p["total_cv_events"] = len(event_names)
                    # Store as event_history format (compatible with profile.py)
                    existing = p.get("event_history") or []
                    existing_names = {e.get("event_name", "") for e in existing if isinstance(e, dict)}
                    for name in event_names:
                        if name not in existing_names:
                            existing.append({"event_name": name})
                    p["event_history"] = existing
                    p["total_events_applied"] = len(existing)
                    # Check-in stats
                    checkin_count = sum(1 for r in rows if r.get("checked_in"))
                    p["checkin_count"] = checkin_count
                    p["checkin_rate"] = round(checkin_count / len(rows), 2) if rows else 0


def _enrich_hackathon_data(conn, people, emails, email_map, batch_size):
    """Pull hackathon submissions and judging scores."""
    # Query submissions with judging scores
    query = """
        SELECT
            LOWER(up.email) as email,
            hs.id as submission_id,
            hs."teamName" as team_name,
            e.title as event_name,
            hs.placement,
            hjs.score,
            hjc.name as criteria_name,
            hjc.weight as criteria_weight
        FROM "UserProfile" up
        JOIN "HackathonTeamMember" htm ON htm."userId" = up."userId"
        JOIN "HackathonSubmission" hs ON hs.id = htm."submissionId"
        JOIN "PlatformEvent" e ON hs."eventId" = e.id
        LEFT JOIN "HackathonJudgingScore" hjs ON hjs."submissionId" = hs.id
        LEFT JOIN "HackathonJudgingCriteria" hjc ON hjs."judgingCriteriaId" = hjc.id
        WHERE LOWER(up.email) = ANY(%s)
        ORDER BY up.email, hs.id, hjc.name
    """
    with conn.cursor(cursor_factory=_dict_cursor(conn)) as cur:
        for batch in _batched(emails, batch_size):
            cur.execute(query, (batch,))
            rows = cur.fetchall()

            # Group: email → submission_id → {meta + scores}
            by_email: dict[str, dict[str, dict]] = defaultdict(dict)
            for row in rows:
                email = row["email"]
                sid = str(row["submission_id"])
                if sid not in by_email[email]:
                    by_email[email][sid] = {
                        "team": row["team_name"] or "?",
                        "event": row["event_name"] or "?",
                        "placement": row["placement"],
                        "scores": [],
                    }
                if row.get("score") is not None:
                    by_email[email][sid]["scores"].append({
                        "name": row.get("criteria_name") or "Overall",
                        "weight": float(row.get("criteria_weight") or 1),
                        "score": float(row["score"]),
                    })

            for email, submissions in by_email.items():
                for idx in email_map.get(email, []):
                    p = people[idx]

                    # Build hackathon_submissions
                    existing_subs = p.get("hackathon_submissions") or []
                    existing_events = {
                        s.get("event", "") for s in existing_subs if isinstance(s, dict)
                    }
                    for sid, sub in submissions.items():
                        if sub["event"] not in existing_events:
                            entry = {
                                "team": sub["team"],
                                "event": sub["event"],
                            }
                            if sub["placement"]:
                                entry["placement"] = str(sub["placement"])
                            existing_subs.append(entry)
                    p["hackathon_submissions"] = existing_subs

                    # Build judging_scores_received (aggregated format with "pct")
                    existing_judging = p.get("judging_scores_received") or []
                    existing_judging_events = {
                        j.get("event", "") for j in existing_judging if isinstance(j, dict)
                    }
                    for sid, sub in submissions.items():
                        if not sub["scores"] or sub["event"] in existing_judging_events:
                            continue
                        # Compute weighted percentage
                        total_weight = sum(s["weight"] for s in sub["scores"])
                        if total_weight > 0:
                            weighted_sum = sum(
                                s["score"] * s["weight"] for s in sub["scores"]
                            )
                            pct = (weighted_sum / total_weight) * 10  # scale to 0-100
                        else:
                            pct = sum(s["score"] for s in sub["scores"]) / len(sub["scores"]) * 10

                        existing_judging.append({
                            "event": sub["event"],
                            "team": sub["team"],
                            "pct": pct,
                            "criteria": [
                                {"name": s["name"], "weight": s["weight"], "score": s["score"]}
                                for s in sub["scores"]
                            ],
                        })
                    if existing_judging:
                        p["judging_scores_received"] = existing_judging

                    # Placements
                    placements = [
                        f"#{sub['placement']} @ {sub['event']}"
                        for sub in submissions.values()
                        if sub.get("placement")
                    ]
                    if placements:
                        existing_placements = p.get("hackathon_placements") or []
                        existing_placements.extend(placements)
                        p["hackathon_placements"] = existing_placements


# ---------------------------------------------------------------------------
# Load applicants directly from platform DB
# ---------------------------------------------------------------------------


def load_from_platform_db(event_name: str, dsn: str) -> list[dict]:
    """Load applicants for *event_name* from the platform PostgreSQL DB.

    Returns a list of person dicts in the same shape as ``csv_io.load_csv``,
    so the rest of the pipeline works identically regardless of source.
    """
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        logger.error("psycopg2 not installed — cannot load from platform DB")
        return []

    logger.info("Loading applicants for %r from platform DB ...", event_name)

    try:
        conn = psycopg2.connect(dsn)
    except Exception as exc:
        logger.error("Platform DB connection failed: %s", exc)
        return []

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            # Load applicants with profile + Q&A answers
            cur.execute("""
                SELECT
                    up."firstName" as first_name,
                    up."lastName" as last_name,
                    up.email,
                    up."linkedinUsername" as linkedin_username,
                    up."githubUsername" as github_username,
                    up."xHandle" as x_handle,
                    up.description as bio,
                    up.location,
                    up."siteUrl" as site_url,
                    eqa_agg.answers
                FROM "EventApplicant" a
                JOIN "PlatformEvent" e ON a."eventId" = e.id
                JOIN "UserProfile" up ON a."userId" = up."userId"
                LEFT JOIN LATERAL (
                    SELECT jsonb_object_agg(eq.question, eqa.answer) as answers
                    FROM "EventQuestionAnswer" eqa
                    JOIN "EventQuestion" eq ON eqa."questionId" = eq.id
                    WHERE eqa."applicantId" = a.id
                ) eqa_agg ON true
                WHERE e.title = %s
                ORDER BY a."createdAt"
            """, (event_name,))
            rows = cur.fetchall()
    finally:
        conn.close()

    if not rows:
        logger.warning("No applicants found for event %r", event_name)
        return []

    seen_emails: set[str] = set()
    people: list[dict] = []

    for row in rows:
        first = (row.get("first_name") or "").strip()
        last = (row.get("last_name") or "").strip()
        name = f"{first} {last}".strip()
        if not name:
            continue

        email = (row.get("email") or "").strip().lower()
        if email:
            if email in seen_emails:
                continue
            seen_emails.add(email)

        answers = row.get("answers") or {}

        # Extract fields from Q&A answers (same questions the CSV has)
        linkedin_url = ""
        github_url = ""
        x_handle = row.get("x_handle") or ""
        self_desc = ""
        ai_project = ""
        looking_for_job = ""

        for q, a in answers.items():
            ql = q.lower()
            a_str = str(a).strip() if a else ""
            if not a_str:
                continue
            if "linkedin" in ql:
                linkedin_url = a_str if "linkedin.com" in a_str else f"https://linkedin.com/in/{a_str}"
            elif "github" in ql and "discord" not in ql:
                github_url = a_str if "github.com" in a_str else f"https://github.com/{a_str}"
            elif "twitter" in ql or "x?" in ql or "/x" in ql:
                x_handle = x_handle or a_str
            elif "ai project" in ql or "most proud" in ql:
                ai_project = a_str
            elif "self description" in ql or "1 line" in ql:
                self_desc = a_str
            elif "looking for a job" in ql:
                looking_for_job = a_str

        # Use profile usernames as fallback for URLs
        if not linkedin_url and row.get("linkedin_username"):
            linkedin_url = f"https://linkedin.com/in/{row['linkedin_username']}"
        if not github_url and row.get("github_username"):
            github_url = f"https://github.com/{row['github_username']}"

        # Build Q&A text from remaining answers
        qa_parts: list[str] = []
        skip_keys = {"linkedin", "github", "twitter", "discord", "hear about"}
        for q, a in answers.items():
            a_str = str(a).strip() if a else ""
            if len(a_str) > 10 and not any(sk in q.lower() for sk in skip_keys):
                qa_parts.append(a_str)

        # Infer company from email domain if not a generic provider
        company = ""
        if "@" in email:
            domain = email.split("@")[1]
            if domain not in _GENERIC_EMAIL_DOMAINS and not domain.endswith(".edu"):
                company = domain.split(".")[0].replace("-", " ").title()

        person: dict = {
            "name": name,
            "first_name": first,
            "last_name": last,
            "email": email,
            "linkedin_url": linkedin_url,
            "github_url": github_url,
            "x_handle": x_handle,
            "self_description": self_desc or (row.get("bio") or ""),
            "ai_project": ai_project,
            "looking_for_job": looking_for_job,
            "company": company,
            "role": "",
            "location": row.get("location") or "",
            "_raw_csv": answers,
            "_source": "platform_db",
        }
        people.append(person)

    logger.info("Loaded %d applicants for %r from platform DB", len(people), event_name)
    return people


def list_platform_events(dsn: str, upcoming_only: bool = False, limit: int = 20) -> list[dict]:
    """List events from the platform DB with applicant counts.

    Returns list of {title, date, applicants, approval_required}.
    """
    try:
        import psycopg2
        import psycopg2.extras
    except ImportError:
        return []

    try:
        conn = psycopg2.connect(dsn)
    except Exception:
        return []

    try:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            where = 'WHERE e."startDateTime" > NOW()' if upcoming_only else 'WHERE e."startDateTime" > NOW() - INTERVAL \'90 days\''
            cur.execute(f"""
                SELECT
                    e.title,
                    e."startDateTime"::date as date,
                    COUNT(a.id) as applicants,
                    e."approvalRequired" as approval_required
                FROM "PlatformEvent" e
                LEFT JOIN "EventApplicant" a ON a."eventId" = e.id
                {where}
                GROUP BY e.id, e.title, e."startDateTime", e."approvalRequired"
                HAVING COUNT(a.id) > 0
                ORDER BY e."startDateTime" DESC
                LIMIT %s
            """, (limit,))
            return [dict(r) for r in cur.fetchall()]
    finally:
        conn.close()


def _fallback_match(conn, people, unenriched_indices, email_map, batch_size):
    """Try to match unenriched people by name, LinkedIn, or GitHub username.

    When a match is found, re-run enrichment queries using the matched
    platform email so the person gets full event history / hackathon data.
    """
    # Build lookup keys for unenriched people
    name_to_idx: dict[str, list[int]] = {}
    li_to_idx: dict[str, list[int]] = {}
    gh_to_idx: dict[str, list[int]] = {}

    for i in unenriched_indices:
        p = people[i]
        full_name = f"{(p.get('first_name') or '').lower().strip()} {(p.get('last_name') or '').lower().strip()}".strip()
        if full_name:
            name_to_idx.setdefault(full_name, []).append(i)
        li_user = parse_linkedin_username(p.get("linkedin_url"))
        if li_user:
            li_to_idx.setdefault(li_user, []).append(i)
        gh_user = parse_github_username(p.get("github_url"))
        if gh_user:
            gh_to_idx.setdefault(gh_user, []).append(i)

    if not name_to_idx and not li_to_idx and not gh_to_idx:
        return 0

    names = list(name_to_idx.keys())
    li_users = list(li_to_idx.keys())
    gh_users = list(gh_to_idx.keys())

    query = """
        SELECT LOWER(email) as email,
               TRIM(LOWER(COALESCE("firstName", '') || ' ' || COALESCE("lastName", ''))) as full_name,
               LOWER("linkedinUsername") as linkedin_username,
               LOWER("githubUsername") as github_username
        FROM "UserProfile"
        WHERE TRIM(LOWER(COALESCE("firstName", '') || ' ' || COALESCE("lastName", ''))) = ANY(%s)
           OR LOWER("linkedinUsername") = ANY(%s)
           OR LOWER("githubUsername") = ANY(%s)
    """

    resolved_emails: dict[int, str] = {}  # person index -> platform email

    # Track names that appear in multiple UserProfile rows (ambiguous)
    name_counts: dict[str, int] = defaultdict(int)

    with conn.cursor(cursor_factory=_dict_cursor(conn)) as cur:
        cur.execute(query, (names or [''], li_users or [''], gh_users or ['']))
        db_rows = cur.fetchall()

    # First pass: count name occurrences to detect ambiguity
    for row in db_rows:
        fn = (row.get("full_name") or "").strip()
        if fn:
            name_counts[fn] += 1

    # Second pass: only match unambiguous names (or use LinkedIn/GitHub regardless)
    for row in db_rows:
        platform_email = row["email"]
        matched_indices: list[int] = []

        # Match by name (skip if ambiguous — multiple DB rows share this name)
        fn = (row.get("full_name") or "").strip()
        if fn in name_to_idx and name_counts.get(fn, 0) == 1 and len(name_to_idx[fn]) == 1:
            matched_indices.extend(name_to_idx[fn])
        # Match by LinkedIn (unique identifiers, no ambiguity concern)
        li = (row.get("linkedin_username") or "").strip()
        if li and li in li_to_idx:
            matched_indices.extend(li_to_idx[li])
        # Match by GitHub (unique identifiers, no ambiguity concern)
        gh = (row.get("github_username") or "").strip()
        if gh and gh in gh_to_idx:
            matched_indices.extend(gh_to_idx[gh])

        for idx in set(matched_indices):
            if idx not in resolved_emails:
                resolved_emails[idx] = platform_email

    if not resolved_emails:
        return 0

    # Re-run enrichment for matched people using their platform emails
    fallback_emails = list(set(resolved_emails.values()))
    fallback_email_map: dict[str, list[int]] = {}
    for idx, plat_email in resolved_emails.items():
        fallback_email_map.setdefault(plat_email, []).append(idx)

    _enrich_profile_basics(conn, people, fallback_emails, fallback_email_map, batch_size)
    _enrich_event_history(conn, people, fallback_emails, fallback_email_map, batch_size)
    _enrich_hackathon_data(conn, people, fallback_emails, fallback_email_map, batch_size)

    for idx, plat_email in resolved_emails.items():
        logger.debug("Fallback matched %s -> platform email %s",
                      people[idx].get("name", "?"), plat_email)

    return len(resolved_emails)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _dict_cursor(conn):
    """Return the RealDictCursor class from psycopg2."""
    import psycopg2.extras
    return psycopg2.extras.RealDictCursor


def _batched(items: list, size: int):
    """Yield successive batches from items."""
    for i in range(0, len(items), size):
        yield items[i:i + size]
