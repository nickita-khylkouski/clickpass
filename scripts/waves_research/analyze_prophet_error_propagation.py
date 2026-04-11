"""Investigate why Prophet performs worse end-to-end despite Stage 0 improvement.

CRITICAL QUESTION:
- Prophet Stage 0: 18.7% MAPE, -1.5 bias (BETTER than Ridge 26.1%, +43.3)
- Prophet End-to-End: 30.2% MAPE, -28.2 bias (WORSE than Ridge 31.0%, -14.4)

WHY does Stage 0 improvement fail to translate?

Hypotheses:
1. Prophet's signup forecasts create bad inputs for Stage 1+2
2. Errors compound non-linearly through the pipeline
3. Prophet's -1.5 bias in one direction amplifies in later stages
4. The additional_signups calculation magnifies Prophet's errors
"""
import csv
import json
import warnings
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from prophet import Prophet

warnings.filterwarnings('ignore')


def load_valid_event_ids():
    """Load valid event IDs."""
    valid_ids_path = Path(__file__).resolve().parents[2] / "results" / "valid_event_ids.txt"
    valid_event_ids = set()
    with open(valid_ids_path, 'r') as f:
        for line in f:
            valid_event_ids.add(line.strip())
    return valid_event_ids


def load_signup_velocity():
    """Load signup velocity data for valid events."""
    valid_ids = load_valid_event_ids()
    csv_path = Path(__file__).resolve().parents[2] / "results" / "signup_velocity.csv"
    events = {}

    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            event_id = row['event_id']
            if event_id not in valid_ids:
                continue

            events[event_id] = {
                'event_id': event_id,
                'title': row['title'],
                'event_start': row['event_start'],
                'approved_t21': int(row['approved_t21'] or 0),
                'approved_t14': int(row['approved_t14'] or 0),
                'approved_t7': int(row['approved_t7'] or 0),
                'approved_t3': int(row['approved_t3'] or 0),
                'approved_t1': int(row['approved_t1'] or 0),
                'approved_t0': int(row['approved_t0'] or 0),
                'actual_attended': int(row['actual_attended'] or 0),
            }

    return events


def load_engagement_profiles():
    """Load engagement profile data."""
    valid_ids = load_valid_event_ids()
    csv_path = Path(__file__).resolve().parents[2] / "results" / "engagement_profiles.csv"

    by_event = defaultdict(lambda: {
        'showed_up': [],
        'signup_bucket': [],
    })

    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            event_id = row['event_id']
            if event_id not in valid_ids:
                continue

            by_event[event_id]['showed_up'].append(int(row['showed_up']))
            by_event[event_id]['signup_bucket'].append(row['signup_bucket'])

    return by_event


def forecast_signups_prophet(event, horizon_days=7):
    """Forecast final approved count using Prophet."""
    event_date = datetime.fromisoformat(event['event_start'].replace(' ', 'T'))

    # Build time series up to T-7
    ts_data = []
    if event['approved_t21'] > 0:
        ts_data.append({'ds': event_date - timedelta(days=21), 'y': event['approved_t21']})
    if event['approved_t14'] > 0:
        ts_data.append({'ds': event_date - timedelta(days=14), 'y': event['approved_t14']})
    if event['approved_t7'] > 0:
        ts_data.append({'ds': event_date - timedelta(days=7), 'y': event['approved_t7']})

    if len(ts_data) < 2:
        return float(event['approved_t7']) * 1.83

    try:
        train_df = pd.DataFrame(ts_data)

        model = Prophet(
            growth='linear',
            changepoint_prior_scale=0.05,
            yearly_seasonality=False,
            weekly_seasonality=False,
            daily_seasonality=False,
        )

        model.fit(train_df)
        future = model.make_future_dataframe(periods=horizon_days, freq='D')
        forecast = model.predict(future)

        final_pred = forecast['yhat'].iloc[-1]
        return max(float(train_df['y'].iloc[-1]), final_pred)

    except Exception:
        return float(event['approved_t7']) * 1.83


def forecast_signups_ridge(approved_t7):
    """Baseline: Simple Ridge regression (1.83x multiplier)."""
    if approved_t7 < 10:
        return float(approved_t7)
    return float(approved_t7) * 1.83


