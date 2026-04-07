# Databases

Use this reference when the event task touches live platform state.

Do not treat all databases as the same.

## Source Priority

Use these in this order:

1. `cv-rank` platform/event PostgreSQL
- best for live event/application/submission/reminder/blast state

2. `platform-api` schema truth
- best for canonical current field names and table structure

3. `cv-rank` Supabase enrichment store
- best for enrichment, applicant fallback, older applicant payloads, LinkedIn/GitHub/X data

## Credentials Source

Read-only credentials are available from:
- `/Users/nickita/cv-rank/.env`

Relevant variables:
- `PLATFORM_DATABASE_URL`
- `SUPABASE_URL`
- `SUPABASE_KEY`

Never print secrets into user-facing output.

## Platform DB Safe Read Pattern

Use local env loading and read-only queries.

Example:

```bash
python3 - <<'PY'
from dotenv import dotenv_values
import psycopg2

vals = dotenv_values('/Users/nickita/cv-rank/.env')
conn = psycopg2.connect(vals['PLATFORM_DATABASE_URL'], connect_timeout=10)
cur = conn.cursor()
cur.execute('SELECT COUNT(*) FROM "PlatformEvent"')
print(cur.fetchone()[0])
cur.close()
conn.close()
PY
```

## Supabase Safe Read Pattern

Use Supabase only when enrichment or fallback data matters.

```bash
python3 - <<'PY'
import json, urllib.request
from dotenv import dotenv_values

vals = dotenv_values('/Users/nickita/cv-rank/.env')
base = vals['SUPABASE_URL'].rstrip('/') + '/rest/v1/'
key = vals['SUPABASE_KEY']

req = urllib.request.Request(
    base + 'event_applicants?select=event_name,status,event_specific_data&limit=3',
    headers={'apikey': key, 'Authorization': f'Bearer {key}'},
)
with urllib.request.urlopen(req, timeout=20) as resp:
    print(json.load(resp))
PY
```

## Tables That Matter Most

Live counts observed from the current platform DB path:
- `PlatformEvent`: `376`
- `EventApplicant`: `41856`
- `EventReminder`: `186`
- `EventNotificationBlast`: `683`
- `HackathonSubmission`: `2412`
- `UTMTracking`: `150052`

### `PlatformEvent`

Use for:
- event page truth
- title / description / description summary
- event timing and location
- capacity
- approval requirement
- city / venue
- event slug
- hackathon gallery and judging flags
- waiver-related fields

Important columns:
- `id`
- `title`
- `description`
- `descriptionSummary`
- `startDateTime`
- `endDateTime`
- `location`
- `venue`
- `capacity`
- `slug`
- `city`
- `approvalRequired`
- `registrationClosed`
- `eventWaiver`
- `showGuestListBeforeApproval`
- `showLocationBeforeApproval`
- `showHackathonGallery`
- `hackathonJudgingOpen`

Use this first when the question is:
- is the event page created
- what copy is live
- is registration closed
- is approval required
- is gallery / judging enabled

### `EventApplicant`

Use for:
- application volume
- status breakdowns
- approval/rejection/waitlist state
- check-in state
- waiver agreement state
- inferred location and timezone hints
- UTM linkage

Important columns:
- `id`
- `status`
- `checkedIn`
- `eventId`
- `userId`
- `createdAt`
- `updatedAt`
- `eventApplicationChannelId`
- `agreedToWaiver`
- `utmTrackingId`
- `inferredCountryCode`
- `inferredRegion`
- `inferredCity`

Use this first when the question is:
- are applications rolling in
- how many were approved
- who checked in
- did waiver acceptance happen
- what did approval operations actually do

### `EventReminder`

Use for:
- scheduled reminder existence
- reminder timing offsets
- whether the reminder was sent
- channel split between text and email

Important columns:
- `id`
- `eventId`
- `reminderOffsetMinutes`
- `sent`
- `isText`
- `isEmail`
- `createdAt`
- `updatedAt`

Use this when the question is:
- did hacker reminders go out
- what reminder cadence exists
- is a reminder node really backed by live platform state

### `EventNotificationBlast`

Use for:
- blast existence
- blast content
- scheduled vs sent timing
- channel type
- target status

Important columns:
- `id`
- `eventId`
- `content`
- `isEmail`
- `isSMS`
- `targetStatus`
- `scheduledAt`
- `sentAt`
- `sendingHostUserId`

Use this when the question is:
- did a specific blast actually happen
- what content went out
- was the blast email or SMS
- when was it scheduled or sent

### `HackathonSubmission`

Use for:
- project submission volume
- team names
- placements and public-vote placements

Important columns:
- `id`
- `eventId`
- `teamName`
- `teamNumber`
- `placement`
- `publicVotePlacement`
- `createdAt`
- `updatedAt`

Use this when the question is:
- are submissions open / populated
- how many projects were submitted
- who won
- is the gallery / judging step backed by real data

### `UTMTracking`

Use for:
- acquisition and attribution context
- source / medium / campaign / term / content dimensions

Important columns:
- `id`
- `utm_source`
- `utm_medium`
- `utm_campaign`
- `utm_term`
- `utm_content`
- `createdAt`

Use this when the question is:
- where applicants came from
- how a campaign performed
- whether a source-specific applicant segment exists

## Supabase Role

The `cv-rank` Supabase store is primarily:
- enrichment
- fallback
- historical applicant context

The enrichment code confirms it pulls from:
- LinkedIn profile tables
- GitHub profile / analytics / repositories
- event history and Q&A answers
- organization tables
- X/Twitter handles

Path:
- `/Users/nickita/cv-rank/src/cv_rank/enrichment/supabase.py`

Prefer platform DB over Supabase for:
- current live event state
- reminders / blasts
- current applicant state
- current submissions

Use Supabase when you need:
- enriched attendee/applicant profile data
- older or fallback applicant payloads
- LinkedIn / GitHub / X context

## Practical Rules

- `PlatformEvent` first for event page truth.
- `EventApplicant` first for approval/intake truth.
- `EventReminder` and `EventNotificationBlast` first for actual comms-send truth.
- `HackathonSubmission` first for project/judging/submission truth.
- `UTMTracking` for acquisition truth.
- Supabase only after the live platform tables or checked-in schema are insufficient.

## Fast Helper

Use the local snapshot CLI added by this skill:

```bash
python3 /Users/nickita/.codex/skills/event-master/scripts/event_platform_snapshot.py --recent 10
python3 /Users/nickita/.codex/skills/event-master/scripts/event_platform_snapshot.py --slug "gemini-3-nyc-hackathon"
python3 /Users/nickita/.codex/skills/event-master/scripts/event_platform_snapshot.py --title-like "Anthropic"
```
