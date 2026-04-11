"""Deep dive into page views and notification read features."""
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
sep = '─' * 70

# =====================================================================
# PAGE VIEWS — FULL DEEP DIVE
# =====================================================================
print('╔' + '═'*68 + '╗')
print('║' + '  PAGE VIEWS — COMPLETE ANALYSIS'.center(68) + '║')
print('╚' + '═'*68 + '╝')

# 1. Raw distribution — every view count
print(f'\n{sep}')
print('  1. EXACT VIEW COUNT DISTRIBUTION')
print(sep)
cur.execute(f'''
    WITH views AS (
        SELECT i."userId", i.properties->>'eventId' AS event_id, COUNT(*) AS view_count
        FROM "Insight" i
        WHERE i."eventName" = 'PAGE_VIEW'
        AND i.properties->>'eventId' IS NOT NULL
        GROUP BY i."userId", i.properties->>'eventId'
    )
    SELECT
        COALESCE(v.view_count, 0) AS views,
        COUNT(*) AS n_applicants,
        SUM(ea."checkedIn"::int) AS showed,
        ROUND(100.0 * SUM(ea."checkedIn"::int) / NULLIF(COUNT(*), 0), 1) AS rate
    FROM "EventApplicant" ea
    LEFT JOIN views v ON v."userId" = ea."userId" AND v.event_id = ea."eventId"::text
    WHERE ea.status='approved' AND ea."eventId" IN ({placeholders})
    GROUP BY COALESCE(v.view_count, 0)
    ORDER BY views
''', event_ids)
rows = cur.fetchall()
print(f'  {"Views":>5} | {"N":>6} | {"Showed":>6} | {"Rate":>6} | Bar')
print(f'  {"─"*5}─┼─{"─"*6}─┼─{"─"*6}─┼─{"─"*6}─┼─{"─"*30}')
for r in rows:
    bar = '█' * int(r[3] / 2) if r[3] else ''
    if r[0] <= 20 or r[1] >= 50:
        print(f'  {r[0]:>5} | {r[1]:>6} | {r[2]:>6} | {r[3]:>5}% | {bar}')
total_applicants = sum(r[1] for r in rows)
print(f'\n  Total: {total_applicants} applicants')

# 2. Cumulative — what % of applicants have N+ views
print(f'\n{sep}')
print('  2. CUMULATIVE DISTRIBUTION')
print(sep)
cum_n = 0
cum_showed = 0
for r in reversed(rows):
    cum_n += r[1]
    cum_showed += r[2]
# Build cumulative from high to low
cum_data = []
cum_n = 0
cum_showed = 0
for r in reversed(rows):
    cum_n += r[1]
    cum_showed += r[2]
    cum_data.append((r[0], cum_n, cum_showed))
cum_data.reverse()
print(f'  {"Views ≥":>8} | {"N Applicants":>13} | {"% of Total":>10} | {"Show Rate":>10}')
print(f'  {"─"*8}─┼─{"─"*13}─┼─{"─"*10}─┼─{"─"*10}')
for views_min, n, showed in cum_data:
    if views_min in [0, 1, 2, 3, 5, 8, 11, 15, 20, 30, 50]:
        rate = round(100 * showed / n, 1) if n > 0 else 0
        pct = round(100 * n / total_applicants, 1)
        print(f'  {views_min:>8} | {n:>13} | {pct:>9}% | {rate:>9}%')

