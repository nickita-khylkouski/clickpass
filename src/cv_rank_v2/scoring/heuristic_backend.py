"""Deterministic native ranking backend for cv-rank v2.

This backend is intended for smoke tests, local validation, and offline
development. It does not make model calls. Instead it builds a ranking from
structured evidence signals so the v2 pipeline can run end to end without
external APIs.
"""

from __future__ import annotations

from typing import Any

from cv_rank.scoring.bradley_terry import compute_bt_strengths

from .borderline import identify_borderline_applicants
from .combine import combine_rankings

_TOP_COMPANIES = {
    "anthropic": 26,
    "openai": 26,
    "deepmind": 26,
    "google": 22,
    "meta": 20,
    "apple": 20,
    "microsoft": 18,
    "amazon": 16,
    "nvidia": 22,
    "perplexity": 18,
    "xai": 18,
    "scale ai": 16,
    "stripe": 14,
    "uber": 12,
    "stanford": 14,
    "mit": 14,
}

_SENIORITY_TOKENS = {
    "founder": 18,
    "co-founder": 18,
    "cto": 20,
    "chief": 18,
    "vp": 16,
    "director": 14,
    "head": 14,
    "principal": 14,
    "staff": 12,
    "senior": 8,
    "researcher": 8,
    "engineer": 6,
    "scientist": 8,
    "student": -8,
    "intern": -10,
    "aspiring": -8,
}


def _contains_any(text: str, tokens: set[str]) -> bool:
    lowered = text.lower()
    return any(token in lowered for token in tokens)


def _company_score(person: dict[str, Any]) -> float:
    company = str(person.get("company") or "").lower()
    score = 0.0
    for token, value in _TOP_COMPANIES.items():
        if token in company:
            score = max(score, float(value))
    return score


def _role_score(person: dict[str, Any]) -> float:
    role = " ".join(
        str(value or "")
        for value in (person.get("role"), person.get("title"), person.get("current_job"))
    ).lower()
    total = 0.0
    for token, value in _SENIORITY_TOKENS.items():
        if token in role:
            total += float(value)
    return max(-12.0, min(24.0, total))


def _numeric(value: Any) -> float:
    if value in (None, ""):
        return 0.0
    try:
        return float(value)
    except (TypeError, ValueError):
        return 0.0


def _count_items(value: Any) -> int:
    if value in (None, ""):
        return 0
    if isinstance(value, list):
        return len(value)
    if isinstance(value, tuple):
        return len(value)
    if isinstance(value, str):
        return len([item for item in value.replace(";", ",").split(",") if item.strip()])
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return 0


def _signal_breakdown(person: dict[str, Any]) -> dict[str, float]:
    years = min(_numeric(person.get("years_experience")), 15.0)
    github_stars = min(_numeric(person.get("gh_api_stars") or person.get("github_stars")), 5000.0)
    github_repos = min(_numeric(person.get("gh_api_repos") or person.get("github_repos")), 200.0)
    github_commits = min(_numeric(person.get("gh_api_commits_year") or person.get("github_commits_year")), 3000.0)
    github_followers = min(_numeric(person.get("gh_api_followers") or person.get("github_followers")), 5000.0)
    linkedin_followers = min(_numeric(person.get("li_follower_count") or person.get("linkedin_followers")), 50000.0)
    linkedin_connections = min(_numeric(person.get("li_connection_count") or person.get("linkedin_connections")), 50000.0)
    publications = max(
        _count_items(person.get("publications_detail")),
        _count_items(person.get("publications")),
        int(_numeric(person.get("publications"))),
    )
    certifications = max(
        _count_items(person.get("certifications")),
        int(_numeric(person.get("certifications"))),
    )
    projects = max(_count_items(person.get("projects")), _count_items(person.get("top_repos_evaluated")))
    events = max(_numeric(person.get("total_cv_events")), _numeric(person.get("total_events_applied")))
    submissions = max(_count_items(person.get("hackathon_submissions")), int(_numeric(person.get("hackathon_submissions"))))
    achievements = _count_items(person.get("notable_achievements"))

    technical_depth = (
        min(35.0, years * 2.2)
        + min(25.0, github_commits / 120.0)
        + min(20.0, github_stars / 40.0)
        + min(10.0, publications * 4.0)
        + max(0.0, _role_score(person))
    )
    industry_experience = (
        min(30.0, years * 2.4)
        + _company_score(person)
        + max(0.0, _role_score(person))
        + min(10.0, achievements * 2.0)
    )
    open_source = (
        (12.0 if person.get("github_url") else 0.0)
        + min(40.0, github_stars / 30.0)
        + min(20.0, github_repos / 3.0)
        + min(18.0, github_commits / 140.0)
        + min(10.0, github_followers / 80.0)
    )
    network_engagement = (
        min(35.0, linkedin_followers / 150.0)
        + min(20.0, linkedin_connections / 250.0)
        + min(12.0, events * 2.0)
        + min(12.0, submissions * 2.5)
    )
    publications_signal = (
        min(45.0, publications * 12.0)
        + min(10.0, certifications * 3.0)
        + (10.0 if _contains_any(str(person.get("education_level") or ""), {"phd", "doctor"}) else 0.0)
    )
    builder_signal = (
        min(25.0, projects * 6.0)
        + min(20.0, submissions * 4.0)
        + min(15.0, achievements * 3.0)
        + min(20.0, github_stars / 50.0)
        + (12.0 if _contains_any(str(person.get("role") or ""), {"founder", "cto"}) else 0.0)
    )
    professional_standing = industry_experience
    community_impact = network_engagement
    uniqueness = min(40.0, achievements * 5.0 + publications * 4.0 + certifications * 2.0 + events * 1.5)

    return {
        "technical_depth": min(100.0, technical_depth),
        "industry_experience": min(100.0, industry_experience),
        "open_source": min(100.0, open_source),
        "network_engagement": min(100.0, network_engagement),
        "publications": min(100.0, publications_signal),
        "builder_signal": min(100.0, builder_signal),
        "professional_standing": min(100.0, professional_standing),
        "community_impact": min(100.0, community_impact),
        "uniqueness": min(100.0, uniqueness),
    }


