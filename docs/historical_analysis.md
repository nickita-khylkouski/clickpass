# Cerebral Valley Historical Event Data Analysis

Generated: 2026-03-04

## Data Sources

| Source | Records | Key Fields |
|--------|---------|------------|
| Supabase `event_applicants` | 8,765 | event_name, status, checked_in, rsvp_date, created_at |
| Supabase `events` | 12 | name, start_date, total_applicants |
| Local `cv_data/event_attendance.csv` | 6,839 | handle, event_name, status, checked_in |
| Local `hackathon_elo.db` (SQLite) | 3,062 people, 100 events | ELO ratings, attendance_rate, events_signed_up, events_checked_in |

---

## A. Event-Level Stats

### Show Rates by Event (from cv_data, events with check-in data, 20+ approved)

| Event | Approved | Checked In | Show Rate |
|-------|----------|------------|-----------|
| Hackathon: Build Your AI Teammate with CodeRabbit & Cline | 194 | 169 | **87.1%** |
| GPT-5 Startup Hackathon NYC | 219 | 135 | 61.6% |
| Mistral AI MCP Hackathon | 218 | 134 | 61.5% |
| OpenAI GPT-5 Startup Hackathon | 455 | 275 | 60.4% |
| Agentic Memory & Context Engineering Hackathon | 466 | 269 | 57.7% |
| Nano Banana Hackathon | 327 | 174 | 53.2% |
| Gemini Vibe Code Hackathon -- London | 221 | 111 | 50.2% |
| Google Gemini Hackathon | 435 | 218 | 50.1% |
| Gemini 3 Hackathon SF | 292 | 141 | 48.3% |
| Hack FLUX: Beyond One | 221 | 103 | 46.6% |
| Agentic Orchestration and Collaboration Hackathon | 779 | 352 | 45.2% |
| Gemini 3 Hackathon London | 233 | 104 | 44.6% |
| Vercel x Equinox v0 Workshop | 107 | 47 | 43.9% |
| Gemini Vibe Code Hackathon -- SF | 323 | 137 | 42.4% |
| AI Fintech Hackathon | 243 | 92 | 37.9% |
| AIE Code Agents Hackathon | 240 | 85 | 35.4% |
| Gemini 3 SuperHack | 346 | 114 | 32.9% |
| Edge Deployments & On-Device AI Hackathon | 203 | 58 | 28.6% |
| The Rejection Challenge | 42 | 4 | 9.5% |

Note: Events with 0% check-in are likely missing check-in data, not true no-shows. The 19 events above have confirmed check-in tracking.

### Summary Statistics

| Metric | Value |
|--------|-------|
| N events with check-in data | 19 |
| **Mean show rate** | **47.2%** |
| Median show rate | 46.6% |
| **Weighted show rate (by event size)** | **48.9%** |
| Std deviation | 16.0% |
| Min | 9.5% |
| Max | 87.1% |

---

## B. Show Rate by Event Size

| Size Bucket | N Events | Mean Show Rate | Weighted Show Rate | Range |
|-------------|----------|----------------|-------------------|-------|
| Small (<100 accepted) | 2 | 5.7% | 5.2% | 2-10% |
| Medium (100-249 accepted) | 11 | 45.3% | 46.9% | 1-87% |
| **Large (250+ accepted)** | **8** | **48.8%** | **49.1%** | **33-60%** |

**Key finding**: Large events (250+) have the most stable show rates at ~49%, likely because they represent the core CV hackathon format with the best data quality. The "small" bucket is polluted by events that may have incomplete check-in data.

For the overbooking model, **use 45-50% as the baseline show rate** for a typical CV hackathon.

---

## C. Application Arrival Curve (from Supabase timestamps)

Analysis of 23 events with 30+ applicants and multi-day application windows.

### Aggregate Timing Statistics

| Metric | Value |
|--------|-------|
| % of apps arriving in **last 25%** of window | **48% mean, 37% median** |
| % of apps arriving in **last 50%** of window | **75% mean, 77% median** |
| 25% of apps arrive by | 49% of window |
| 50% of apps arrive by | 65% of window |
| 75% of apps arrive by | 82% of window |

### Interpretation

Applications follow a **heavy right-skew / late-surge pattern**:
- The first half of the application window captures only ~25% of applications
- 75% of applications arrive in the latter half of the window
- Nearly half of all applications arrive in the last quarter of the window

This is a **power-law-like arrival curve**, not linear or S-shaped. It resembles:
- Conference registration patterns (exponential near deadline)
- College application deadlines (massive surge in final days)

### Per-Event Arrival Detail (selected large events)

