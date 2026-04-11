"""CSV loading and ranked CSV/JSON export for cv-rank."""

from __future__ import annotations

import csv
import hashlib
import json
import logging
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Column name normalisation
# ---------------------------------------------------------------------------

# Maps lowercased substrings/variants to canonical internal field names.
# Order matters: first match wins for each canonical name.
_COLUMN_ALIASES: dict[str, list[str]] = {
    "first_name": ["first name", "first_name", "firstname", "fname"],
    "last_name": ["last name", "last_name", "lastname", "lname", "surname"],
    "name": ["full name", "full_name", "fullname", "name"],
    "email": ["email", "e-mail", "email address", "email_address"],
    "linkedin_url": ["linkedin url", "linkedin_url", "linkedin link", "linkedin",
                      "linkedinusername", "linkedin username"],
    "github_url": ["github url", "github_url", "github link", "github",
                    "githubusername", "github username"],
    "company": ["company", "organisation", "organization", "org"],
    "role": ["role", "title", "job title", "job_title", "position"],
    "location": ["location", "city", "country"],
    "x_handle": ["x handle", "x_handle", "xhandle", "twitter", "twitter handle",
                  "twitter_handle", "twitter/x"],
    "self_description": ["self description", "self_description", "description",
                          "bio", "about", "1 line self description"],
    "years_experience": ["years experience", "years_experience", "experience"],
    "education_level": ["education level", "education_level", "education"],
    "is_founder": ["is founder", "is_founder", "founder"],
    "is_big_tech": ["is big tech", "is_big_tech", "big tech", "big_tech"],
    "is_student": ["is student", "is_student", "student"],
    "top_school": ["top school", "top_school"],
    "employment_category": ["employment category", "employment_category"],
    "ai_project": ["ai project", "ai_project"],
    "looking_for_job": ["looking for job", "looking_for_job"],
    "linkedin_headline": ["linkedin headline", "linkedin_headline"],
    "linkedin_followers": ["linkedin followers", "linkedin_followers", "li_follower_count"],
    "linkedin_connections": ["linkedin connections", "linkedin_connections", "li_connection_count"],
    "github_bio": ["github bio", "github_bio", "gh_bio"],
    "github_stars": ["github stars", "github_stars", "gh_api_stars"],
    "github_followers": ["github followers", "github_followers", "gh_api_followers"],
    "github_repos": ["github repos", "github_repos", "gh_api_repos", "gh_api_public_repos"],
    "github_commits_year": ["github commits year", "github_commits_year", "gh_api_commits_year"],
    "publications": ["publications"],
    "certifications": ["certifications"],
    "notable_achievements": ["notable achievements", "notable_achievements", "achievements"],
    "hackathon_submissions": ["hackathon submissions", "hackathon_submissions"],
    "total_cv_events": ["total cv events", "total_cv_events"],
}


def _build_header_map(headers: list[str]) -> dict[str, str]:
    """Map canonical field names to actual CSV header strings.

    Returns a dict ``{canonical_name: original_header}`` for every
    canonical name that has a matching column in *headers*.
    """
    mapped: dict[str, str] = {}
    header_lower = {h: h.lower().strip() for h in headers}

    for canonical, aliases in _COLUMN_ALIASES.items():
        if canonical in mapped:
            continue
        for alias in aliases:
            for orig, low in header_lower.items():
                if low == alias:
                    mapped[canonical] = orig
                    break
            if canonical in mapped:
                break

    # Fallback heuristic: substring matching for columns not yet mapped.
    # This mirrors the original rank.py pattern of ``if 'name' in kl``.
    _SUBSTR_HINTS = {
        "name": "name",
        "email": "email",
        "linkedin_url": "linkedin",
        "github_url": "github",
        "company": "company",
        "role": "title",
        "self_description": "self description",
        "ai_project": "ai project",
        "looking_for_job": "looking for",
        "x_handle": "twitter",
    }
    # Headers already claimed by exact-match aliases should not be re-claimed
    # by substring hints (e.g. "firstName" contains "name" but belongs to first_name)
    _claimed = set(mapped.values())
    for canonical, substr in _SUBSTR_HINTS.items():
        if canonical not in mapped:
            # If we already have first_name + last_name, don't use substring
            # matching for "name" — it grabs unrelated columns like "team's name?"
            if canonical == "name" and "first_name" in mapped and "last_name" in mapped:
                continue
            for orig, low in header_lower.items():
                if substr in low and orig not in _claimed:
                    mapped[canonical] = orig
                    break

    return mapped


