"""Analyze calibration and suggest corrections."""
import os
import sys
from pathlib import Path
load_dotenv = __import__('dotenv').load_dotenv
load_dotenv(Path(__file__).resolve().parents[2] / ".env")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

import psycopg2
import psycopg2.extras
import math
from collections import defaultdict

dsn = os.environ["PLATFORM_DATABASE_URL"]
conn = psycopg2.connect(dsn)

# Import model
from cv_rank.waves.model import predict_show_probability
from cv_rank.waves.predict import timing_bucket, prior_rate_bucket, tz_distance_bucket, city_to_timezone, compute_tz_offset_diff

# Get Nebius data at different horizons
cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

# Nebius event
event_id = '1a89ea59-0a50-4f3c-bac9-99dd12a1b1c1'

# Horizon analysis for Nebius: predict at T-0, T-1, T-3, T-7, T-14
horizons = [0, 1, 3, 7, 14]

print("=== NEBIUS PREDICTIONS AT MULTIPLE HORIZONS ===\n")
print("Testing if predictions increase as we get closer (horizon decreases)")
print("If they DON'T increase much, the horizon feature isn't working.\n")

for h in horizons:
    # Just get ONE sample person to see how their prediction changes
    cur.execute("""
        SELECT up."firstName", up."lastName",
               ea."appliedFromTimeZone" AS applicant_tz,
               EXTRACT(EPOCH FROM ('2026-03-15 16:00:00'::timestamp - ea."createdAt")) / 86400.0 AS days_before
        FROM "EventApplicant" ea
        JOIN "UserProfile" up ON up."userId" = ea."userId"
        WHERE ea."eventId" = %s AND ea.status = 'approved'
        LIMIT 1
    """, (event_id,))
    
    row = cur.fetchone()
    if row:
        days_before = float(row['days_before'])
        applicant_tz = row.get('applicant_tz') or 'America/Los_Angeles'
        
        # Baseline person: local, 7-14 days, new, 2 page views, no notification
        tz_dist = 'local'
        
        p_at_h = predict_show_probability(
            days_before_event=days_before,
            prior_attendance_rate=None,
            tz_distance=tz_dist,
            prior_events=0,
            page_views=2,
            notification_read=False,
            horizon_days=float(h),
        )
        
        print(f"  T-{h:2d} (horizon={h:2d} days):  P_Show = {p_at_h:.1%}")

print("\n" + "="*60)
print("If predictions DECREASE as horizon increases, the model is working.")
print("If they're all similar, horizon feature has no effect.")
print("="*60)

conn.close()