def analyze_error_propagation():
    """Analyze how errors propagate through the three-stage pipeline."""
    print("=" * 100)
    print("ERROR PROPAGATION ANALYSIS: Prophet vs Ridge")
    print("=" * 100)
    print("\nQuestion: Why does Prophet's Stage 0 improvement fail to translate end-to-end?")

    # Load data
    signup_data = load_signup_velocity()
    engagement_data = load_engagement_profiles()

    # Temporal split
    events = list(signup_data.values())
    events.sort(key=lambda x: x['event_start'])

    n = len(events)
    cal_end = int(n * 0.8)
    test_events = events[cal_end:]

    # Parameters
    AVG_SHOW_RATE_T7 = 0.382
    LATE_SHOW_RATE = 0.306

    print(f"\n{'=' * 100}")
    print("STAGE-BY-STAGE BREAKDOWN (Test Set)")
    print("=" * 100)

    results = []

    for event in test_events:
        approved_t7 = event['approved_t7']
        if approved_t7 < 20:
            continue

        engagement = engagement_data.get(event['event_id'])
        if not engagement:
            continue

        # Count early signups
        early_signups = [i for i, bucket in enumerate(engagement['signup_bucket'])
                        if bucket in ['T-14+', 'T-14 to T-7']]
        n_early = len(early_signups)

        if abs(n_early - approved_t7) > approved_t7 * 0.3:
            continue

        actual = event['actual_attended']

        # Stage 1+2: Expected from current approved
        expected_from_current = n_early * AVG_SHOW_RATE_T7

        # Prophet pipeline
        prophet_total = forecast_signups_prophet(event, horizon_days=7)
        prophet_additional = max(0, int(prophet_total - approved_t7))
        prophet_from_late = prophet_additional * LATE_SHOW_RATE
        prophet_prediction = expected_from_current + prophet_from_late

        # Ridge pipeline
        ridge_total = forecast_signups_ridge(approved_t7)
        ridge_additional = max(0, int(ridge_total - approved_t7))
        ridge_from_late = ridge_additional * LATE_SHOW_RATE
        ridge_prediction = expected_from_current + ridge_from_late

        # Stage 0 errors
        prophet_stage0_error = prophet_total - event['approved_t0']
        ridge_stage0_error = ridge_total - event['approved_t0']

        # End-to-end errors
        prophet_e2e_error = prophet_prediction - actual
        ridge_e2e_error = ridge_prediction - actual

        results.append({
            'title': event['title'][:40],
            'actual_attended': actual,
            'approved_t7': approved_t7,
            'approved_t0': event['approved_t0'],
            'n_early': n_early,
            'expected_from_current': expected_from_current,
            # Prophet
            'prophet_total': prophet_total,
            'prophet_additional': prophet_additional,
            'prophet_from_late': prophet_from_late,
            'prophet_prediction': prophet_prediction,
            'prophet_stage0_error': prophet_stage0_error,
            'prophet_e2e_error': prophet_e2e_error,
            # Ridge
            'ridge_total': ridge_total,
            'ridge_additional': ridge_additional,
            'ridge_from_late': ridge_from_late,
            'ridge_prediction': ridge_prediction,
            'ridge_stage0_error': ridge_stage0_error,
            'ridge_e2e_error': ridge_e2e_error,
        })

    # Print detailed breakdown
    print(f"\n{'Event':<42} {'Actual':>7} {'Appr T7':>8} {'Appr T0':>8}")
    print("-" * 100)
    for r in results:
        print(f"{r['title']:<42} {r['actual_attended']:>7.0f} {r['approved_t7']:>8.0f} {r['approved_t0']:>8.0f}")

    print(f"\n{'=' * 100}")
    print("STAGE 0: SIGNUP FORECASTING ERRORS")
    print("=" * 100)
    print(f"\n{'Event':<42} {'Actual T0':>9} {'Prophet':>9} {'Err':>6} {'Ridge':>7} {'Err':>6}")
    print("-" * 100)

    for r in results:
        print(f"{r['title']:<42} {r['approved_t0']:>9.0f} "
              f"{r['prophet_total']:>9.0f} {r['prophet_stage0_error']:>6.0f} "
              f"{r['ridge_total']:>7.0f} {r['ridge_stage0_error']:>6.0f}")

    # Stage 0 metrics
    prophet_s0_mae = np.mean([abs(r['prophet_stage0_error']) for r in results])
    prophet_s0_bias = np.mean([r['prophet_stage0_error'] for r in results])
    ridge_s0_mae = np.mean([abs(r['ridge_stage0_error']) for r in results])
    ridge_s0_bias = np.mean([r['ridge_stage0_error'] for r in results])

    print(f"\nStage 0 Metrics:")
    print(f"  Prophet: MAE={prophet_s0_mae:.1f}, Bias={prophet_s0_bias:+.1f}")
    print(f"  Ridge:   MAE={ridge_s0_mae:.1f}, Bias={ridge_s0_bias:+.1f}")

    print(f"\n{'=' * 100}")
    print("STAGE 3: LATE SIGNUP CONTRIBUTION")
    print("=" * 100)
    print(f"\n{'Event':<42} {'Prophet Add':>12} {'Late':>6} {'Ridge Add':>11} {'Late':>6}")
    print("-" * 100)

    for r in results:
        print(f"{r['title']:<42} {r['prophet_additional']:>12.0f} {r['prophet_from_late']:>6.1f} "
              f"{r['ridge_additional']:>11.0f} {r['ridge_from_late']:>6.1f}")

    print(f"\n{'=' * 100}")
    print("END-TO-END: FINAL PREDICTION ERRORS")
    print("=" * 100)
    print(f"\n{'Event':<42} {'Actual':>7} {'Prophet':>9} {'Err':>6} {'Ridge':>7} {'Err':>6}")
    print("-" * 100)

    for r in results:
        print(f"{r['title']:<42} {r['actual_attended']:>7.0f} "
              f"{r['prophet_prediction']:>9.1f} {r['prophet_e2e_error']:>6.1f} "
              f"{r['ridge_prediction']:>7.1f} {r['ridge_e2e_error']:>6.1f}")

    # End-to-end metrics
    prophet_e2e_mae = np.mean([abs(r['prophet_e2e_error']) for r in results])
    prophet_e2e_bias = np.mean([r['prophet_e2e_error'] for r in results])
    ridge_e2e_mae = np.mean([abs(r['ridge_e2e_error']) for r in results])
    ridge_e2e_bias = np.mean([r['ridge_e2e_error'] for r in results])

    print(f"\nEnd-to-End Metrics:")
    print(f"  Prophet: MAE={prophet_e2e_mae:.1f}, Bias={prophet_e2e_bias:+.1f}")
    print(f"  Ridge:   MAE={ridge_e2e_mae:.1f}, Bias={ridge_e2e_bias:+.1f}")

    # Error amplification analysis
    print(f"\n{'=' * 100}")
    print("ERROR AMPLIFICATION ANALYSIS")
    print("=" * 100)

    # Calculate correlation between Stage 0 error and end-to-end error
    prophet_s0_errors = np.array([r['prophet_stage0_error'] for r in results])
    prophet_e2e_errors = np.array([r['prophet_e2e_error'] for r in results])
    ridge_s0_errors = np.array([r['ridge_stage0_error'] for r in results])
    ridge_e2e_errors = np.array([r['ridge_e2e_error'] for r in results])

    prophet_corr = np.corrcoef(prophet_s0_errors, prophet_e2e_errors)[0, 1]
    ridge_corr = np.corrcoef(ridge_s0_errors, ridge_e2e_errors)[0, 1]

    print(f"\nCorrelation between Stage 0 error and End-to-End error:")
    print(f"  Prophet: r={prophet_corr:.3f}")
    print(f"  Ridge:   r={ridge_corr:.3f}")

    # Amplification factor
    prophet_amplification = prophet_e2e_bias / prophet_s0_bias if prophet_s0_bias != 0 else 0
    ridge_amplification = ridge_e2e_bias / ridge_s0_bias if ridge_s0_bias != 0 else 0

    print(f"\nBias Amplification Factor (End-to-End / Stage 0):")
    print(f"  Prophet: {prophet_amplification:.2f}x ({prophet_s0_bias:+.1f} → {prophet_e2e_bias:+.1f})")
    print(f"  Ridge:   {ridge_amplification:.2f}x ({ridge_s0_bias:+.1f} → {ridge_e2e_bias:+.1f})")

    # Additional signups contribution
    prophet_avg_additional = np.mean([r['prophet_additional'] for r in results])
    ridge_avg_additional = np.mean([r['ridge_additional'] for r in results])
    prophet_avg_late_contrib = np.mean([r['prophet_from_late'] for r in results])
    ridge_avg_late_contrib = np.mean([r['ridge_from_late'] for r in results])

    print(f"\nAverage Late Signup Contribution:")
    print(f"  Prophet: {prophet_avg_additional:.1f} additional signups → {prophet_avg_late_contrib:.1f} attendees")
    print(f"  Ridge:   {ridge_avg_additional:.1f} additional signups → {ridge_avg_late_contrib:.1f} attendees")

    # Key insights
    print(f"\n{'=' * 100}")
    print("KEY INSIGHTS")
    print("=" * 100)

    print(f"\n1. Stage 0 Performance (Signup Forecasting):")
    print(f"   Prophet MAE: {prophet_s0_mae:.1f} vs Ridge MAE: {ridge_s0_mae:.1f} ({(ridge_s0_mae-prophet_s0_mae)/ridge_s0_mae*100:+.1f}%)")
    print(f"   Prophet Bias: {prophet_s0_bias:+.1f} vs Ridge Bias: {ridge_s0_bias:+.1f}")

    print(f"\n2. End-to-End Performance:")
    print(f"   Prophet MAE: {prophet_e2e_mae:.1f} vs Ridge MAE: {ridge_e2e_mae:.1f} ({(ridge_e2e_mae-prophet_e2e_mae)/ridge_e2e_mae*100:+.1f}%)")
    print(f"   Prophet Bias: {prophet_e2e_bias:+.1f} vs Ridge Bias: {ridge_e2e_bias:+.1f}")

    print(f"\n3. Error Propagation:")
    if abs(prophet_amplification) > abs(ridge_amplification):
        print(f"   ⚠️ Prophet's errors amplify MORE through the pipeline ({prophet_amplification:.2f}x vs {ridge_amplification:.2f}x)")
    else:
        print(f"   Prophet's errors amplify LESS through the pipeline ({prophet_amplification:.2f}x vs {ridge_amplification:.2f}x)")

    print(f"\n4. Correlation Analysis:")
    print(f"   Prophet: Stage 0 → End-to-End correlation = {prophet_corr:.3f}")
    print(f"   Ridge:   Stage 0 → End-to-End correlation = {ridge_corr:.3f}")

    if abs(prophet_corr) > abs(ridge_corr):
        print(f"   Prophet's Stage 0 errors are MORE predictive of end-to-end errors")
    else:
        print(f"   Prophet's Stage 0 errors are LESS predictive of end-to-end errors")

    # Root cause hypothesis
    print(f"\n{'=' * 100}")
    print("ROOT CAUSE HYPOTHESIS")
    print("=" * 100)

    if abs(prophet_e2e_bias) > abs(ridge_e2e_bias):
        print(f"\n❌ PROPHET PERFORMS WORSE END-TO-END")
        print(f"\nPossible reasons:")
        print(f"  1. Prophet's Stage 0 errors affect Stage 3 (late signup contribution)")
        print(f"  2. Prophet forecasts {prophet_avg_additional:.1f} additional signups vs Ridge {ridge_avg_additional:.1f}")
        print(f"  3. Each additional signup contributes {LATE_SHOW_RATE*100:.1f}% show probability")
        print(f"  4. Prophet's errors in additional_signups get multiplied by {LATE_SHOW_RATE}")

        if prophet_s0_bias < 0:
            print(f"\n  Prophet UNDER-forecasts signups ({prophet_s0_bias:+.1f})")
            print(f"  → This reduces late signup contribution")
            print(f"  → End-to-end bias becomes MORE negative ({prophet_e2e_bias:+.1f})")
        else:
            print(f"\n  Prophet OVER-forecasts signups ({prophet_s0_bias:+.1f})")
            print(f"  → This increases late signup contribution")
            print(f"  → End-to-end bias becomes MORE positive ({prophet_e2e_bias:+.1f})")

    print("\n" + "=" * 100)

    # Save detailed results
    results_dir = Path(__file__).resolve().parents[2] / "models"
    results_path = results_dir / "error_propagation_analysis.json"

    with open(results_path, 'w') as f:
        json.dump({
            'stage0': {
                'prophet': {'mae': float(prophet_s0_mae), 'bias': float(prophet_s0_bias)},
                'ridge': {'mae': float(ridge_s0_mae), 'bias': float(ridge_s0_bias)},
            },
            'end_to_end': {
                'prophet': {'mae': float(prophet_e2e_mae), 'bias': float(prophet_e2e_bias)},
                'ridge': {'mae': float(ridge_e2e_mae), 'bias': float(ridge_e2e_bias)},
            },
            'amplification': {
                'prophet': float(prophet_amplification),
                'ridge': float(ridge_amplification),
            },
            'correlation': {
                'prophet': float(prophet_corr),
                'ridge': float(ridge_corr),
            },
            'late_signups': {
                'prophet_avg_additional': float(prophet_avg_additional),
                'ridge_avg_additional': float(ridge_avg_additional),
                'prophet_avg_contribution': float(prophet_avg_late_contrib),
                'ridge_avg_contribution': float(ridge_avg_late_contrib),
            },
            'events': results,
        }, f, indent=2)

    print(f"\nDetailed analysis saved to: {results_path}")


if __name__ == "__main__":
    analyze_error_propagation()
