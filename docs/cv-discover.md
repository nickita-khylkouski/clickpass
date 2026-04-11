# cv-discover

**Find the people who should be at your events but don't know about you yet.**

cv-discover is the outbound inverse of cv-rank. cv-rank scores people who already applied. cv-discover finds people who *should* apply but haven't.

```
cv-rank discover --seeds RANKED.csv --top 50 --method github-graph
```

---

## The Problem

Cerebral Valley has 15,988 registered users. Of those:

- **11,000 (70%)** signed up but never attended an event
- **4,877 (30%)** attended at least one event
- **79%** of attendees come to exactly one event and never return
- **60%** of approved applicants no-show

If you keep ranking the same applicant pool, you optimize a closed loop. The community doesn't grow, events don't get better, and you miss people who'd be great but never heard of you.

## The Solution

Build a "golden profile" from your best members, then find people who match it across GitHub, HuggingFace, DevPost, and your own dormant user base.

### Pipeline

```
Seeds (your best members)
  → Crawl (find similar people on GitHub/HF/DevPost)
    → Score (rank by similarity to golden profile)
      → Outreach (invite via referrals or personalized cold contact)
```

---

## Stage 1: Seed Extraction

### Who are the seeds?

The 44 "elite" Cerebral Valley members — defined by real data:

| Criteria | Threshold |
|----------|-----------|
| Events attended | 5+ |
| Hackathon submissions | 3+ |
| Podium finishes | 1+ |
| Average judging score | 8.5+/10 |

### What makes them different (from actual data analysis)

| Signal | Elite (44) | Casual (13,902) |
|--------|------------|-----------------|
| Has GitHub | 100% | 53% |
| Has Twitter/X | 86% | 30% |
| Production AI deployed | Nearly all | Rare |
| Specific numbers in application | Always | Never |
| Self-ID skill level | Expert/Experienced | Beginner/Learning |

The biggest differentiator in judging: **presentation and demo ability** (9.38/10 for top scorers vs 2.64 for bottom), not raw technical skill.

### Golden profile construction

Extract features from all seeds, compute centroid:

```python
features = [
    gh_api_stars, gh_api_commits_year, gh_api_prs_year,
    gh_api_followers, len(github_best_languages),
    li_follower_count, years_experience,
    is_founder, is_in_big_tech,
    events_approved / total_events_applied,  # approval rate
    len(self_description),                    # application effort
    hackathon_wins, finalist_count,
]

golden_profile = mean(normalize(seed_features))
```

No ML. Just averages across normalized features. Research (Dawes 1979, replicated dozens of times) shows equal-weight models match or beat regression at small sample sizes.

For text-based matching, embed profile bios with SBERT (`all-mpnet-base-v2`, 768-dim) and compute seed centroid in embedding space.

---

## Stage 2: Discovery Sources

### Source 1: Dormant Users (highest ROI, zero API calls)

11,000 registered users who never attended. Score each against the golden profile using whatever fields exist in Supabase (GitHub username, Twitter, bio). The top 500 who look like your elite members = easiest growth lever.

**Expected yield:** 500-1,000 high-similarity matches
**Cost:** $0 (data already in your DB)

### Source 2: GitHub Graph Walk

For each of the 44 elite members, crawl their GitHub social graph:

**Co-stargazers** — Get repos the seed starred. Get other people who starred those same repos. People who star the same niche repos as 3+ of your seeds = strong signal.

```graphql
query {
  user(login: "seed_username") {
    starredRepositories(first: 50, orderBy: {field: STARRED_AT, direction: DESC}) {
      nodes {
        name
        stargazers(first: 100) {
          nodes { login }
        }
      }
    }
  }
}
```

**Followers** — People already following your elite members are paying attention to the right things.

**Co-contributors** — People contributing to the same repos are building in the same space.

**Expected yield:** 2,000-3,000 unique candidates from 44 seeds
**Cost:** Free (5,000 GraphQL points/hr, ~2 hours for full crawl)
**Rate limit strategy:** Token rotation, ETags for conditional requests, `per_page=100`, exponential backoff on 429

