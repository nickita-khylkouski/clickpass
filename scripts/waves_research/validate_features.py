"""Validate P_Show feature hypotheses against platform DB."""
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

load_dotenv(Path(__file__).resolve().parents[2] / ".env")

import psycopg2
import psycopg2.extras

DSN = os.environ["PLATFORM_DATABASE_URL"]

def run_query(cur, sql, params=None):
    cur.execute(sql, params or ())
    return cur.fetchall()

def print_table(title, rows, headers, spread_col=2):
    print(f"\n{'='*60}")
    print(f"  {title}")
    print(f"{'='*60}")
    widths = [max(len(str(h)), max((len(str(r[i])) for r in rows), default=0)) for i, h in enumerate(headers)]
    header_line = " | ".join(f"{h:<{widths[i]}}" for i, h in enumerate(headers))
    print(f"  {header_line}")
    print(f"  {'-' * len(header_line)}")
    rates = []
    for row in rows:
        line = " | ".join(f"{str(row[i]):<{widths[i]}}" for i in range(len(headers)))
        print(f"  {line}")
        if row[spread_col] and str(row[spread_col]).endswith('%'):
            rates.append(float(str(row[spread_col]).rstrip('%')))
    if rates:
        print(f"\n  Spread: {min(rates):.1f}% → {max(rates):.1f}% = {max(rates)-min(rates):.1f} pp")
    return rates

