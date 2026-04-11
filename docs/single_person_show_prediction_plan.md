# Single-Person Show Prediction (ML + Leakage-Safe Backtest)

## Task List
- [x] Build leakage-safe dataset for person-level prediction
- [x] Add stronger features (timing, prior history, city/theme/context)
- [x] Train ML model (logistic regression) per fold
- [x] Add LOEO backtest (event holdout)
- [x] Add temporal backtest (train only on earlier events)
- [x] Add optional OpenAI scorer + cache for comparison
- [x] Add single-person inference with top feature contributions

## What This Script Does
Use [predict_single_person_show.py](/Users/nickita/cv-rank/scripts/waves_research/predict_single_person_show.py).

Modes:
1. **Single prediction** for one candidate.
2. **Event holdout backtest** (`--backtest --backtest-split loeo`).
3. **Temporal holdout backtest** (`--backtest --backtest-split temporal`).

OpenAI scoring is optional (`--use-openai`) and cached.

## Leakage Controls
- Train/test split is event-based (no row leakage).
- Temporal mode trains only on events strictly before holdout date.
- User history features are prior-only windowed by event time:
  - `prior_approved`
  - `prior_checked`
- Day-of/after-start rows removed (`createdAt < startDateTime`).
- Low-signal events filtered (`event_checked >= min_event_checkins`).

## Features Used (ML)
Candidate-level:
- timing (`days_before_event` transforms + bucket)
- prior attendance rate bucket
- prior event count bucket

Event/context-level:
- city
- event theme (title keyword bucket)
- day-of-week
- same-day same-city overlap
- capacity bucket

Optional OpenAI feature path:
- uses the same structured features + profile description text
- returns `probability` and is blended/cached

## Commands
```bash
cd /Users/nickita/cv-rank

# LOEO event holdout
uv run python scripts/waves_research/predict_single_person_show.py --backtest --backtest-split loeo

# stricter temporal holdout
uv run python scripts/waves_research/predict_single_person_show.py --backtest --backtest-split temporal

# temporal holdout on random sample per event + OpenAI pilot
uv run python scripts/waves_research/predict_single_person_show.py \
  --backtest --backtest-split temporal \
  --backtest-sample-per-event 5 \
  --use-openai --openai-max-calls 10

# single person prediction
uv run python scripts/waves_research/predict_single_person_show.py \
  --event-date 2026-03-20 \
  --application-date 2026-03-18 \
  --prior-approved 0 \
  --prior-checked 0 \
  --city "San Francisco, CA, USA" \
  --event-title "Agents Hackathon" \
  --event-capacity 300
```

## Backtest Snapshot
LOEO (22 events, 7,006 rows):
- ML-only: AUC `0.605`, Brier `0.2401`, LogLoss `0.6731`
- Best blend: AUC `0.606`, Brier `0.2397`, LogLoss `0.6723`

Temporal holdout (6,586 rows):
- ML-only: AUC `0.599`, Brier `0.2404`, LogLoss `0.6738`
- Best blend: AUC `0.600`, Brier `0.2402`, LogLoss `0.6733`

Interpretation:
- Temporal holdout is the more realistic deployment estimate.
- ML + empirical features consistently beat heuristic-only.

## Extra Data To Add Next (Highest Impact)
1. `approved_at` timestamp (clean approval-lead-time feature)
2. RSVP behavior (`responded`, response latency, reminder response)
3. Recency features (`days_since_last_attended`, attendance streak)
4. Travel friction (candidate city/timezone distance to event)
5. External competition feed (same-city ±3d, same-topic overlap)
6. Event ops metadata (check-in friction, queue length, transit/weather disruptions)