### Source 3: DevPost Dataset

Pre-scraped dataset on HuggingFace: `alvanlii/devpost-hackathon-projects` (372 MB, 6,700+ hackathons). Includes team members, prizes, tags, descriptions.

```python
from datasets import load_dataset
ds = load_dataset("alvanlii/devpost-hackathon-projects")

# Find serial winners (3+ prizes across different hackathons)
winners = ds.filter(lambda x: x["prize"] is not None)
# Explode team members, group by person, count wins
```

Cross-reference: DevPost username → DevPost profile (linked GitHub) → GitHub API (full profile).

**Expected yield:** Thousands of proven hackathon builders
**Cost:** Free download
**Legal:** Using a pre-existing public dataset sidesteps DevPost's no-scraping ToS

### Source 4: HuggingFace

Find AI builders publishing models, datasets, and Spaces.

```python
from huggingface_hub import HfApi
api = HfApi()

# Top model publishers in AI-relevant areas
models = api.list_models(
    pipeline_tag="text-generation",
    sort="likes",
    direction=-1,
    limit=500
)
authors = set(m.author for m in models if m.downloads > 100)
```

Filter for individual users (not orgs), cross-reference to GitHub via profile scraping.

**Expected yield:** Hundreds of serious AI builders
**Cost:** Free API, no auth required
**Limitation:** No user search endpoint. Must discover users via their published artifacts.

### Source 5: Bonus Sources

| Source | What | How |
|--------|------|-----|
| **lablab.ai** | AI-specific hackathon winners | Scrape `lablab.ai/apps/recent-winners` |
| **ETHGlobal** | Crypto-AI builders | Scrape `ethglobal.com/showcase` |
| **MLH Top 50** | Annual elite hacker list | Scrape `top.mlh.io` |
| **GH Archive** | Historical star/fork data | BigQuery (~$5/TB scanned) |

---

## Stage 3: Scoring

### Level 1: Heuristic Score (zero ML, instant)

```python
def discovery_score(candidate: dict) -> float:
    score = 0.0
    if candidate.get("gh_api_stars", 0) > 50:     score += 0.15
    if candidate.get("gh_api_commits_year", 0) > 100: score += 0.15
    if candidate.get("hackathon_wins", 0) > 0:    score += 0.20
    if candidate.get("is_founder"):                score += 0.10
    if candidate.get("years_experience", 0) > 3:   score += 0.10
    if candidate.get("builds_ai"):                 score += 0.15
    if candidate.get("shared_seeds", 0) >= 3:      score += 0.15  # follows 3+ seeds
    return score
```

### Level 2: Embedding Similarity (better, still simple)

```python
from sentence_transformers import SentenceTransformer
import numpy as np

model = SentenceTransformer("all-mpnet-base-v2")

# Embed seed bios → compute centroid
seed_embeddings = model.encode([s["bio"] for s in seeds])
centroid = np.mean(seed_embeddings, axis=0)

# Score each candidate
candidate_embedding = model.encode(candidate["bio"])
similarity = np.dot(centroid, candidate_embedding) / (
    np.linalg.norm(centroid) * np.linalg.norm(candidate_embedding)
)
```

Expected precision at K:

| Top-K | Precision |
|-------|-----------|
| 10 | 60-80% |
| 25 | 45-65% |
| 50 | 30-50% |
| 100 | 20-35% |

### Level 3: Combined Score

```python
final_score = (
    0.4 * cosine_similarity(bio_embedding, centroid) +
    0.3 * heuristic_score(features) +
    0.2 * jaccard(candidate_skills, seed_skills) +
    0.1 * graph_proximity  # how many seeds they're connected to
)
```

---

## Stage 4: Outreach

### Conversion Rates (from research)

| Method | Response Rate |
|--------|-------------|
| Generic cold email | 1-3% |
| Personalized cold email | 5-10% |
| Warm intro from member | **30-50%** |

