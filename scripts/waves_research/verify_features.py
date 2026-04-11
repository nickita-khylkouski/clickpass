"""Deep verification of surprising feature findings."""
import os, psycopg2
from dotenv import load_dotenv
from pathlib import Path
load_dotenv(Path(__file__).resolve().parents[2] / ".env")
conn = psycopg2.connect(os.environ['PLATFORM_DATABASE_URL'])
cur = conn.cursor()

cur.execute('''
    SELECT pe.id FROM "PlatformEvent" pe
    WHERE pe.id IN (
        SELECT "eventId" FROM "EventApplicant" WHERE status='approved'
        GROUP BY "eventId" HAVING COUNT(*) >= 20
    )
    AND pe.id IN (
        SELECT "eventId" FROM "EventApplicant" WHERE "checkedIn"=true
        GROUP BY "eventId" HAVING COUNT(*) >= 5
    )
''')
event_ids = [r[0] for r in cur.fetchall()]
placeholders = ','.join(['%s'] * len(event_ids))
sep = '-' * 55

print('=' * 70)
print('  VERIFICATION 4: Page views — causal or just recency?')
print('=' * 70)
cur.execute(f'''
    WITH views AS (
        SELECT i."userId", i.properties->>'eventId' AS event_id, COUNT(*) AS view_count
        FROM "Insight" i
        WHERE i."eventName" = 'PAGE_VIEW'
        AND i.properties->>'eventId' IS NOT NULL
        GROUP BY i."userId", i.properties->>'eventId'
    )
    SELECT
        CASE
            WHEN COALESCE(v.view_count, 0) = 0 THEN '0_none'
            WHEN v.view_count = 1 THEN '1_one'
            WHEN v.view_count <= 3 THEN '2_two_three'
            WHEN v.view_count <= 5 THEN '3_four_five'
            WHEN v.view_count <= 10 THEN '4_six_ten'
            ELSE '5_eleven_plus'
        END AS view_bucket,
        CASE
            WHEN days_before >= 14 THEN 'early'
            WHEN days_before >= 3 THEN 'mid'
            ELSE 'late'
        END AS timing,
        COUNT(*) AS n,
        SUM(ea."checkedIn"::int) AS showed,
        ROUND(100.0 * SUM(ea."checkedIn"::int) / NULLIF(COUNT(*), 0), 1) AS rate
    FROM (
        SELECT ea.*,
               EXTRACT(EPOCH FROM (pe."startDateTime" - ea."createdAt")) / 86400.0 AS days_before
        FROM "EventApplicant" ea
        JOIN "PlatformEvent" pe ON ea."eventId" = pe.id
        WHERE ea.status='approved' AND ea."eventId" IN ({placeholders})
    ) ea
    LEFT JOIN views v ON v."userId" = ea."userId" AND v.event_id = ea."eventId"::text
    GROUP BY view_bucket, timing
    HAVING COUNT(*) >= 20
    ORDER BY view_bucket, timing
''', event_ids)
print(f'  View Bucket    | Timing | N     | Rate')
print(f'  {sep}')
for r in cur.fetchall():
    print(f'  {r[0]:<16} | {r[1]:<6} | {r[2]:<5} | {r[4]}%')

print()
print('=' * 70)
print('  VERIFICATION 5: Notification read — controlling for timing')
print('=' * 70)
cur.execute(f'''
    WITH notifs AS (
        SELECT pn."userId", pn.data->>'eventId' AS event_id, pn.read
        FROM "PlatformNotification" pn
        WHERE pn.type = 'event_application_status_change'
        AND pn.data->>'status' = 'approved'
    )
    SELECT
        CASE WHEN n.read IS NULL THEN 'no_notif'
             WHEN n.read THEN 'read'
             ELSE 'unread'
        END AS notif_status,
        CASE
            WHEN days_before >= 14 THEN 'early'
            WHEN days_before >= 3 THEN 'mid'
            ELSE 'late'
        END AS timing,
        COUNT(*) AS n,
        SUM(ea."checkedIn"::int) AS showed,
        ROUND(100.0 * SUM(ea."checkedIn"::int) / NULLIF(COUNT(*), 0), 1) AS rate
    FROM (
        SELECT ea.*,
               EXTRACT(EPOCH FROM (pe."startDateTime" - ea."createdAt")) / 86400.0 AS days_before
        FROM "EventApplicant" ea
        JOIN "PlatformEvent" pe ON ea."eventId" = pe.id
        WHERE ea.status='approved' AND ea."eventId" IN ({placeholders})
    ) ea
    LEFT JOIN notifs n ON n."userId" = ea."userId" AND n.event_id = ea."eventId"::text
    GROUP BY notif_status, timing
    HAVING COUNT(*) >= 20
    ORDER BY notif_status, timing
''', event_ids)
print(f'  Notif Status | Timing | N     | Rate')
print(f'  {sep}')
for r in cur.fetchall():
    print(f'  {r[0]:<14} | {r[1]:<6} | {r[2]:<5} | {r[4]}%')