| Event | N | Window | % in Last 25% | % in Last 50% |
|-------|---|--------|----------------|----------------|
| Anthropic Hackathon London | 903 | 25 days | 19% | 65% |
| CV 2020 Vision | 636 | 10 days | 76% | 88% |
| Mistral AI SF Hackathon | 532 | 18 days | 29% | 75% |
| Meta Llama 3 Hackathon | 439 | 11 days | 29% | 80% |
| TED AI Multimodal Hackathon | 428 | 26 days | 96% | 100% |
| National Security Hackathon | 418 | 37 days | 57% | 73% |
| Mistral AI Paris Hackathon | 395 | 15 days | 30% | 74% |
| Llama Impact London | 373 | 17 days | 28% | 88% |
| Mistral AI London Hackathon | 340 | 11 days | 24% | 79% |
| CV Codegen Hackathon | 274 | 5 days | 27% | 53% |
| Consumer AI NYC | 265 | 18 days | 54% | 77% |

### Approval Rate: Early vs Late Applicants

| Metric | Value |
|--------|-------|
| Early-half mean approval rate | 58% |
| Late-half mean approval rate | 57% |

**No significant difference** in approval rates between early and late applicants. This suggests the current process does not strongly favor early applicants in acceptance decisions.

---

## D. Repeat Attendee Patterns (from ELO Database)

### Attendance Rate by Visit Frequency

| Frequency | N People | Avg Attendance Rate | Avg Check-ins | Avg ELO |
|-----------|----------|---------------------|---------------|---------|
| 1 event | 1,015 | **77.1%** | 0.8 | 921 |
| 2 events | 309 | **62.1%** | 1.2 | 926 |
| 3-5 events | 214 | **51.8%** | 1.8 | 934 |
| 6+ events | 73 | **44.9%** | 3.5 | 949 |

**Counter-intuitive finding**: People who sign up for MORE events have LOWER per-event attendance rates. This makes sense -- serial registrants are "sampling" events and skip more often, while one-time registrants are more committed to the single event they signed up for.

However, in absolute terms, frequent registrants attend MORE total events (3.5 check-ins for 6+ registrants vs 0.8 for single-event).

### Attendance Rate Distribution

| Bucket | N People | Avg ELO | Avg Events |
|--------|----------|---------|------------|
| 90-100% | 927 | 926 | 1.2 |
| 70-89% | 24 | 955 | 2.9 |
| 50-69% | 246 | 930 | 1.7 |
| 30-49% | 92 | 935 | 1.8 |
| 1-29% | 36 | 925 | 1.6 |
| 0% | 286 | 915 | 1.2 |

**Key finding**: 286 people (18% of registrants) have a 0% attendance rate -- they sign up but never show. The 927 people (58%) with 90-100% attendance are highly reliable.

Overall mean attendance rate across all people: **69.4%**

### Top Repeat Registrants

| Signed Up | Checked In | Attendance Rate | ELO |
|-----------|------------|-----------------|-----|
| 15 | 0 | 0% | 900 |
| 15 | 4 | 27% | 926 |
| 12 | 2 | 17% | 914 |
| 12 | 4 | 33% | 856 |
| 12 | 6 | 50% | 901 |

Even the most prolific registrants have attendance rates of 0-50%, confirming the "serial sampler" pattern.

---

## E. ELO Database Event-Level Stats (with check-in data)

From the ELO DB, events where both approved_count and checked_in_count are nonzero:

| Event | Date | Applied | Approved | Checked In | Show Rate |
|-------|------|---------|----------|------------|-----------|
| Agentic Orchestration Hackathon | 2026-01-10 | 853 | 779 | 352 | 45% |
| Gemini 3 Hackathon London | 2025-12-13 | 272 | 233 | 104 | 45% |
| Gemini 3 Hackathon SF | 2025-12-06 | 413 | 292 | 141 | 48% |
| Hack FLUX: Beyond One | 2025-11-22 | 307 | 221 | 103 | 47% |
| AIE Code Agents Hackathon | 2025-11-22 | 297 | 240 | 85 | 35% |
| Gemini Vibe Code London | 2025-11-15 | 316 | 221 | 111 | 50% |
| The Rejection Challenge | 2025-11-14 | 42 | 42 | 4 | 10% |
| Gemini Vibe Code SF | 2025-11-08 | 562 | 323 | 137 | 42% |
| Edge Deployments Hackathon | 2025-10-20 | 357 | 203 | 58 | 29% |
| Google Gemini Hackathon | 2025-10-18 | 666 | 435 | 218 | 50% |
| Agentic Memory Hackathon | 2025-10-11 | 506 | 466 | 269 | 58% |
| GPT-5 Startup NYC | 2025-09-27 | 281 | 219 | 135 | 62% |
| Enterprise MCP Hackathon | 2025-09-13 | 54 | 54 | 1 | 2% |
| CodeRabbit & Cline Hackathon | 2025-09-13 | 195 | 194 | 169 | 87% |
| Mistral AI MCP Hackathon | 2025-09-13 | 355 | 218 | 134 | 61% |
| Nano Banana Hackathon | 2025-09-06 | 618 | 327 | 174 | 53% |
| AI Fintech Hackathon | 2025-08-23 | 311 | 243 | 92 | 38% |
| OpenAI GPT-5 Hackathon | 2025-08-09 | 864 | 455 | 275 | 60% |

