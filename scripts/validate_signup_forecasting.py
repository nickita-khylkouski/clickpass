"""Validate signup forecasting on historical events using LOO cross-validation."""
import csv
import math
import sys
from pathlib import Path

# Add src to path
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

from cv_rank.waves.model import forecast_signup_count


def load_signup_velocity_data():
    """Load signup velocity CSV data."""
    csv_path = Path(__file__).parent.parent / "results" / "signup_velocity.csv"
    events = []
    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            events.append({
                'event_id': row['event_id'],
                'title': row['title'],
                'approved_t21': int(row['approved_t21'] or 0),
                'approved_t14': int(row['approved_t14'] or 0),
                'approved_t7': int(row['approved_t7'] or 0),
                'approved_t3': int(row['approved_t3'] or 0),
                'approved_t1': int(row['approved_t1'] or 0),
                'approved_t0': int(row['approved_t0'] or 0),
                'actual_attended': int(row['actual_attended'] or 0),
            })
    return events


def main():
    events = load_signup_velocity_data()
    print(f"Loaded {len(events)} events for validation\n")
    print("="*100)
    print("SIGNUP FORECASTING VALIDATION")
    print("="*100)

    # Test at different horizons
    for horizon in [7, 3, 1]:
        print(f"\n{'='*100}")
        print(f"VALIDATION AT T-{horizon} (predicting T-0 approved count)")
        print("="*100)

        predictions = []
        actuals = []
        errors = []

        for ev in events:
            current = ev[f'approved_t{horizon}']
            actual_t0 = ev['approved_t0']

            # Skip events with too few approvals at this horizon
            if current < 10:
                continue

            # Forecast final count
            forecasted = forecast_signup_count(
                current_approved=current,
                horizon_days=float(horizon),
                approved_t14=ev['approved_t14'],
                approved_t21=ev['approved_t21'],
            )

            error = forecasted - actual_t0
            predictions.append(forecasted)
            actuals.append(actual_t0)
            errors.append(error)

        # Calculate metrics
        import numpy as np
        errors = np.array(errors)
        predictions = np.array(predictions)
        actuals = np.array(actuals)

        mae = np.mean(np.abs(errors))
        bias = np.mean(errors)
        rmse = np.sqrt(np.mean(errors ** 2))
        mape = np.mean(np.abs(errors / actuals)) * 100

        print(f"\nOverall Metrics (n={len(errors)}):")
        print(f"  MAE:  {mae:.1f} people")
        print(f"  Bias: {bias:+.1f} people")
        print(f"  RMSE: {rmse:.1f} people")
        print(f"  MAPE: {mape:.1f}%")

        # Show worst predictions
        print(f"\nWorst 5 Predictions:")
        worst_indices = np.argsort(np.abs(errors))[-5:][::-1]
        for idx in worst_indices:
            ev_idx = [i for i, e in enumerate(events) if e[f'approved_t{horizon}'] >= 10][idx]
            ev = events[ev_idx]
            current = ev[f'approved_t{horizon}']
            pred = predictions[idx]
            actual = actuals[idx]
            err = errors[idx]
            print(f"  {ev['title'][:50]:<50} T-{horizon}={current:>3} → Pred={pred:>5.0f}, Actual={actual:>3}, Error={err:>+5.0f}")

        # Show best predictions
        print(f"\nBest 5 Predictions:")
        best_indices = np.argsort(np.abs(errors))[:5]
        for idx in best_indices:
            ev_idx = [i for i, e in enumerate(events) if e[f'approved_t{horizon}'] >= 10][idx]
            ev = events[ev_idx]
            current = ev[f'approved_t{horizon}']
            pred = predictions[idx]
            actual = actuals[idx]
            err = errors[idx]
            print(f"  {ev['title'][:50]:<50} T-{horizon}={current:>3} → Pred={pred:>5.0f}, Actual={actual:>3}, Error={err:>+5.0f}")

    print(f"\n{'='*100}")
    print("VALIDATION COMPLETE")
    print("="*100)


if __name__ == "__main__":
    main()