# 3. Per-event stability — does the pattern hold across events?
print(f'\n{sep}')
print('  3. PER-EVENT STABILITY (does page views predict in EVERY event?)')
print(sep)
cur.execute(f'''
    WITH views AS (
        SELECT i."userId", i.properties->>'eventId' AS event_id, COUNT(*) AS view_count
        FROM "Insight" i
        WHERE i."eventName" = 'PAGE_VIEW'
        AND i.properties->>'eventId' IS NOT NULL
        GROUP BY i."userId", i.properties->>'eventId'
    )
    SELECT
        pe.title,
        COUNT(*) AS n_approved,
        ROUND(100.0 * SUM(ea."checkedIn"::int) / NULLIF(COUNT(*), 0), 1) AS overall_rate,
        ROUND(100.0 * SUM(CASE WHEN COALESCE(v.view_count,0) <= 3 THEN ea."checkedIn"::int END)
            / NULLIF(SUM(CASE WHEN COALESCE(v.view_count,0) <= 3 THEN 1 END), 0), 1) AS low_view_rate,
        ROUND(100.0 * SUM(CASE WHEN COALESCE(v.view_count,0) > 3 THEN ea."checkedIn"::int END)
            / NULLIF(SUM(CASE WHEN COALESCE(v.view_count,0) > 3 THEN 1 END), 0), 1) AS high_view_rate,
        SUM(CASE WHEN COALESCE(v.view_count,0) <= 3 THEN 1 ELSE 0 END) AS n_low,
        SUM(CASE WHEN COALESCE(v.view_count,0) > 3 THEN 1 ELSE 0 END) AS n_high
    FROM "EventApplicant" ea
    JOIN "PlatformEvent" pe ON ea."eventId" = pe.id
    LEFT JOIN views v ON v."userId" = ea."userId" AND v.event_id = ea."eventId"::text
    WHERE ea.status='approved' AND ea."eventId" IN ({placeholders})
    GROUP BY pe.title, pe.id
    HAVING COUNT(*) >= 30
    ORDER BY COUNT(*) DESC
''', event_ids)
rows = cur.fetchall()
consistent = 0
print(f'  {"Event":<45} | {"N":>4} | {"Low≤3":>6} | {"High>3":>7} | {"Gap":>5}')
print(f'  {"─"*45}─┼─{"─"*4}─┼─{"─"*6}─┼─{"─"*7}─┼─{"─"*5}')
for r in rows:
    gap = (r[4] or 0) - (r[3] or 0)
    if gap > 0:
        consistent += 1
    marker = '✓' if gap > 0 else '✗'
    print(f'  {r[0][:45]:<45} | {r[1]:>4} | {r[3] or 0:>5}% | {r[4] or 0:>6}% | {gap:>+4.0f} {marker}')
print(f'\n  Pattern holds in {consistent}/{len(rows)} events ({round(100*consistent/len(rows))}%)')

# 4. View timing — are views before or after application?
print(f'\n{sep}')
print('  4. VIEW TIMING — before vs after application')
print(sep)
cur.execute(f'''
    WITH view_timing AS (
        SELECT
            i."userId",
            i.properties->>'eventId' AS event_id,
            COUNT(*) AS total_views,
            COUNT(*) FILTER (WHERE i."createdAt" < ea."createdAt") AS views_before_apply,
            COUNT(*) FILTER (WHERE i."createdAt" >= ea."createdAt") AS views_after_apply
        FROM "Insight" i
        JOIN "EventApplicant" ea ON ea."userId" = i."userId"
            AND i.properties->>'eventId' = ea."eventId"::text
        WHERE i."eventName" = 'PAGE_VIEW'
        AND i.properties->>'eventId' IS NOT NULL
        AND ea.status='approved' AND ea."eventId" IN ({placeholders})
        GROUP BY i."userId", i.properties->>'eventId', ea."createdAt"
    )
    SELECT
        CASE
            WHEN views_before_apply = 0 AND views_after_apply > 0 THEN 'only_after'
            WHEN views_before_apply > 0 AND views_after_apply = 0 THEN 'only_before'
            ELSE 'both'
        END AS pattern,
        CASE
            WHEN total_views <= 3 THEN 'low'
            WHEN total_views <= 10 THEN 'mid'
            ELSE 'high'
        END AS view_level,
        COUNT(*) AS n,
        SUM(ea."checkedIn"::int) AS showed,
        ROUND(100.0 * SUM(ea."checkedIn"::int) / NULLIF(COUNT(*), 0), 1) AS rate
    FROM view_timing vt
    JOIN "EventApplicant" ea ON ea."userId" = vt."userId"
        AND vt.event_id = ea."eventId"::text
    WHERE ea.status='approved' AND ea."eventId" IN ({placeholders})
    GROUP BY pattern, view_level
    HAVING COUNT(*) >= 20
    ORDER BY pattern, view_level
''', event_ids + event_ids)
print(f'  {"Pattern":<14} | {"Level":<5} | {"N":>5} | {"Rate":>6}')
print(f'  {"─"*14}─┼─{"─"*5}─┼─{"─"*5}─┼─{"─"*6}')
for r in cur.fetchall():
    print(f'  {r[0]:<14} | {r[1]:<5} | {r[2]:>5} | {r[4]:>5}%')

