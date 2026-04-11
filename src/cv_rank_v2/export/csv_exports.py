"""Typed CSV exports for the v2 ranking pipeline."""

from __future__ import annotations

import csv
from pathlib import Path
from typing import Any

from cv_rank_v2._compat import applicant_id_of, display_name_of, email_of


def _ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def _stringify(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _ranking_status(rank: int, target_accepts: int) -> str:
    return "ACCEPT" if rank <= target_accepts else "WAITLIST"


def write_ranked_csv(
    applicants: list[Any],
    rankings: list[dict[str, Any]],
    quality_results: dict[str, dict[str, Any]],
    output_path: str | Path,
    *,
    target_accepts: int,
) -> Path:
    """Write a concise but operationally useful ranked CSV."""
    output = Path(output_path)
    _ensure_parent(output)

    applicant_index = {applicant_id_of(applicant): applicant for applicant in applicants}
    fieldnames = [
        "Rank",
        "Applicant_ID",
        "Name",
        "Email",
        "Status",
        "Final_Score",
        "Pointwise_Score",
        "Swiss_Wins",
        "Swiss_Losses",
        "BT_Strength",
        "Verdict",
        "Specific_Why",
        "Confidence",
        "Strongest_Signal",
        "Concerns",
    ]

    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rankings:
            applicant_id = row["applicant_id"]
            applicant = applicant_index.get(applicant_id)
            quality = quality_results.get(applicant_id, {})
            writer.writerow(
                {
                    "Rank": row["rank"],
                    "Applicant_ID": applicant_id,
                    "Name": display_name_of(applicant) if applicant is not None else row.get("display_name", applicant_id),
                    "Email": email_of(applicant) if applicant is not None else "",
                    "Status": _ranking_status(int(row["rank"]), target_accepts),
                    "Final_Score": f"{float(row['final_score']):.6f}",
                    "Pointwise_Score": f"{float(row['pointwise_score']):.1f}",
                    "Swiss_Wins": int(row.get("swiss_wins", 0)),
                    "Swiss_Losses": int(row.get("swiss_losses", 0)),
                    "BT_Strength": "" if row.get("bt_strength") is None else f"{float(row['bt_strength']):.6f}",
                    "Verdict": quality.get("verdict", ""),
                    "Specific_Why": quality.get("specific_why", quality.get("why", "")),
                    "Confidence": row.get("confidence", ""),
                    "Strongest_Signal": row.get("strongest_signal", ""),
                    "Concerns": row.get("concerns", ""),
                }
            )
    return output


def write_failures_csv(
    applicants: list[Any],
    stage_name: str,
    stage_results: list[dict[str, Any]],
    output_path: str | Path,
) -> Path:
    """Write rows that failed in a specific stage."""
    output = Path(output_path)
    _ensure_parent(output)

    applicant_index = {applicant_id_of(applicant): applicant for applicant in applicants}
    fieldnames = ["Applicant_ID", "Name", "Email", "Stage", "Error"]
    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in stage_results:
            if not row.get("error"):
                continue
            applicant_id = row.get("applicant_id", "")
            applicant = applicant_index.get(applicant_id)
            writer.writerow(
                {
                    "Applicant_ID": applicant_id,
                    "Name": display_name_of(applicant) if applicant is not None else row.get("display_name", ""),
                    "Email": email_of(applicant) if applicant is not None else "",
                    "Stage": stage_name,
                    "Error": row.get("error", ""),
                }
            )
    return output


def write_needs_review_csv(
    applicants: list[Any],
    rankings: list[dict[str, Any]],
    output_path: str | Path,
    *,
    target_accepts: int,
    boundary_width_pct: float = 0.10,
    disagreement_threshold: float = 0.25,
) -> Path:
    """Write a review queue around the cutline and for disagreement cases."""
    output = Path(output_path)
    _ensure_parent(output)

    applicant_index = {applicant_id_of(applicant): applicant for applicant in applicants}
    boundary = max(1, int(len(rankings) * boundary_width_pct))
    low = max(1, target_accepts - boundary)
    high = min(len(rankings), target_accepts + boundary)
    fieldnames = [
        "Applicant_ID",
        "Name",
        "Email",
        "Rank",
        "Final_Score",
        "Pointwise_Score",
        "Swiss_Record",
        "Reasons",
    ]

    with output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=fieldnames)
        writer.writeheader()
        for row in rankings:
            reasons: list[str] = []
            rank = int(row["rank"])
            if low <= rank <= high:
                reasons.append("cutline_band")
            final_score = float(row.get("final_score", 0.0))
            pointwise_score = float(row.get("pointwise_score", 0.0))
            if row.get("swiss_evaluated") and abs(final_score - pointwise_score / 100.0) >= disagreement_threshold:
                reasons.append("signal_disagreement")
            if not reasons:
                continue

            applicant_id = row["applicant_id"]
            applicant = applicant_index.get(applicant_id)
            writer.writerow(
                {
                    "Applicant_ID": applicant_id,
                    "Name": display_name_of(applicant) if applicant is not None else row.get("display_name", ""),
                    "Email": email_of(applicant) if applicant is not None else "",
                    "Rank": rank,
                    "Final_Score": f"{final_score:.6f}",
                    "Pointwise_Score": f"{pointwise_score:.1f}",
                    "Swiss_Record": f"{int(row.get('swiss_wins', 0))}W-{int(row.get('swiss_losses', 0))}L",
                    "Reasons": ",".join(reasons),
                }
            )
    return output