def _weighted_score(signals: dict[str, float], criteria: dict[str, float]) -> float:
    if not criteria:
        criteria = {"technical_depth": 0.30, "industry_experience": 0.25, "open_source": 0.20, "network_engagement": 0.15, "publications": 0.10}
    total_weight = sum(criteria.values()) or 1.0
    weighted = 0.0
    for criterion, weight in criteria.items():
        signal = signals.get(criterion)
        if signal is None:
            signal = sum(signals.values()) / max(len(signals), 1)
        weighted += signal * float(weight)
    return weighted / total_weight


def _confidence(person: dict[str, Any], signals: dict[str, float]) -> str:
    evidence_points = 0
    if person.get("linkedin_url"):
        evidence_points += 1
    if person.get("github_url"):
        evidence_points += 1
    if _numeric(person.get("years_experience")) > 0:
        evidence_points += 1
    if max(signals.values()) >= 70:
        evidence_points += 1
    if _count_items(person.get("notable_achievements")) > 0:
        evidence_points += 1
    if evidence_points >= 4:
        return "high"
    if evidence_points >= 2:
        return "medium"
    return "low"


def native_pointwise_scores(
    people: list[dict[str, Any]],
    criteria: dict[str, float],
) -> list[dict[str, Any]]:
    """Produce deterministic pointwise scores from structured evidence."""
    raw_results: list[dict[str, Any]] = []
    for person in people:
        signals = _signal_breakdown(person)
        raw_score = _weighted_score(signals, criteria)
        strongest = max(signals.items(), key=lambda item: item[1])[0]
        concerns: list[str] = []
        if not person.get("github_url"):
            concerns.append("no github")
        if not person.get("linkedin_url"):
            concerns.append("no linkedin")
        if _numeric(person.get("years_experience")) < 1:
            concerns.append("limited experience")
        raw_results.append(
            {
                "name": person["name"],
                "display_name": person.get("display_name", person["name"]),
                "score": raw_score,
                "confidence": _confidence(person, signals),
                "strongest_signal": strongest.replace("_", " "),
                "concerns": ", ".join(concerns) if concerns else "none",
                "why": f"Deterministic score from {strongest.replace('_', ' ')} evidence.",
                "reasoning": " / ".join(f"{key}={value:.1f}" for key, value in sorted(signals.items())),
                "dimensions": signals,
            }
        )
    if not raw_results:
        return []

    raw_values = [float(row["score"]) for row in raw_results]
    minimum = min(raw_values)
    maximum = max(raw_values)
    spread = maximum - minimum
    results: list[dict[str, Any]] = []
    for row in raw_results:
        if spread == 0:
            calibrated = 50.0
        else:
            calibrated = 1.0 + 98.0 * ((float(row["score"]) - minimum) / spread)
        calibrated_row = dict(row)
        calibrated_row["raw_score"] = round(float(row["score"]), 2)
        calibrated_row["score"] = round(max(1.0, min(99.0, calibrated)), 1)
        results.append(calibrated_row)
    return sorted(results, key=lambda row: (-float(row["score"]), row["name"]))


