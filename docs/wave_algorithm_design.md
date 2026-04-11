# cv-rank Wave-Based Acceptance Algorithm Design

## Goal
Fill venue capacity reliably when show rate is low (~47%) without over-optimizing rejection.

Practical policy:
- Accept almost everyone who is not clearly weak.
- Reject only the bottom ~30 applicants.
- Optimize *timing and ordering* of admits: strongest candidates get offers earliest so they have more planning time.

---

## Key Inputs and Constants

- `C`: physical venue capacity (target check-ins), e.g. `250`
- `A`: total applicants by final decision point, e.g. `~500`
- `p_pop`: baseline show probability from historical data, `0.47`
- `R_floor`: minimum hard rejects, default `30`
- `Cu`: cost of an empty seat (underfill), default `5`
- `Co`: cost of an over-capacity attendee (overflow), default `1`
- Waves at: `T-21`, `T-14`, `T-7`, `T-2` days

Why this matters numerically:

- Naive overbooking requirement: `C / p_pop = 250 / 0.47 = 531.9` → need about `532` accepts to fill `250` seats.
- If only `A=500` applicants exist, we cannot hit 532.
- With `R_floor=30`, max accepts are `A - R_floor = 470`.
- So system should accept ~`470/500` (94%) and only reject bottom 30.

---

## 1) Core Algorithm (Newsvendor Overbooking)

### 1.1 Candidate show probability

For candidate `i`, estimate show probability with empirical-Bayes shrinkage:

`p_i = (n_i * r_i + k * p_pop) / (n_i + k)`

- `r_i`: personal historical attendance rate (if available)
- `n_i`: number of historical events for `i`
- `k`: prior strength (recommended `k=3`)
- If no history (`n_i=0`), `p_i = p_pop = 0.47`

Optional tier fallback (from existing docs): use tier priors like `0.70, 0.68, 0.57, 0.66, 0.40, 0.24` when attendance history is missing but profile tier is known.

### 1.2 Decision variable

Let `N` be cumulative accepted offers (confirmed + pending) after a wave.
Sort candidates by cv-rank quality score (combined pointwise + Swiss BT).
Accepted set is top `N` not manually excluded.

### 1.3 Show distribution

For accepted set `S_N`:

`X_i ~ Bernoulli(p_i)`

`Show_N = Σ X_i` for `i in accepted set`

`E[Show_N] = μ_N = Σ p_i`

`Var(Show_N) = σ_N^2 = Σ p_i(1-p_i)`

### 1.4 Newsvendor objective

Minimize expected underfill/overflow cost:

`Cost(N) = Cu * E[(C - Show_N)_+] + Co * E[(Show_N - C)_+]`

Choose:

`N* = argmin_N Cost(N)`

where `N` is searched on integer grid `0..A_current` (or via normal approximation).

Critical fractile interpretation (classical newsvendor):

`tau = Cu / (Cu + Co)`

For homogeneous show probability `p` and large `N`, if `q*` is the target show quantile
(`F_Show(q*) = tau`), then rough accept count is:

`N ≈ q* / p`

In practice, we use grid search because `p_i` differs per candidate (Poisson-binomial, not simple binomial).

### 1.5 Hard policy constraints (business rule)

`N_target = min(N*, A_current - R_floor)`

- Guarantees bottom `R_floor` remain rejected.
- Encodes “accept almost everyone else.”

If `N* > A_current - R_floor`, emit warning:

`underfill_expected = max(0, C - Σ_top(A_current-R_floor) p_i)`

This is expected empty seats even after accepting everyone except bottom 30.

---

## 2) Wave Timing Design (`T-21`, `T-14`, `T-7`, `T-2`)

Use cumulative release fractions of `N_target_final` as defaults, then adjust with feedback.

| Wave | Default Cumulative Target | Intent |
|---|---:|---|
| `T-21` | `20%` | Early lock for strongest applicants (max planning lead time) |
| `T-14` | `45%` | Expand to next strong tier after first confirmations |
| `T-7`  | `80%` | Main fill wave once most applications are in |
| `T-2`  | `100%` | Final top-up to hit occupancy target |

Release count at wave `w`:

`release_w = max(0, target_cumulative_w - open_offers_before_w)`

Where `open_offers` = invited candidates not declined/expired.

Operational deadlines:
- `T-21` offer expiry: 72h
- `T-14` expiry: 48h
- `T-7` expiry: 24h
- `T-2` expiry: 12h

This preserves urgency while keeping early top candidates informed first.

---

## 3) Confirmation Feedback Loop Between Waves

After each wave, ingest outcomes: `confirmed`, `declined`, `expired`, `pending`.

### 3.1 Confirmation-adjusted show probability

At wave `w`, replace base `p_i` with status-adjusted `p_i^(w)`:

- Declined: `p_i^(w) = 0`
- Confirmed: `p_i^(w) = p_confirm` (default `0.85`)
- Invited but pending: `p_i^(w) = p_pending * p_i` (default `p_pending=0.35`)
- Not invited yet: use base `p_i`

