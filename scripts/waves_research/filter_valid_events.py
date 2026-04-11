"""Filter to valid hackathons/events with real attendance data.

Criteria for valid events:
1. Has actual attendance data (actual_attended > 0)
2. Has sufficient signups (approved_t0 >= 20)
3. Reasonable show rate (5% - 95%)
4. Has signup velocity data (not all zeros)
"""
import csv
from pathlib import Path


def main():
    # Load signup velocity data (already filtered to real events)
    csv_path = Path(__file__).resolve().parents[2] / "results" / "signup_velocity.csv"

    events = []
    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            event_id = row['event_id']
            title = row['title']
            approved_t0 = int(row['approved_t0'] or 0)
            actual_attended = int(row['actual_attended'] or 0)
            show_rate = float(row['show_rate'] or 0)

            # Filter criteria
            has_attendance = actual_attended > 0
            has_signups = approved_t0 >= 20
            reasonable_show_rate = 0.05 <= show_rate <= 0.95

            events.append({
                'event_id': event_id,
                'title': title,
                'approved_t0': approved_t0,
                'actual_attended': actual_attended,
                'show_rate': show_rate,
                'event_start': row['event_start'],
                'valid': has_attendance and has_signups and reasonable_show_rate,
            })

    print("=" * 100)
    print("EVENT FILTERING ANALYSIS")
    print("=" * 100)

    total = len(events)
    valid = sum(1 for e in events if e['valid'])

    print(f"\nTotal events in signup_velocity.csv: {total}")
    print(f"Valid events (has attendance, >=20 signups, 5-95% show rate): {valid}")
    print(f"Filtered out: {total - valid}")

    # Show invalid events
    print(f"\n{'=' * 100}")
    print("INVALID EVENTS (Filtered Out)")
    print("=" * 100)
    print(f"\n{'Title':<50} {'Approved':>10} {'Attended':>10} {'Show Rate':>12} {'Reason':<30}")
    print("-" * 100)

    for e in events:
        if not e['valid']:
            reason = []
            if e['actual_attended'] == 0:
                reason.append("no attendance")
            if e['approved_t0'] < 20:
                reason.append("too few signups")
            if not (0.05 <= e['show_rate'] <= 0.95):
                reason.append("unrealistic show rate")

            reason_str = ", ".join(reason)
            print(f"{e['title'][:48]:<50} {e['approved_t0']:>10} {e['actual_attended']:>10} {e['show_rate']:>11.1%} {reason_str:<30}")

    # Show valid events for split
    valid_events = [e for e in events if e['valid']]
    valid_events.sort(key=lambda x: x['event_start'])

    print(f"\n{'=' * 100}")
    print(f"VALID EVENTS ({len(valid_events)} total)")
    print("=" * 100)

    # Temporal split
    n = len(valid_events)
    train_end = int(n * 0.6)
    cal_end = int(n * 0.8)

    train_events = valid_events[:train_end]
    cal_events = valid_events[train_end:cal_end]
    test_events = valid_events[cal_end:]

    print(f"\nTemporal Split:")
    print(f"  Training:     {len(train_events)} events (60%)")
    print(f"  Calibration:  {len(cal_events)} events (20%)")
    print(f"  Test:         {len(test_events)} events (20%)")

    # Check show rates
    for split_name, event_list in [("Training", train_events), ("Calibration", cal_events), ("Test", test_events)]:
        total_approved = sum(e['approved_t0'] for e in event_list)
        total_attended = sum(e['actual_attended'] for e in event_list)
        avg_show_rate = total_attended / total_approved if total_approved > 0 else 0

        print(f"\n{split_name}:")
        print(f"  Events: {len(event_list)}")
        print(f"  Total approved: {total_approved}")
        print(f"  Total attended: {total_attended}")
        print(f"  Avg show rate: {avg_show_rate:.1%}")

    # Show test events
    print(f"\n{'=' * 100}")
    print("TEST SET EVENTS (Most Recent)")
    print("=" * 100)
    print(f"\n{'Title':<50} {'Date':<20} {'Approved':>10} {'Attended':>10} {'Rate':>8}")
    print("-" * 100)

    for e in test_events:
        print(f"{e['title'][:48]:<50} {e['event_start'][:10]:<20} {e['approved_t0']:>10} {e['actual_attended']:>10} {e['show_rate']:>7.1%}")

    # Save filtered event IDs
    output_path = Path(__file__).resolve().parents[2] / "results" / "valid_event_ids.txt"
    with open(output_path, 'w') as f:
        for e in valid_events:
            f.write(f"{e['event_id']}\n")

    print(f"\n{'=' * 100}")
    print(f"Valid event IDs saved to: {output_path}")
    print("=" * 100)


if __name__ == "__main__":
    main()
