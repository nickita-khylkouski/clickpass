# Optimal Stopping Research for Wave-Based Hackathon Acceptance

Generated: 2026-03-04

## Executive Summary

Given:
- Applicant pool `N ≈ 500`
- Target attendees (checked-in) `S = 250`
- Historical show rate `p = 0.47`

The expected offers needed are:

`A* = S / p = 250 / 0.47 = 531.9`

So the mathematically correct operating point is roughly **532 offers**. With only ~500 applicants, this implies:
- You cannot reliably fill 250 seats from this pool alone.
- The optimization is not "which 250 to accept".
- The optimization is:
  1. **Who to accept first** (wave order), and
  2. **Which ~30 to reject** (the tail), while trying to source extra offers outside this pool.

This is an inversion of classic selective admissions.

---

## 1. Problem Formulation: Capacity with No-Shows

Let:
- `X ~ Binomial(A, p)` be check-ins from `A` accepted applicants.
- `E[X] = A p`, `Var[X] = A p (1-p)`.

For `A = 500`:
- `E[X] = 235`
- `P(X >= 250) ≈ 9.7%`

For `A = 530`:
- `E[X] = 249.1`
- `P(X >= 250) ≈ 48.6%`

Key implication: even at the mean-matching point (`~532 offers`), probability of hitting/exceeding 250 is only about 50%. Confidence targets require more overbooking:

| Confidence target | Minimum offers needed (at p=0.47) |
|---|---:|
| 50% | 531 |
| 80% | 552 |
| 90% | 564 |
| 95% | 573 |
| 97.5% | 581 |

If only 500 applicants exist, high-confidence fill is impossible without external invites/waitlist expansion.

---

## 2. Multiple-Choice Secretary Problem When `K/N ~ 1`

Classical secretary logic (skip an observation window, then pick better-than-best) is designed for scarce slots (`K << N`).

Here:
- If rejecting ~30 out of 500, then `K/N ≈ 470/500 = 0.94`.
- If operationally targeting `K_accept ≈ 530` with only 500 applicants, effective `K/N` is beyond 1.

Consequences:
- The opportunity cost of early rejection is very high.
- Large "look-then-leap" sampling windows are harmful.
- Optimal policy shifts from "find top few" to "avoid bottom few".

In secretary terms, you are in a **high-capacity regime** where optimal thresholds are permissive and mostly used for identifying the worst tail.

---

## 3. Prophet Inequalities Lens (`gamma_K` for `K=250`)

A useful framing for online acceptance under uncertainty:
- Offline prophet sees all applicants and picks best feasible set.
- Online policy commits as applicants arrive.
- `gamma_K` is the competitive fraction vs offline optimum.

For `K`-choice prophet settings with independent values, the guarantee improves with `K` and is often written as:

`gamma_K = 1 - Theta(1/sqrt(K))`

For `K = 250`, this scale implies a high constant around:

`1 - 1/sqrt(250) ≈ 0.937`

Interpretation for this use case:
- Online wave decisions can be near-offline-optimal in value capture.
- But seat-fill risk is dominated by no-show variance, not ranking regret.
- Therefore, allocate most algorithmic effort to **show-probability-aware ordering and overbooking control**, not ultra-fine top-k quality discrimination.

Note: exact `gamma_K` depends on the exact model class and algorithm family; the `~0.94` figure is a large-`K` scale estimate, not a universal exact constant.

---

## 4. Dynamic Thresholding with Overbooking

### 4.1 Utility to Rank Applicants

Use a blended score:

`u_i = alpha * q_i + (1 - alpha) * r_i`

Where:
- `q_i`: quality score (project potential, technical depth, etc.)
- `r_i`: predicted show probability
- `alpha`: business weight (e.g., 0.6 quality / 0.4 reliability)

Given high overbooking pressure, `r_i` should be heavily weighted relative to typical admissions.

### 4.2 Online Acceptance State

At step `t`:
- `a_t`: offers already sent
- `p_hat_t`: current show-rate estimate
- `S_rem = max(0, S - a_t * p_hat_t)` expected seat gap
- Needed future offers: `A_need_t = ceil(S_rem / p_hat_t)`

Let `R_t` be remaining applicants in pipeline. Set a dynamic threshold so expected acceptances among `R_t` roughly matches `A_need_t` plus safety buffer.

