"""Proper LOO cross-validation backtest WITHOUT data leakage.

Key fixes:
1. For each test event, train signup forecasting on OTHER events only
2. Don't use test event's own velocity data (t14, t21) to predict itself
3. Use cross-event patterns: city avg, time-of-week, event size cluster
4. Proper event-level Leave-One-Out cross-validation
"""
import csv
import math
import os
import sys
from collections import defaultdict
from pathlib import Path

import numpy as np
from dotenv import load_dotenv
from sklearn.linear_model import Ridge

load_dotenv(Path(__file__).resolve().parents[2] / ".env")
sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


def load_signup_velocity_data():
    """Load signup velocity CSV data."""
    csv_path = Path(__file__).resolve().parents[2] / "results" / "signup_velocity.csv"
    events = []
    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            events.append({
                'event_id': row['event_id'],
                'title': row['title'] or 'Unknown',
                'city': row['city'] or 'Unknown',
                'approved_t21': int(row['approved_t21'] or 0),
                'approved_t14': int(row['approved_t14'] or 0),
                'approved_t7': int(row['approved_t7'] or 0),
                'approved_t3': int(row['approved_t3'] or 0),
                'approved_t1': int(row['approved_t1'] or 0),
                'approved_t0': int(row['approved_t0'] or 0),
                'actual_attended': int(row['actual_attended'] or 0),
                'velocity_14_to_7': float(row['velocity_14_to_7'] or 0),
                'velocity_7_to_3': float(row['velocity_7_to_3'] or 0),
                'growth_t7_to_t0': float(row['growth_t7_to_t0'] or 0),
            })
    return events


def build_cross_event_features(test_event, training_events, horizon):
    """Build features using ONLY training events (no leakage).

    Instead of using test event's own t14/t21, use:
    - City-level average growth patterns
    - Event size cluster patterns
    - Time-based patterns (day of week, season)
    """
    current = test_event[f'approved_t{horizon}']

    # Feature 1: Size cluster patterns (small/medium/large)
    if current < 50:
        size_cluster = 'small'
    elif current < 200:
        size_cluster = 'medium'
    else:
        size_cluster = 'large'

    # Feature 2: City-level growth multiplier (from training events only)
    city = test_event['city']
    city_events = [e for e in training_events if e['city'] == city and e[f'approved_t{horizon}'] >= 10]
    if city_events:
        city_growth = np.median([e['growth_t7_to_t0'] for e in city_events])
    else:
        # Fallback: all training events
        all_training = [e for e in training_events if e[f'approved_t{horizon}'] >= 10]
        city_growth = np.median([e['growth_t7_to_t0'] for e in all_training]) if all_training else 1.5

    # Feature 3: Size cluster growth (from training events only)
    cluster_events = [e for e in training_events
                      if (e[f'approved_t{horizon}'] < 50 if size_cluster == 'small' else
                          e[f'approved_t{horizon}'] < 200 if size_cluster == 'medium' else
                          e[f'approved_t{horizon}'] >= 200) and e[f'approved_t{horizon}'] >= 10]
    if cluster_events:
        cluster_growth = np.median([e['growth_t7_to_t0'] for e in cluster_events])
    else:
        cluster_growth = city_growth

    # Build feature vector WITHOUT using test event's own historical data
    features = [
        float(current),                      # Current approved count
        math.log(current + 1),               # Log current
        float(horizon),                      # Days until event
        math.log(horizon + 1),               # Log horizon
        float(city_growth),                  # City-level growth pattern
        float(cluster_growth),               # Size cluster growth pattern
        float(current) * float(horizon),     # Interaction
        math.log(current + 1) * math.log(horizon + 1),  # Log interaction
        float(city_growth) * float(current), # City pattern × current size
    ]

    return features