# 5. Interaction: page views × tz_distance
print(f'\n{sep}')
print('  5. INTERACTION: Page Views × TZ Distance')
print(sep)
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
            WHEN COALESCE(v.view_count, 0) <= 3 THEN 'low_views'
            WHEN v.view_count <= 10 THEN 'mid_views'
            ELSE 'high_views'
        END AS view_bucket,
        CASE
            WHEN ea."appliedFromTimeZone" LIKE '%%Los_Angeles%%' OR ea."appliedFromTimeZone" LIKE '%%San_Francisco%%'
                THEN 'local_SF'
            WHEN ea."appliedFromTimeZone" LIKE '%%America/%%'
                THEN 'US_other'
            WHEN ea."appliedFromTimeZone" LIKE '%%Europe/%%'
                THEN 'Europe'
            WHEN ea."appliedFromTimeZone" LIKE '%%Asia/%%'
                THEN 'Asia'
            ELSE 'other'
        END AS tz_group,
        COUNT(*) AS n,
        SUM(ea."checkedIn"::int) AS showed,
        ROUND(100.0 * SUM(ea."checkedIn"::int) / NULLIF(COUNT(*), 0), 1) AS rate
    FROM "EventApplicant" ea
    LEFT JOIN views v ON v."userId" = ea."userId" AND v.event_id = ea."eventId"::text
    WHERE ea.status='approved' AND ea."eventId" IN ({placeholders})
    GROUP BY view_bucket, tz_group
    HAVING COUNT(*) >= 20
    ORDER BY view_bucket, tz_group
''', event_ids)
print(f'  {"Views":<12} | {"TZ Group":<10} | {"N":>5} | {"Rate":>6}')
print(f'  {"─"*12}─┼─{"─"*10}─┼─{"─"*5}─┼─{"─"*6}')
for r in cur.fetchall():
    print(f'  {r[0]:<12} | {r[1]:<10} | {r[2]:>5} | {r[4]:>5}%')


# =====================================================================
# NOTIFICATION READ — FULL DEEP DIVE
# =====================================================================
print('\n')
print('╔' + '═'*68 + '╗')
print('║' + '  NOTIFICATION READ — COMPLETE ANALYSIS'.center(68) + '║')
print('╚' + '═'*68 + '╝')

# 1. Per-event stability
print(f'\n{sep}')
print('  1. PER-EVENT STABILITY')
print(sep)
cur.execute(f'''
    WITH notifs AS (
        SELECT pn."userId", pn.data->>'eventId' AS event_id, pn.read
        FROM "PlatformNotification" pn
        WHERE pn.type = 'event_application_status_change'
        AND pn.data->>'status' = 'approved'
    )
    SELECT
        pe.title,
        COUNT(*) AS n_approved,
        ROUND(100.0 * SUM(CASE WHEN n.read THEN ea."checkedIn"::int END)
            / NULLIF(SUM(CASE WHEN n.read THEN 1 END), 0), 1) AS read_rate,
        ROUND(100.0 * SUM(CASE WHEN n.read = false THEN ea."checkedIn"::int END)
            / NULLIF(SUM(CASE WHEN n.read = false THEN 1 END), 0), 1) AS unread_rate,
        SUM(CASE WHEN n.read THEN 1 ELSE 0 END) AS n_read,
        SUM(CASE WHEN n.read = false THEN 1 ELSE 0 END) AS n_unread
    FROM "EventApplicant" ea
    JOIN "PlatformEvent" pe ON ea."eventId" = pe.id
    LEFT JOIN notifs n ON n."userId" = ea."userId" AND n.event_id = ea."eventId"::text
    WHERE ea.status='approved' AND ea."eventId" IN ({placeholders})
    GROUP BY pe.title, pe.id
    HAVING COUNT(*) >= 30
    ORDER BY COUNT(*) DESC
