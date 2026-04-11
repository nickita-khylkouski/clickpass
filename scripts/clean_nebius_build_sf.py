#!/usr/bin/env python3
from __future__ import annotations

import csv
from pathlib import Path


INPUT = Path("/Users/nickita/Desktop/[EXTERNAL] 03_15 Nebius.Build SF Applicants - Applicant Approvals.csv")
OUTPUT = Path("/Users/nickita/cv-rank/data/nebius_build_sf.csv")

HEADER_ROW_INDEX = 1

COLUMN_MAP = {
    "First Name": "first_name",
    "Last Name": "last_name",
    "Email": "email",
    "What's your Twitter/X?": "x_handle",
    "What's your LinkedIn?": "linkedin_url",
    "What's your Github?": "github_url",
    "Are you looking for a job? (yes/no)": "looking_for_job",
    "What are you going to build at this hackathon?": "self_description",
    "What AI project are you most proud of building? (1 line)": "ai_project",
    "Applied At": "applied_at",
    "CV Recommended": "cv_recommended",
    "Partner Approved": "partner_approved",
}


def main() -> None:
    with INPUT.open(newline="", encoding="utf-8-sig") as f:
        rows = list(csv.reader(f))

    if len(rows) <= HEADER_ROW_INDEX:
        raise SystemExit("CSV is missing the expected header row")

    header = rows[HEADER_ROW_INDEX]
    data_rows = rows[HEADER_ROW_INDEX + 1 :]

    idx_map: dict[str, int] = {}
    for i, col in enumerate(header):
        key = col.strip()
        if key in COLUMN_MAP:
            idx_map[COLUMN_MAP[key]] = i

    if "email" not in idx_map:
        raise SystemExit("Could not find Email column in Nebius CSV")

    cleaned: list[dict[str, str]] = []
    seen_emails: set[str] = set()

    for row in data_rows:
        if not any(cell.strip() for cell in row):
            continue

        email_idx = idx_map["email"]
        if email_idx >= len(row):
            continue

        email = row[email_idx].strip().lower()
        if not email or "@" not in email:
            continue
        if email in seen_emails:
            continue
        seen_emails.add(email)

        person: dict[str, str] = {}
        for canonical, col_idx in idx_map.items():
            person[canonical] = row[col_idx].strip() if col_idx < len(row) else ""

        cleaned.append(person)

    fieldnames = [
        "first_name",
        "last_name",
        "email",
        "linkedin_url",
        "github_url",
        "x_handle",
        "looking_for_job",
        "self_description",
        "ai_project",
        "applied_at",
        "cv_recommended",
        "partner_approved",
    ]

    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    with OUTPUT.open("w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(cleaned)

    print(f"Input: {INPUT}")
    print(f"Output: {OUTPUT}")
    print(f"Rows written: {len(cleaned)}")
    print(f"Columns mapped: {sorted(idx_map.keys())}")


if __name__ == "__main__":
    main()