def _strip_dict(d: dict[str, str]) -> dict[str, str]:
    """Strip whitespace from all keys and values."""
    return {k.strip(): v.strip() if isinstance(v, str) else v for k, v in d.items()}


import re as _re

_LINKEDIN_SLUG_RE = _re.compile(r"^[a-zA-Z0-9][\w.-]{1,99}$")
_GITHUB_USERNAME_RE = _re.compile(r"^[a-zA-Z0-9][\w.-]{0,38}$")
from cv_rank.utils import _GITHUB_RESERVED  # single source of truth


def _normalise_linkedin_url(raw: str) -> str:
    """Normalise a LinkedIn URL or bare slug to a full profile URL."""
    if not raw:
        return ""
    raw = raw.strip()
    # Already a full URL with /in/ → keep as-is (strip trailing slash)
    if "linkedin.com/in/" in raw.lower():
        return raw.rstrip("/")
    # Looks like linkedin.com but not /in/ (e.g. /feed/) → discard
    if "linkedin.com" in raw.lower():
        return ""
    # Bare slug or partial → wrap in full URL
    slug = raw.lstrip("/").rstrip("/")
    if slug and _LINKEDIN_SLUG_RE.match(slug):
        return f"https://www.linkedin.com/in/{slug}"
    return raw


def _normalise_github_url(raw: str) -> str:
    """Normalise a GitHub URL or bare username to a full profile URL."""
    if not raw:
        return ""
    raw = raw.strip()
    # Already has github.com → normalise to https://
    if "github.com/" in raw.lower():
        url = raw if raw.startswith("http") else f"https://{raw}"
        return url.rstrip("/")
    # Bare username → wrap in full URL
    username = raw.lstrip("@").strip()
    if username and _GITHUB_USERNAME_RE.match(username) and username.lower() not in _GITHUB_RESERVED:
        return f"https://github.com/{username}"
    return raw


# ---------------------------------------------------------------------------
# load_csv
# ---------------------------------------------------------------------------


