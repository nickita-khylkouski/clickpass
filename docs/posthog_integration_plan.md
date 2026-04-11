# PostHog Integration Plan For Waves V2

## Objective

Use PostHog to improve:

- early-horizon Stage 0 event demand forecasting (`T-14`, `T-7`)
- person-level attendance prediction (`T-3`, `T-1`)

without introducing identity mismatch, leakage, or mixing anonymous traffic with approved-user behavior.

## Core Conclusion

There are two different PostHog feature families and they should not be mixed blindly:

- `event_posthog_*`
  Event-level demand and intent signals, including anonymous traffic.
  Best for Stage 0.

- `person_posthog_*`
  Approved-applicant behavioral signals joined on canonical app `userId`.
  Best for attendance prediction.

## What We Can Reliably Join

### Safe person key

Use app `userId` as the canonical person key.

Safe join path:

- `UserProfile.email -> UserProfile.userId`
- `EventApplicant.userId`
- `Insight.userId`
- `PlatformNotification.userId` or `data.applicantUserId`

Do **not** use raw PostHog `distinct_id` as the primary production join key.

### Safe event key

Preferred:

- `properties.eventId`

Fallback only for event-level aggregates:

- parse event slug from `url`, `$current_url`, or `$pathname`

## Verified PostHog Signals

High-volume event-linked events already observed:

- `$pageview`
- `completion/apply`
- `click/event-register-button`
- `event/registration`
- `event/view-guest-list`
- `event/open-messages`
- `event/chat-send-message`
- `event/add-to-calendar`
- `hackathon/view-submission`

## Recommended Feature Split

## 1. Stage 0: Event-Level PostHog Features

Use only event-level horizon-safe aggregates here.

### Highest-priority features

- `ph_pageview_tH`
- `ph_register_click_tH`
- `ph_registration_tH`
- `ph_guest_list_tH`
- `ph_open_messages_tH`
- `ph_chat_send_tH`
- `ph_add_to_calendar_tH`
- `ph_submission_view_tH`

### Derived features

- `ph_register_click_per_pageview_tH`
- `ph_registration_per_register_click_tH`
- `ph_apply_completion_per_registration_tH`
- `ph_guest_list_per_current_approved_tH`
- `ph_open_messages_per_current_approved_tH`
- `ph_chat_send_per_open_messages_tH`
- `ph_add_to_calendar_per_current_approved_tH`

### Horizon guidance

- `T-14`
  Emphasize `$pageview`, register clicks, registrations, apply completions.
- `T-7`
  Keep `T-14` features and add guest-list and add-to-calendar.
- `T-3`
  Shift toward open-messages, chat-send-message, add-to-calendar, submission views.
- `T-1`
  Mostly commitment/activation: add-to-calendar, messages, chat, submission views.

### Important restriction

Do not use approved-user-only behavior inside Stage 0 until it is explicitly aggregated as an event-level feature with a clear denominator.

## 2. Attendance: Person-Level PostHog Features

Use only signals that can be joined to approved applicants by canonical `userId` and cut off at the snapshot time.

### Replace coarse features with richer versions

Current:

- `page_views`
- `notification_read`

Upgrade to:

- `person_recent_view_count_1d`
- `person_recent_view_count_3d`
- `person_recent_view_count_7d`
- `person_days_since_last_view`
- `person_distinct_active_view_days`
- `person_views_after_approval`
- `person_approval_to_first_view_hours`
- `person_notification_read_latency_hours`
- `person_notification_read_bucket`
- `person_notif_then_view_24h`

### Strong app-native non-PostHog additions

- `person_chat_user_msgs`
- `person_application_answer_chars`
- `person_waiver_agreed`
- `person_invited`

## Main Risks

### 1. Selection bias

If approved-user behavior leaks into Stage 0, the signup model will learn from the current approved cohort instead of future demand.

### 2. Anonymous traffic pollution

Raw event traffic includes many anonymous or non-applicant visitors. Event-level features must keep anonymous and identified traffic explicit.

### 3. Leakage

Every feature must be truncated at:

- `snapshot_time = event_start - horizon`

No post-snapshot reads, views, chats, or submissions.

### 4. Sparse early approvals

`T-14` and `T-7` approval pools are often tiny. That is exactly why Stage 0 should use event-level demand features from PostHog rather than approved-person features.

## Implementation Plan

## Phase 1. Event-level PostHog artifact for Stage 0

Add:

- `scripts/extract_posthog_event_velocity.py`

Output:

- `results/posthog_event_velocity.csv`

Rows by event, with cumulative counts at:

- `t21`, `t14`, `t7`, `t3`, `t1`, `t0`

for:

- `$pageview`
- `completion/apply`
- `click/event-register-button`
- `event/registration`
- `event/view-guest-list`
- `event/open-messages`
- `event/chat-send-message`
- `event/add-to-calendar`
- `hackathon/view-submission`

Also include:

- anonymous vs identified counts where possible
- URL/slug fallback resolution quality flags

## Phase 2. Wire PostHog event features into Stage 0

Modify:

- `src/cv_rank/waves/stage0_funnel.py`

Add:

- raw cumulative counts
- recent velocity
- acceleration
- normalized funnel ratios

First target:

- hybrid/two-stage Stage 0 only

Then compare against current Stage 0 on held-out `T-14/T-7/T-3/T-1`.

## Phase 3. Rich person-level attendance snapshot features

Modify:

- `src/cv_rank/waves/v2_pipeline.py`

Extend:

- `fetch_attendance_snapshot_rows()`
- `build_attendance_frame()`
- `attendance_feature_columns()`

Add the richer per-person recency, sequence, and notification-latency features above.

## Phase 4. Validation and ablations

Add:

- PostHog feature coverage audit by horizon
- anonymous share audit
- event-id resolution audit
- train/serve parity checks

Required ablations:

- baseline
- Stage 0 + event-level PostHog only
- attendance + person-level PostHog only
- combined

## Success Criteria

### Stage 0

- materially better `T-14` and `T-7` signup MAE/bias than current corrected baseline

### Attendance

- improve `T-3` / `T-1` without hurting calibration

### End-to-end

- reduce total attendance MAE at `T-7`
- no evidence of leakage
- stable train/live feature definitions

## Recommended First Slice

Implement only this first:

1. event-level PostHog extractor
2. Stage 0 features:
   - `$pageview`
   - `click/event-register-button`
   - `event/registration`
   - `event/view-guest-list`
   - `event/add-to-calendar`
3. rerun Stage 0 and end-to-end backtests

This is the smallest slice most likely to help the weak early horizons.
