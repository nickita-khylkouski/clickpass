"""Export cv-rank decisions back onto an original applicant CSV order.

The source event export sometimes contains a leading summary row before the real
header row. This script preserves the original applicant row order, keeps the
original columns, and appends cv-rank outputs using human-facing statuses:
Approved / Pending.
"""

from __future__ import annotations

import argparse
import csv
from pathlib import Path


def load_original_rows(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        raw_rows = list(csv.reader(f))
    if len(raw_rows) < 2:
        raise ValueError(f"Original CSV is too short: {path}")

    header = raw_rows[1]
    data_rows = raw_rows[2:]
    rows = [dict(zip(header, row)) for row in data_rows]
    return header, rows


def load_ranked_by_email(path: Path) -> dict[str, dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    by_email: dict[str, dict[str, str]] = {}
    for row in rows:
        email = (row.get("Email") or "").strip().lower()
        if email and email not in by_email:
            by_email[email] = row
    return by_email


def load_ranked_by_name(path: Path) -> dict[str, dict[str, str]]:
    with path.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.DictReader(f))
    counts: dict[str, int] = {}
    by_name: dict[str, dict[str, str]] = {}
    for row in rows:
        name = normalize_name(row.get("Name", ""))
        if not name:
            continue
        counts[name] = counts.get(name, 0) + 1
        by_name[name] = row
    return {name: row for name, row in by_name.items() if counts.get(name) == 1}


def normalize_name(value: str) -> str:
    return " ".join((value or "").lower().strip().split())


def normalize_status(status: str) -> str:
    return "Approved" if status == "ACCEPT" else "Pending"


def export_annotated(
    original_header: list[str],
    original_rows: list[dict[str, str]],
    ranked_by_email: dict[str, dict[str, str]],
    ranked_by_name: dict[str, dict[str, str]],
    output_path: Path,
) -> None:
    appended = [
        "CV Rank Recommendation",
        "CV Rank WHY",
        "CV Rank Verdict",
        "CV Rank Rank",
        "CV Rank Score",
    ]
    header = original_header + appended

    with output_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=header)
        writer.writeheader()

        for row in original_rows:
            out = dict(row)
            email = (row.get("Email") or "").strip().lower()
            ranked = ranked_by_email.get(email)
            if not ranked:
                full_name = normalize_name(
                    f"{row.get('First Name', '')} {row.get('Last Name', '')}"
                )
                ranked = ranked_by_name.get(full_name)
            if ranked:
                out["CV Rank Recommendation"] = normalize_status(
                    ranked.get("Nebius.Build_SF_Status", "WAITLIST")
                )
                out["CV Rank WHY"] = ranked.get("Specific_WHY", "")
                out["CV Rank Verdict"] = ranked.get("Verdict", "")
                out["CV Rank Rank"] = ranked.get("Rank", "")
                out["CV Rank Score"] = ranked.get("Combined_Score", "")
            else:
                out["CV Rank Recommendation"] = ""
                out["CV Rank WHY"] = ""
                out["CV Rank Verdict"] = ""
                out["CV Rank Rank"] = ""
                out["CV Rank Score"] = ""
            writer.writerow(out)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--original-csv", required=True, type=Path)
    parser.add_argument("--ranked-csv", required=True, type=Path)
    parser.add_argument("--output-csv", required=True, type=Path)
    args = parser.parse_args()

    header, original_rows = load_original_rows(args.original_csv)
    ranked_by_email = load_ranked_by_email(args.ranked_csv)
    ranked_by_name = load_ranked_by_name(args.ranked_csv)
    export_annotated(
        header,
        original_rows,
        ranked_by_email,
        ranked_by_name,
        args.output_csv,
    )

    matched = sum(
        1
        for row in original_rows
        if (
            (row.get("Email") or "").strip().lower() in ranked_by_email
            or normalize_name(f"{row.get('First Name', '')} {row.get('Last Name', '')}")
            in ranked_by_name
        )
    )
    print(f"Wrote {len(original_rows)} rows to {args.output_csv}")
    print(f"Matched ranked decisions for {matched}/{len(original_rows)} rows")


if __name__ == "__main__":
    main()