### Referral-First Strategy

Primary path: generate referral suggestions for existing members.

```
"Hey [member], we found 5 people in your GitHub network who'd
be great for our next hackathon. Mind sending them a note?"
```

200 members x 5 referrals x 30% conversion = **300 new applicants** per event.

### Cold Outreach (secondary, capped)

Maximum 50 cold contacts per event. Every message must reference specific work:

```
"Hey [name], saw your [repo/model/project] — the [specific detail]
is really impressive. We're running [event] on [date] in SF and
think you'd crush it. Here's the link: [url]"
```

### Anti-Spam Rules

- Max 50 cold contacts per event
- Must reference a specific repo, model, or project
- One contact per person per quarter
- Never open GitHub issues/PRs as outreach (community norm violation)
- Email or Twitter DM only
- Track all outreach in persistent store to prevent duplicates

---

## Architecture

```
cv_rank/
  discovery/              # NEW module
    __init__.py
    seeds.py              # Seed extraction from Supabase + past results
    github_graph.py       # GitHub follower/stargazer crawling
    devpost.py            # DevPost dataset loader + serial winner finder
    huggingface.py        # HuggingFace model author discovery
    scoring.py            # Heuristic + embedding similarity scoring
    outreach.py           # Personalized message generation
    candidates_db.py      # SQLite store for discovered candidates + outreach state
```

### CLI Commands

```bash
# Full discovery pipeline
cv-rank discover --seeds RANKED.csv --top 50 --method github-graph

# Score dormant users from Supabase
cv-rank discover --seeds RANKED.csv --method dormant-users

# Load DevPost dataset and find serial winners
cv-rank discover --method devpost --tags "ai,machine-learning"

# Find HuggingFace model publishers
cv-rank discover --method huggingface --pipeline-tag text-generation

# Generate referral suggestions for existing members
cv-rank discover --referrals --seeds RANKED.csv --members all_users.csv
```

### Output

`DISCOVERED_CANDIDATES.csv`:

| Column | Description |
|--------|-------------|
| name | Full name (if available) |
| github_url | GitHub profile |
| huggingface_url | HuggingFace profile (if found) |
| discovery_source | github-graph / devpost / huggingface / dormant |
| similarity_score | 0-1 similarity to golden profile |
| matched_archetype | Which seed cluster they resemble |
| discovered_via | Which seed member connected them |
| shared_seeds | Number of elite members they're connected to |
| hackathon_wins | Past wins (from DevPost) |
| outreach_reason | Auto-generated personalized hook |
| contact_method | email / twitter / referral |

---

## Data Dependencies

### What We Already Have

| Data | Location | Status |
|------|----------|--------|
| 15,988 users with GitHub/Twitter handles | Supabase `all_users` | Ready |
| 6,839 attendance records (47 events) | `hackathon-elo/cv_data/event_attendance.csv` | Ready |
| 2,512 judging scores (23 events) | `hackathon-elo/data/opus_applicants/prior_judging_scores.csv` | Ready |
| 1,157 submissions with placements | `hackathon-elo/cv_data/submissions.csv` | Ready |
| GitHub enrichment pipeline | `cv_rank/enrichment/github_api.py` | Ready to reuse |
| Supabase enrichment pipeline | `cv_rank/enrichment/supabase.py` | Ready to reuse |

### What We Need

| Data | How to Get It | Effort |
|------|--------------|--------|
| DevPost dataset | `load_dataset("alvanlii/devpost-hackathon-projects")` | 1 line |
| GitHub social graph | GraphQL API calls | Build crawler |
| HuggingFace authors | `huggingface_hub` library | Build scraper |
| Cross-platform identity resolution | Username matching + profile scraping | Medium effort |

---

## How Discovery Platforms Actually Work (Research)

### Industry Approaches