def load_csv(path: str | Path) -> list[dict[str, Any]]:
    """Load a CSV of applicants and return normalised dicts.

    Features
    --------
    * Auto-detects column names case-insensitively.
    * Maps common header variants to canonical internal names.
    * Synthesises ``name`` from ``first_name`` + ``last_name`` if missing.
    * De-duplicates by email (keeps first occurrence).
    * Strips whitespace from all values.
    * Preserves all original columns in ``_raw_csv``.
    """
    path = Path(path)
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        raw_rows: list[dict[str, str]] = [_strip_dict(r) for r in reader]

    if not raw_rows:
        logger.warning("CSV file is empty: %s", path)
        return []

    headers = list(raw_rows[0].keys())
    col_map = _build_header_map(headers)
    logger.info("Detected column mapping: %s", col_map)
    logger.info("All headers: %s", headers)

    def _get(row: dict, canonical: str, default: str = "") -> str:
        orig = col_map.get(canonical)
        if orig is None:
            return default
        return row.get(orig, default).strip()

    seen_emails: set[str] = set()
    seen_names: set[str] = set()
    people: list[dict[str, Any]] = []

    for row in raw_rows:
        person: dict[str, Any] = {"_raw_csv": {k: v for k, v in row.items() if v}}

        # Core identity fields
        person["first_name"] = _get(row, "first_name")
        person["last_name"] = _get(row, "last_name")
        person["name"] = _get(row, "name")

        # Synthesise name if absent but first+last present
        if not person["name"] and person["first_name"] and person["last_name"]:
            person["name"] = f"{person['first_name']} {person['last_name']}"
        # Derive first/last from full name if those are absent
        if person["name"] and not person["first_name"]:
            parts = person["name"].split(None, 1)
            person["first_name"] = parts[0] if parts else ""
            person["last_name"] = parts[1] if len(parts) > 1 else ""

        # Skip rows with no usable name
        if not person["name"]:
            continue

        # Contact / links
        person["email"] = _get(row, "email").lower()
        person["linkedin_url"] = _normalise_linkedin_url(_get(row, "linkedin_url"))
        person["github_url"] = _normalise_github_url(_get(row, "github_url"))
        if person["email"]:
            stable_key = person["email"]
        else:
            raw_fingerprint = json.dumps(person["_raw_csv"], sort_keys=True, separators=(",", ":"))
            stable_key = f"{person['name'].lower().strip()}::{raw_fingerprint}"
        digest = hashlib.sha1(stable_key.encode("utf-8")).hexdigest()[:12]
        person["candidate_id"] = f"cand_{digest}"

        # Professional info
        person["company"] = _get(row, "company")
        person["role"] = _get(row, "role")
        person["location"] = _get(row, "location")
        person["x_handle"] = _get(row, "x_handle")
        person["self_description"] = _get(row, "self_description")
        person["ai_project"] = _get(row, "ai_project")

        # Numeric / boolean profile fields
        person["years_experience"] = _get(row, "years_experience")
        person["is_founder"] = _get(row, "is_founder")
        person["is_big_tech"] = _get(row, "is_big_tech")
        person["is_student"] = _get(row, "is_student")
        person["top_school"] = _get(row, "top_school")
        person["education_level"] = _get(row, "education_level")
        person["employment_category"] = _get(row, "employment_category")
        person["looking_for_job"] = _get(row, "looking_for_job")

        # Social / GitHub stats
        person["linkedin_headline"] = _get(row, "linkedin_headline")
        person["linkedin_followers"] = _get(row, "linkedin_followers")
        person["linkedin_connections"] = _get(row, "linkedin_connections")
        person["github_bio"] = _get(row, "github_bio")
        person["github_stars"] = _get(row, "github_stars")
        person["github_followers"] = _get(row, "github_followers")
        person["github_repos"] = _get(row, "github_repos")
        person["github_commits_year"] = _get(row, "github_commits_year")

        # Event history
        person["publications"] = _get(row, "publications")
        person["certifications"] = _get(row, "certifications")
        person["notable_achievements"] = _get(row, "notable_achievements")
        person["hackathon_submissions"] = _get(row, "hackathon_submissions")
        person["total_cv_events"] = _get(row, "total_cv_events")

        # De-dup by email (keep first occurrence)
        email = person["email"]
        if email:
            if email in seen_emails:
                logger.debug("Skipping duplicate email: %s (%s)", email, person["name"])
                continue
            seen_emails.add(email)

        # De-dup by name (keep first occurrence, case-insensitive) — same
        # person registering twice with different emails or casing still
        # causes dict-key collisions later in the pipeline.
        name = person["name"]
        name_key = name.lower().strip()
        if name_key in seen_names:
            logger.info("Skipping duplicate name: %s (email=%s, kept earlier entry)", name, email)
            continue
        seen_names.add(name_key)

        people.append(person)

    logger.info("Loaded %d people from %s", len(people), path)

    # Detect name collisions early — the pipeline uses name as a unique key
    # everywhere (scoring, Swiss, combine, export).  Duplicate names cause
    # silent data loss: dict comprehensions drop earlier entries.
    name_counts: dict[str, int] = {}
    for p in people:
        n = p.get("name", "?")
        name_counts[n] = name_counts.get(n, 0) + 1
    dupes = {n: c for n, c in name_counts.items() if c > 1}
    if dupes:
        logger.warning(
            "DUPLICATE NAMES: %d names appear more than once. The pipeline "
            "uses name as a unique key — duplicates will cause scoring/ranking "
            "collisions: %s",
            len(dupes),
            ", ".join(f'"{n}" (x{c})' for n, c in sorted(dupes.items())[:10]),
        )

    return people


# ---------------------------------------------------------------------------
# validate_csv
# ---------------------------------------------------------------------------