### 3.2 Forecast before next wave

For current open offers:

`μ_open = Σ p_i^(w)`

`gap = C - μ_open`

Let `p_next` be mean `p_i` of next-ranked uninvited block (e.g. next 50).

Feedback top-up estimate:

`topup_w = max(0, ceil(gap / max(p_next, 0.05)))`

Then recompute newsvendor with updated `p_i^(w)` and set next `target_cumulative`.

### 3.3 Practical interpretation

- If confirmations are weak, next wave size expands automatically.
- If confirmations are strong, next wave shrinks/freeze to avoid overcrowding.

---

## 4) Integration with cv-rank Incremental Swiss Scoring

### 4.1 Ranking source of truth

Wave invites are always selected from latest `RANKED.csv` order (best to worst).

### 4.2 Execution pattern

- First wave (`T-21`): full run
  - `cv-rank run --csv applicants_t21.csv --accept <cutline_for_ranking>`
- Later waves (`T-14`, `T-7`, `T-2`): incremental refresh
  - `cv-rank incremental --prev-run <last_run_dir> --csv applicants_latest.csv --accept <cutline_for_ranking>`

Set `cutline_for_ranking = min(N_target_wave, A_current - R_floor)`.

Reason: incremental Swiss Phase B focuses on the accept/reject boundary; using wave cutline keeps model effort on the current decision frontier.

### 4.3 Invite selection rule

From latest ranking:
1. Exclude already invited/confirmed/declined and hard rejects.
2. Take next `release_w` highest-ranked candidates.
3. Persist immutable wave snapshot (`invites_T-14.csv`, etc.) for auditability.

### 4.4 Bottom-30 protection

At each wave, maintain a dynamic holdout set:
- `reject_pool = bottom R_floor by latest rank`
- Never invite from this pool unless explicit manual override.

---

## 5) CLI Design: `cv-rank waves`

Add a new top-level command with subcommands:

### 5.1 `cv-rank waves plan`

Purpose: initialize wave strategy and baseline targets.

Example:

```bash
cv-rank waves plan \
  --csv applicants.csv \
  --capacity 250 \
  --event-date 2026-04-20 \
  --show-rate 0.47 \
  --reject-floor 30 \
  --underfill-cost 5 \
  --overflow-cost 1
```

Outputs:
- `results/waves/<event>/wave_plan.json`
- `results/waves/<event>/wave_state.json` (empty status ledger)

### 5.2 `cv-rank waves run`

Purpose: execute a specific wave (`T-21`/`T-14`/`T-7`/`T-2`), refresh ranking, compute release set.

Example:

```bash
cv-rank waves run \
  --plan results/waves/openenv/wave_plan.json \
  --wave T-14 \
  --csv applicants_latest.csv \
  --prev-run results/run_20260310_091500
```

Behavior:
- Runs `cv-rank run` for first wave or `cv-rank incremental` otherwise.
- Recomputes `N_target` from confirmation-adjusted probabilities.
- Writes `invites_<wave>.csv` and `forecast_<wave>.json`.

### 5.3 `cv-rank waves confirm`

Purpose: ingest RSVP/confirmation changes.

Example:

```bash
cv-rank waves confirm \
  --plan results/waves/openenv/wave_plan.json \
  --input confirmations_2026-04-06.csv
```

Expected columns: `email,status,timestamp` where status in `{confirmed,declined,expired,pending}`.

### 5.4 `cv-rank waves status`

Purpose: show current occupancy forecast and next action.

Example:

```bash
cv-rank waves status --plan results/waves/openenv/wave_plan.json
```

Prints:
- current open offers / confirmed / declined
- expected shows (`μ_open`)
- expected underfill at current state
- recommended next release count

### 5.5 `cv-rank waves simulate` (optional)

Purpose: Monte Carlo sanity check for `P(underfill)` and `P(overfill)` under current plan.

---

## 6) Edge Cases

1. **Applicants fewer than overbooking target**
- If `A_current - R_floor < N*`, accept everyone except bottom 30.
- Surface underfill risk explicitly.

2. **Very high confirmation rate early**
- Freeze later waves if projected overflow exceeds threshold.
- Keep remaining candidates on waitlist.

3. **Very low confirmations / spike in declines**
- Increase `topup_w` and pull more from next-ranked candidates.
- At `T-2`, release all remaining non-reject candidates if still below target.

4. **Late high-quality applicants**
- Re-rank with incremental Swiss each wave so late applicants can leapfrog earlier weaker candidates.

5. **Ranking ties around reject floor**
- Tie-break by higher BT strength, then higher pointwise, then deterministic key (email hash).

6. **Missing attendance history**
- Fallback to `p_pop=0.47` or tier prior.

7. **No confirmation feed available**
- Run with base probabilities only; still works, but uncertainty increases.

8. **Duplicate applicant records**
- Deduplicate by normalized email before ranking/invites.