print()
print('=' * 70)
print('  VERIFICATION 6: Invitation — why do invitees show LESS?')
print('=' * 70)
cur.execute(f'''
    SELECT
        CASE WHEN ei.id IS NOT NULL THEN 'invited' ELSE 'organic' END AS source,
        CASE
            WHEN days_before >= 14 THEN 'early'
            WHEN days_before >= 3 THEN 'mid'
            ELSE 'late'
        END AS timing,
        COUNT(*) AS n,
        SUM(ea."checkedIn"::int) AS showed,
        ROUND(100.0 * SUM(ea."checkedIn"::int) / NULLIF(COUNT(*), 0), 1) AS rate
    FROM (
        SELECT ea.*,
               EXTRACT(EPOCH FROM (pe."startDateTime" - ea."createdAt")) / 86400.0 AS days_before
        FROM "EventApplicant" ea
        JOIN "PlatformEvent" pe ON ea."eventId" = pe.id
        WHERE ea.status='approved' AND ea."eventId" IN ({placeholders})
    ) ea
    LEFT JOIN "EventInvitation" ei ON ei."userId" = ea."userId" AND ei."eventId" = ea."eventId"
    GROUP BY source, timing
    HAVING COUNT(*) >= 20
    ORDER BY source, timing
''', event_ids)
print(f'  Source   | Timing | N     | Rate')
print(f'  {sep}')
for r in cur.fetchall():
    print(f'  {r[0]:<8} | {r[1]:<6} | {r[2]:<5} | {r[4]}%')

# Invitee tz distribution
cur.execute(f'''
    SELECT
        ea."appliedFromTimeZone",
        COUNT(*) AS n,
        SUM(ea."checkedIn"::int) AS showed
    FROM "EventApplicant" ea
    JOIN "EventInvitation" ei ON ei."userId" = ea."userId" AND ei."eventId" = ea."eventId"
    WHERE ea.status='approved' AND ea."eventId" IN ({placeholders})
    GROUP BY ea."appliedFromTimeZone"
    HAVING COUNT(*) >= 10
    ORDER BY COUNT(*) DESC
    LIMIT 10
''', event_ids)
print()
print('  Top timezones for invited people:')
for r in cur.fetchall():
    rate = round(100 * r[2] / r[1], 1) if r[1] > 0 else 0
    print(f'    {r[0]}: n={r[1]}, show_rate={rate}%')

print()
print('=' * 70)
print('  VERIFICATION 7: Chi-squared significance for top features')
print('=' * 70)

# Page views: chi-squared
cur.execute(f'''
    WITH views AS (
        SELECT i."userId", i.properties->>'eventId' AS event_id, COUNT(*) AS view_count
        FROM "Insight" i
        WHERE i."eventName" = 'PAGE_VIEW'
        AND i.properties->>'eventId' IS NOT NULL
        GROUP BY i."userId", i.properties->>'eventId'
    )
    SELECT
        CASE WHEN COALESCE(v.view_count, 0) <= 3 THEN 'low_views' ELSE 'high_views' END AS grp,
        SUM(ea."checkedIn"::int) AS showed,
        SUM((NOT ea."checkedIn")::int) AS no_show
    FROM "EventApplicant" ea
    LEFT JOIN views v ON v."userId" = ea."userId" AND v.event_id = ea."eventId"::text
    WHERE ea.status='approved' AND ea."eventId" IN ({placeholders})
    GROUP BY grp
''', event_ids)
rows = cur.fetchall()
print('  Page Views (low <= 3 vs high > 3):')
for r in rows:
    print(f'    {r[0]}: showed={r[1]}, no_show={r[2]}')
# Manual chi-squared
a, b = rows[0][1], rows[0][2]  # high: showed, no_show
c, d = rows[1][1], rows[1][2]  # low: showed, no_show
n = a + b + c + d
chi2 = (n * (a * d - b * c) ** 2) / ((a + b) * (c + d) * (a + c) * (b + d))
print(f'    Chi-squared = {chi2:.1f} (>10.83 = p<0.001)')

# Notification read: chi-squared
cur.execute(f'''
    WITH notifs AS (
        SELECT pn."userId", pn.data->>'eventId' AS event_id, pn.read
        FROM "PlatformNotification" pn
        WHERE pn.type = 'event_application_status_change'
        AND pn.data->>'status' = 'approved'
    )
    SELECT
        CASE WHEN n.read THEN 'read' ELSE 'not_read' END AS grp,
        SUM(ea."checkedIn"::int) AS showed,
        SUM((NOT ea."checkedIn")::int) AS no_show
    FROM "EventApplicant" ea
    LEFT JOIN notifs n ON n."userId" = ea."userId" AND n.event_id = ea."eventId"::text
    WHERE ea.status='approved' AND ea."eventId" IN ({placeholders})
    AND n."userId" IS NOT NULL
    GROUP BY grp
''', event_ids)
rows = cur.fetchall()
print()
print('  Notification Read vs Unread:')
for r in rows:
    print(f'    {r[0]}: showed={r[1]}, no_show={r[2]}')
a, b = rows[0][1], rows[0][2]
c, d = rows[1][1], rows[1][2]
n = a + b + c + d
chi2 = (n * (a * d - b * c) ** 2) / ((a + b) * (c + d) * (a + c) * (b + d))
print(f'    Chi-squared = {chi2:.1f} (>10.83 = p<0.001)')

cur.close()
conn.close()