def validate_csv(path: str | Path) -> tuple[bool, list[str]]:
    """Validate a CSV file for use with cv-rank.

    Returns ``(is_valid, messages)`` where *messages* is a list of
    warnings and errors.  The file is considered valid if it has at least
    one data row and a usable name column.
    """
    path = Path(path)
    messages: list[str] = []

    # Existence
    if not path.exists():
        return False, [f"File not found: {path}"]
    if not path.is_file():
        return False, [f"Not a file: {path}"]

    # Readability
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames or []
            rows = list(reader)
    except Exception as exc:
        return False, [f"Cannot read CSV: {exc}"]

    if not headers:
        return False, ["CSV has no headers."]

    if not rows:
        messages.append("WARNING: CSV has headers but no data rows.")
        return False, messages

    # Column check
    col_map = _build_header_map(list(headers))

    has_name = "name" in col_map
    has_first_last = "first_name" in col_map and "last_name" in col_map

    if not has_name and not has_first_last:
        messages.append(
            "ERROR: No usable name column found. "
            "Need 'Name' or both 'First Name' and 'Last Name'."
        )
        return False, messages

    if not has_name and has_first_last:
        messages.append(
            "INFO: No 'Name' column; will synthesise from First_Name + Last_Name."
        )

    # Helpful warnings for missing-but-useful columns
    useful = ["email", "linkedin_url", "github_url", "company", "role"]
    for col in useful:
        if col not in col_map:
            messages.append(f"WARNING: No '{col}' column detected.")

    # Check for email column (strongly recommended for de-dup)
    if "email" not in col_map:
        messages.append("WARNING: No email column; de-duplication will be skipped.")

    messages.append(f"OK: {len(rows)} rows, {len(headers)} columns.")
    return True, messages


# ---------------------------------------------------------------------------
# export_ranked_csv
# ---------------------------------------------------------------------------

# The 45-column output format (two columns are event-dynamic).
_STATIC_COLUMNS = [
    "Rank",
    "Name",
    "First_Name",
    "Last_Name",
    "Email",
    "Specific_WHY",
    "Best_Number",
    "Company",
    "Role",
    "Location",
    "Verdict",
    "Combined_Score",
    "Swiss_Wins",
    "Swiss_Losses",
    "Pointwise_Score",
    "Win_Rate",
    "LinkedIn_URL",
    "LinkedIn_Headline",
    "LinkedIn_Followers",
    "LinkedIn_Connections",
    "GitHub_URL",
    "GitHub_Bio",
    "GitHub_Stars",
    "GitHub_Followers",
    "GitHub_Repos",
    "GitHub_Commits_Year",
    "X_Handle",
    "Years_Experience",
    "Is_Founder",
    "Is_Big_Tech",
    "Is_Student",
    "Top_School",
    "Education_Level",
    "Employment_Category",
    "Self_Description",
    "AI_Project",
    "Looking_For_Job",
    "Total_CV_Events",
    "Hackathon_Submissions",
    "Judging_Scores_Avg",
    "P_Show",
]

_TRAILING_COLUMNS = [
    "Publications",
    "Certifications",
    "Notable_Achievements",
]


def _safe_int(val: Any, default: int = 0) -> int:
    """Coerce *val* to int, returning *default* on failure."""
    if val is None or val == "":
        return default
    try:
        return int(float(val))
    except (ValueError, TypeError):
        return default


def _safe_float(val: Any, default: float = 0.0) -> float:
    """Coerce *val* to float, returning *default* on failure."""
    if val is None or val == "":
        return default
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _count_items(val: Any) -> int:
    """Count list items or comma-separated items in a string."""
    if isinstance(val, list):
        return len(val)
    if isinstance(val, (int, float)):
        return int(val)
    if isinstance(val, str):
        val = val.strip()
        if not val or val == "0":
            return 0
        # Comma- or semicolon-separated
        return len([x for x in val.replace(";", ",").split(",") if x.strip()])
    return 0


def _join_items(val: Any, sep: str = "; ") -> str:
    """Join a list or return string as-is."""
    if isinstance(val, list):
        return sep.join(str(x) for x in val)
    if val is None:
        return ""
    return str(val)