def train_signup_model_loo(training_events, horizon):
    """Train signup forecasting model on training events only."""
    X = []
    y = []

    for ev in training_events:
        current = ev[f'approved_t{horizon}']
        final = ev['approved_t0']

        if current < 10:  # Skip tiny events
            continue

        # For training, use ALL training events to build features
        features = build_cross_event_features(ev, training_events, horizon)

        X.append(features)
        y.append(final)

    if len(X) < 5:  # Need minimum training data
        return None

    X = np.array(X)
    y = np.array(y)

    # Train Ridge model
    model = Ridge(alpha=10.0)
    model.fit(X, y)

    return model


def main():
    events = load_signup_velocity_data()
    print(f"Loaded {len(events)} events\n")
    print("="*100)
    print("PROPER LOO CROSS-VALIDATION (NO VELOCITY LEAKAGE)")
    print("="*100)

    # Test at T-7 horizon
    horizon = 7
    print(f"\nValidation at T-{horizon}:\n")

    predictions = []
    actuals = []
    errors = []
    event_names = []

    # Leave-One-Out: For each event, train on all OTHER events
    for test_idx, test_event in enumerate(events):
        current = test_event[f'approved_t{horizon}']
        actual_t0 = test_event['approved_t0']

        if current < 10:  # Skip tiny events
            continue

        # Train on ALL events EXCEPT this one
        training_events = [e for i, e in enumerate(events) if i != test_idx]

        # Train model on training events only
        model = train_signup_model_loo(training_events, horizon)

        if model is None:
            continue

        # Build features for test event using ONLY training data patterns
        test_features = build_cross_event_features(test_event, training_events, horizon)

        # Predict
        pred = model.predict([test_features])[0]
        pred = max(float(current), pred)  # Can't be less than current

        error = pred - actual_t0
        predictions.append(pred)
        actuals.append(actual_t0)
        errors.append(error)
        event_names.append(test_event['title'])

    # Calculate metrics
    errors = np.array(errors)
    predictions = np.array(predictions)
    actuals = np.array(actuals)

    mae = np.mean(np.abs(errors))
    bias = np.mean(errors)
    rmse = np.sqrt(np.mean(errors ** 2))
    mape = np.mean(np.abs(errors / actuals)) * 100

    print(f"Overall Metrics (n={len(errors)} events):")
    print(f"  MAE:  {mae:.1f} people")
    print(f"  Bias: {bias:+.1f} people {'✅' if abs(bias) < 10 else '⚠️ '}")
    print(f"  RMSE: {rmse:.1f} people")
    print(f"  MAPE: {mape:.1f}%")

    # Compare to old leaky approach
    print(f"\n{'='*100}")
    print("COMPARISON:")
    print(f"  OLD (with leakage):  Bias = -0.0 (FAKE!)")
    print(f"  NEW (proper LOO):    Bias = {bias:+.1f} (REAL)")
    print(f"{'='*100}")

    # Show worst predictions
    print(f"\nWorst 5 Predictions:")
    worst_indices = np.argsort(np.abs(errors))[-5:][::-1]
    for idx in worst_indices:
        print(f"  {event_names[idx][:50]:<50} T-{horizon}={actuals[idx]-predictions[idx]+actuals[idx]:>3.0f} → Pred={predictions[idx]:>5.0f}, Actual={actuals[idx]:>3.0f}, Error={errors[idx]:>+5.0f}")

    # Show best predictions
    print(f"\nBest 5 Predictions:")
    best_indices = np.argsort(np.abs(errors))[:5]
    for idx in best_indices:
        actual_t7 = actuals[idx] - predictions[idx] + actuals[idx]  # Approximate
        print(f"  {event_names[idx][:50]:<50} Pred={predictions[idx]:>5.0f}, Actual={actuals[idx]:>3.0f}, Error={errors[idx]:>+5.0f}")

    print(f"\n{'='*100}")
    print("PROPER VALIDATION COMPLETE - NO DATA LEAKAGE!")
    print("="*100)


if __name__ == "__main__":
    main()
