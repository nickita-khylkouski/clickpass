# Cerebral Valley Tools — Vision & Research

Everything we've researched across 30+ agents, synthesized into one place.

---

## How CV Makes Money

Labs and companies pay CV to host hackathons. Example: the entire Google series was fully funded by Google. Sponsors pay $5K-$150K per event + API credits ($50K-$150K).

### The 6 Reasons Sponsors Pay

| # | Goal | What They Want |
|---|------|---------------|
| 1 | **Developer Mindshare** | Promote their product, get it in as many devs' hands as possible |
| 2 | **Recruiting** | Find and recruit engineering talent from events |
| 3 | **Marketing** | Big splash upon new model/product release |
| 4 | **Investments** | VCs and partners invest in winners and talent |
| 5 | **Integration** | Top devs integrate the new model into their startups/companies after the event |
| 6 | **Product Feedback** | Direct developer feedback on new releases (hackathons hosted close to launch date) |

---

## Tools Mapped to Sponsor Goals

### Goal 1: Developer Mindshare — "get it in as many devs' hands as possible"

**What helps:**
- **cv-rank** (exists) — Select the right 150-500 people who will actually BUILD with the API, not just attend
- **cv-discover** (designed) — Find MORE qualified devs beyond the organic applicant pool. GitHub graph walk, DevPost serial winners, HuggingFace model publishers, dormant CV users
- **No-show predictor** (designed) — 60% no-show rate means 60% of mindshare is wasted. Predict who won't come, over-accept intelligently to fill every seat
- **NEW: Sponsor Usage Report** — After the event, show the sponsor exactly how many devs used their API and how deeply. "142 teams used your API, 87 used advanced features like tool_use"

**Metrics to track:** Teams that used sponsor API (%), integration depth score (1-6), API calls during event, unique developers who made their first API call at the hackathon

---

### Goal 2: Recruiting — "recruit engineering talent from events"

**What helps:**
- **cv-rank** (exists) — Already produces a ranked CSV with GitHub, LinkedIn, skills, experience, company data. This IS a talent database.
- **NEW: Talent Report for Sponsors** — Filtered view of top performers: "Here are the 20 best engineers at your event, with GitHub profiles, what they built, and their judge scores." Opt-in only (attendees consent to share profile with sponsors).
- **hackathon-elo** (exists) — Global ELO rankings across events. A sponsor can see "who are the top 50 rated hackers in the CV community?"
- **Hidden Gem Detector** (designed) — Find people whose code quality exceeds their resume. Low pointwise (resume looks average) + high BT strength (beats everyone head-to-head) = undervalued talent sponsors should recruit.

**Metrics to track:** Attendees hired by sponsors (need to start tracking this), talent report click-through rate, recruiter engagement

---

### Goal 3: Marketing — "big splash upon new release"

**What helps:**
- **NEW: Project Showcase Generator** — Auto-generate a public gallery of the best projects built with the new product. Pull from submissions: project name, one-line summary (LLM-generated from README), demo video, GitHub link. Sponsor uses this in their blog post / Twitter thread / launch announcement.
- **NEW: Stats Package** — "In 24 hours, 150 teams built 150 projects with [Product]. The best ones: [links]." Ready-to-tweet, ready-to-blog content package for the sponsor's marketing team.
- **Submission Analysis** — Scan all 150 repos to find the most impressive demos, categorize by use case (chatbots, agents, tools, creative), extract the best screenshots/videos.

**Metrics to track:** Social media impressions mentioning the event, blog posts published by attendees, projects featured in sponsor's marketing

---

### Goal 4: Investments — "VCs invest in winners & talent"

**What helps:**
- **hackathon-elo** (exists) — Longitudinal tracking of who keeps winning. VCs can see "this person has placed top 3 in 5 hackathons over 12 months"
- **cv-rank enrichment** (exists) — Full profiles with company, role, GitHub quality, is_founder, etc. Already a mini due-diligence package.
- **NEW: Founder Signal Report** — For attendees who are founders: company stage, what they built, judge scores, past hackathon performance. VC partners at the event get this instead of trying to collect business cards.
- **Post-hackathon tracking** — Which hackathon projects became real products? Track GitHub activity at 30/60/90 days. "3 projects from your last hackathon are now live products with users."