def _compute_win_rate(wins: int, losses: int) -> str:
    """Return win rate as 'XX%' or '' if no games played."""
    total = wins + losses
    if total == 0:
        return ""
    pct = (wins / total) * 100
    return f"{pct:.0f}%"


# Mapping from canonical CSV field names to enriched field name alternatives.
# The export checks the canonical name first, then falls back to these.
_ENRICHED_FIELD_ALIASES: dict[str, list[str]] = {
    "linkedin_followers": ["li_follower_count"],
    "linkedin_connections": ["li_connection_count"],
    "github_stars": ["gh_api_stars"],
    "github_followers": ["gh_api_followers"],
    "github_repos": ["gh_api_repos", "gh_api_public_repos"],
    "github_commits_year": ["gh_api_commits_year"],
    "location": ["li_country"],
    "is_big_tech": ["is_in_big_tech"],
    "total_cv_events": ["total_events_applied"],
    "role": ["title", "job_title"],
}


def _get_field(person: dict, canonical: str, default: Any = "") -> Any:
    """Get a field value, checking enriched name aliases if canonical is empty."""
    val = person.get(canonical)
    if val is not None and val != "":
        return val
    for alias in _ENRICHED_FIELD_ALIASES.get(canonical, []):
        val = person.get(alias)
        if val is not None and val != "":
            return val
    return default


def _compute_judging_avg(person: dict) -> str:
    """Compute average of judging_scores_received if present."""
    scores = person.get("judging_scores_received")
    if not scores:
        return "0"
    if isinstance(scores, list) and scores:
        nums = [_safe_float(s.get("pct", 0) if isinstance(s, dict) else s) for s in scores]
        if nums:
            return f"{sum(nums) / len(nums):.1f}"
    if isinstance(scores, (int, float)):
        return str(scores)
    return "0"