### 4.3 Safety Buffer

Use a z-buffer for uncertainty in `p` and binomial variance:

`A_target = ceil((S + z * sqrt(S * (1-p)/p)) / p)` (practical approximation)

Or directly solve for smallest `A` with `P(Binomial(A,p) >= S) >= c`.

---

## 5. The Inversion: Rejecting 30, Not Selecting 250

With ~500 applicants and show rate 47%:
- Selectivity objective becomes tail-pruning.
- Workflow should explicitly target:
  - `reject_count = 30` (or similar),
  - accept everyone else quickly,
  - and backfill externally to reach confidence-adjusted offers.

Operationally:
1. Build a bottom-tail risk list (low quality + low show propensity + policy constraints).
2. Reject that tail early only if needed.
3. Approve remaining applicants in waves prioritizing high `u_i` first.
4. Continuously re-estimate seat risk and trigger extra outreach if needed.

---

## 6. Practical Wave Strategy (`N=500, K_accept=530, target_seats=250, show_rate=0.47`)

### 6.1 Feasibility Reality Check

`K_accept=530 > N=500` means:
- From the current pool alone, max offers = 500.
- Expected attendance cap = `500 * 0.47 = 235`.
- Expected shortfall to 250 = ~15 seats.

So the plan must include one or more:
- additional applicant sourcing,
- rolling standby list,
- re-invites from prior cohorts,
- referral-based fast-track admits.

### 6.2 Recommended Wave Template

Example wave plan (for applicant pool + expansion pool):
- Wave 1 (T-14 to T-10 days): top reliability-adjusted applicants, ~220 offers
- Wave 2 (T-9 to T-6): next tranche, ~170 offers (cum 390)
- Wave 3 (T-5 to T-3): broad approve, ~110 offers (cum 500)
- Wave 4 (T-2 to T-1): expansion pool / reactivation, +30 to +80 as needed

Decision rule each wave:
- Compute posterior `p_hat` from confirmations/check-in proxies.
- Recompute offers needed for chosen confidence (e.g., 80-90%).
- If required offers exceed remaining pool, trigger external sourcing immediately.

---

## 7. Python Reference Implementation

