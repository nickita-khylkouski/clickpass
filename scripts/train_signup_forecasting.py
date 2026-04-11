"""Train signup forecasting model to predict final approved count from T-h observations."""
import csv
import json
import math
import os
import sys
from pathlib import Path

from dotenv import load_dotenv

load_dotenv(Path(__file__).parent.parent / ".env")
sys.path.insert(0, str(Path(__file__).parent.parent / "src"))

import numpy as np
from sklearn.ensemble import GradientBoostingRegressor
from sklearn.linear_model import Ridge
from sklearn.model_selection import LeaveOneOut, cross_val_score


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
                'city': row['city'] or 'Unknown',
                'approved_t21': int(row['approved_t21'] or 0),
                'approved_t14': int(row['approved_t14'] or 0),
                'approved_t7': int(row['approved_t7'] or 0),
                'approved_t3': int(row['approved_t3'] or 0),
                'approved_t1': int(row['approved_t1'] or 0),
                'approved_t0': int(row['approved_t0'] or 0),
            })
    return events


def build_training_data(events, horizon):
    """
    Build training data for predicting T-0 count from T-h observations.

    Features:
    - Current approved count at T-h
    - Signup velocity (people/day)
    - Signup acceleration
    - Days until event (horizon)
    - Trajectory similarity features
    """
    X = []
    y = []

    for event in events:
        current_approved = event[f'approved_t{horizon}']
        final_approved = event['approved_t0']

        # Skip if no approvals at this horizon
        if current_approved < 10:
            continue

        # Calculate velocity and acceleration
        if horizon == 7:
            t14 = event['approved_t14']
            t21 = event['approved_t21']

            velocity_7d = (current_approved - t14) / 7 if t14 > 0 else 0
            velocity_14d = (t14 - t21) / 7 if t21 > 0 else 0
            acceleration = velocity_7d - velocity_14d

        elif horizon == 3:
            t7 = event['approved_t7']
            t14 = event['approved_t14']

            velocity_3d = (current_approved - t7) / 4 if t7 > 0 else 0
            velocity_7d = (t7 - t14) / 7 if t14 > 0 else 0
            acceleration = velocity_3d - velocity_7d

        elif horizon == 1:
            t3 = event['approved_t3']
            t7 = event['approved_t7']

            velocity_7d = (current_approved - t3) / 2 if t3 > 0 else 0
            velocity_3d = (t3 - t7) / 4 if t7 > 0 else 0
            acceleration = velocity_7d - velocity_3d
        else:
            velocity_7d = 0
            acceleration = 0

        # Build feature vector
        features = [
            float(current_approved),
            math.log(current_approved + 1),
            float(velocity_7d),
            math.log(abs(velocity_7d) + 1) * (1 if velocity_7d >= 0 else -1),
            float(acceleration),
            math.log(abs(acceleration) + 1) * (1 if acceleration >= 0 else -1),
            float(horizon),
            math.log(horizon + 1),
            # Interaction terms
            float(current_approved) * float(horizon),
            math.log(current_approved + 1) * math.log(horizon + 1),
            float(velocity_7d) * float(horizon),
        ]

        X.append(features)
        y.append(final_approved)

    return np.array(X), np.array(y)


def train_model_at_horizon(events, horizon, model_type='ridge'):
    """Train signup forecasting model at specific horizon."""
    X, y = build_training_data(events, horizon)

    print(f"\nTraining at T-{horizon}:")
    print(f"  Training samples: {len(X)}")
    print(f"  Avg current approved: {X[:, 0].mean():.0f}")
    print(f"  Avg final approved: {y.mean():.0f}")
    print(f"  Avg growth multiplier: {(y / X[:, 0]).mean():.2f}")

    if model_type == 'gbm':
        model = GradientBoostingRegressor(
            n_estimators=100,
            max_depth=4,
            learning_rate=0.1,
            min_samples_leaf=3,
            random_state=42,
        )
    else:  # ridge
        model = Ridge(alpha=10.0)

    # LOO cross-validation
    loo = LeaveOneOut()
    predictions = []
    actuals = []

    for train_idx, test_idx in loo.split(X):
        X_train, X_test = X[train_idx], X[test_idx]
        y_train, y_test = y[train_idx], y[test_idx]

        model.fit(X_train, y_train)
        pred = model.predict(X_test)[0]

        predictions.append(pred)
        actuals.append(y_test[0])

    predictions = np.array(predictions)
    actuals = np.array(actuals)

    # Evaluation metrics
    mae = np.mean(np.abs(predictions - actuals))
    bias = np.mean(predictions - actuals)
    rmse = np.sqrt(np.mean((predictions - actuals) ** 2))
    mape = np.mean(np.abs((predictions - actuals) / actuals)) * 100

    print(f"  LOO CV Results:")
    print(f"    MAE:  {mae:.1f} people")
    print(f"    Bias: {bias:+.1f} people")
    print(f"    RMSE: {rmse:.1f} people")
    print(f"    MAPE: {mape:.1f}%")

    # Train final model on all data
    model.fit(X, y)

    # Extract coefficients
    if model_type == 'ridge':
        intercept = float(model.intercept_)
        weights = [float(w) for w in model.coef_]
    else:  # GBM doesn't have simple coefficients
        intercept = 0.0
        weights = []

    return {
        'horizon': horizon,
        'model_type': model_type,
        'intercept': round(intercept, 4),
        'weights': [round(w, 4) for w in weights],
        'num_features': len(X[0]),
        'training_size': len(X),
        'loo_mae': round(mae, 2),
        'loo_bias': round(bias, 2),
        'loo_rmse': round(rmse, 2),
        'loo_mape': round(mape, 2),
    }


def main():
    events = load_signup_velocity_data()
    print(f"Loaded {len(events)} events with signup velocity data\n")
    print("="*80)
    print("SIGNUP FORECASTING MODEL TRAINING")
    print("="*80)

    # Train models at different horizons
    results = {}
    for horizon in [7, 3, 1]:
        result = train_model_at_horizon(events, horizon, model_type='ridge')
        results[f't{horizon}'] = result

    # Save results
    output_path = Path(__file__).parent.parent / "results" / "signup_forecasting_models.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with open(output_path, 'w') as f:
        json.dump(results, f, indent=2)

    print(f"\n{'='*80}")
    print(f"Saved signup forecasting models to: {output_path}")

    # Print coefficients for copying into model.py
    print(f"\n{'='*80}")
    print("COPY THIS INTO model.py:")
    print("="*80)
    print("\nSIGNUP_FORECASTING_MODELS = {")
    for horizon_key, model_data in results.items():
        print(f"    \"{horizon_key}\": {{")
        print(f"        \"intercept\": {model_data['intercept']},")
        print(f"        \"weights\": {model_data['weights']},")
        print(f"    }},")
    print("}")

    print("\n# Feature order:")
    print("# [current_approved, log_current, velocity, log_velocity,")
    print("#  acceleration, log_accel, horizon, log_horizon,")
    print("#  current*horizon, log_current*log_horizon, velocity*horizon]")


if __name__ == "__main__":
    main()