def _pairings(
    applicant_ids: list[str],
    records: dict[str, dict[str, int]],
    pointwise_by_id: dict[str, dict[str, Any]],
    past_matchups: set[tuple[str, str]],
) -> tuple[list[tuple[str, str]], list[str]]:
    ordered = sorted(
        applicant_ids,
        key=lambda applicant_id: (
            -records[applicant_id]["wins"],
            records[applicant_id]["losses"],
            -float(pointwise_by_id[applicant_id]["score"]),
            applicant_id,
        ),
    )
    paired: set[str] = set()
    matches: list[tuple[str, str]] = []
    for index, applicant_id in enumerate(ordered):
        if applicant_id in paired:
            continue
        opponent = None
        for candidate in ordered[index + 1 :]:
            if candidate in paired:
                continue
            matchup = tuple(sorted((applicant_id, candidate)))
            if matchup not in past_matchups:
                opponent = candidate
                break
            if opponent is None:
                opponent = candidate
        if opponent is None:
            continue
        paired.add(applicant_id)
        paired.add(opponent)
        matches.append((applicant_id, opponent))
    byes = [applicant_id for applicant_id in ordered if applicant_id not in paired]
    return matches, byes


def native_run_swiss(
    people: list[dict[str, Any]],
    pointwise_results: list[dict[str, Any]],
    *,
    rounds: int,
) -> tuple[dict[str, dict[str, int]], dict[str, float], list[dict[str, Any]]]:
    """Run deterministic Swiss rounds using the pointwise scores as the judge."""
    applicant_ids = [person["name"] for person in people]
    pointwise_by_id = {row["name"]: row for row in pointwise_results}
    records = {applicant_id: {"wins": 0, "losses": 0, "byes": 0} for applicant_id in applicant_ids}
    past_matchups: set[tuple[str, str]] = set()
    matches_out: list[dict[str, Any]] = []
    comparisons: list[tuple[int, int]] = []
    index_by_id = {applicant_id: index for index, applicant_id in enumerate(applicant_ids)}

    for round_number in range(1, rounds + 1):
        matches, byes = _pairings(applicant_ids, records, pointwise_by_id, past_matchups)
        for applicant_id in byes:
            records[applicant_id]["wins"] += 1
            records[applicant_id]["byes"] += 1
        for left_id, right_id in matches:
            left_score = float(pointwise_by_id[left_id]["score"])
            right_score = float(pointwise_by_id[right_id]["score"])
            if left_score > right_score:
                winner, loser = left_id, right_id
            elif right_score > left_score:
                winner, loser = right_id, left_id
            else:
                winner, loser = sorted((left_id, right_id))[0], sorted((left_id, right_id))[1]
            records[winner]["wins"] += 1
            records[loser]["losses"] += 1
            matchup = tuple(sorted((left_id, right_id)))
            past_matchups.add(matchup)
            comparisons.append((index_by_id[winner], index_by_id[loser]))
            matches_out.append(
                {
                    "round": round_number,
                    "name_a": left_id,
                    "name_b": right_id,
                    "winner": winner,
                    "why": f"{winner} had the stronger deterministic pointwise score.",
                }
            )

    pointwise_prior = {row["name"]: float(row["score"]) for row in pointwise_results if row.get("score") is not None}
    bt_strengths = compute_bt_strengths(applicant_ids, comparisons, pointwise_prior=pointwise_prior, prior_strength=2)
    return records, bt_strengths, matches_out


def native_run_borderline(
    people: list[dict[str, Any]],
    pointwise_results: list[dict[str, Any]],
    combined_rankings: list[dict[str, Any]],
    swiss_records: dict[str, dict[str, int]],
    bt_strengths: dict[str, float],
    *,
    accept_count: int,
    band_pct: float,
    extra_rounds: int,
    disagreement_threshold: float,
) -> tuple[dict[str, dict[str, int]], dict[str, float], list[dict[str, Any]]]:
    """Run extra deterministic Swiss rounds on the borderline subset."""
    subset_ids = identify_borderline_applicants(
        combined_rankings,
        cutline=accept_count,
        band_pct=band_pct,
        pointwise_results=[
            {
                "applicant_id": row["name"],
                "score": row.get("score"),
            }
            for row in pointwise_results
        ],
        swiss_records=swiss_records,
        bt_strengths=bt_strengths,
        disagreement_threshold=disagreement_threshold,
    )
    if len(subset_ids) < 2 or extra_rounds <= 0:
        return {}, {}, []
    subset_people = [person for person in people if person["name"] in set(subset_ids)]
    subset_pointwise = [row for row in pointwise_results if row["name"] in set(subset_ids)]
    return native_run_swiss(subset_people, subset_pointwise, rounds=extra_rounds)