''', event_ids)
rows = cur.fetchall()
consistent = 0
print(f'  {"Event":<45} | {"N":>4} | {"Read":>6} | {"Unread":>7} | {"Gap":>5}')
print(f'  {"─"*45}─┼─{"─"*4}─┼─{"─"*6}─┼─{"─"*7}─┼─{"─"*5}')
for r in rows:
    gap = (r[2] or 0) - (r[3] or 0)
    if gap > 0:
        consistent += 1
    marker = '✓' if gap > 0 else '✗'
    print(f'  {r[0][:45]:<45} | {r[1]:>4} | {r[2] or 0:>5}% | {r[3] or 0:>6}% | {gap:>+4.0f} {marker}')
print(f'\n  Pattern holds in {consistent}/{len(rows)} events ({round(100*consistent/len(rows))}%)')

# 2. Time between approval notification and event
print(f'\n{sep}')
print('  2. NOTIFICATION READ TIMING — when do people read it?')
print(sep)
cur.execute(f'''
    WITH notifs AS (
        SELECT pn."userId", pn.data->>'eventId' AS event_id, pn.read,
               pn."createdAt" AS notif_created,
               pn."updatedAt" AS notif_updated
        FROM "PlatformNotification" pn
        WHERE pn.type = 'event_application_status_change'
        AND pn.data->>'status' = 'approved'
    )
    SELECT
        CASE
            WHEN NOT n.read THEN 'never_read'
            WHEN EXTRACT(EPOCH FROM (n.notif_updated - n.notif_created)) / 3600 < 1 THEN 'within_1hr'
            WHEN EXTRACT(EPOCH FROM (n.notif_updated - n.notif_created)) / 3600 < 24 THEN 'within_1day'
            WHEN EXTRACT(EPOCH FROM (n.notif_updated - n.notif_created)) / 3600 < 72 THEN 'within_3days'
            ELSE 'after_3days'
        END AS read_speed,
        COUNT(*) AS n,
        SUM(ea."checkedIn"::int) AS showed,
        ROUND(100.0 * SUM(ea."checkedIn"::int) / NULLIF(COUNT(*), 0), 1) AS rate
    FROM "EventApplicant" ea
    LEFT JOIN notifs n ON n."userId" = ea."userId" AND n.event_id = ea."eventId"::text
    WHERE ea.status='approved' AND ea."eventId" IN ({placeholders})
    AND n."userId" IS NOT NULL
    GROUP BY read_speed
    ORDER BY rate DESC