9. **API/model failure in a wave**
- Reuse previous run ranking and continue with conservative release cap.
- Mark wave as degraded mode in state file.

10. **Manual override required**
- Support `--manual-include` and `--manual-exclude` lists in `waves run`.
- Overrides are logged for audit.

---

## 7) Pseudocode and Formulas

### 7.1 Main orchestration

```python
def run_wave(plan, wave, applicants_csv, prev_run=None):
    state = load_state(plan)

    # A) refresh ranking
    if wave == "T-21" or prev_run is None:
        run_dir = cv_rank_run(applicants_csv, accept=plan.rank_cutline_guess)
    else:
        run_dir = cv_rank_incremental(prev_run, applicants_csv, accept=plan.rank_cutline_guess)

    ranking = load_ranked(run_dir)  # best -> worst
    applicants = dedupe(load_applicants(applicants_csv))

    # B) update show probabilities with confirmation ledger
    p = {}
    for person in applicants:
        p_base = empirical_bayes_show_prob(person, p_pop=plan.show_rate, k=3)
        status = state.status_by_email.get(person.email, "not_invited")
        p[person.email] = adjust_for_confirmation(p_base, status)

    # C) compute overbooking target with hard reject floor
    A = len(applicants)
    reject_floor = plan.reject_floor
    n_max = max(0, A - reject_floor)

    N_star = argmin_expected_cost(
        ranking=ranking,
        p_map=p,
        capacity=plan.capacity,
        underfill_cost=plan.Cu,
        overflow_cost=plan.Co,
        n_max=n_max,
    )
    N_target = min(N_star, n_max)

    # D) map target to wave cumulative cap
    base_frac = {"T-21": 0.20, "T-14": 0.45, "T-7": 0.80, "T-2": 1.00}[wave]
    base_cum = ceil(base_frac * N_target)

    open_offers = count_open_offers(state)
    mu_open = sum(p[email] for email in state.open_offer_emails())
    p_next = mean_next_ranked_probabilities(ranking, p, state, window=50)
    topup = max(0, ceil((plan.capacity - mu_open) / max(p_next, 0.05)))

    target_cumulative = min(N_target, max(base_cum, open_offers + topup))
    release_n = max(0, target_cumulative - open_offers)

    # E) select invitees (strongest first, excluding bottom reject floor)
    reject_pool = set(bottom_k_emails(ranking, k=reject_floor))
    invitees = []
    for row in ranking:
        email = row.email
        if email in reject_pool:
            continue
        if already_handled(email, state):
            continue
        invitees.append(email)
        if len(invitees) == release_n:
            break

    # F) persist artifacts
    write_invites_csv(plan, wave, invitees, ranking)
    update_state_with_new_invites(state, wave, invitees)
    write_forecast_json(plan, wave, N_star, N_target, mu_open, release_n)
    save_state(plan, state)

    return invitees
```

### 7.2 Cost minimization helper

```python
def argmin_expected_cost(ranking, p_map, capacity, underfill_cost, overflow_cost, n_max):
    best_n = 0
    best_cost = float("inf")

    for n in range(0, n_max + 1):
        accepted = ranking[:n]
        probs = [p_map[row.email] for row in accepted]

        # Poisson-binomial normal approximation
        mu = sum(probs)
        var = sum(pi * (1 - pi) for pi in probs)
        sigma = max(var, 1e-9) ** 0.5
        z = (capacity - mu) / sigma

        # E[(C-S)+] and E[(S-C)+] for normal S
        underfill = (capacity - mu) * Phi(z) + sigma * phi(z)
        overflow = (mu - capacity) * (1 - Phi(z)) + sigma * phi(z)

        cost = underfill_cost * underfill + overflow_cost * overflow
        if cost < best_cost:
            best_cost = cost
            best_n = n

    return best_n
```

### 7.3 Core formulas summary

- Personal show prior:
  - `p_i = (n_i r_i + k p_pop)/(n_i + k)`
- Expected shows for accepted set `N`:
  - `μ_N = Σ p_i`
  - `σ_N^2 = Σ p_i(1-p_i)`
- Newsvendor objective:
  - `N* = argmin_N [Cu E[(C-S_N)_+] + Co E[(S_N-C)_+]]`
- Hard rejection policy:
  - `N_target = min(N*, A - R_floor)` with `R_floor ≈ 30`
- Wave release:
  - `release_w = max(0, target_cumulative_w - open_offers_before_w)`

---

## Recommended Defaults

- `show_rate`: `0.47`
- `reject_floor`: `30`
- `Cu:Co`: `5:1` (empty seats hurt more than slight crowding)
- Wave cumulative fractions: `20%`, `45%`, `80%`, `100%`
- Confirmation multipliers: `confirmed=0.85`, `pending=0.35 * p_base`, `declined=0`

These defaults directly reflect current CV data and the operating rule: reject only the bottom tail; optimize who gets accepted first and when.