**Metrics to track:** Investments made in hackathon participants, projects that became companies, follow-on funding for hackathon winners

---

### Goal 5: Integration — "integrate the new model into their startups/companies"

**What helps:**
- **NEW: 30-Day Follow-Up Report** — The killer feature nobody else does. 30 days after the event, check: are hackathon teams still using the API? Did they deploy to production? New commits on the repo? This is the metric that proves long-term integration. Currently 93% of projects die — even moving that to 80% is massive.
- **cv-rank data** — You know which attendees are founders, at what companies, with how many employees. A sponsor can see "12 founders with funded startups used our API at this hackathon — here are their companies"
- **Post-event nurture** — Auto-generate personalized follow-up for the top 20 teams: "Your project [X] was great. Here's how to deploy it for real: [sponsor's production docs link]. Need help? Here's a direct line to our DevRel team."

**Metrics to track:** API usage at 30/60/90 days post-event, projects deployed to production, startups that switched to sponsor's API

---

### Goal 6: Product Feedback — "developers give direct feedback on new releases"

**What helps:**
- **NEW: Feedback Aggregator** — During the hackathon, collect structured feedback: What API endpoints did you use? What was confusing? What broke? What's missing? Can be a simple form at submission time, or a bot in Discord collecting complaints in real-time.
- **NEW: Bug/Friction Report** — Scan Discord/Slack messages during the event for error messages, complaints, confusion. NLP categorization: "authentication issues (23 teams), rate limiting (15 teams), unclear docs for function calling (12 teams)." This is incredibly valuable for the sponsor's product team.
- **Submission Analysis** — Look at what teams actually built. If 80% built chatbots and 2% built agents, that tells the sponsor where their product is intuitive vs. where it's not.
- **Code pattern analysis** — Semgrep the repos: which API features were used vs ignored? If nobody used streaming, maybe the docs are bad. If everyone used the same wrapper pattern, maybe it should be in the SDK.

**Metrics to track:** Bug reports categorized by severity, feature usage distribution, docs pages visited, common error patterns, time-to-first-successful-API-call

---

## The Hackathon Lifecycle — Where Tools Fit

### Pre-Event (weeks before)

| Problem | Tool | Status |
|---------|------|--------|
| Review 1K-13K applications | **cv-rank** | Built (336 tests) |
| 60% no-show rate wastes spots | **No-show predictor** | Designed, needs labeled data join |
| Pool doesn't grow | **cv-discover** | Designed (docs/cv-discover.md) |
| Same people keep applying | **Dormant user scorer** | Concept |

### Day of Event (hour 0-2)

| Problem | Tool | Status |
|---------|------|--------|
| Solo attendees waste 2-4hrs finding teams | **cv-match** | Concept |
| Teams scope too big | **Scope advisor** | Concept |

### During Hackathon (hour 2-24)

| Problem | Tool | Status |
|---------|------|--------|
| Teams get stuck silently | **cv-pulse** (team health) | Concept |
| Wrong mentor for the problem | **Mentor router** | Concept |
| No progress visibility for organizers | **Live dashboard** | Concept |
| Sponsor wants to see API usage | **Live usage tracker** | Concept |

### Judging (hour 24-26)

| Problem | Tool | Status |
|---------|------|--------|
| 24hrs evaluated in 3min | **AI judge pre-screen** | Concept |
| Inconsistent judging criteria | **Judge calibration** | Concept |
| Pre-built code not caught | **Integrity checker** | Concept |
| Presentation > substance | **Code quality score** | Concept |

### Post-Event (day 1-90)

| Problem | Tool | Status |
|---------|------|--------|
| Sponsor can't prove ROI | **cv-sponsor report** | Researched |
| 93% of projects die | **Post-hackathon nurture** | Concept |
| No talent report for sponsors | **Talent report** | Concept |
| No content for sponsor marketing | **Project showcase** | Concept |
| No 30-day follow-up | **Retention tracker** | Concept |
| No feedback report for product team | **Feedback aggregator** | Concept |

---

## What We've Built vs. What Exists

| Tool | Status | Lines | Tests |
|------|--------|-------|-------|
| **cv-rank** | Production-ready | 12,500 | 336 passing |
| **hackathon-elo** | Working (local) | ~5,000+ | — |
| **cv-discover** | Design doc complete | — | — |
| Everything else | Research + concepts | — | — |