ELO DB Show Rate: **mean=45.7%, median=47.4%** (consistent with CV data analysis)

---

## F. Acceptance Selectivity

From Supabase `event_applicants`, status breakdown:

| Metric | Value |
|--------|-------|
| Total applicants across all events | 8,765 |
| Approved | 5,443 (62%) |
| Pending (rejected/waitlisted) | 3,322 (38%) |

Typical acceptance rate: **~62%** of applicants get approved.

Effective conversion funnel:
- 100% apply
- ~62% accepted
- ~47% of accepted show up
- = **~29% of applicants actually attend**

---

## G. Empirical Parameters for Overbooking Model

### Core Parameters

| Parameter | Best Estimate | Range | Source |
|-----------|---------------|-------|--------|
| **Base show rate (p)** | **0.47** | 0.35-0.60 | 19 events with check-in data |
| Show rate std dev | 0.16 | - | Across events |
| Show rate for large events (250+) | 0.49 | 0.33-0.60 | 8 large events |
| Show rate for medium events (100-249) | 0.47 | 0.29-0.87 | 11 medium events |
| Acceptance rate | 0.62 | 0.35-1.00 | Supabase data |

### Application Arrival Curve Parameters

| Parameter | Value | Notes |
|-----------|-------|-------|
| % of apps in last 25% of window | 48% (mean), 37% (median) | Heavy right skew |
| % of apps in last 50% of window | 75% (mean) | Most apps come late |
| 50th percentile of apps at | 65% of window elapsed | Median app arrives in last 35% |
| 75th percentile of apps at | 82% of window elapsed | - |

### Repeat Attendee Parameters

| Parameter | Value | Notes |
|-----------|-------|-------|
| Overall mean person-level attendance rate | 69.4% | Higher than event-level show rate due to selection |
| 1-event registrant attendance rate | 77.1% | Most reliable per-event |
| Multi-event (3+) attendance rate | 51.8% | Serial samplers |
| Zero-show registrants (never attend) | 18% of all registrants | Chronic no-shows |
| Perfect attendance (90-100%) | 58% of all registrants | Highly reliable |

### Derived Overbooking Parameters

For a venue with capacity C:

| Scenario | Recommended Accepts | Rationale |
|----------|--------------------|-----------|
| Conservative (p=0.50) | 2.0 * C | Expect 50% show, accept 2x capacity |
| Moderate (p=0.45) | 2.2 * C | Slightly lower expected show rate |
| Aggressive (p=0.40) | 2.5 * C | Account for events with lower engagement |

**Risk of overcrowding** (more than C show up when accepting N):
- At N = 2C with p=0.47: P(exceed) = P(Binomial(2C, 0.47) > C) -- very low for large C
- The high variance (std=0.16) means event-specific factors dominate
- Recommendation: Use **individual-level priors** (person's attendance_rate) rather than a single p

---

## H. Key Takeaways

1. **Show rate is ~47% on average** but varies enormously (10% to 87%) across events. A single number is insufficient for planning.

2. **Large events (250+ accepted) stabilize around 45-50%** show rate. This is the most reliable baseline for the overbooking model.

3. **Applications arrive in a heavy right-skew pattern**: 75% of applications come in the second half of the window, and nearly half come in the last quarter. This means early wave decisions must be made with incomplete applicant pools.

4. **Early vs late applicants have similar approval rates** (~58% vs 57%), suggesting no quality bias in timing.

5. **Repeat attendees are NOT more reliable per-event** -- they actually have lower attendance rates (45-62% vs 77% for one-timers). However, they have richer signal for prediction.

6. **18% of registrants are chronic no-shows** (0% attendance across all events). Identifying and down-weighting these individuals could improve show rate predictions.

7. **58% of registrants have near-perfect attendance** (90-100%). These are the most predictable and valuable for capacity planning.

8. **The effective funnel is: 100% apply -> 62% accepted -> 47% show = ~29% conversion**. This means for every 100 applicants, only ~29 will actually walk in the door.

9. **For the wave acceptance algorithm**: Use a per-person show probability based on their historical `attendance_rate` (if available) as a Bayesian prior, with the event-level mean (0.47) as the population prior for first-time applicants.

10. **Application timing should NOT affect acceptance decisions** based on current data (no quality signal from timing), but SHOULD affect wave scheduling (most applicants come late, so early waves will be small).
