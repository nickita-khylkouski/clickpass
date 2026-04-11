"""Debug test set to understand why AUC is so low."""
import csv
from collections import defaultdict
from pathlib import Path


def load_engagement_data():
    """Load person-level engagement data."""
    csv_path = Path(__file__).resolve().parents[2] / "results" / "engagement_profiles.csv"

    records = []
    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            records.append({
                'event_id': row['event_id'],
                'event_title': row['event_title'],
                'showed_up': int(row['showed_up']),
            })

    return records


def main():
    records = load_engagement_data()

    # Group by event
    by_event = defaultdict(list)
    for r in records:
        by_event[r['event_id']].append(r)

    events = list(by_event.keys())

    # Same split as XGBoost training
    n_events = len(events)
    train_end = int(n_events * 0.6)
    cal_end = int(n_events * 0.8)

    train_events = events[:train_end]
    cal_events = events[train_end:cal_end]
    test_events = events[cal_end:]

    print("=" * 100)
    print("TEST SET DIAGNOSIS")
    print("=" * 100)

    for split_name, event_list in [("Training", train_events), ("Calibration", cal_events), ("Test", test_events)]:
        split_records = [r for e in event_list for r in by_event[e]]
        total = len(split_records)
        showed_up = sum(r['showed_up'] for r in split_records)
        show_rate = showed_up / total if total > 0 else 0

        print(f"\n{split_name}:")
        print(f"  Events: {len(event_list)}")
        print(f"  Observations: {total}")
        print(f"  Showed up: {showed_up}")
        print(f"  Show rate: {show_rate:.1%}")

    # Check individual test events
    print(f"\n{'=' * 100}")
    print("TEST SET EVENTS (Detailed)")
    print("=" * 100)
    print(f"\n{'Event Title':<50} {'Total':>8} {'Showed':>8} {'Rate':>8}")
    print("-" * 100)

    for event_id in test_events:
        event_records = by_event[event_id]
        total = len(event_records)
        showed_up = sum(r['showed_up'] for r in event_records)
        rate = showed_up / total if total > 0 else 0
        title = event_records[0]['event_title'][:48]

        print(f"{title:<50} {total:>8} {showed_up:>8} {rate:>7.1%}")

    print("=" * 100)


if __name__ == "__main__":
    main()