''', event_ids)
print(f'  {"Read Speed":<15} | {"N":>6} | {"Showed":>6} | {"Rate":>6}')
print(f'  {"─"*15}─┼─{"─"*6}─┼─{"─"*6}─┼─{"─"*6}')
for r in cur.fetchall():
    print(f'  {r[0]:<15} | {r[1]:>6} | {r[2]:>6} | {r[3]:>5}%')

# 3. Interaction: notification × page views
print(f'\n{sep}')
print('  3. INTERACTION: Notification Read × Page Views')
print(sep)
cur.execute(f'''
    WITH views AS (
        SELECT i."userId", i.properties->>'eventId' AS event_id, COUNT(*) AS view_count
        FROM "Insight" i
        WHERE i."eventName" = 'PAGE_VIEW'
        AND i.properties->>'eventId' IS NOT NULL
        GROUP BY i."userId", i.properties->>'eventId'
    ),
    notifs AS (
        SELECT pn."userId", pn.data->>'eventId' AS event_id, pn.read
        FROM "PlatformNotification" pn
        WHERE pn.type = 'event_application_status_change'
        AND pn.data->>'status' = 'approved'
    )
    SELECT
        CASE WHEN n.read THEN 'read' ELSE 'unread' END AS notif,
        CASE
            WHEN COALESCE(v.view_count, 0) <= 3 THEN 'low_views'
            WHEN v.view_count <= 10 THEN 'mid_views'
            ELSE 'high_views'
        END AS view_bucket,
        COUNT(*) AS n,
        SUM(ea."checkedIn"::int) AS showed,
        ROUND(100.0 * SUM(ea."checkedIn"::int) / NULLIF(COUNT(*), 0), 1) AS rate
    FROM "EventApplicant" ea
    LEFT JOIN views v ON v."userId" = ea."userId" AND v.event_id = ea."eventId"::text
    LEFT JOIN notifs n ON n."userId" = ea."userId" AND n.event_id = ea."eventId"::text
    WHERE ea.status='approved' AND ea."eventId" IN ({placeholders})
    AND n."userId" IS NOT NULL
    GROUP BY notif, view_bucket
    HAVING COUNT(*) >= 20
    ORDER BY notif, view_bucket
''', event_ids)
print(f'  {"Notif":<8} | {"Views":<12} | {"N":>5} | {"Rate":>6}')
print(f'  {"─"*8}─┼─{"─"*12}─┼─{"─"*5}─┼─{"─"*6}')
for r in cur.fetchall():
    print(f'  {r[0]:<8} | {r[1]:<12} | {r[2]:>5} | {r[4]:>5}%')

# 4. Combined extreme profiles
print(f'\n{sep}')
print('  4. COMBINED EXTREMES — best vs worst profiles')
print(sep)
cur.execute(f'''
    WITH views AS (
        SELECT i."userId", i.properties->>'eventId' AS event_id, COUNT(*) AS view_count
        FROM "Insight" i
        WHERE i."eventName" = 'PAGE_VIEW'
        AND i.properties->>'eventId' IS NOT NULL
        GROUP BY i."userId", i.properties->>'eventId'
    ),
    notifs AS (
        SELECT pn."userId", pn.data->>'eventId' AS event_id, pn.read
        FROM "PlatformNotification" pn
        WHERE pn.type = 'event_application_status_change'
        AND pn.data->>'status' = 'approved'
    ),
    prior AS (
        SELECT ea2."userId", pe2.id AS event_id,
               COUNT(*) AS prior_events,
               SUM(ea2."checkedIn"::int) AS prior_attended
        FROM "EventApplicant" ea2
        JOIN "PlatformEvent" pe2 ON ea2."eventId" = pe2.id
        WHERE ea2.status = 'approved'
        GROUP BY ea2."userId", pe2.id
    )
    SELECT
        profile,
        COUNT(*) AS n,
        SUM(showed) AS total_showed,
        ROUND(100.0 * SUM(showed) / NULLIF(COUNT(*), 0), 1) AS rate
    FROM (
        SELECT
            ea."userId",
            ea."checkedIn"::int AS showed,
            CASE
                WHEN COALESCE(v.view_count, 0) >= 11
                     AND COALESCE(n.read, false)
                     AND EXTRACT(EPOCH FROM (pe."startDateTime" - ea."createdAt")) / 86400.0 < 3
                     AND ea."appliedFromTimeZone" LIKE '%%Los_Angeles%%'
                THEN 'BEST: 11+views + read + late + local'
                WHEN COALESCE(v.view_count, 0) >= 11
                     AND COALESCE(n.read, false)
                THEN 'GOOD: 11+views + read notif'
                WHEN COALESCE(v.view_count, 0) <= 1
                     AND NOT COALESCE(n.read, false)
                THEN 'WORST: ≤1view + unread notif'
                WHEN COALESCE(v.view_count, 0) <= 1
                     AND NOT COALESCE(n.read, false)
                     AND EXTRACT(EPOCH FROM (pe."startDateTime" - ea."createdAt")) / 86400.0 >= 14
                     AND ea."appliedFromTimeZone" LIKE '%%Asia/%%'
                THEN 'TERRIBLE: 1view + unread + early + Asia'
                ELSE NULL
            END AS profile
        FROM "EventApplicant" ea
        JOIN "PlatformEvent" pe ON ea."eventId" = pe.id
        LEFT JOIN views v ON v."userId" = ea."userId" AND v.event_id = ea."eventId"::text
        LEFT JOIN notifs n ON n."userId" = ea."userId" AND n.event_id = ea."eventId"::text
        WHERE ea.status='approved' AND ea."eventId" IN ({placeholders})
    ) sub
    WHERE profile IS NOT NULL
    GROUP BY profile
    ORDER BY rate DESC
