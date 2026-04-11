"""Predict P(show) for upcoming events. Outputs per-person CSV + event totals."""
import csv
import os
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import psycopg2
import psycopg2.extras

from cv_rank.waves.model import (
    expected_future_attendance,
    forecast_engagement,
    forecast_signup_count,
    future_approved_show_rate,
    predict_show_probability,
)
from cv_rank.waves.predict import (
    timing_bucket,
    prior_rate_bucket,
    city_to_timezone,
    compute_tz_offset_diff,
    tz_distance_bucket,
    page_views_bucket,
)


def main():
    dsn = os.environ["PLATFORM_DATABASE_URL"]
    conn = psycopg2.connect(dsn)

    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)

        # 1. Find upcoming events (next 14 days) with approved applicants + signup velocity
        cur.execute("""
            SELECT pe.id, pe.title, pe.city, pe."startDateTime",
                   COUNT(*) FILTER (WHERE ea.status = 'approved') AS approved_count,
                   COUNT(*) FILTER (WHERE ea.status = 'approved' AND ea."createdAt" <= NOW() - INTERVAL '14 days') AS approved_t14,
                   COUNT(*) FILTER (WHERE ea.status = 'approved' AND ea."createdAt" <= NOW() - INTERVAL '21 days') AS approved_t21
            FROM "PlatformEvent" pe
            JOIN "EventApplicant" ea ON ea."eventId" = pe.id
            WHERE pe."startDateTime" >= NOW()
              AND pe."startDateTime" <= NOW() + INTERVAL '14 days'
              AND ea.status = 'approved'
            GROUP BY pe.id, pe.title, pe.city, pe."startDateTime"
            HAVING COUNT(*) FILTER (WHERE ea.status = 'approved') >= 5
            ORDER BY pe."startDateTime"
        """)
        events = cur.fetchall()

        if not events:
            print("No upcoming events with approved applicants found in the next 14 days.")
            return

        print(f"Found {len(events)} upcoming event(s):\n")
        for ev in events:
            print(f"  - {ev['title']} | {ev['city']} | {ev['startDateTime']} | {ev['approved_count']} approved")

        all_predictions = []

        for ev in events:
            event_id = ev["id"]
            event_title = ev["title"] or ""
            event_city = ev["city"] or ""
            event_start = ev["startDateTime"]
            event_tz = city_to_timezone(event_city, event_title)

            # Calculate horizon
            now_utc = datetime.now(timezone.utc)
            event_start_aware = event_start.replace(tzinfo=timezone.utc) if event_start.tzinfo is None else event_start
            horizon_days = max(0.0, (event_start_aware - now_utc).total_seconds() / 86400.0)

            # STAGE 0: Forecast final approved count (signup velocity)
            current_approved = ev["approved_count"]
            approved_t14 = ev.get("approved_t14") or 0
            approved_t21 = ev.get("approved_t21") or 0

            forecasted_total_approved = forecast_signup_count(
                current_approved=current_approved,
                horizon_days=horizon_days,
                approved_t14=approved_t14,
                approved_t21=approved_t21,
            )
            additional_approved = max(0.0, forecasted_total_approved - current_approved)

            print(f"\n{'='*80}")
            print(f"EVENT: {event_title}")
            print(f"  City: {event_city} | TZ: {event_tz}")
            print(f"  Date: {event_start}")
            print(f"  Horizon: T-{horizon_days:.1f} days")
            print(f"  Current approved: {current_approved}")
            print(
                f"  Forecasted total: {forecasted_total_approved:.0f} "
                f"(+{additional_approved:.0f} more approvals expected)"
            )
            print(f"{'='*80}")

            # 2. Get all approved applicants with full feature data
            cur.execute("""
                WITH prior_agg AS (
                    SELECT ea2."userId", pe2.id AS event_id, pe2."startDateTime" AS event_start,
                        COUNT(*) FILTER (WHERE ea2.status = 'approved') AS prior_events,
                        COALESCE(SUM(ea2."checkedIn"::int) FILTER (WHERE ea2.status = 'approved'), 0) AS prior_attended
                    FROM "EventApplicant" ea2
                    JOIN "PlatformEvent" pe2 ON ea2."eventId" = pe2.id
                    WHERE pe2."startDateTime" IS NOT NULL
                    GROUP BY ea2."userId", pe2.id, pe2."startDateTime"
                ),
                page_view_counts AS (
                    SELECT i."userId", i.properties->>'eventId' AS event_id, COUNT(*) AS view_count
                    FROM "Insight" i
                    JOIN "PlatformEvent" pe_pv ON pe_pv.id::text = i.properties->>'eventId'
                    WHERE i."eventName" = 'PAGE_VIEW'
                      AND i.properties->>'eventId' IS NOT NULL
                      AND i."createdAt" < pe_pv."startDateTime"
                    GROUP BY i."userId", i.properties->>'eventId'
                ),
                notif_reads AS (
                    SELECT DISTINCT ON (pn."userId", pn.data->>'eventId')
                        pn."userId", pn.data->>'eventId' AS event_id, pn.read AS notif_read
                    FROM "PlatformNotification" pn
                    WHERE pn.type = 'event_application_status_change'
                      AND pn.data->>'status' = 'approved'
                    ORDER BY pn."userId", pn.data->>'eventId', pn."createdAt" DESC
                )
                SELECT
                    up."firstName", up."lastName", LOWER(up.email) AS email,
                    ea."appliedFromTimeZone" AS applicant_tz,
                    EXTRACT(EPOCH FROM (%s::timestamp - ea."createdAt")) / 86400.0 AS days_before,
                    ea."createdAt" AS applied_at,
                    (SELECT COALESCE(SUM(pa.prior_events), 0) FROM prior_agg pa
                     WHERE pa."userId" = ea."userId" AND pa.event_start < %s::timestamp AND pa.event_id != %s) AS prior_events,
                    (SELECT COALESCE(SUM(pa.prior_attended), 0) FROM prior_agg pa
                     WHERE pa."userId" = ea."userId" AND pa.event_start < %s::timestamp AND pa.event_id != %s) AS prior_attended,
                    COALESCE(pv.view_count, 0) AS page_views,
                    COALESCE(nr.notif_read, false) AS notif_read
                FROM "EventApplicant" ea
                JOIN "UserProfile" up ON up."userId" = ea."userId"
                LEFT JOIN page_view_counts pv ON pv."userId" = ea."userId" AND pv.event_id = %s::text
                LEFT JOIN notif_reads nr ON nr."userId" = ea."userId" AND nr.event_id = %s::text
                WHERE ea."eventId" = %s
                  AND ea.status = 'approved'
                  AND ea."createdAt" IS NOT NULL
                ORDER BY ea."createdAt"
            """, (event_start, event_start, event_id, event_start, event_id,
                  event_id, event_id, event_id))

            applicants = cur.fetchall()
            print(f"\n  Fetched {len(applicants)} approved applicants with features.\n")

            event_predictions = []

            for app in applicants:
                first = app.get("firstName") or ""
                last = app.get("lastName") or ""
                name = f"{first} {last}".strip()
                email = app.get("email", "")
                applicant_tz = app.get("applicant_tz") or "America/Los_Angeles"
                days_before = float(app.get("days_before") or 0)
                prior_events = int(app.get("prior_events") or 0)
                prior_attended = int(app.get("prior_attended") or 0)
                pv = int(app.get("page_views") or 0)
                notif = bool(app.get("notif_read"))

                # Compute features
                prior_rate = prior_attended / prior_events if prior_events > 0 else None
                offset = compute_tz_offset_diff(applicant_tz, event_tz, event_date=event_start)
                tz_dist = tz_distance_bucket(offset)
                tb = timing_bucket(days_before)
                pr_bucket = prior_rate_bucket(prior_rate)
                pv_bucket = page_views_bucket(pv)

                # Prediction horizon: days from NOW until event
                now_utc = datetime.now(timezone.utc)
                event_start_aware = event_start.replace(tzinfo=timezone.utc) if event_start.tzinfo is None else event_start
                horizon_days = max(0.0, (event_start_aware - now_utc).total_seconds() / 86400.0)

                # TWO-STAGE PREDICTION:
                # Stage 1: Forecast what engagement will be at T-0 (event time)
                # Current engagement is incomplete (e.g., 2 views at T-8),
                # but by T-0 it will grow (e.g., to 6 views). We forecast final values.
                if horizon_days > 0.5:  # Only forecast if >12 hours out
                    forecasted_views, forecasted_notif_prob = forecast_engagement(
                        current_page_views=pv,
                        current_notif_read=notif,
                        horizon_days=horizon_days,
                        tz_offset_hrs=offset,
                        tz_distance=tz_dist,
                        prior_events=prior_events,
                    )
                    # Use forecasted values for prediction
                    pv_for_prediction = int(round(forecasted_views))
                    notif_for_prediction = forecasted_notif_prob > 0.5
                else:
                    # Close to event time, current engagement is already final
                    pv_for_prediction = pv
                    notif_for_prediction = notif

                # Stage 2: Predict show probability using forecasted engagement
                p_show = predict_show_probability(
                    days_before_event=days_before,
                    prior_attendance_rate=prior_rate,
                    tz_distance=tz_dist,
                    prior_events=prior_events,
                    page_views=pv_for_prediction,
                    notification_read=notif_for_prediction,
                    horizon_days=horizon_days,
                )

                event_predictions.append({
                    "event": event_title,
                    "event_date": str(event_start),
                    "event_city": event_city,
                    "name": name,
                    "email": email,
                    "p_show": round(p_show, 4),
                    "p_show_pct": f"{p_show:.1%}",
                    "timing_bucket": tb,
                    "days_before": round(days_before, 1),
                    "horizon_days": round(horizon_days, 2),
                    "tz_distance": tz_dist,
                    "tz_offset_hrs": round(offset, 1),
                    "prior_rate": f"{prior_rate:.0%}" if prior_rate is not None else "new",
                    "prior_events": prior_events,
                    "prior_attended": prior_attended,
                    "page_views": pv,
                    "page_views_bucket": pv_bucket,
                    "notif_read": "Yes" if notif else "No",
                })

            # Sort by P_Show descending
            event_predictions.sort(key=lambda x: x["p_show"], reverse=True)

            # Calculate predictions for current approved people
            p_shows = [p["p_show"] for p in event_predictions]
            expected_from_current = sum(p_shows)
            n_current = len(p_shows)

            # Estimate expected attendees from future approvals using descriptive,
            # pooled historical rates by remaining horizon window.
            future_show_rate = future_approved_show_rate(horizon_days) if additional_approved > 0 else 0.0
            expected_from_future = expected_future_attendance(additional_approved, horizon_days)

            expected_total = expected_from_current + expected_from_future
            total_approved = n_current + additional_approved

            print(f"\n  THREE-STAGE FORECAST:")
            print(f"  Stage 0 (Signup Velocity):")
            print(f"    Current approved:       {n_current}")
            print(f"    Forecasted total:       {total_approved:.0f} (+{additional_approved:.0f} more approvals)")
            print(f"\n  Stage 1+2 (Current Approvals with Engagement Forecasting):")
            print(f"    Expected from current:  {expected_from_current:.0f} ({expected_from_current/n_current:.1%} avg)")
            print(f"    P_Show range:           {min(p_shows):.1%} – {max(p_shows):.1%}")
            print(f"    Median P_Show:          {sorted(p_shows)[n_current//2]:.1%}")
            if additional_approved > 0:
                print(f"\n  Stage 3 (Future Approvals Estimate):")
                print(f"    Expected future approvals: {additional_approved:.0f}")
                print(f"    Future approval show rate: {future_show_rate:.1%} (pooled historical rate)")
                print(f"    Expected from future:   {expected_from_future:.0f}")
            print(f"\n  TOTAL EXPECTED ATTENDEES: {expected_total:.0f} / {total_approved:.0f} ({expected_total/total_approved:.1%})")

            # Feature breakdown
            by_timing = defaultdict(list)
            by_tz = defaultdict(list)
            by_prior = defaultdict(list)
            by_pv = defaultdict(list)
            by_notif = defaultdict(list)

            for p in event_predictions:
                by_timing[p["timing_bucket"]].append(p["p_show"])
                by_tz[p["tz_distance"]].append(p["p_show"])
                by_prior[p["prior_rate"]].append(p["p_show"])
                by_pv[p["page_views_bucket"]].append(p["p_show"])
                by_notif[p["notif_read"]].append(p["p_show"])

            print(f"\n  By Timing:")
            for bucket in ["21+", "14-21", "7-14", "3-7", "1-3", "<1"]:
                if bucket in by_timing:
                    vals = by_timing[bucket]
                    print(f"    {bucket:>6}: {len(vals):3d} people, avg P_Show={sum(vals)/len(vals):.1%}, expected={sum(vals):.0f}")

            print(f"\n  By Timezone Distance:")
            for bucket in ["local", "near", "medium", "far"]:
                if bucket in by_tz:
                    vals = by_tz[bucket]
                    print(f"    {bucket:>8}: {len(vals):3d} people, avg P_Show={sum(vals)/len(vals):.1%}, expected={sum(vals):.0f}")

            print(f"\n  By Prior Attendance Rate:")
            for bucket in ["new", "0-25%", "25-50%", "50-75%", "75-100%"]:
                if bucket in by_prior:
                    vals = by_prior[bucket]
                    print(f"    {bucket:>8}: {len(vals):3d} people, avg P_Show={sum(vals)/len(vals):.1%}, expected={sum(vals):.0f}")

            print(f"\n  By Page Views:")
            for bucket in ["0", "1", "2-3", "4-5", "6-10", "11+"]:
                if bucket in by_pv:
                    vals = by_pv[bucket]
                    print(f"    {bucket:>6}: {len(vals):3d} people, avg P_Show={sum(vals)/len(vals):.1%}, expected={sum(vals):.0f}")

            print(f"\n  Notification Read:")
            for bucket in ["Yes", "No"]:
                if bucket in by_notif:
                    vals = by_notif[bucket]
                    print(f"    {bucket:>4}: {len(vals):3d} people, avg P_Show={sum(vals)/len(vals):.1%}, expected={sum(vals):.0f}")

            # Top 10 most likely
            print(f"\n  TOP 10 Most Likely to Show:")
            for i, p in enumerate(event_predictions[:10], 1):
                print(f"    {i:2d}. {p['name']:<30} P_Show={p['p_show_pct']:>5}  timing={p['timing_bucket']:<5}  tz={p['tz_distance']:<6}  prior={p['prior_rate']:<5}  views={p['page_views']}  notif={p['notif_read']}")

            # Bottom 10 least likely
            print(f"\n  BOTTOM 10 Least Likely to Show:")
            for i, p in enumerate(event_predictions[-10:], 1):
                print(f"    {i:2d}. {p['name']:<30} P_Show={p['p_show_pct']:>5}  timing={p['timing_bucket']:<5}  tz={p['tz_distance']:<6}  prior={p['prior_rate']:<5}  views={p['page_views']}  notif={p['notif_read']}")

            all_predictions.extend(event_predictions)

        cur.close()
    finally:
        conn.close()

    # Write CSV
    output_path = Path(__file__).parent.parent / "results" / "upcoming_predictions.csv"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    fields = [
        "event", "event_date", "event_city", "name", "email", "p_show", "p_show_pct",
        "timing_bucket", "days_before", "horizon_days", "tz_distance", "tz_offset_hrs",
        "prior_rate", "prior_events", "prior_attended",
        "page_views", "page_views_bucket", "notif_read",
    ]

    with open(output_path, "w", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fields)
        writer.writeheader()
        writer.writerows(all_predictions)

    print(f"\n{'='*80}")
    print(f"CSV saved to: {output_path}")
    print(f"Total predictions: {len(all_predictions)}")

    # Event-level summary
    print(f"\n{'='*80}")
    print(f"EVENT SUMMARY:")
    by_event = defaultdict(list)
    for p in all_predictions:
        by_event[p["event"]].append(p["p_show"])
    for event_name, p_shows in by_event.items():
        n = len(p_shows)
        expected = sum(p_shows)
        print(f"\n  {event_name}:")
        print(f"    Approved: {n}")
        print(f"    Expected attendees: {expected:.0f}")
        print(f"    Expected show rate: {expected/n:.0%}")
        print(f"    P_Show range: {min(p_shows):.1%} – {max(p_shows):.1%}")


if __name__ == "__main__":
    main()
