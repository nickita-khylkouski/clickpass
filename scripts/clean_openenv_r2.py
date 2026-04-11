"""Clean OpenEnv hackathon CSV - extract Round 2 only."""
import csv
import sys

INPUT = '/Users/nickita/Downloads/[EXTERNAL] 03_07 OpenEnv Hackathon Applicants - APPLICANT APPROVALS [updated March 4 1_07am PT].csv'
OUTPUT = '/Users/nickita/cv-rank/data/openenv_r2.csv'

# Column mapping: source -> canonical
COLUMN_MAP = {
    'First Name': 'first_name',
    'Last Name': 'last_name',
    'Email': 'email',
    "What's your Twitter/X?": 'x_handle',
    "What's your LinkedIn?": 'linkedin_url',
    "What's your Github?": 'github_url',
    'Are you looking for a job? (yes/no)': 'looking_for_job',
    '1 Line self description (including job / industry)': 'self_description',
    'What AI project are you most proud of building? (1 line)': 'ai_project',
}

# Extra columns to keep (not in canonical but useful for scoring)
EXTRA_COLS = {
    'If any, please give examples from your relevant experience/projects in the past as they relate to agentic AI training and reinforcement learning. Some examples include coding, computer-use, etc': 'relevant_experience',
    "Joining as a team or an individual? If as a team, what is your team's name?": 'team_or_individual',
    'What is your discord handle? We need it to assign you a role in the PyTorch Discord Server (https://discord.gg/bAEFBxkt)': 'discord_handle',
    'WHY/Extra info': 'extra_info',
    'Applied At': 'applied_at',
    'CV Recommended': 'cv_recommended',
    'Meta Approved': 'meta_approved',
}

with open(INPUT, 'r', encoding='utf-8') as f:
    reader = csv.reader(f)
    all_rows = list(reader)

# Find header row (line 1, 0-indexed) and Round Two marker
header_row = all_rows[1]  # row index 1 has headers
round_two_idx = None
for i, row in enumerate(all_rows):
    if row and row[0].strip() == 'Round Two':
        round_two_idx = i
        break

if round_two_idx is None:
    print("ERROR: Could not find 'Round Two' marker")
    sys.exit(1)

print(f"Header at row 1, Round Two marker at row {round_two_idx}")
print(f"Header columns: {header_row}")

# Extract Round 2 data rows (after Round Two marker to end)
data_rows = all_rows[round_two_idx + 1:]

# Filter out empty rows
data_rows = [r for r in data_rows if any(cell.strip() for cell in r)]

print(f"Round 2 applicants (raw): {len(data_rows)}")

# Build column index mapping
col_indices = {}
for i, col_name in enumerate(header_row):
    col_name = col_name.strip()
    if col_name in COLUMN_MAP:
        col_indices[COLUMN_MAP[col_name]] = i
    elif col_name in EXTRA_COLS:
        col_indices[EXTRA_COLS[col_name]] = i

print(f"Mapped columns: {list(col_indices.keys())}")

# Write cleaned CSV
canonical_cols = ['first_name', 'last_name', 'email', 'linkedin_url', 'github_url',
                  'x_handle', 'self_description', 'ai_project', 'looking_for_job',
                  'relevant_experience', 'team_or_individual', 'discord_handle',
                  'extra_info', 'applied_at']

written = 0
skipped = 0
with open(OUTPUT, 'w', newline='', encoding='utf-8') as f:
    writer = csv.DictWriter(f, fieldnames=canonical_cols)
    writer.writeheader()
    for row in data_rows:
        record = {}
        for col_name, idx in col_indices.items():
            if idx < len(row):
                record[col_name] = row[idx].strip()
            else:
                record[col_name] = ''
        # Skip rows without email (separator/empty rows)
        if not record.get('email') or '@' not in record.get('email', ''):
            skipped += 1
            continue
        # Only write canonical columns
        out = {c: record.get(c, '') for c in canonical_cols}
        writer.writerow(out)
        written += 1

print(f"\nResult: {written} applicants written, {skipped} skipped (no email)")
print(f"Output: {OUTPUT}")
