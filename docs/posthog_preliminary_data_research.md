# PostHog Preliminary Data Research

## Goal

Use more meaningful product analytics data to improve hackathon attendance forecasting, especially at `T-7` and `T-3`.

The current model already benefits from basic PostHog event-level demand signals, but it is still leaving useful information on the table.

## What We Checked

- Raw PostHog event taxonomy and linkability from `results/posthog_analysis/README.md`
- Current event-level extractor in `src/cv_rank/waves/posthog_features.py`
- Current Stage 0 artifact in `results/posthog_event_velocity.csv`
- Current attendance pipeline in `src/cv_rank/waves/v2_pipeline.py`
- Current evaluation artifact in `results/waves_v2/latest/evaluation_summary.json`

## Key Findings

### 1. The current Stage 0 PostHog artifact is incomplete

The live artifact currently shows only two event-level signals with real coverage:

- `pageview`
- `register_click`

But raw PostHog data for the same modeled hackathon slugs also has meaningful coverage for:

- `completion/apply`
- `event/registration`
- `event/view-guest-list`
- `event/open-messages`
- `event/add-to-calendar`

This means the current model is not limited by data availability. It is limited by extraction.

### 2. There is a real extractor/query-cap bug

When querying all modeled slugs at once, the grouped Stage 0 PostHog query returns exactly `50,000` rows and only includes:

- `$pageview`
- `click/event-register-button`

That indicates the PostHog query path is hitting an API row cap and truncating richer, later-sorted event types.

Practical implication:

- current Stage 0 event-level PostHog features are under-extracted
- adding more signals to the same monolithic query will not help
- extraction must batch by signal or slug chunk

### 3. Best event-level signals for early horizons

Raw coverage across the `61` modeled event slugs:

- `pageview`
  - `T-14`: `44/61` nonzero
  - `T-7`: `52/61`
  - `T-3`: `58/61`
  - `T-1`: `60/61`
- `completion/apply`
  - `T-14`: `32/61`
  - `T-7`: `44/61`
  - `T-3`: `52/61`
  - `T-1`: `56/61`
- `register_click`
  - `T-14`: `35/61`
  - `T-7`: `46/61`
  - `T-3`: `58/61`
  - `T-1`: `60/61`
- `registration`
  - `T-14`: `31/61`
  - `T-7`: `44/61`
  - `T-3`: `56/61`
  - `T-1`: `59/61`

These are the strongest candidates for improving early-horizon signup forecasting.

### 4. Best person-level signals for attendance

Raw coverage suggests these are the best next commitment features:

- `event/open-messages`
  - `T-3`: `31/61` events nonzero
  - `T-1`: `35/61`
- `event/view-guest-list`
  - `T-3`: `32/61`
  - `T-1`: `35/61`
- `event/add-to-calendar`
  - `T-3`: `30/61`
  - `T-1`: `33/61`

Likely lower-priority:

- `event/chat-send-message`
  - very sparse across modeled slugs
- `hackathon/view-submission`
  - high-value semantically, but currently absent in the modeled-slug horizon counts and should be investigated separately before integration

### 5. Current train/live skew still exists for person-level PostHog

Training can merge person-level PostHog features offline.

Live prediction currently zero-fills missing `ph_*` columns unless those features are explicitly fetched in the serving path.

That means:

- we should not promote new person-level PostHog attendance features until train and live extraction match

### 6. Evaluation constraints

Current evaluation support is strongest at:

- `T-3`
- `T-1`

Current `T-7` is operationally important, but still data-thin.

Current `T-14` should not drive promotion decisions.

## What This Means

### Highest-value next data integration

#### Stage 0: event-level demand

Add these first:

1. `completion/apply`
2. `event/registration`
3. `click/event-register-button`
4. `$pageview`

Then add derived features:

- short-window velocity
- acceleration
- funnel ratios:
  - `apply / click`
  - `registration / click`
  - `click / pageview`

#### Attendance: person-level commitment

Add these next:

1. `event/open-messages`
2. `event/view-guest-list`
3. `event/add-to-calendar`

And only after that:

4. `event/chat-send-message`
5. `hackathon/view-submission`

## Execution Plan

### Phase 1: Fix event-level PostHog extraction

- Replace the current all-signals-at-once Stage 0 query with batched extraction:
  - query one signal at a time, or
  - query slug chunks small enough to stay below PostHog row caps
- Regenerate `results/posthog_event_velocity.csv`
- Verify that `completion/apply`, `registration`, `guest_list`, and `add_to_calendar` are now nonzero in the artifact

Success criteria:

- nonzero coverage for `completion/apply` and `registration` at `T-7`
- no silent truncation to `50,000` grouped rows

### Phase 2: Upgrade Stage 0 with real demand signals

- Add these event-level features to Stage 0:
  - `ph_apply_*`
  - `ph_registration_*`
  - existing `ph_pageview_*`
  - existing `ph_register_click_*`
- Add velocity and ratio features
- Retrain Stage 0 only
- Judge on:
  - `signup.final_approved` MAE/bias at `T-7`
  - secondary check on end-to-end attendance at `T-7` and `T-3`

Promotion rule:

- must beat current Stage 0 on `T-3`
- should directionally improve `T-7`
- must not regress `T-1`

### Phase 3: Add person-level commitment actions

- Extend PostHog person extraction to include:
  - `open-messages`
  - `view-guest-list`
  - `add-to-calendar`
- Add:
  - count
  - recent-window count
  - after-approval count
  - recency to last commitment action
- Implement matching live-serving extraction so train/live stay aligned

Judge on:

- person AUC
- event-level `current_only` attendance MAE at `T-3` and `T-1`

### Phase 4: Combined bakeoff

Run four variants:

1. current baseline
2. Stage 0 PostHog only
3. attendance PostHog only
4. combined

Promote only the winning variant.

## Practical Recommendation

If we want the most likely meaningful improvement soonest:

1. Fix Stage 0 PostHog extraction
2. Add `completion/apply` and `registration`
3. Re-run `T-7` and `T-3`
4. Only then spend effort on richer person-level attendance signals

That is the shortest path to more real signal instead of more noise.