def native_merge_borderline(
    pointwise_results: list[dict[str, Any]],
    swiss_records: dict[str, dict[str, int]],
    bt_strengths: dict[str, float],
    borderline_records: dict[str, dict[str, int]],
    borderline_bt: dict[str, float],
    *,
    swiss_weight: float,
    pointwise_weight: float,
    auto_weight: bool,
    tie_breaker: str = "pointwise",
) -> list[dict[str, Any]]:
    """Merge borderline extra rounds back into the main combined ranking."""
    merged_records = {applicant_id: dict(record) for applicant_id, record in swiss_records.items()}
    for applicant_id, extra in borderline_records.items():
        if applicant_id not in merged_records:
            merged_records[applicant_id] = dict(extra)
            continue
        merged_records[applicant_id]["wins"] += int(extra.get("wins", 0))
        merged_records[applicant_id]["losses"] += int(extra.get("losses", 0))
        merged_records[applicant_id]["byes"] += int(extra.get("byes", 0))

    merged_bt = dict(bt_strengths)
    for applicant_id, strength in borderline_bt.items():
        if applicant_id in merged_bt:
            merged_bt[applicant_id] = 0.4 * float(merged_bt[applicant_id]) + 0.6 * float(strength)
        else:
            merged_bt[applicant_id] = float(strength)

    return combine_rankings(
        [
            {
                "applicant_id": row["name"],
                "display_name": row.get("display_name", row["name"]),
                "score": row.get("score"),
                "confidence": row.get("confidence", ""),
                "strongest_signal": row.get("strongest_signal", ""),
                "concerns": row.get("concerns", ""),
                "reasoning": row.get("reasoning") or row.get("why") or "",
                "error": row.get("error"),
            }
            for row in pointwise_results
        ],
        merged_records,
        merged_bt,
        swiss_weight=swiss_weight,
        pointwise_weight=pointwise_weight,
        auto_weight=auto_weight,
        tie_breaker=tie_breaker,
    )


def native_quality_review(
    people: list[dict[str, Any]],
    rankings: list[dict[str, Any]],
    pointwise_results: list[dict[str, Any]],
    *,
    accept_count: int,
) -> list[dict[str, Any]]:
    """Produce deterministic final decisions."""
    person_by_id = {person["name"]: person for person in people}
    pointwise_by_id = {row["name"]: row for row in pointwise_results}
    results: list[dict[str, Any]] = []
    for row in rankings:
        applicant_id = row["applicant_id"]
        pointwise = pointwise_by_id.get(applicant_id, {})
        person = person_by_id.get(applicant_id, {})
        rank = int(row["rank"])
        final_score = float(row["final_score"])
        if rank <= max(1, accept_count // 4) and final_score >= 0.85:
            verdict = "STRONG YES"
        elif rank <= accept_count and final_score >= 0.55:
            verdict = "YES"
        elif final_score >= 0.40:
            verdict = "BORDERLINE"
        else:
            verdict = "NO"

        highlights: list[str] = []
        if person.get("company"):
            highlights.append(str(person["company"]))
        stars = _numeric(person.get("gh_api_stars") or person.get("github_stars"))
        if stars > 0:
            highlights.append(f"{int(stars)} GitHub stars")
        years = _numeric(person.get("years_experience"))
        if years > 0:
            highlights.append(f"{years:.0f} years experience")
        if not highlights:
            highlights.append(str(pointwise.get("strongest_signal", "limited evidence")))
        specific_why = ", ".join(highlights[:2])
        results.append(
            {
                "name": applicant_id,
                "verdict": verdict,
                "specific_why": specific_why,
                "best_number": highlights[1] if len(highlights) > 1 else "N/A",
                "company": person.get("company", "Unknown") or "Unknown",
                "role": person.get("role", "Unknown") or "Unknown",
                "standout_achievements": list(person.get("notable_achievements", []))[:2],
                "bs_check": "legit" if pointwise.get("confidence") != "low" else "unclear",
            }
        )
    return results
