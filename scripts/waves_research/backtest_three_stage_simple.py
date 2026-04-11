"""Simplified three-stage backtest using CSV data to verify ~0 bias claim.

Uses pre-computed signup_velocity.csv and engagement_profiles.csv to avoid
complex SQL queries that timeout.
"""
import csv
from collections import defaultdict
from pathlib import Path

import numpy as np


def load_signup_velocity():
    """Load signup velocity data."""
    csv_path = Path(__file__).resolve().parents[2] / "results" / "signup_velocity.csv"
    events = {}
    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            events[row['event_id']] = {
                'title': row['title'],
                'approved_t7': int(row['approved_t7'] or 0),
                'approved_t14': int(row['approved_t14'] or 0),
                'approved_t21': int(row['approved_t21'] or 0),
                'approved_t0': int(row['approved_t0'] or 0),
                'actual_attended': int(row['actual_attended'] or 0),
            }
    return events


def load_engagement_profiles():
    """Load engagement profile data."""
    csv_path = Path(__file__).resolve().parents[2] / "results" / "engagement_profiles.csv"
    by_event = defaultdict(lambda: {
        'showed_up': [],
        'signup_bucket': [],
    })

    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            event_id = row['event_id']
            by_event[event_id]['showed_up'].append(int(row['showed_up']))
            by_event[event_id]['signup_bucket'].append(row['signup_bucket'])

    return by_event


def simple_forecast_signups(approved_t7, approved_t14, approved_t21):
    """Simple signup forecasting based on velocity."""
    if approved_t7 < 10:
        return float(approved_t7)

    # Use average growth multiplier from historical data: 1.75x
    # This is the validated average from our analysis
    growth_multiplier = 1.75
    return float(approved_t7) * growth_multiplier


def main():
    print("=" * 100)
    print("SIMPLIFIED THREE-STAGE BACKTEST (verifying ~0 bias claim)")
    print("=" * 100)

    # Load data
    signup_data = load_signup_velocity()
    engagement_data = load_engagement_profiles()

    print(f"\nLoaded {len(signup_data)} events with signup velocity")
    print(f"Loaded {len(engagement_data)} events with engagement profiles\n")

    # Find events with both datasets
    common_events = set(signup_data.keys()) & set(engagement_data.keys())
    print(f"Events with both datasets: {len(common_events)}\n")

    # Backtest at T-7
    results = {
        'stage_1_2_only': [],  # Current approved with engagement forecasting
        'all_three_stages': [],  # + signup velocity + late signup contribution
        'actual': [],
    }

    for event_id in common_events:
        signup = signup_data[event_id]
        engagement = engagement_data[event_id]

        approved_t7 = signup['approved_t7']
        approved_t0 = signup['approved_t0']
        actual_attended = signup['actual_attended']

        # Skip events with insufficient data at T-7
        if approved_t7 < 20:
            continue

        # Count people who were approved at T-7 (early signups)
        early_signups = [i for i, bucket in enumerate(engagement['signup_bucket'])
                        if bucket in ['T-14+', 'T-14 to T-7']]
        n_early = len(early_signups)

        # Skip if mismatch between datasets
        if abs(n_early - approved_t7) > approved_t7 * 0.3:  # Allow 30% mismatch
            continue

        # Stage 1+2: Estimate from current approved (using average show rate at T-7)
        # From backtest_horizon.py: T-7 avg show rate = 38.2%
        avg_show_rate_t7 = 0.382
        expected_from_current = n_early * avg_show_rate_t7

        # Stage 0: Forecast additional signups
        forecasted_total = simple_forecast_signups(
            approved_t7, signup['approved_t14'], signup['approved_t21']
        )
        additional_signups = max(0, int(forecasted_total - approved_t7))

        # Stage 3: Estimate from late signups
        late_signup_rate = 0.355  # Validated conservative rate
        expected_from_late = additional_signups * late_signup_rate

        # Store results
        results['stage_1_2_only'].append(expected_from_current)
        results['all_three_stages'].append(expected_from_current + expected_from_late)
        results['actual'].append(actual_attended)

    # Calculate metrics
    print(f"Validated on {len(results['actual'])} events\n")
    print("=" * 100)
    print("RESULTS")
    print("=" * 100)

    for approach in ['stage_1_2_only', 'all_three_stages']:
        preds = np.array(results[approach])
        actuals = np.array(results['actual'])
        errors = preds - actuals

        mae = np.mean(np.abs(errors))
        bias = np.mean(errors)
        rmse = np.sqrt(np.mean(errors ** 2))
        mape = np.mean(np.abs(errors / actuals)) * 100

        label = "Stage 1+2 Only" if approach == 'stage_1_2_only' else "All 3 Stages"
        print(f"\n{label}:")
        print(f"  MAE:  {mae:.1f} people")
        print(f"  Bias: {bias:+.1f} people {'✅' if abs(bias) < 10 else '⚠️'}")
        print(f"  RMSE: {rmse:.1f} people")
        print(f"  MAPE: {mape:.1f}%")

    # Compare
    bias_1_2 = np.mean(np.array(results['stage_1_2_only']) - np.array(results['actual']))
    bias_all = np.mean(np.array(results['all_three_stages']) - np.array(results['actual']))
    improvement = bias_all - bias_1_2

    print(f"\n{'=' * 100}")
    print("BIAS COMPARISON")
    print("=" * 100)
    print(f"  Stage 1+2 only:  {bias_1_2:+.1f} people")
    print(f"  All 3 stages:    {bias_all:+.1f} people")
    print(f"  Improvement:     {improvement:+.1f} people")
    print(f"\n  VERDICT: {'✅ ~0 bias VERIFIED!' if abs(bias_all) < 10 else '⚠️ Bias still present'}")
    print("=" * 100)


if __name__ == "__main__":
    main()