def main():
    conn = psycopg2.connect(DSN)
    cur = conn.cursor()

    # Define usable events: approved >= 20, checkins >= 5
    cur.execute("""
        SELECT pe.id FROM "PlatformEvent" pe
        WHERE pe.id IN (
            SELECT "eventId" FROM "EventApplicant" WHERE status='approved'
            GROUP BY "eventId" HAVING COUNT(*) >= 20
        )
        AND pe.id IN (
            SELECT "eventId" FROM "EventApplicant" WHERE "checkedIn"=true
            GROUP BY "eventId" HAVING COUNT(*) >= 5
        )
    """)
    event_ids = [r[0] for r in cur.fetchall()]
    placeholders = ",".join(["%s"] * len(event_ids))
    print(f"Usable events: {len(event_ids)}")

    # Baseline
    cur.execute(f"""
        SELECT COUNT(*), SUM("checkedIn"::int)
        FROM "EventApplicant"
        WHERE status='approved' AND "eventId" IN ({placeholders})
    """, event_ids)
    total, showed = cur.fetchone()
    print(f"Approved applicants: {total:,}")
    print(f"Showed up: {showed:,} ({showed/total:.1%})")

    results = {}

    # ─────────────────────────────────────────────────────────────
    # 0. WAIVER: Can you check in without signing?
    # ─────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  WAIVER CROSS-TAB: Is waiver required for check-in?")
    print(f"{'='*60}")
    rows = run_query(cur, f"""
        SELECT "agreedToWaiver", "checkedIn", COUNT(*)
        FROM "EventApplicant"
        WHERE status='approved' AND "eventId" IN ({placeholders})
        GROUP BY "agreedToWaiver", "checkedIn"
        ORDER BY "agreedToWaiver", "checkedIn"
    """, event_ids)
    for agreed, checked, n in rows:
        print(f"  waiver={str(agreed):<6} checkedIn={str(checked):<6} n={n:,}")

    # Check if any events require waivers
    cur.execute("""SELECT column_name FROM information_schema.columns
                   WHERE table_name='PlatformEvent' AND column_name ILIKE '%waiver%'""")
    waiver_cols = [r[0] for r in cur.fetchall()]
    print(f"\n  PlatformEvent waiver columns: {waiver_cols}")
    if waiver_cols:
        for col in waiver_cols:
            if col == 'eventWaiver':
                continue  # skip — contains full waiver text JSON
            rows = run_query(cur, f"""
                SELECT pe."{col}", COUNT(DISTINCT ea."eventId"),
                       COUNT(*), SUM(ea."checkedIn"::int)
                FROM "EventApplicant" ea
                JOIN "PlatformEvent" pe ON ea."eventId"=pe.id
                WHERE ea.status='approved' AND ea."eventId" IN ({placeholders})
                GROUP BY pe."{col}"
            """, event_ids)
            for val, n_events, n_app, n_show in rows:
                rate = n_show/n_app if n_app else 0
                print(f"  {col}={val}: {n_events} events, {n_app:,} applicants, {rate:.1%} show rate")

    # ─────────────────────────────────────────────────────────────
    # 1. WAIVER as feature
    # ─────────────────────────────────────────────────────────────
    rows = run_query(cur, f"""
        SELECT
            CASE WHEN "agreedToWaiver" THEN 'agreed' ELSE 'not_agreed' END AS bucket,
            COUNT(*) AS n,
            SUM("checkedIn"::int) AS showed,
            ROUND(100.0 * SUM("checkedIn"::int) / COUNT(*), 1) || '%%' AS rate
        FROM "EventApplicant"
        WHERE status='approved' AND "eventId" IN ({placeholders})
        GROUP BY "agreedToWaiver"
        ORDER BY "agreedToWaiver"
    """, event_ids)
    pop_total = sum(r[1] for r in rows)
    pop_agreed = sum(r[1] for r in rows if r[0]=='agreed')
    print(f"\n  Population: {pop_agreed}/{pop_total} agreed ({pop_agreed/pop_total:.0%})")
    rates = print_table("Feature: Waiver Agreement", rows, ["Bucket", "N Approved", "N Showed", "Show Rate"], 3)
    if rates: results["waiver"] = max(rates) - min(rates)

    # ─────────────────────────────────────────────────────────────
    # 2. PAGE VIEWS (Insight table)
    # ─────────────────────────────────────────────────────────────
    rows = run_query(cur, f"""
        WITH views AS (
            SELECT
                i."userId",
                (i.properties->>'eventId')::uuid AS event_id,
                COUNT(*) AS view_count
            FROM "Insight" i
            WHERE i."eventName" = 'PAGE_VIEW'
              AND i."userId" IS NOT NULL
              AND i.properties->>'eventId' IS NOT NULL
            GROUP BY i."userId", (i.properties->>'eventId')::uuid
        )
        SELECT
            CASE
                WHEN COALESCE(v.view_count, 0) = 0 THEN '0_none'
                WHEN v.view_count = 1 THEN '1_one'
                WHEN v.view_count <= 3 THEN '2_two_three'
                WHEN v.view_count <= 5 THEN '3_four_five'
                WHEN v.view_count <= 10 THEN '4_six_ten'
                ELSE '5_eleven_plus'
            END AS bucket,
            COUNT(*) AS n,
            SUM(ea."checkedIn"::int) AS showed,
            ROUND(100.0 * SUM(ea."checkedIn"::int) / NULLIF(COUNT(*), 0), 1) || '%%' AS rate
        FROM "EventApplicant" ea
        LEFT JOIN views v ON v."userId" = ea."userId" AND v.event_id = ea."eventId"
        WHERE ea.status='approved' AND ea."eventId" IN ({placeholders})
        GROUP BY bucket
        ORDER BY bucket
    """, event_ids)
    has_views = sum(r[1] for r in rows if r[0] != '0_none')
    rates = print_table("Feature: Event Page Views", rows, ["Bucket", "N Approved", "N Showed", "Show Rate"], 3)
    print(f"  Population: {has_views}/{total} have view data ({has_views/total:.0%})")
    if rates: results["page_views"] = max(rates) - min(rates)

    # ─────────────────────────────────────────────────────────────
    # 3. NOTIFICATION READ STATUS
    # ─────────────────────────────────────────────────────────────
    # Approval notification: type=event_application_status_change, data->>'status'='approved'
    # eventId is in data->>'eventId', userId is pn."userId"
    rows = run_query(cur, f"""
        WITH notifs AS (
            SELECT DISTINCT ON (pn."userId", (pn.data->>'eventId')::uuid)
                pn."userId",
                (pn.data->>'eventId')::uuid AS event_id,
                pn.read
            FROM "PlatformNotification" pn
            WHERE pn.type = 'event_application_status_change'
              AND pn.data->>'status' = 'approved'
              AND pn.data->>'eventId' IS NOT NULL
            ORDER BY pn."userId", (pn.data->>'eventId')::uuid, pn."createdAt" DESC
        )
        SELECT
            CASE WHEN n."userId" IS NULL THEN 'no_notification'
                 WHEN n.read THEN 'read'
                 ELSE 'unread' END AS bucket,
            COUNT(*) AS n_app,
            SUM(ea."checkedIn"::int) AS showed,
            ROUND(100.0 * SUM(ea."checkedIn"::int) / NULLIF(COUNT(*), 0), 1) || '%%' AS rate
        FROM "EventApplicant" ea
        LEFT JOIN notifs n ON n."userId" = ea."userId" AND n.event_id = ea."eventId"
        WHERE ea.status='approved' AND ea."eventId" IN ({placeholders})
        GROUP BY bucket
        ORDER BY bucket
    """, event_ids)
    has_notif = sum(r[1] for r in rows if r[0] != 'no_notification')
    rates = print_table("Feature: Approval Notification Read", rows, ["Bucket", "N Approved", "N Showed", "Show Rate"], 3)
    print(f"  Population: {has_notif}/{total} have approval notification ({has_notif/total:.0%})")
    if rates: results["notification_read"] = max(rates) - min(rates)

    # ─────────────────────────────────────────────────────────────
    # 4. UTM SOURCE
    # ─────────────────────────────────────────────────────────────
    rows = run_query(cur, f"""
        SELECT
            COALESCE(ut.utm_source, 'no_utm') AS bucket,
            COUNT(*) AS n_app,
            SUM(ea."checkedIn"::int) AS showed,
            ROUND(100.0 * SUM(ea."checkedIn"::int) / NULLIF(COUNT(*), 0), 1) || '%%' AS rate
        FROM "EventApplicant" ea
        LEFT JOIN "UTMTracking" ut ON ea."utmTrackingId" = ut.id
        WHERE ea.status='approved' AND ea."eventId" IN ({placeholders})
        GROUP BY COALESCE(ut.utm_source, 'no_utm')
        HAVING COUNT(*) >= 20
        ORDER BY SUM(ea."checkedIn"::int)::float / NULLIF(COUNT(*), 0) DESC
    """, event_ids)
    has_utm = sum(r[1] for r in rows if r[0] != 'no_utm')
    rates = print_table("Feature: UTM Source", rows, ["Source", "N Approved", "N Showed", "Show Rate"], 3)
    print(f"  Population: {has_utm}/{total} have UTM data ({has_utm/total:.0%})")
    if rates: results["utm_source"] = max(rates) - min(rates)

    # ─────────────────────────────────────────────────────────────
    # 5. PROFILE COMPLETENESS
    # ─────────────────────────────────────────────────────────────
    rows = run_query(cur, f"""
        SELECT
            (CASE WHEN up."linkedinUsername" IS NOT NULL AND up."linkedinUsername" != '' THEN 1 ELSE 0 END +
             CASE WHEN up."githubUsername" IS NOT NULL AND up."githubUsername" != '' THEN 1 ELSE 0 END +
             CASE WHEN up."xHandle" IS NOT NULL AND up."xHandle" != '' THEN 1 ELSE 0 END +
             CASE WHEN up."phoneNumber" IS NOT NULL AND up."phoneNumber" != '' THEN 1 ELSE 0 END +
             CASE WHEN up."avatarUrl" IS NOT NULL AND up."avatarUrl" != '' THEN 1 ELSE 0 END
            ) AS profile_score,
            COUNT(*) AS n_app,
            SUM(ea."checkedIn"::int) AS showed,
            ROUND(100.0 * SUM(ea."checkedIn"::int) / NULLIF(COUNT(*), 0), 1) || '%%' AS rate
        FROM "EventApplicant" ea
        JOIN "UserProfile" up ON ea."userId" = up."userId"
        WHERE ea.status='approved' AND ea."eventId" IN ({placeholders})
        GROUP BY profile_score
        ORDER BY profile_score
    """, event_ids)
    rates = print_table("Feature: Profile Completeness (0-5)", rows, ["Score", "N Approved", "N Showed", "Show Rate"], 3)
    if rates: results["profile_completeness"] = max(rates) - min(rates)

    # ─────────────────────────────────────────────────────────────
    # 6. SOCIAL FOLLOWS
    # ─────────────────────────────────────────────────────────────
    rows = run_query(cur, f"""
        WITH follow_counts AS (
            SELECT "followerUserId" AS uid, COUNT(*) AS cnt FROM "UserFollow" GROUP BY "followerUserId"
            UNION ALL
            SELECT "followingUserId" AS uid, COUNT(*) AS cnt FROM "UserFollow" GROUP BY "followingUserId"
        ),
        user_follows AS (
            SELECT uid, SUM(cnt) AS total_follows FROM follow_counts GROUP BY uid
        )
        SELECT
            CASE
                WHEN COALESCE(uf.total_follows, 0) = 0 THEN '0_none'
                WHEN uf.total_follows <= 5 THEN '1_one_to_five'
                WHEN uf.total_follows <= 20 THEN '2_six_to_twenty'
                ELSE '3_twentyplus'
            END AS bucket,
            COUNT(*) AS n_app,
            SUM(ea."checkedIn"::int) AS showed,
            ROUND(100.0 * SUM(ea."checkedIn"::int) / NULLIF(COUNT(*), 0), 1) || '%%' AS rate
        FROM "EventApplicant" ea
        LEFT JOIN user_follows uf ON uf.uid = ea."userId"
        WHERE ea.status='approved' AND ea."eventId" IN ({placeholders})
        GROUP BY bucket
        ORDER BY bucket
    """, event_ids)
    rates = print_table("Feature: Social Follows", rows, ["Bucket", "N Approved", "N Showed", "Show Rate"], 3)
    if rates: results["social_follows"] = max(rates) - min(rates)

    # ─────────────────────────────────────────────────────────────
    # 7. PERSONAL INVITATION
    # ─────────────────────────────────────────────────────────────
    rows = run_query(cur, f"""
        SELECT
            CASE WHEN ei.id IS NOT NULL THEN 'invited' ELSE 'not_invited' END AS bucket,
            COUNT(*) AS n_app,
            SUM(ea."checkedIn"::int) AS showed,
            ROUND(100.0 * SUM(ea."checkedIn"::int) / NULLIF(COUNT(*), 0), 1) || '%%' AS rate
        FROM "EventApplicant" ea
        LEFT JOIN "EventInvitation" ei ON ei."userId" = ea."userId" AND ei."eventId" = ea."eventId"
        WHERE ea.status='approved' AND ea."eventId" IN ({placeholders})
        GROUP BY CASE WHEN ei.id IS NOT NULL THEN 'invited' ELSE 'not_invited' END
        ORDER BY bucket
    """, event_ids)
    rates = print_table("Feature: Personal Invitation", rows, ["Bucket", "N Approved", "N Showed", "Show Rate"], 3)
    if rates: results["invitation"] = max(rates) - min(rates)

    # ─────────────────────────────────────────────────────────────
    # 8. CHAT ENGAGEMENT
    # ─────────────────────────────────────────────────────────────
    rows = run_query(cur, f"""
        WITH chat AS (
            SELECT eac.id AS channel_id, eac."userId", eac."eventId",
                   COUNT(*) FILTER (WHERE eacm.role = 'user') AS user_msgs,
                   AVG(LENGTH(eacm.content)) FILTER (WHERE eacm.role = 'user') AS avg_len
            FROM "EventApplicationChannel" eac
            LEFT JOIN "EventApplicationChannelMessage" eacm ON eacm."channelId" = eac.id
            GROUP BY eac.id, eac."userId", eac."eventId"
        )
        SELECT
            CASE
                WHEN c."userId" IS NULL THEN '0_no_chat'
                WHEN COALESCE(c.user_msgs, 0) = 0 THEN '1_zero_msgs'
                WHEN c.user_msgs <= 3 THEN '2_one_to_three'
                WHEN c.user_msgs <= 10 THEN '3_four_to_ten'
                ELSE '4_eleven_plus'
            END AS bucket,
            COUNT(*) AS n_app,
            SUM(ea."checkedIn"::int) AS showed,
            ROUND(100.0 * SUM(ea."checkedIn"::int) / NULLIF(COUNT(*), 0), 1) || '%%' AS rate
        FROM "EventApplicant" ea
        LEFT JOIN chat c ON c."userId" = ea."userId" AND c."eventId" = ea."eventId"
        WHERE ea.status='approved' AND ea."eventId" IN ({placeholders})
        GROUP BY bucket
        ORDER BY bucket
    """, event_ids)
    rates = print_table("Feature: Chat Engagement (user messages)", rows, ["Bucket", "N Approved", "N Showed", "Show Rate"], 3)
    if rates: results["chat_engagement"] = max(rates) - min(rates)

    # ─────────────────────────────────────────────────────────────
    # 9. APPLICATION ANSWER QUALITY
    # ─────────────────────────────────────────────────────────────
    rows = run_query(cur, f"""
        WITH answers AS (
            SELECT eqa."applicantId",
                   COUNT(*) AS answer_count,
                   SUM(LENGTH(COALESCE(eqa.answer, ''))) AS total_chars
            FROM "EventQuestionAnswer" eqa
            GROUP BY eqa."applicantId"
        )
        SELECT
            CASE
                WHEN a."applicantId" IS NULL THEN '0_no_answers'
                WHEN a.total_chars < 100 THEN '1_short'
                WHEN a.total_chars < 500 THEN '2_medium'
                WHEN a.total_chars < 1000 THEN '3_long'
                ELSE '4_very_long'
            END AS bucket,
            COUNT(*) AS n_app,
            SUM(ea."checkedIn"::int) AS showed,
            ROUND(100.0 * SUM(ea."checkedIn"::int) / NULLIF(COUNT(*), 0), 1) || '%%' AS rate
        FROM "EventApplicant" ea
        LEFT JOIN answers a ON a."applicantId" = ea.id
        WHERE ea.status='approved' AND ea."eventId" IN ({placeholders})
        GROUP BY bucket
        ORDER BY bucket
    """, event_ids)
    rates = print_table("Feature: Application Answer Quality", rows, ["Bucket", "N Approved", "N Showed", "Show Rate"], 3)
    if rates: results["answer_quality"] = max(rates) - min(rates)

    # ─────────────────────────────────────────────────────────────
    # 10. DAY OF WEEK
    # ─────────────────────────────────────────────────────────────
    rows = run_query(cur, f"""
        SELECT
            TO_CHAR(pe."startDateTime", 'Dy') AS dow,
            EXTRACT(DOW FROM pe."startDateTime") AS dow_num,
            COUNT(*) AS n_app,
            SUM(ea."checkedIn"::int) AS showed,
            ROUND(100.0 * SUM(ea."checkedIn"::int) / NULLIF(COUNT(*), 0), 1) || '%%' AS rate
        FROM "EventApplicant" ea
        JOIN "PlatformEvent" pe ON ea."eventId" = pe.id
        WHERE ea.status='approved' AND ea."eventId" IN ({placeholders})
        GROUP BY TO_CHAR(pe."startDateTime", 'Dy'), EXTRACT(DOW FROM pe."startDateTime")
        ORDER BY dow_num
    """, event_ids)
    # strip dow_num from display
    display_rows = [(r[0], r[2], r[3], r[4]) for r in rows]
    rates = print_table("Feature: Day of Week", display_rows, ["Day", "N Approved", "N Showed", "Show Rate"], 3)
    if rates: results["day_of_week"] = max(rates) - min(rates)

    # ─────────────────────────────────────────────────────────────
    # SUMMARY RANKING
    # ─────────────────────────────────────────────────────────────
    print(f"\n{'='*60}")
    print(f"  FEATURE RANKING BY SPREAD")
    print(f"{'='*60}")
    for name, spread in sorted(results.items(), key=lambda x: -x[1]):
        bar = "█" * int(spread / 2)
        print(f"  {name:<25} {spread:>6.1f} pp  {bar}")

    cur.close()
    conn.close()

if __name__ == "__main__":
    main()
