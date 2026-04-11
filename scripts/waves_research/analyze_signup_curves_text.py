"""Comprehensive text-based analysis of signup curves, velocity, and acceleration."""
import csv
from pathlib import Path
import numpy as np


def load_events():
    """Load signup velocity data."""
    csv_path = Path(__file__).resolve().parents[2] / "results" / "signup_velocity.csv"
    events = []
    with open(csv_path, 'r') as f:
        reader = csv.DictReader(f)
        for row in reader:
            events.append({
                'id': row['event_id'],
                'title': row['title'],
                'city': row['city'] or 'Unknown',
                't21': int(row['approved_t21'] or 0),
                't14': int(row['approved_t14'] or 0),
                't7': int(row['approved_t7'] or 0),
                't3': int(row['approved_t3'] or 0),
                't1': int(row['approved_t1'] or 0),
                't0': int(row['approved_t0'] or 0),
                'attended': int(row['actual_attended'] or 0),
            })
    return events


def analyze_event(event):
    """Calculate velocity and acceleration for an event."""
    days = [21, 14, 7, 3, 1, 0]
    counts = [event['t21'], event['t14'], event['t7'], event['t3'], event['t1'], event['t0']]
    
    # Velocity (signups per day between checkpoints)
    velocities = []
    intervals = [(21,14), (14,7), (7,3), (3,1), (1,0)]
    for i, (start, end) in enumerate(intervals):
        delta_signups = counts[i+1] - counts[i]
        delta_days = start - end
        velocity = delta_signups / delta_days if delta_days > 0 else 0
        velocities.append(velocity)
    
    # Acceleration (change in velocity)
    accelerations = []
    for i in range(len(velocities) - 1):
        accel = velocities[i+1] - velocities[i]
        accelerations.append(accel)
    
    return {
        'days': days,
        'counts': counts,
        'velocities': velocities,
        'accelerations': accelerations,
        'intervals': intervals,
    }


def plot_ascii(values, width=60, height=10, label=""):
    """Create ASCII plot."""
    if not values or all(v == 0 for v in values):
        return
    
    min_val = min(values)
    max_val = max(values)
    range_val = max_val - min_val if max_val != min_val else 1
    
    print(f"\n{label}:")
    for row in range(height, -1, -1):
        threshold = min_val + (row / height) * range_val
        line = ""
        for val in values:
            if val >= threshold:
                line += "█"
            else:
                line += " "
        
        # Y-axis label
        val_at_row = min_val + (row / height) * range_val
        print(f"{val_at_row:>6.1f} |{line}")
    
    print("       " + "-" * len(values))
    print(f"       Range: {min_val:.1f} to {max_val:.1f}")


