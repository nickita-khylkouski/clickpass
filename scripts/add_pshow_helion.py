"""Add P_Show to existing Helion RANKED.csv using applied_at dates."""
import csv
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent / "src"))
from cv_rank.waves.model import predict_show_probability
from cv_rank.waves.predict import timing_bucket, prior_rate_bucket

RANKED = Path("/Users/nickita/cv-rank/results/run_20260306_212348/RANKED.csv")
ORIGINAL = Path("/Users/nickita/cv-rank/data/helion_hackathon.csv")
EVENT_DATE = datetime(2026, 3, 14)
EVENT_CITY = "San Francisco"

# Load original data for applied_at and kernel_authoring
orig_data = {}
with open(ORIGINAL, newline="", encoding="utf-8") as f:
    for row in csv.DictReader(f):
        email = (row.get("email") or "").lower().strip()
        if email:
            orig_data[email] = row

# Load and update RANKED.csv
with open(RANKED, newline="", encoding="utf-8") as f:
    rows = list(csv.DictReader(f))
    fieldnames = list(rows[0].keys()) if rows else []

computed = 0
for r in rows:
    email = (r.get("Email") or "").lower().strip()
    orig = orig_data.get(email, {})
    applied_at = orig.get("applied_at", "")

    # Compute days_before from applied_at
    days_before = 7.0  # default
    if applied_at:
        for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
            try:
                app_dt = datetime.strptime(applied_at.strip()[:19], fmt)
                days_before = (EVENT_DATE - app_dt).total_seconds() / 86400.0
                days_before = max(days_before, 0)
                break
            except ValueError:
                continue

    # Prior attendance from enrichment
    try:
        total_events = int(float(r.get("Total_CV_Events", 0) or 0))
    except (ValueError, TypeError):
        total_events = 0

    prior_rate = None  # no check-in data in CSV mode

    p = predict_show_probability(
        days_before_event=days_before,
        prior_attendance_rate=prior_rate,
        tz_distance="local",  # SF event, most applicants local
        page_views=0,
        notification_read=False,
    )

    r["P_Show"] = f"{p:.0%}"
    computed += 1

# Write back
with open(RANKED, "w", newline="", encoding="utf-8") as f:
    writer = csv.DictWriter(f, fieldnames=fieldnames)
    writer.writeheader()
    writer.writerows(rows)

print(f"Updated P_Show for {computed}/{len(rows)} applicants")

# Quick distribution
pshows = [float(r["P_Show"].replace("%", "")) / 100 for r in rows]
pshows.sort(reverse=True)
print(f"  Mean: {sum(pshows)/len(pshows):.1%}")
print(f"  Range: {min(pshows):.1%} — {max(pshows):.1%}")