''', event_ids)
print(f'  {"Profile":<45} | {"N":>5} | {"Rate":>6}')
print(f'  {"─"*45}─┼─{"─"*5}─┼─{"─"*6}')
for r in cur.fetchall():
    print(f'  {r[0]:<45} | {r[1]:>5} | {r[3]:>5}%')

# 5. What % of approved applicants fall into each combined bucket?
print(f'\n{sep}')
print('  5. FULL 3×2 MATRIX: Views × Notification')
print(sep)
cur.execute(f'''
    WITH views AS (
        SELECT i."userId", i.properties->>'eventId' AS event_id, COUNT(*) AS view_count
        FROM "Insight" i
        WHERE i."eventName" = 'PAGE_VIEW'
        AND i.properties->>'eventId' IS NOT NULL
        GROUP BY i."userId", i.properties->>'eventId'
    ),
    notifs AS (
        SELECT pn."userId", pn.data->>'eventId' AS event_id, pn.read
        FROM "PlatformNotification" pn
        WHERE pn.type = 'event_application_status_change'
        AND pn.data->>'status' = 'approved'
    )
    SELECT
        CASE
            WHEN COALESCE(v.view_count, 0) <= 3 THEN '1_low'
            WHEN v.view_count <= 10 THEN '2_mid'
            ELSE '3_high'
        END AS views,
        CASE WHEN COALESCE(n.read, false) THEN 'read' ELSE 'unread' END AS notif,
        COUNT(*) AS n,
        SUM(ea."checkedIn"::int) AS showed,
        ROUND(100.0 * SUM(ea."checkedIn"::int) / NULLIF(COUNT(*), 0), 1) AS rate,
        ROUND(100.0 * COUNT(*) / {total_applicants}, 1) AS pct_of_total
    FROM "EventApplicant" ea
    LEFT JOIN views v ON v."userId" = ea."userId" AND v.event_id = ea."eventId"::text
    LEFT JOIN notifs n ON n."userId" = ea."userId" AND n.event_id = ea."eventId"::text
    WHERE ea.status='approved' AND ea."eventId" IN ({placeholders})
    GROUP BY views, notif
    ORDER BY views, notif
''', event_ids)
print(f'  {"Views":<8} | {"Notif":<8} | {"N":>5} | {"Rate":>6} | {"% Total":>8}')
print(f'  {"─"*8}─┼─{"─"*8}─┼─{"─"*5}─┼─{"─"*6}─┼─{"─"*8}')
for r in cur.fetchall():
    print(f'  {r[0]:<8} | {r[1]:<8} | {r[2]:>5} | {r[4]:>5}% | {r[5]:>7}%')

cur.close()
conn.close()
