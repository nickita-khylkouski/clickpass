"""Comprehensive visual analysis of signup curves, velocity, and acceleration."""
import csv
from pathlib import Path

import matplotlib.pyplot as plt
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
    for i in range(len(days) - 1):
        delta_signups = counts[i+1] - counts[i]
        delta_days = days[i] - days[i+1]
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
    }


def main():
    events = load_events()
    
    # Take 20 most recent events with reasonable size
    recent_events = [e for e in events if e['t0'] >= 50][:20]
    
    print(f"Analyzing {len(recent_events)} events\n")
    
    # Create figure with 3 subplots
    fig, (ax1, ax2, ax3) = plt.subplots(3, 1, figsize=(14, 12))
    fig.suptitle('Signup Curve Analysis: 20 Recent Events', fontsize=16, fontweight='bold')
    
    # Track patterns
    growth_types = {'linear': 0, 'sigmoid': 0, 'exponential': 0, 'late_surge': 0}
    
    for event in recent_events:
        analysis = analyze_event(event)
        days = analysis['days']
        counts = analysis['counts']
        velocities = analysis['velocities']
        accelerations = analysis['accelerations']
        
        # Plot 1: Signup curves
        ax1.plot(days[::-1], counts[::-1], marker='o', alpha=0.6, linewidth=2)
        
        # Plot 2: Velocity (gradient)
        vel_days = [(days[i] + days[i+1]) / 2 for i in range(len(velocities))]
        ax2.plot(vel_days[::-1], velocities[::-1], marker='s', alpha=0.6, linewidth=2)
        
        # Plot 3: Acceleration
        accel_days = [(vel_days[i] + vel_days[i+1]) / 2 for i in range(len(accelerations))]
        ax3.plot(accel_days[::-1], accelerations[::-1], marker='^', alpha=0.6, linewidth=2)
        
        # Classify growth pattern
        final_velocity = velocities[-1]
        avg_velocity = np.mean(velocities)
        avg_accel = np.mean(accelerations)
        
        if abs(avg_accel) < 0.5:  # Relatively constant velocity
            growth_types['linear'] += 1
        elif final_velocity > avg_velocity * 1.5:  # Accelerating at end
            growth_types['late_surge'] += 1
        elif avg_accel < -0.5:  # Decelerating
            growth_types['sigmoid'] += 1
        else:
            growth_types['exponential'] += 1
    
    # Configure plot 1
    ax1.set_xlabel('Days Before Event', fontsize=12)
    ax1.set_ylabel('Approved Signups', fontsize=12)
    ax1.set_title('Signup Growth Curves', fontsize=14, fontweight='bold')
    ax1.grid(True, alpha=0.3)
    ax1.set_xlim(22, -1)
    
    # Configure plot 2
    ax2.set_xlabel('Days Before Event', fontsize=12)
    ax2.set_ylabel('Velocity (signups/day)', fontsize=12)
    ax2.set_title('Signup Velocity (Gradient)', fontsize=14, fontweight='bold')
    ax2.grid(True, alpha=0.3)
    ax2.axhline(y=0, color='red', linestyle='--', alpha=0.5)
    ax2.set_xlim(22, -1)
    
    # Configure plot 3
    ax3.set_xlabel('Days Before Event', fontsize=12)
    ax3.set_ylabel('Acceleration (Δvelocity/day)', fontsize=12)
    ax3.set_title('Signup Acceleration', fontsize=14, fontweight='bold')
    ax3.grid(True, alpha=0.3)
    ax3.axhline(y=0, color='red', linestyle='--', alpha=0.5)
    ax3.set_xlim(22, -1)
    
    plt.tight_layout()
    
    # Save figure
    output_path = Path(__file__).resolve().parents[2] / "results" / "signup_curve_analysis.png"
    plt.savefig(output_path, dpi=150, bbox_inches='tight')
    print(f"\nPlot saved to: {output_path}")
    
    # Print analysis
    print("\n" + "=" * 100)
    print("GROWTH PATTERN CLASSIFICATION")
    print("=" * 100)
    for pattern, count in sorted(growth_types.items(), key=lambda x: x[1], reverse=True):
        pct = count / len(recent_events) * 100
        print(f"  {pattern:15s}: {count:2d} events ({pct:5.1f}%)")
    
    # Detailed event analysis
    print("\n" + "=" * 100)
    print("DETAILED EVENT ANALYSIS")
    print("=" * 100)
    print(f"{'Event':<45s} {'T-7':>6s} {'T-0':>6s} {'Growth':>7s} {'Velocity':>10s} {'Pattern':>12s}")
    print("-" * 100)
    
    for event in recent_events[:15]:
        analysis = analyze_event(event)
        growth_mult = event['t0'] / max(event['t7'], 1)
        avg_velocity = np.mean(analysis['velocities'])
        
        # Pattern detection
        final_vel = analysis['velocities'][-1]
        avg_vel = np.mean(analysis['velocities'])
        if abs(np.mean(analysis['accelerations'])) < 0.5:
            pattern = 'Linear'
        elif final_vel > avg_vel * 1.5:
            pattern = 'Late surge'
        elif np.mean(analysis['accelerations']) < -0.5:
            pattern = 'Sigmoid'
        else:
            pattern = 'Exponential'
        
        print(f"{event['title'][:43]:45s} {event['t7']:>6d} {event['t0']:>6d} {growth_mult:>6.2f}x "
              f"{avg_velocity:>9.1f}/d {pattern:>12s}")
    
    print("\n" + "=" * 100)


if __name__ == "__main__":
    main()
