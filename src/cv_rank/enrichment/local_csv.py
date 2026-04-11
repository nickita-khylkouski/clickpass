"""
Local CSV enrichment: load judging scores, submissions, and submission details.

Reads from configured file paths or falls back to the legacy
``data/opus_applicants/`` directory layout.

Ported from hackathon-elo/pipeline_v2/rank.py
``load_local_hackathon_data`` and ``aggregate_judging_scores``.
"""

import csv
import logging
from collections import defaultdict
from pathlib import Path
from typing import Any

logger = logging.getLogger("cv_rank")

# Legacy directory (relative to cwd, matching original pipeline layout).
_LEGACY_DATA_DIR = Path("data")

# Submission-detail fields worth keeping.
_USEFUL_FIELDS = frozenset({
    "Project Description",
    "Project Title",
    "Demo Video",
    "AI tools used",
    "GitHub Repository (make sure is public)",
    "Public GitHub Repository",
    "Project Demo Video",
    "1-minute Demo Video (drive works)",
    "Live Project URL",
    "Project Website",
})


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_csv_safe(path: Path) -> list[dict[str, str]]:
    """Load a CSV file if it exists; return ``[]`` otherwise."""
    if not path.exists():
        return []
    with open(path) as f:
        return list(csv.DictReader(f))


def _resolve_paths(config: dict) -> dict[str, Path | None]:
    """Resolve CSV file paths from config or legacy fallback.

    Returns a dict with keys ``profiles``, ``judging``, ``submissions``,
    ``details`` mapping to ``Path`` objects (or ``None`` if unavailable).
    """
    csv_cfg = config.get("enrichment", {}).get("local_csv", {})
    path_list: list[dict[str, str]] = csv_cfg.get("paths", [])

    # Build a flat mapping from the list-of-dicts config format:
    #   paths:
    #     - profiles: "/path/to/profiles.csv"
    #     - judging: "/path/to/judging_scores.csv"
    configured: dict[str, str] = {}
    for entry in path_list:
        if isinstance(entry, dict):
            configured.update(entry)

    result: dict[str, Path | None] = {}
    keys_and_legacy = {
        "profiles": [
            _LEGACY_DATA_DIR / "opus_applicants" / "profiles.csv",
            _LEGACY_DATA_DIR / "opus_applicants" / "profiles_fresh.csv",
        ],
        "judging": [
            _LEGACY_DATA_DIR / "opus_applicants" / "prior_judging_scores.csv",
        ],
        "submissions": [
            _LEGACY_DATA_DIR / "opus_applicants" / "prior_submissions.csv",
        ],
        "details": [
            _LEGACY_DATA_DIR / "opus_applicants" / "prior_submission_details.csv",
        ],
    }

    for key, legacy_paths in keys_and_legacy.items():
        if key in configured and configured[key]:
            result[key] = Path(configured[key])
        else:
            # Use the first legacy path that exists, or the first one
            # (will silently return [] when loaded).
            found = None
            for lp in legacy_paths:
                if lp.exists():
                    found = lp
                    break
            result[key] = found if found else (legacy_paths[0] if legacy_paths else None)

    return result


# ---------------------------------------------------------------------------
# Core data loading
# ---------------------------------------------------------------------------

def _load_hackathon_data(
    paths: dict[str, Path | None],
) -> tuple[dict[str, list], dict[str, list], dict[str, list]]:
    """Load judging scores, submissions, and details from local CSVs.

    Returns three dicts keyed by email:
      judging_by_email:     ``{email: [{event, team, criteria, weight, score, ...}, ...]}``
      submissions_by_email: ``{email: [{team, event, placement, ...}, ...]}``
      details_by_email:     ``{email: [{event, team, field, value}, ...]}``
    """
    # Step 1: Build userId -> email mapping from profiles
    uid_to_email: dict[str, str] = {}

    profiles_path = paths.get("profiles")
    if profiles_path:
        # Check both the configured path and the "fresh" variant
        for p in _profile_paths(profiles_path):
            for r in _load_csv_safe(p):
                uid = r.get("userId", "")
                email = r.get("email", "").lower().strip()
                if uid and email:
                    uid_to_email[uid] = email

    if not uid_to_email:
        logger.debug("  No userId->email mapping found; local CSV enrichment skipped")
        return {}, {}, {}

    logger.info("  Mapped %d userIds to emails from profiles", len(uid_to_email))

    # Step 2: Judging scores
    judging_by_email: dict[str, list] = defaultdict(list)
    judging_path = paths.get("judging")
    if judging_path:
        for r in _load_csv_safe(judging_path):
            email = uid_to_email.get(r.get("userId", ""))
            if not email:
                continue
            try:
                weight = int(float(r["weight"])) if r.get("weight") else 0
            except (ValueError, TypeError):
                weight = 0
            try:
                score = int(float(r["score"])) if r.get("score") else 0
            except (ValueError, TypeError):
                score = 0
            judging_by_email[email].append({
                "event": r.get("event_name", ""),
                "team": r.get("teamName", ""),
                "criteria": r.get("criteria_name", ""),
                "weight": weight,
                "score": score,
                "finalist_rec": r.get("finalistRecommendation", "") == "True",
            })

    # Step 3: Submissions (teams, placements)
    submissions_by_email: dict[str, list] = defaultdict(list)
    subs_path = paths.get("submissions")
    if subs_path:
        for r in _load_csv_safe(subs_path):
            email = uid_to_email.get(r.get("userId", ""))
            if not email:
                continue
            submissions_by_email[email].append({
                "team": r.get("teamName", ""),
                "event": r.get("event_name", ""),
                "event_date": r.get("event_date", ""),
                "placement": r.get("placement", ""),
            })

    # Step 4: Submission details (project descriptions, demos, etc.)
    details_by_email: dict[str, list] = defaultdict(list)
    details_path = paths.get("details")
    if details_path:
        for r in _load_csv_safe(details_path):
            email = uid_to_email.get(r.get("userId", ""))
            if not email:
                continue
            field = r.get("field_name", "")
            val = r.get("field_value", "")
            if field in _USEFUL_FIELDS and val:
                details_by_email[email].append({
                    "event": r.get("event_name", ""),
                    "team": r.get("teamName", ""),
                    "field": field,
                    "value": val[:500],
                })

    return dict(judging_by_email), dict(submissions_by_email), dict(details_by_email)


