"""Clean Helion Hackathon CSV for cv-rank."""
import csv
from pathlib import Path

INPUT = Path("/Users/nickita/Downloads/[EXTERNAL] 03_14 Helion Hackathon Applicants - Sheet1.csv")
OUTPUT = Path("/Users/nickita/cv-rank/data/helion_hackathon.csv")

COLUMN_MAP = {
    "First Name": "first_name",
    "Last Name": "last_name",
    "Email": "email",
    "What's your Twitter/X?": "x_handle",
    "What's your LinkedIn?": "linkedin_url",
    "What's your Github?": "github_url",
    "Are you looking for a job? (yes/no)": "looking_for_job",
    "What background do you have on kernel authoring?": "kernel_authoring",
    "1 Line self description (including job / industry)": "self_description",
    "What AI project are you most proud of building? (1 line)": "ai_project",
    "Applied At": "applied_at",
}

with open(INPUT, newline="", encoding="utf-8") as f:
    lines = list(csv.reader(f))

# Row 0 = summary, Row 1 = header, Row 2 = "Round One" separator
header = lines[1]
data_rows = lines[2:]

# Build index map
idx_map = {}
for i, col in enumerate(header):
    col_stripped = col.strip()
    if col_stripped in COLUMN_MAP:
        idx_map[COLUMN_MAP[col_stripped]] = i

print(f"Columns mapped: {list(idx_map.keys())}")
print(f"Total data rows (incl separators): {len(data_rows)}")

# Filter out separator/empty rows
clean = []
seen_emails = set()
for row in data_rows:
    # Skip separator rows like "Round One", "Round Two", empty rows
    first_cell = row[0].strip() if row else ""
    if first_cell in ("", "Round One", "Round Two", "Round Three", "Round Four"):
        continue
    # Skip rows without email
    email_idx = idx_map.get("email")
    if email_idx is None or email_idx >= len(row):
        continue
    email = row[email_idx].strip().lower()
    if not email or "@" not in email:
        continue
    # Deduplicate by email
    if email in seen_emails:
        continue
    seen_emails.add(email)

    person = {}
    for canonical, col_idx in idx_map.items():
        if col_idx < len(row):
            person[canonical] = row[col_idx].strip()
        else:
            person[canonical] = ""
    # Combine kernel_authoring into self_description for scoring context
    # but also keep it as a separate field
    clean.append(person)

OUTPUT.parent.mkdir(parents=True, exist_ok=True)

# Write with kernel_authoring as a separate column so the LLM sees it
fieldnames = ["first_name", "last_name", "email", "linkedin_url", "github_url",
              "x_handle", "self_description", "ai_project", "looking_for_job",
              "kernel_authoring", "applied_at"]

with open(OUTPUT, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(clean)

print(f"\nCleaned: {len(clean)} applicants")
print(f"Output: {OUTPUT}")

# Quick stats
kernel_has = sum(1 for p in clean if p.get("kernel_authoring") and len(p["kernel_authoring"]) > 5)
linkedin_has = sum(1 for p in clean if p.get("linkedin_url"))
github_has = sum(1 for p in clean if p.get("github_url"))
print(f"  With kernel authoring answer: {kernel_has}")
print(f"  With LinkedIn: {linkedin_has}")
print(f"  With GitHub: {github_has}")