---

## Research Highlights (from 30+ agent runs)

### On Prediction
- No-show prediction: **82-91% accuracy** in literature, and we have **6,839 labeled rows** to train on
- Hackathon winner prediction: **zero published research** — genuinely unproven
- Resume → actual performance: ceiling is **r=0.42** (17% variance explained). Best method humans have ever found.
- Simple equal-weight models match complex ML at small sample sizes (Dawes 1979, replicated dozens of times)

### On Sponsors
- **60.7%** of DevRel teams say proving ROI is their #1 challenge
- Sponsors who see ROI data renew at **70%+ rates** (vs 65% baseline)
- The 30-day follow-up is the single highest-value thing nobody sends
- Only **7% of hackathon projects** have ANY activity 6 months post-event

### On the Hackathon Experience
- **10-20%** of hacking time wasted on team formation
- **60%** no-show rate for approved applicants
- **79%** of attendees come to exactly one event
- The #1 differentiator in judging: **presentation ability** (9.38/10 top vs 2.64 bottom), not technical skill
- Only **9.14%** of code in hackathon repos is actually written during the event

### On Discovery
- **11,000** registered CV users have never attended an event
- GitHub graph walk from 44 elite members yields **2,000-3,000** candidates
- DevPost dataset: **372 MB**, 6,700+ hackathons, free on HuggingFace
- Cost per discovery run: **~$0.51**

### On the Community
- **15,988** registered users, **4,877** have attended at least one event
- **44** elite members (5+ events, 3+ submissions, podium finisher)
- **100%** of elite members have GitHub, only **53%** of casuals
- Top 10 super-repeaters attend 10-18 events each

---

## Key Data Assets

### Supabase (live production DB)
- 14 tables: linkedin, linkedin_analytics, linkedin_position, linkedin_education, linkedin_publication, linkedin_certification, linkedin_project, linkedin_volunteer_experience, linkedin_organization, github, github_analytics, github_repository, event_applicants, x
- Connection: `supa_get(url, key, table_name, params)` via cv-rank utils

### hackathon-elo SQLite (local, 27MB)
- 3,062 persons, 116 events, 2,308 submissions, 11,732 judge scores, 3,559 rating snapshots
- 1,183 GitHub repo URLs in submissions (analyzable for sponsor reports)
- Path: `/Users/nickita/hackathon-elo/hackathon_elo.db`

### CV Platform CSVs
- 15,988 users, 6,839 attendance records, 13,223 judging scores, 1,156 submissions, 2,026 team memberships
- Path: `/Users/nickita/hackathon-elo/cv_data/`

---

## Priority: What to Build Next

Based on sponsor goals and data availability:

| Priority | Tool | Why | Effort |
|----------|------|-----|--------|
| 1 | **Sponsor Usage Report** | Directly proves ROI (Goal 1, 5, 6). We have 1,183 repos to analyze. | 1 week |
| 2 | **No-show predictor** | Fills more seats = more mindshare (Goal 1). We have 6,839 labeled rows. | 1 week |
| 3 | **30-day follow-up** | Proves long-term integration (Goal 5). Re-check repos via GitHub API. | 3 days |
| 4 | **Talent report** | Recruiting value (Goal 2). cv-rank already has the data. | 3 days |
| 5 | **cv-discover** | Grow the pool (Goal 1). Design doc ready. | 1 week |
| 6 | **Feedback aggregator** | Product feedback (Goal 6). Scan Discord + submission forms. | 1 week |
| 7 | **Project showcase** | Marketing content (Goal 3). Auto-generate from submissions. | 3 days |

---

## Open Questions for Manager

1. What takes the most time during event prep right now?
2. How do you currently decide the top 6 for stage demos?
3. What's the biggest complaint from attendees? From sponsors?
4. Do you track who actually checked in vs who was approved?
5. Would sponsors pay more for a detailed usage/talent report?
6. Is growing the applicant pool a priority, or is current volume enough?
7. What's the timeline for new tooling — next event or long-term?
8. If you could have ONE tool that doesn't exist today, what would it be?
9. Do sponsors ever ask for recruiting/talent data from events?
10. How close to launch date are hackathons typically hosted?