def _profile_paths(base: Path) -> list[Path]:
    """Return profile CSV paths to try (original + *_fresh* variant)."""
    paths = [base]
    # Also try the "fresh" variant if base is named profiles.csv
    if base.stem == "profiles":
        fresh = base.parent / f"{base.stem}_fresh{base.suffix}"
        paths.append(fresh)
    return paths


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def aggregate_judging_scores(scores_list: list[dict]) -> list[dict]:
    """Aggregate raw per-criteria scores into per-event summaries.

    Parameters
    ----------
    scores_list:
        List of raw score dicts, each with keys ``event``, ``team``,
        ``criteria``, ``weight``, ``score``, and optionally ``finalist_rec``.

    Returns
    -------
    list[dict]
        Per-event summaries sorted by weighted percentage (descending)::

            [{
                "event": str,
                "team": str,
                "weighted_score": float,
                "max_score": float,
                "pct": float,
                "criteria": [{"name": str, "weight": int, "score": int}, ...],
                "finalist_rec": bool,
            }, ...]
    """
    by_event: dict[tuple, dict[str, Any]] = defaultdict(
        lambda: {"criteria": [], "team": "", "finalist_rec": False}
    )
    for s in scores_list:
        key = (s["event"], s["team"])
        entry = by_event[key]
        entry["event"] = s["event"]
        entry["team"] = s["team"]
        entry["criteria"].append({
            "name": s["criteria"],
            "weight": s["weight"],
            "score": s["score"],
        })
        if s.get("finalist_rec"):
            entry["finalist_rec"] = True

    result = []
    for _key, entry in by_event.items():
        weighted = sum(c["score"] * c["weight"] for c in entry["criteria"])
        max_weighted = sum(10 * c["weight"] for c in entry["criteria"])
        pct = (weighted / max_weighted * 100) if max_weighted > 0 else 0
        result.append({
            "event": entry["event"],
            "team": entry["team"],
            "weighted_score": weighted,
            "max_score": max_weighted,
            "pct": round(pct, 1),
            "criteria": entry["criteria"],
            "finalist_rec": entry["finalist_rec"],
        })

    return sorted(result, key=lambda x: -x["pct"])


def enrich_from_local_csv(people: list[dict], config: dict) -> list[dict]:
    """Enrich *people* with judging scores and submissions from local CSVs.

    Reads file paths from ``config["enrichment"]["local_csv"]["paths"]``.
    Falls back to legacy ``data/opus_applicants/`` paths if the config
    key is empty and the files exist on disk.

    Parameters
    ----------
    people:
        List of person dicts (mutated in place and returned).
    config:
        Full configuration dict.

    Returns
    -------
    list[dict]
        The same *people* list, enriched with hackathon history fields.
    """
    logger.info("  Loading local hackathon data (judging scores, submissions)...")

    paths = _resolve_paths(config)
    judging_by_email, submissions_by_email, details_by_email = _load_hackathon_data(paths)

    judging_enriched = 0
    submission_enriched = 0

    for p in people:
        email = p.get("email", "").lower().strip()
        if not email:
            continue

        # Judging scores
        raw_scores = judging_by_email.get(email)
        if raw_scores:
            p["judging_scores_received"] = aggregate_judging_scores(raw_scores)
            judging_enriched += 1

        # Submissions + placements
        subs = submissions_by_email.get(email)
        if subs:
            p["hackathon_submissions"] = subs
            submission_enriched += 1
            # Track placements
            placements = [s["placement"] for s in subs if s.get("placement")]
            if placements:
                p["hackathon_placements"] = placements

        # Submission details (project descriptions, demos)
        dets = details_by_email.get(email)
        if dets:
            p["submission_details"] = dets

    logger.info(
        "  Local data: %d with judging scores, %d with submissions",
        judging_enriched,
        submission_enriched,
    )

    return people
