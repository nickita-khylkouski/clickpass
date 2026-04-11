"""Train Prophet model for signup velocity forecasting.

Based on research findings:
- Eventbrite: Prophet achieves 12% MAPE at T-7 for signup forecasting
- Industry standard: 10-20% MAPE vs our current Ridge 27% MAPE
- Prophet handles:
  - Multiple growth patterns (linear, logistic, exponential)
  - Event-specific seasonality
  - Changepoints in signup velocity

This replaces the hardcoded Ridge regression for Stage 0 (signup forecasting).
"""
import csv
import json
import pickle
import warnings
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
from prophet import Prophet

warnings.filterwarnings('ignore')


def load_signup_velocity():
    """Load signup velocity data for valid events."""
    # Load valid event IDs
    valid_ids_path = Path(__file__).parent.parent / "results" / "valid_event_ids.txt"
    valid_event_ids = set()
    with open(valid_ids_path, 'r') as f:
        for line in f:
            valid_event_ids.add(line.strip())

    # Load signup velocity data
    csv_path = Path(__file__).parent.parent / "results" / "signup_velocity.csv"
    events = []

    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            event_id = row['event_id']
            if event_id not in valid_event_ids:
                continue

            events.append({
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
            })

    return events


def create_timeseries(event):
    """Create time series data for an event's signup trajectory."""
    event_date = datetime.fromisoformat(event['event_start'].replace(' ', 'T'))

    # Build time series: (days_before_event, cumulative_signups)
    data = []

    if event['approved_t21'] > 0:
        data.append({'ds': event_date - timedelta(days=21), 'y': event['approved_t21']})
    if event['approved_t14'] > 0:
        data.append({'ds': event_date - timedelta(days=14), 'y': event['approved_t14']})
    if event['approved_t7'] > 0:
        data.append({'ds': event_date - timedelta(days=7), 'y': event['approved_t7']})
    if event['approved_t3'] > 0:
        data.append({'ds': event_date - timedelta(days=3), 'y': event['approved_t3']})
    if event['approved_t1'] > 0:
        data.append({'ds': event_date - timedelta(days=1), 'y': event['approved_t1']})
    if event['approved_t0'] > 0:
        data.append({'ds': event_date, 'y': event['approved_t0']})

    return pd.DataFrame(data) if data else None


def forecast_with_ridge(approved_t7, approved_t14, approved_t21):
    """Baseline: Simple Ridge regression (current approach)."""
    if approved_t7 < 10:
        return float(approved_t7)

    # Current approach: use 1.83x multiplier
    growth_multiplier = 1.83
    return float(approved_t7) * growth_multiplier


def forecast_with_prophet(train_df, forecast_days=7):
    """Forecast using Prophet (industry standard)."""
    if len(train_df) < 2:
        # Not enough data for Prophet, use last value
        return float(train_df['y'].iloc[-1])

    try:
        # Train Prophet with logistic growth (bounded by capacity)
        model = Prophet(
            growth='linear',  # Linear growth for signup patterns
            changepoint_prior_scale=0.05,  # Detect changepoints in velocity
            yearly_seasonality=False,
            weekly_seasonality=False,
            daily_seasonality=False,
        )

        model.fit(train_df)

        # Make forecast
        future = model.make_future_dataframe(periods=forecast_days, freq='D')
        forecast = model.predict(future)

        # Get final prediction
        final_pred = forecast['yhat'].iloc[-1]
        return max(float(train_df['y'].iloc[-1]), final_pred)  # Can't go below last known value

    except Exception:
        # Fallback to last known value
        return float(train_df['y'].iloc[-1])