| Platform | Data Sources | Scoring | Moat |
|----------|-------------|---------|------|
| SignalFire Beacon | 650M profiles, GitHub events, z-score composites | GBDT on career trajectory | 10 years of data |
| Clay.com | 50-150 providers in waterfall enrichment | Aggregation, not AI | Provider breadth |
| Common Room | GitHub + Discord + Slack + Twitter | Person360 identity resolution | 79% dedup rate |
| OpenSauced | Real-time GitHub Events, NL search | Open source | Community |
| LinkedIn Recruiter | Custom Lucene (Galene), GLMix personalization | Multi-armed bandit | Data monopoly |

**Key insight:** Most "AI talent discovery" is glorified data aggregation + gradient-boosted trees. The real technical moat is **identity resolution** (matching the same person across platforms).

### What Nobody Else Is Doing

No tool exists specifically for **event organizers** to discover attendees. Every tool is optimized for hiring. A system that:

1. Scores on community fit (not just code output)
2. Matches against event themes (AI agents, LLMs, specific tracks)
3. Uses past event performance as signal (judging scores, attendance)
4. Does referral-first outreach through existing members

...would be genuinely new.

---

## The Flywheel

```
Event 1: Rank 500 applicants → 50 accepted → collect judging + attendance data
    |
    v
Better golden profile (based on real outcomes, not just applications)
    |
    v
Discovery run: find 200 new people from GitHub graph + DevPost + dormant users
    |
    v
Event 2: 500 organic + 200 discovered = 700 applicants → more selective → better event
    |
    v
More alumni → bigger referral network → more discovered people per run
    |
    v
Event N: the system compounds with every event
```

Each event produces:
- Better seeds (who actually performed well, not just who applied)
- More graph edges (attendees' GitHub networks expand the crawl surface)
- More training data (for no-show prediction, score prediction)
- More referral paths (alumni know people who'd be great)

---

## Cost Per Discovery Run

| Resource | Cost |
|----------|------|
| GitHub API | Free (5,000 req/hr) |
| HuggingFace API | Free |
| DevPost dataset | Free (one-time download) |
| SBERT embeddings | Free (local inference) |
| OpenAI (optional: cluster labeling) | ~$0.01 |
| OpenAI (optional: outreach generation) | ~$0.50 |
| **Total** | **~$0.51** |

---

## MVP Scope (~5 days)

| Day | Deliverable |
|-----|-------------|
| 1-2 | Seed extraction from Supabase + `event_attendance.csv`. Feature vector construction. Golden profile centroid. |
| 2-3 | GitHub graph crawler: followers + co-stargazers of top 44 seeds. Dedup + rate limit handling. |
| 3-4 | Scoring: heuristic + cosine similarity. Rank candidates. |
| 4-5 | CLI (`cv-rank discover`), CSV output, personalized outreach reason per candidate. |

### Stretch Goals

- HuggingFace model author discovery
- DevPost serial winner finder
- SBERT embedding similarity (requires `sentence-transformers` dependency)
- Referral suggestion generator
- SQLite outreach tracking (prevent re-contacting)

---

## Risks and Mitigations

| Risk | Mitigation |
|------|-----------|
| GitHub rate limits | Token rotation, ETags, GraphQL batching, GH Archive for bulk historical |
| Low cross-platform match rate | Start with GitHub-only (highest coverage in your community) |
| Spammy outreach damages brand | Hard cap at 50 cold contacts, referral-first, personalization required |
| Legal/GDPR | Official APIs only, opt-out in every message, data minimization |
| Low precision on discovery | Start with dormant users (already interested), validate before scaling to cold sources |
| Identity resolution is hard | Start with exact username matching, upgrade to fuzzy matching later |

---

## Success Metrics

| Metric | Target |
|--------|--------|
| New applicants per event from discovery | 50+ |
| Referral response rate | >25% |
| Cold outreach response rate | >5% |
| Discovered candidates who attend | >30% |
| Precision@50 (similarity scoring) | >40% |
| Cost per discovered attendee | <$0.10 |
| Time to run full discovery pipeline | <30 minutes |
