"""Monte Carlo simulation for wave-based acceptance strategies.

Scenario:
- 500 applicants over 28 days (right-skewed arrivals, 48% expected in last 7 days)
- Quality ~ Normal(60, 15), clipped to [0, 100]
- Show probability = base 0.47 + quality bonus (+/- 0.10) + repeat attendee bonus (+0.15)
- Repeat attendees are 30% of the applicant pool
- Target capacity: 250 seats

Strategies:
A) Accept-all-at-once: accept top 530 by quality (bounded by available applicants)
B) Equal waves: 4 waves, fixed 133 accepts per wave, no quota carryover
C) Adaptive waves: 4 waves with feedback loop + dynamic overbooking quota adjustment
D) Prophet optimal: omniscient upper bound (knows eventual show outcomes)

Only numpy/scipy are used for simulation math.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass

import numpy as np


# Problem setup
N_APPLICANTS = 500
N_DAYS = 28
LAST_7_TARGET_SHARE = 0.48
QUALITY_MEAN = 60.0
QUALITY_STD = 15.0
QUALITY_MIN = 0.0
QUALITY_MAX = 100.0
BASE_SHOW_RATE = 0.47
QUALITY_BONUS_MAX = 0.10
REPEAT_ATTENDEE_RATE = 0.30
REPEAT_ATTENDEE_BONUS = 0.15
TARGET_SEATS = 250
OVERCROWD_THRESHOLD = int(np.floor(TARGET_SEATS * 1.10))  # >10% over target

# Strategy setup
ALL_AT_ONCE_ACCEPTS = 530
WAVE_COUNT = 4
EQUAL_WAVE_ACCEPTS = 133
WAVE_EDGES = np.array([7, 14, 21, 28], dtype=int)


@dataclass
class StrategyResult:
    attendance: int
    mean_quality: float


@dataclass
class Applicants:
    arrival_day: np.ndarray  # shape (N,), values in [0, N_DAYS-1]
    quality: np.ndarray  # shape (N,), float in [0, 100]
    show_prob: np.ndarray  # shape (N,), float in [0, 1]
    would_show: np.ndarray  # shape (N,), bool


def _arrival_lambda_for_last7_share(target_share: float) -> float:
    """Solve for lambda in exp(lambda * day) so last 7 days share matches target."""

    days = np.arange(N_DAYS)

    def last7_share(lam: float) -> float:
        weights = np.exp(lam * days)
        return float(weights[-7:].sum() / weights.sum())

    lo, hi = -0.5, 0.5
    for _ in range(100):
        mid = 0.5 * (lo + hi)
        if last7_share(mid) < target_share:
            lo = mid
        else:
            hi = mid
    return 0.5 * (lo + hi)


ARRIVAL_LAMBDA = _arrival_lambda_for_last7_share(LAST_7_TARGET_SHARE)
ARRIVAL_WEIGHTS = np.exp(ARRIVAL_LAMBDA * np.arange(N_DAYS))
ARRIVAL_PROBS = ARRIVAL_WEIGHTS / ARRIVAL_WEIGHTS.sum()


def _quality_bonus(scores: np.ndarray) -> np.ndarray:
    """Map score in [0,100] to bonus in [-0.10, +0.10]."""

    centered = (scores - 50.0) / 50.0
    return np.clip(centered * QUALITY_BONUS_MAX, -QUALITY_BONUS_MAX, QUALITY_BONUS_MAX)


def generate_applicants(rng: np.random.Generator) -> Applicants:
    arrival_day = rng.choice(N_DAYS, size=N_APPLICANTS, p=ARRIVAL_PROBS)
    quality = np.clip(rng.normal(QUALITY_MEAN, QUALITY_STD, size=N_APPLICANTS), QUALITY_MIN, QUALITY_MAX)
    repeat = rng.random(N_APPLICANTS) < REPEAT_ATTENDEE_RATE

    show_prob = BASE_SHOW_RATE + _quality_bonus(quality) + repeat.astype(float) * REPEAT_ATTENDEE_BONUS
    show_prob = np.clip(show_prob, 0.01, 0.99)
    would_show = rng.random(N_APPLICANTS) < show_prob

    return Applicants(
        arrival_day=arrival_day,
        quality=quality,
        show_prob=show_prob,
        would_show=would_show,
    )


def _mean_quality_of_attendees(quality: np.ndarray, attendees_mask: np.ndarray) -> float:
    attendees = quality[attendees_mask]
    if attendees.size == 0:
        return 0.0
    return float(attendees.mean())


def _evaluate_accepts(accepted_mask: np.ndarray, applicants: Applicants) -> StrategyResult:
    attendees_mask = accepted_mask & applicants.would_show
    attendance = int(attendees_mask.sum())
    mean_quality = _mean_quality_of_attendees(applicants.quality, attendees_mask)
    return StrategyResult(attendance=attendance, mean_quality=mean_quality)


def simulate_accept_all_at_once(applicants: Applicants) -> StrategyResult:
    order = np.argsort(-applicants.quality)
    n_accept = min(ALL_AT_ONCE_ACCEPTS, N_APPLICANTS)
    accepted_mask = np.zeros(N_APPLICANTS, dtype=bool)
    accepted_mask[order[:n_accept]] = True
    return _evaluate_accepts(accepted_mask, applicants)


def _eligible_until(arrival_day: np.ndarray, accepted_mask: np.ndarray, cutoff_day: int) -> np.ndarray:
    return np.where((arrival_day < cutoff_day) & (~accepted_mask))[0]


def simulate_equal_waves(applicants: Applicants) -> StrategyResult:
    accepted_mask = np.zeros(N_APPLICANTS, dtype=bool)

    for cutoff in WAVE_EDGES:
        eligible = _eligible_until(applicants.arrival_day, accepted_mask, cutoff)
        if eligible.size == 0:
            continue
        ranked = eligible[np.argsort(-applicants.quality[eligible])]
        take = min(EQUAL_WAVE_ACCEPTS, ranked.size)
        accepted_mask[ranked[:take]] = True

    return _evaluate_accepts(accepted_mask, applicants)


def simulate_adaptive_waves(applicants: Applicants) -> StrategyResult:
    accepted_mask = np.zeros(N_APPLICANTS, dtype=bool)
    accepted_count = 0
    expected_attendance = 0.0

    for wave_idx, cutoff in enumerate(WAVE_EDGES):
        remaining_waves = WAVE_COUNT - wave_idx
        remaining_needed = max(0.0, TARGET_SEATS - expected_attendance)
        if remaining_needed <= 0.0:
            break

        observed_rate = BASE_SHOW_RATE
        if accepted_count > 0:
            observed_rate = expected_attendance / accepted_count

        # Overbooking adjustment: if observed yield is below baseline, boost invites.
        underperformance = max(0.0, BASE_SHOW_RATE - observed_rate)
        overbook_factor = 1.0 + min(0.30, underperformance * 2.0)

        total_accepts_needed = int(np.ceil((remaining_needed / max(observed_rate, 0.15)) * overbook_factor))
        wave_quota = max(0, int(np.ceil(total_accepts_needed / remaining_waves)))

        eligible = _eligible_until(applicants.arrival_day, accepted_mask, cutoff)
        if eligible.size == 0:
            continue
        ranked = eligible[np.argsort(-applicants.quality[eligible])]
        take = min(wave_quota, ranked.size)
        chosen = ranked[:take]

        accepted_mask[chosen] = True
        accepted_count += take
        expected_attendance += float(applicants.show_prob[chosen].sum())

    return _evaluate_accepts(accepted_mask, applicants)


def simulate_prophet_optimal(applicants: Applicants) -> StrategyResult:
    showers = np.where(applicants.would_show)[0]
    accepted_mask = np.zeros(N_APPLICANTS, dtype=bool)

    if showers.size <= TARGET_SEATS:
        accepted_mask[showers] = True
        return _evaluate_accepts(accepted_mask, applicants)

    # Omniscient upper bound: pick best-quality attendees among those who would show.
    ranked_showers = showers[np.argsort(-applicants.quality[showers])]
    accepted_mask[ranked_showers[:TARGET_SEATS]] = True
    return _evaluate_accepts(accepted_mask, applicants)


def run_monte_carlo(n_runs: int, seed: int | None) -> dict[str, dict[str, float]]:
    rng = np.random.default_rng(seed)

    names = [
        "A) Accept-all-at-once",
        "B) Equal waves",
        "C) Adaptive waves",
        "D) Prophet optimal",
    ]
    attendance = {name: np.zeros(n_runs, dtype=float) for name in names}
    mean_quality = {name: np.zeros(n_runs, dtype=float) for name in names}

    for i in range(n_runs):
        applicants = generate_applicants(rng)

        out_a = simulate_accept_all_at_once(applicants)
        out_b = simulate_equal_waves(applicants)
        out_c = simulate_adaptive_waves(applicants)
        out_d = simulate_prophet_optimal(applicants)

        outputs = {
            names[0]: out_a,
            names[1]: out_b,
            names[2]: out_c,
            names[3]: out_d,
        }
        for name, out in outputs.items():
            attendance[name][i] = out.attendance
            mean_quality[name][i] = out.mean_quality

    summary: dict[str, dict[str, float]] = {}
    for name in names:
        a = attendance[name]
        q = mean_quality[name]
        summary[name] = {
            "expected_attendance": float(a.mean()),
            "p_at_least_target": float((a >= TARGET_SEATS).mean()),
            "mean_quality": float(q.mean()),
            "p_overcrowd_10pct": float((a > OVERCROWD_THRESHOLD).mean()),
        }
    return summary


def _fmt_pct(x: float) -> str:
    return f"{100.0 * x:6.2f}%"


def print_table(summary: dict[str, dict[str, float]], n_runs: int, seed: int | None) -> None:
    print("Monte Carlo: Wave Acceptance Strategy Comparison")
    print(f"Runs: {n_runs} | Seed: {seed if seed is not None else 'None'}")
    print(
        "Setup: 500 applicants, 28 days, right-skewed arrivals (48% in last 7), "
        "quality N(60,15) clipped [0,100], base show 0.47, quality bonus +/-0.10, "
        "repeat attendee +0.15 (30%), target 250 seats."
    )
    print()

    headers = [
        "Strategy",
        "E[Attendance]",
        "P(>=250)",
        "Mean Quality",
        "P(Overcrowd>10%)",
    ]
    print(f"{headers[0]:<28} {headers[1]:>14} {headers[2]:>12} {headers[3]:>14} {headers[4]:>18}")
    print("-" * 92)

    for strategy, vals in summary.items():
        print(
            f"{strategy:<28} "
            f"{vals['expected_attendance']:14.2f} "
            f"{_fmt_pct(vals['p_at_least_target']):>12} "
            f"{vals['mean_quality']:14.2f} "
            f"{_fmt_pct(vals['p_overcrowd_10pct']):>18}"
        )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Monte Carlo simulation for wave acceptance strategies")
    parser.add_argument("--runs", type=int, default=1000, help="Number of Monte Carlo runs (default: 1000)")
    parser.add_argument("--seed", type=int, default=42, help="Random seed (default: 42)")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    summary = run_monte_carlo(n_runs=args.runs, seed=args.seed)
    print_table(summary, n_runs=args.runs, seed=args.seed)


if __name__ == "__main__":
    main()