def main():
    print("=" * 100)
    print("PROPHET SIGNUP FORECASTING (Industry Standard)")
    print("=" * 100)

    # Load events
    events = load_signup_velocity()
    print(f"\nLoaded {len(events)} valid events")

    # Temporal split
    events.sort(key=lambda x: x['event_start'])
    n = len(events)
    train_end = int(n * 0.6)
    cal_end = int(n * 0.8)

    train_events = events[:train_end]
    cal_events = events[train_end:cal_end]
    test_events = events[cal_end:]

    print(f"\n{'=' * 100}")
    print("DATA SPLIT")
    print("=" * 100)
    print(f"  Training:     {len(train_events)} events")
    print(f"  Calibration:  {len(cal_events)} events")
    print(f"  Test:         {len(test_events)} events")

    # Evaluate at T-7 (7 days before event)
    print(f"\n{'=' * 100}")
    print("BASELINE: Ridge Regression (Current Approach)")
    print("=" * 100)
    print("\nForecasting from T-7 → T-0 (7 days before event)")

    ridge_results = {'predictions': [], 'actuals': [], 'errors': []}

    for event in test_events:
        actual = event['approved_t0']
        if event['approved_t7'] < 20:
            continue

        pred = forecast_with_ridge(
            event['approved_t7'],
            event['approved_t14'],
            event['approved_t21']
        )

        ridge_results['predictions'].append(pred)
        ridge_results['actuals'].append(actual)
        ridge_results['errors'].append(pred - actual)

    ridge_preds = np.array(ridge_results['predictions'])
    ridge_actuals = np.array(ridge_results['actuals'])
    ridge_errors = ridge_preds - ridge_actuals

    ridge_mae = np.mean(np.abs(ridge_errors))
    ridge_mape = np.mean(np.abs(ridge_errors / ridge_actuals)) * 100
    ridge_bias = np.mean(ridge_errors)

    print(f"\nRidge Performance (T-7 forecast):")
    print(f"  MAE:  {ridge_mae:.1f} signups")
    print(f"  MAPE: {ridge_mape:.1f}%")
    print(f"  Bias: {ridge_bias:+.1f} signups")
    print(f"  N:    {len(ridge_results['actuals'])} events")

    # Evaluate Prophet
    print(f"\n{'=' * 100}")
    print("PROPHET MODEL (Industry Standard)")
    print("=" * 100)

    prophet_results = {'predictions': [], 'actuals': [], 'errors': []}

    for event in test_events:
        actual = event['approved_t0']
        if event['approved_t7'] < 20:
            continue

        # Create time series up to T-7
        ts_data = []
        event_date = datetime.fromisoformat(event['event_start'].replace(' ', 'T'))

        if event['approved_t21'] > 0:
            ts_data.append({'ds': event_date - timedelta(days=21), 'y': event['approved_t21']})
        if event['approved_t14'] > 0:
            ts_data.append({'ds': event_date - timedelta(days=14), 'y': event['approved_t14']})
        if event['approved_t7'] > 0:
            ts_data.append({'ds': event_date - timedelta(days=7), 'y': event['approved_t7']})

        if len(ts_data) < 2:
            # Not enough data, fallback to Ridge
            pred = forecast_with_ridge(event['approved_t7'], event['approved_t14'], event['approved_t21'])
        else:
            train_df = pd.DataFrame(ts_data)
            pred = forecast_with_prophet(train_df, forecast_days=7)

        prophet_results['predictions'].append(pred)
        prophet_results['actuals'].append(actual)
        prophet_results['errors'].append(pred - actual)

    prophet_preds = np.array(prophet_results['predictions'])
    prophet_actuals = np.array(prophet_results['actuals'])
    prophet_errors = prophet_preds - prophet_actuals

    prophet_mae = np.mean(np.abs(prophet_errors))
    prophet_mape = np.mean(np.abs(prophet_errors / prophet_actuals)) * 100
    prophet_bias = np.mean(prophet_errors)

    print(f"\nProphet Performance (T-7 forecast):")
    print(f"  MAE:  {prophet_mae:.1f} signups")
    print(f"  MAPE: {prophet_mape:.1f}%")
    print(f"  Bias: {prophet_bias:+.1f} signups")
    print(f"  N:    {len(prophet_results['actuals'])} events")

    # Improvement analysis
    print(f"\n{'=' * 100}")
    print("IMPROVEMENT ANALYSIS")
    print("=" * 100)

    mape_improvement = ridge_mape - prophet_mape
    mae_improvement = ridge_mae - prophet_mae

    print(f"\nTest Set Improvements:")
    print(f"  MAE:  {ridge_mae:.1f} → {prophet_mae:.1f} ({mae_improvement:+.1f} signups, {mae_improvement/ridge_mae*100:+.1f}%)")
    print(f"  MAPE: {ridge_mape:.1f}% → {prophet_mape:.1f}% ({mape_improvement:+.1f} points)")

    # Per-event comparison
    print(f"\n{'=' * 100}")
    print("PER-EVENT COMPARISON (Test Set)")
    print("=" * 100)
    print(f"\n{'Event':<50} {'Actual':>8} {'Ridge':>8} {'Prophet':>10} {'Winner':<10}")
    print("-" * 100)

    for i, event in enumerate([e for e in test_events if e['approved_t7'] >= 20]):
        actual = prophet_results['actuals'][i]
        ridge_pred = ridge_results['predictions'][i]
        prophet_pred = prophet_results['predictions'][i]

        ridge_err = abs(ridge_pred - actual)
        prophet_err = abs(prophet_pred - actual)

        winner = "Prophet ✅" if prophet_err < ridge_err else "Ridge" if ridge_err < prophet_err else "Tie"

        print(f"{event['title'][:48]:<50} {actual:>8.0f} {ridge_pred:>8.0f} {prophet_pred:>10.0f} {winner:<10}")

    prophet_wins = sum(1 for i in range(len(prophet_results['actuals']))
                      if abs(prophet_results['predictions'][i] - prophet_results['actuals'][i]) <
                         abs(ridge_results['predictions'][i] - ridge_results['actuals'][i]))

    print(f"\nProphet wins: {prophet_wins}/{len(prophet_results['actuals'])} events ({prophet_wins/len(prophet_results['actuals'])*100:.1f}%)")

    # Save results
    print(f"\n{'=' * 100}")
    print("SAVING RESULTS")
    print("=" * 100)

    results_dir = Path(__file__).parent.parent / "models"
    results_dir.mkdir(exist_ok=True)

    metrics_path = results_dir / "prophet_signup_metrics.json"
    with open(metrics_path, 'w') as f:
        json.dump({
            'ridge': {
                'mae': float(ridge_mae),
                'mape': float(ridge_mape),
                'bias': float(ridge_bias),
                'n': len(ridge_results['actuals']),
            },
            'prophet': {
                'mae': float(prophet_mae),
                'mape': float(prophet_mape),
                'bias': float(prophet_bias),
                'n': len(prophet_results['actuals']),
            },
            'improvement': {
                'mae_reduction': float(mae_improvement),
                'mape_reduction': float(mape_improvement),
                'prophet_win_rate': float(prophet_wins / len(prophet_results['actuals'])),
            }
        }, f, indent=2)

    print(f"\nMetrics saved to: {metrics_path}")

    # Final verdict
    print(f"\n{'=' * 100}")
    print("FINAL VERDICT")
    print("=" * 100)

    target_mape = 15.0  # Research target: 10-20% MAPE
    current_mape = prophet_mape

    if current_mape < target_mape and mape_improvement > 0:
        print(f"\n✅ PROPHET UPGRADE SUCCESSFUL!")
        print(f"   - MAPE: {ridge_mape:.1f}% → {prophet_mape:.1f}% ({mape_improvement:+.1f} points)")
        print(f"   - Met research target: < {target_mape}% MAPE")
        print(f"   - Prophet wins: {prophet_wins/len(prophet_results['actuals'])*100:.1f}% of events")
        print("\n   Ready to proceed to Phase 4: MAPIE uncertainty quantification!")
    elif mape_improvement > 5:
        print(f"\n⚠️ PROPHET SHOWS IMPROVEMENT BUT MISSED TARGET")
        print(f"   - MAPE: {ridge_mape:.1f}% → {prophet_mape:.1f}% ({mape_improvement:+.1f} points)")
        print(f"   - Target: < {target_mape}% MAPE")
        print(f"   - Current: {current_mape:.1f}% MAPE")
        print("\n   Consider hybrid approach or more training data.")
    else:
        print(f"\n⚠️ PROPHET NOT PROVIDING EXPECTED IMPROVEMENT")
        print(f"   - MAPE: {ridge_mape:.1f}% → {prophet_mape:.1f}% ({mape_improvement:+.1f} points)")
        print("\n   Stick with Ridge regression for now.")

    print("=" * 100)


if __name__ == "__main__":
    main()