```python
from __future__ import annotations

import math
import random
from dataclasses import dataclass
from typing import List, Dict, Tuple


@dataclass
class Applicant:
    id: int
    quality: float          # q_i in [0, 1]
    show_prob: float        # r_i in [0, 1]

    def utility(self, alpha: float = 0.6) -> float:
        return alpha * self.quality + (1 - alpha) * self.show_prob


def binom_tail_prob_at_least(n: int, p: float, k: int) -> float:
    """P[X >= k] for X~Binomial(n,p). Exact summation (fine for n~600)."""
    return sum(math.comb(n, x) * (p ** x) * ((1 - p) ** (n - x)) for x in range(k, n + 1))


def min_offers_for_confidence(target_seats: int, p: float, confidence: float, start: int = 0, stop: int = 2000) -> int:
    for n in range(max(start, target_seats), stop + 1):
        if binom_tail_prob_at_least(n, p, target_seats) >= confidence:
            return n
    raise ValueError("Increase stop; no solution found")


def reject_bottom_tail(applicants: List[Applicant], reject_count: int, alpha: float = 0.6) -> Tuple[List[Applicant], List[Applicant]]:
    ranked = sorted(applicants, key=lambda a: a.utility(alpha), reverse=True)
    accepted_pool = ranked[:-reject_count] if reject_count > 0 else ranked
    rejected = ranked[-reject_count:] if reject_count > 0 else []
    return accepted_pool, rejected


def make_waves(applicants: List[Applicant], wave_sizes: List[int], alpha: float = 0.6) -> List[List[Applicant]]:
    ranked = sorted(applicants, key=lambda a: a.utility(alpha), reverse=True)
    waves = []
    idx = 0
    for size in wave_sizes:
        waves.append(ranked[idx: idx + size])
        idx += size
    if idx < len(ranked):
        waves.append(ranked[idx:])
    return waves


def monte_carlo_attendance(offered: List[Applicant], trials: int = 20000, rng_seed: int = 7) -> Dict[str, float]:
    rng = random.Random(rng_seed)
    counts = []
    for _ in range(trials):
        c = sum(1 for a in offered if rng.random() < a.show_prob)
        counts.append(c)
    counts.sort()
    mean = sum(counts) / len(counts)
    q10 = counts[int(0.10 * len(counts))]
    q50 = counts[int(0.50 * len(counts))]
    q90 = counts[int(0.90 * len(counts))]
    return {"mean": mean, "p10": q10, "p50": q50, "p90": q90}


def demo_strategy(
    N: int = 500,
    K_accept: int = 530,
    target_seats: int = 250,
    baseline_show_rate: float = 0.47,
    reject_count: int = 30,
    confidence: float = 0.80,
) -> None:
    # Synthetic applicant set. In production, replace with model outputs.
    rng = random.Random(42)
    applicants = [
        Applicant(
            id=i,
            quality=min(1.0, max(0.0, rng.betavariate(2.0, 2.0))),
            show_prob=min(1.0, max(0.0, rng.betavariate(5.0, 5.6)))  # centered near ~0.47
        )
        for i in range(N)
    ]

    # 1) Inversion step: reject tail, keep most people.
    accepted_pool, rejected = reject_bottom_tail(applicants, reject_count=reject_count, alpha=0.6)

    # 2) Required offers by confidence under baseline show rate.
    need_for_conf = min_offers_for_confidence(target_seats, baseline_show_rate, confidence, start=N)

    # 3) If K_accept exceeds pool size, external sourcing required.
    max_internal_offers = len(accepted_pool)
    internal_offers = min(max_internal_offers, K_accept)
    external_needed = max(0, max(K_accept, need_for_conf) - internal_offers)

    # 4) Wave plan for internal pool.
    # Front-load high utility for early commitment + quality floor.
    waves = make_waves(accepted_pool, wave_sizes=[220, 170, 110], alpha=0.6)
    offered_internal = [a for w in waves for a in w][:internal_offers]

    # 5) Attendance simulation (internal only, as a lower bound).
    sim = monte_carlo_attendance(offered_internal, trials=20000)

    print("=== Parameters ===")
    print(f"N={N}, K_accept={K_accept}, target_seats={target_seats}, baseline_show_rate={baseline_show_rate:.2f}")
    print(f"Reject count={reject_count}, confidence target={confidence:.0%}")

    print("\n=== Core math ===")
    print(f"Expected offers for mean fill: {target_seats / baseline_show_rate:.1f}")
    print(f"Offers needed for {confidence:.0%} fill probability: {need_for_conf}")

    print("\n=== Operational outputs ===")
    print(f"Internal pool after rejection: {len(accepted_pool)}")
    print(f"Internal offers possible now: {internal_offers}")
    print(f"Externally sourced offers needed: {external_needed}")
    print(f"Rejected IDs (sample 10): {[a.id for a in rejected[:10]]}")

    print("\n=== Internal-only attendance distribution (lower bound) ===")
    print(sim)


if __name__ == "__main__":
    demo_strategy(
        N=500,
        K_accept=530,
        target_seats=250,
        baseline_show_rate=0.47,
        reject_count=30,
        confidence=0.80,
    )
```

---

## 8. Policy Recommendation

For this parameter regime, use a **high-acceptance, reliability-aware, wave-controlled policy**:

1. Treat acceptance as default; treat rejection as exception (bottom-tail pruning).
2. Rank by reliability-adjusted utility, not quality alone.
3. Front-load top utility into early waves.
4. Continuously recompute overbooking need using observed response signals.
5. Pre-commit to external backfill because `N=500` cannot reliably deliver `250` seats at `p=0.47`.

This is the correct optimal-stopping adaptation when `K/N` is near (or above) 1.

---

## 9. Quick Numerical Cheat Sheet

- `E[checkins | A=500, p=0.47] = 235`
- `E[checkins | A=530, p=0.47] = 249.1`
- `P(checkins>=250 | A=500) ≈ 9.7%`
- `P(checkins>=250 | A=530) ≈ 48.6%`
- `A needed for 80% confidence ≈ 552`
- `A needed for 90% confidence ≈ 564`

If constrained to ~500 applicants, your optimization target is exactly as stated:
- **Who to accept first**, and
- **Which ~30 to reject**,
while building an external offer channel.