def main():
    events = load_events()
    
    # Take 20 most recent events with reasonable size
    recent_events = [e for e in events if e['t0'] >= 50][:20]
    
    print("=" * 100)
    print(f"SIGNUP CURVE ANALYSIS: {len(recent_events)} RECENT EVENTS")
    print("=" * 100)
    
    # Track patterns
    growth_types = {'linear': 0, 'sigmoid': 0, 'exponential': 0, 'late_surge': 0}
    
    # Detailed event analysis
    print("\n" + "=" * 100)
    print("DETAILED EVENT ANALYSIS")
    print("=" * 100)
    print(f"{'Event':<45s} {'T-21':>5s} {'T-14':>5s} {'T-7':>5s} {'T-3':>5s} {'T-1':>5s} {'T-0':>5s} {'Growth':>7s} {'Pattern':>12s}")
    print("-" * 100)
    
    for event in recent_events:
        analysis = analyze_event(event)
        growth_mult = event['t0'] / max(event['t7'], 1)
        
        # Pattern detection
        velocities = analysis['velocities']
        accelerations = analysis['accelerations']
        
        final_vel = velocities[-1]
        avg_vel = np.mean(velocities)
        avg_accel = np.mean(accelerations)
        
        if abs(avg_accel) < 0.5:
            pattern = 'Linear'
            growth_types['linear'] += 1
        elif final_vel > avg_vel * 1.5:
            pattern = 'Late surge'
            growth_types['late_surge'] += 1
        elif avg_accel < -0.5:
            pattern = 'Sigmoid'
            growth_types['sigmoid'] += 1
        else:
            pattern = 'Exponential'
            growth_types['exponential'] += 1
        
        print(f"{event['title'][:43]:45s} {event['t21']:>5d} {event['t14']:>5d} {event['t7']:>5d} "
              f"{event['t3']:>5d} {event['t1']:>5d} {event['t0']:>5d} {growth_mult:>6.2f}x {pattern:>12s}")
    
    # Velocity analysis
    print("\n" + "=" * 100)
    print("VELOCITY ANALYSIS (signups per day)")
    print("=" * 100)
    print(f"{'Event':<45s} {'T21→T14':>8s} {'T14→T7':>8s} {'T7→T3':>8s} {'T3→T1':>8s} {'T1→T0':>8s} {'Trend':>12s}")
    print("-" * 100)
    
    for event in recent_events[:15]:
        analysis = analyze_event(event)
        vels = analysis['velocities']
        
        # Determine trend
        if vels[-1] > vels[0] * 1.5:
            trend = 'Accelerating'
        elif vels[-1] < vels[0] * 0.7:
            trend = 'Decelerating'
        else:
            trend = 'Steady'
        
        print(f"{event['title'][:43]:45s} {vels[0]:>7.1f} {vels[1]:>7.1f} {vels[2]:>7.1f} "
              f"{vels[3]:>7.1f} {vels[4]:>7.1f} {trend:>12s}")
    
    # Acceleration analysis
    print("\n" + "=" * 100)
    print("ACCELERATION ANALYSIS (change in velocity)")
    print("=" * 100)
    print(f"{'Event':<45s} {'Accel 1':>9s} {'Accel 2':>9s} {'Accel 3':>9s} {'Accel 4':>9s}")
    print("-" * 100)
    
    for event in recent_events[:15]:
        analysis = analyze_event(event)
        accels = analysis['accelerations']
        
        print(f"{event['title'][:43]:45s} {accels[0]:>+8.2f} {accels[1]:>+8.2f} "
              f"{accels[2]:>+8.2f} {accels[3]:>+8.2f}")
    
    # Pattern summary
    print("\n" + "=" * 100)
    print("GROWTH PATTERN CLASSIFICATION")
    print("=" * 100)
    for pattern, count in sorted(growth_types.items(), key=lambda x: x[1], reverse=True):
        pct = count / len(recent_events) * 100
        bar = '█' * int(pct / 5)
        print(f"  {pattern:15s}: {count:2d} events ({pct:5.1f}%) {bar}")
    
    # Key insights
    print("\n" + "=" * 100)
    print("KEY INSIGHTS")
    print("=" * 100)
    
    all_growths = [e['t0'] / max(e['t7'], 1) for e in recent_events if e['t7'] > 0]
    avg_growth = np.mean(all_growths)
    median_growth = np.median(all_growths)
    std_growth = np.std(all_growths)
    
    print(f"\nT-7 → T-0 Growth Multipliers:")
    print(f"  Average:   {avg_growth:.2f}x")
    print(f"  Median:    {median_growth:.2f}x")
    print(f"  Std Dev:   {std_growth:.2f}x")
    print(f"  Range:     {min(all_growths):.2f}x - {max(all_growths):.2f}x")
    
    # Velocity insights
    all_late_vels = []
    all_early_vels = []
    for event in recent_events:
        analysis = analyze_event(event)
        all_early_vels.append(np.mean(analysis['velocities'][:2]))  # T21→T14, T14→T7
        all_late_vels.append(np.mean(analysis['velocities'][2:]))   # T7→T3, T3→T1, T1→T0
    
    print(f"\nVelocity Comparison:")
    print(f"  Early (T-21 to T-7):  {np.mean(all_early_vels):.1f} signups/day")
    print(f"  Late (T-7 to T-0):    {np.mean(all_late_vels):.1f} signups/day")
    print(f"  Ratio:                {np.mean(all_late_vels)/max(np.mean(all_early_vels),1):.2f}x")
    
    print("\n" + "=" * 100)


if __name__ == "__main__":
    main()