def export_ranked_csv(
    people: list[dict[str, Any]],
    scores: dict[str, float],
    swiss_records: dict[str, dict[str, int]],
    pointwise_scores: dict[str, float],
    quality_results: dict[str, dict[str, Any]],
    config: dict[str, Any],
    output_path: str | Path,
) -> Path:
    """Export ranked results to CSV and JSON.

    Parameters
    ----------
    people:
        List of person dicts (as returned by ``load_csv``).
    scores:
        Mapping of ``name -> combined_score``.
    swiss_records:
        Mapping of ``name -> {"wins": int, "losses": int}``.
    pointwise_scores:
        Mapping of ``name -> pointwise_score``.
    quality_results:
        Mapping of ``name -> {"verdict": str, "why": str, ...}``.
    config:
        Run configuration dict.  Must contain ``config["event"]["name"]``
        for the dynamic column names.
    output_path:
        Where to write the CSV.  JSON is written alongside with ``.json``
        suffix.

    Returns
    -------
    Path to the written CSV file.
    """
    output_path = Path(output_path)
    event_name = config.get("event", {}).get("name", "Event")
    # Sanitise for column name: spaces to underscores, strip non-alnum
    event_col = event_name.replace(" ", "_").replace("-", "_")

    applied_col = f"Applied_To_{event_col}"
    status_col = f"{event_col}_Status"

    fieldnames = _STATIC_COLUMNS + [applied_col, status_col] + _TRAILING_COLUMNS

    # Index people by name for lookup
    people_by_name = {p["name"]: p for p in people}

    # Sort by combined score descending
    sorted_names = sorted(scores.keys(), key=lambda n: scores.get(n, 0), reverse=True)

    target_accepts = config.get("event", {}).get("target_accepts", len(sorted_names))

    rows: list[dict[str, Any]] = []
    for rank_idx, name in enumerate(sorted_names, start=1):
        person = people_by_name.get(name, {})
        swiss = swiss_records.get(name, {})
        qc = quality_results.get(name, {})

        wins = _safe_int(swiss.get("wins", 0))
        losses = _safe_int(swiss.get("losses", 0))
        combined = _safe_float(scores.get(name, 0))
        pw = _safe_float(pointwise_scores.get(name, 0))

        status = "ACCEPT" if rank_idx <= target_accepts else "WAITLIST"

        row: dict[str, Any] = {
            "Rank": rank_idx,
            "Name": name,
            "First_Name": person.get("first_name", ""),
            "Last_Name": person.get("last_name", ""),
            "Email": person.get("email", ""),
            "Specific_WHY": qc.get("specific_why", qc.get("why", "")),
            "Best_Number": qc.get("best_number", ""),
            "Company": person.get("company", ""),
            "Role": _get_field(person, "role"),
            "Location": _get_field(person, "location"),
            "Verdict": qc.get("verdict", ""),
            "Combined_Score": f"{combined:.1f}" if combined is not None else "",
            "Swiss_Wins": wins,
            "Swiss_Losses": losses,
            "Pointwise_Score": f"{pw:.1f}" if pw is not None else "",
            "Win_Rate": _compute_win_rate(wins, losses),
            "LinkedIn_URL": person.get("linkedin_url", ""),
            "LinkedIn_Headline": person.get("linkedin_headline", ""),
            "LinkedIn_Followers": _get_field(person, "linkedin_followers", 0),
            "LinkedIn_Connections": _get_field(person, "linkedin_connections", 0),
            "GitHub_URL": person.get("github_url", ""),
            "GitHub_Bio": person.get("github_bio", ""),
            "GitHub_Stars": _get_field(person, "github_stars", 0),
            "GitHub_Followers": _get_field(person, "github_followers", 0),
            "GitHub_Repos": _get_field(person, "github_repos", 0),
            "GitHub_Commits_Year": _get_field(person, "github_commits_year", 0),
            "X_Handle": person.get("x_handle", ""),
            "Years_Experience": person.get("years_experience", 0),
            "Is_Founder": person.get("is_founder", ""),
            "Is_Big_Tech": _get_field(person, "is_big_tech"),
            "Is_Student": person.get("is_student", ""),
            "Top_School": person.get("top_school", ""),
            "Education_Level": person.get("education_level", ""),
            "Employment_Category": person.get("employment_category", ""),
            "Self_Description": person.get("self_description", ""),
            "AI_Project": person.get("ai_project", ""),
            "Looking_For_Job": person.get("looking_for_job", ""),
            "Total_CV_Events": _get_field(person, "total_cv_events", 0),
            "Hackathon_Submissions": _count_items(person.get("hackathon_submissions")),
            "Judging_Scores_Avg": _compute_judging_avg(person),
            "P_Show": f"{person['p_show']:.0%}" if person.get("p_show") is not None else "",
            applied_col: True,
            status_col: status,
            "Publications": _count_items(person.get("publications")),
            "Certifications": _count_items(person.get("certifications")),
            "Notable_Achievements": _join_items(
                person.get("notable_achievements"), "; "
            ),
        }

        rows.append(row)

    # Write CSV
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(rows)

    logger.info("Wrote %d rows to %s", len(rows), output_path)

    # Write JSON alongside
    json_path = output_path.with_suffix(".json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(rows, f, indent=2, default=str, ensure_ascii=False)

    logger.info("Wrote JSON to %s", json_path)

    # Summary
    from collections import Counter

    verdicts = Counter(r.get("Verdict", "") for r in rows[:target_accepts])
    statuses = Counter(r[status_col] for r in rows)
    logger.info(
        "Export summary: %d total, statuses=%s, top-%d verdicts=%s",
        len(rows),
        dict(statuses),
        target_accepts,
        dict(verdicts),
    )

    return output_path


# ---------------------------------------------------------------------------
# export_clean_csv — simplified output with only useful columns
# ---------------------------------------------------------------------------

_CLEAN_COLUMNS = [
    "Rank",
    "Name",
    "Email",
    "Verdict",
    "Combined_Score",
    "Specific_WHY",
    "Best_Number",
    "Company",
    "Role",
    "LinkedIn_URL",
    "LinkedIn_Headline",
    "GitHub_URL",
    "Notable_Achievements",
    "Self_Description",
    "AI_Project",
    "P_Show",
]


def _normalise_github_export(url: str) -> str:
    """Ensure GitHub URL is always https://github.com/username format."""
    if not url:
        return ""
    url = url.strip().rstrip("/")
    # discard junk values
    if url.lower() in ("n/a", "na", "none", "-", "n.a", "n.a."):
        return ""
    # must contain github.com to be valid
    if "github.com" not in url.lower():
        return ""
    # strip query params and fragments
    for sep in ("?", "#"):
        if sep in url:
            url = url[:url.index(sep)]
    # fix double-scheme like "https://Https://github.com/user"
    import re
    url = re.sub(r'^https?://https?://', 'https://', url, flags=re.IGNORECASE)
    # normalise to https://github.com
    if "github.com" in url.lower():
        # strip any scheme and rebuild
        cleaned = re.sub(r'^https?://', '', url, flags=re.IGNORECASE)
        cleaned = cleaned.lstrip("/")
        # ensure github.com is lowercase
        cleaned = re.sub(r'^github\.com', 'github.com', cleaned, flags=re.IGNORECASE)
        url = f"https://{cleaned}"
    # strip www.
    url = url.replace("://www.", "://")
    return url.rstrip("/")


def apply_rankings_to_original(
    ranked_csv_path: str | Path,
    original_csv_path: str | Path,
    output_path: str | Path | None = None,
    columns: list[str] | None = None,
    into_col: str | None = None,
) -> tuple[Path, int, int]:
    """Stamp cv-rank decisions back onto the original applicant CSV.

    Parameters
    ----------
    ranked_csv_path:
        Path to RANKED.csv or RANKED_CLEAN.csv.
    original_csv_path:
        Path to the original applicant CSV (columns and order preserved).
    output_path:
        Where to write. Defaults to ``{original_stem}_applied.csv``.
    columns:
        Which RANKED.csv columns to copy. Defaults to Rank, Verdict, and
        the auto-detected ``*_Status`` column.
    into_col:
        If set, write the Status value into this existing column in the
        original CSV instead of appending a new one.

    Returns
    -------
    (output_path, matched_count, unmatched_count)
    """
    ranked_csv_path = Path(ranked_csv_path)
    original_csv_path = Path(original_csv_path)
    if output_path is None:
        output_path = original_csv_path.parent / f"{original_csv_path.stem}_applied.csv"
    else:
        output_path = Path(output_path)

    # Read RANKED.csv
    with open(ranked_csv_path, encoding="utf-8", newline="") as f:
        ranked_rows = list(csv.DictReader(f))

    if not ranked_rows:
        logger.warning("RANKED.csv is empty: %s", ranked_csv_path)
        return output_path, 0, 0

    # Auto-detect *_Status column
    status_col = None
    for col in ranked_rows[0]:
        if col.endswith("_Status") and col != "Status":
            status_col = col
            break

    # Default columns
    if columns is None:
        columns = ["Rank", "Verdict"]
        if status_col:
            columns.append(status_col)

    # Validate requested columns exist
    available = set(ranked_rows[0].keys())
    missing = [c for c in columns if c not in available]
    if missing:
        logger.warning("Columns not found in RANKED.csv: %s (available: %s)",
                       missing, sorted(available))
        columns = [c for c in columns if c in available]

    # Build lookup: email -> row, name -> row
    by_email: dict[str, dict] = {}
    by_name: dict[str, dict] = {}
    for row in ranked_rows:
        email = row.get("Email", "").strip().lower()
        name = row.get("Name", "").strip().lower()
        if email:
            by_email[email] = row
        if name:
            by_name[name] = row

    # Read original CSV
    with open(original_csv_path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        orig_fieldnames = list(reader.fieldnames or [])
        orig_rows = list(reader)

    # Detect email column in original (fuzzy)
    email_aliases = {"email", "e-mail", "email address", "email_address"}
    orig_email_col = None
    for col in orig_fieldnames:
        if col.strip().lower() in email_aliases:
            orig_email_col = col
            break

    # Detect name columns in original
    name_aliases = {"name", "full name", "full_name", "applicant name"}
    first_aliases = {"first name", "first_name", "firstname"}
    last_aliases = {"last name", "last_name", "lastname"}
    orig_name_col = None
    orig_first_col = None
    orig_last_col = None
    for col in orig_fieldnames:
        cl = col.strip().lower()
        if cl in name_aliases:
            orig_name_col = col
        if cl in first_aliases:
            orig_first_col = col
        if cl in last_aliases:
            orig_last_col = col

    # Build output fieldnames
    out_fieldnames = list(orig_fieldnames)
    append_cols = []
    if into_col:
        # --into mode: write status into existing column, append other columns
        for c in columns:
            if c == status_col or c.endswith("_Status"):
                continue  # this one goes into --into col
            if c not in out_fieldnames:
                append_cols.append(c)
                out_fieldnames.append(c)
    else:
        for c in columns:
            if c not in out_fieldnames:
                append_cols.append(c)
                out_fieldnames.append(c)

    # Match and stamp
    matched = 0
    unmatched = 0
    out_rows = []
    for orig_row in orig_rows:
        # Try email match
        ranked_row = None
        if orig_email_col:
            email = orig_row.get(orig_email_col, "").strip().lower()
            if email:
                ranked_row = by_email.get(email)

        # Fallback: name match
        if ranked_row is None:
            name = ""
            if orig_name_col:
                name = orig_row.get(orig_name_col, "").strip().lower()
            elif orig_first_col and orig_last_col:
                first = orig_row.get(orig_first_col, "").strip()
                last = orig_row.get(orig_last_col, "").strip()
                name = f"{first} {last}".strip().lower()
            if name:
                ranked_row = by_name.get(name)

        row_out = dict(orig_row)
        if ranked_row:
            matched += 1
            if into_col:
                # Write status into the specified column
                status_val = ranked_row.get(status_col, "") if status_col else ""
                row_out[into_col] = status_val
                # Append other columns
                for c in columns:
                    if c == status_col or c.endswith("_Status"):
                        continue
                    row_out[c] = ranked_row.get(c, "")
            else:
                for c in columns:
                    row_out[c] = ranked_row.get(c, "")
        else:
            unmatched += 1

        out_rows.append(row_out)

    # Write
    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=out_fieldnames, extrasaction="ignore")
        writer.writeheader()
        writer.writerows(out_rows)

    logger.info("Applied rankings: %d matched, %d unmatched → %s",
                matched, unmatched, output_path)
    return output_path, matched, unmatched


def export_clean_csv(ranked_csv_path: str | Path, output_path: str | Path | None = None) -> Path:
    """Read a full RANKED.csv and write a clean version with only useful columns.

    Parameters
    ----------
    ranked_csv_path:
        Path to the 45-column RANKED.csv produced by ``export_ranked_csv``.
    output_path:
        Where to write the clean CSV. Defaults to ``RANKED_CLEAN.csv`` next
        to the input file.

    Returns
    -------
    Path to the written clean CSV.
    """
    ranked_csv_path = Path(ranked_csv_path)
    if output_path is None:
        output_path = ranked_csv_path.parent / "RANKED_CLEAN.csv"
    else:
        output_path = Path(output_path)

    with open(ranked_csv_path, encoding="utf-8", newline="") as f:
        reader = csv.DictReader(f)
        rows = list(reader)

    if not rows:
        logger.warning("RANKED.csv is empty: %s", ranked_csv_path)
        return output_path

    # detect the dynamic status column (e.g. "OpenEnv_Hackathon_Status")
    status_col = None
    for col in rows[0]:
        if col.endswith("_Status") and col != "Status":
            status_col = col
            break

    fieldnames = list(_CLEAN_COLUMNS)
    if status_col:
        fieldnames.append(status_col)

    output_path.parent.mkdir(parents=True, exist_ok=True)
    with open(output_path, "w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for row in rows:
            clean_row = {}
            for col in fieldnames:
                val = row.get(col, "")
                if col == "GitHub_URL":
                    val = _normalise_github_export(val)
                clean_row[col] = val
            writer.writerow(clean_row)

    logger.info("Wrote clean CSV: %d rows, %d columns → %s",
                len(rows), len(fieldnames), output_path)
    return output_path
