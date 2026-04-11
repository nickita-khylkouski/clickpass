# Open-Source Applicant Ranking Plan

## Goal

Turn `cv-rank` from a Cerebral-Valley-shaped internal tool into a reusable applicant ranking platform:

- open-source core package for applicant ingestion, enrichment interfaces, ranking, review, export, and evaluation
- private CV adapters and policies that continue to use CV data
- explicit fairness, debiasing, and audit layers suitable for applicant selection workflows

This is not a rename-only project. The current code mixes:

- core ranking logic
- CV-specific data sources and schemas
- CV-specific prompts and event language
- sponsor analytics and forecasting features

The plan below separates those concerns without losing the existing product value.

## Current Coupling Problems

### 1. Product assumptions leak into core prompts

Examples today:

- prompts repeatedly say "AI community event in San Francisco"
- Swiss prompt asks who belongs in an "exclusive AI community event"
- quality review is framed around a fixed invite cutline and event-centric language

That makes the engine less reusable for:

- fellowship applications
- accelerator admissions
- grant or residency review
- job applicant triage
- conference speaker selection

### 2. Supabase / platform data are treated as near-core dependencies

Today the README and runtime defaults make Supabase effectively required for normal operation. That is incompatible with an open-source product where users need to plug in their own:

- ATS data
- CSVs
- form submissions
- HRIS exports
- custom profile APIs

### 3. Identity is still partially keyed by name/email in critical paths

The code has improved with `candidate_id`, but some ranking and merge paths still key by `name` or `email`. That is fragile and unsuitable for a general applicant platform because:

- names collide
- emails may be missing
- anonymized review should not depend on human-readable identifiers
- cross-system joins need stable, opaque IDs

### 4. Bias mitigation is too narrow

Current mitigation is mainly:

- name anonymization
- profile-length debiasing
- some cutline re-review

That is useful, but too small for applicant selection. It does not yet cover:

- structured evidence normalization
- protected-attribute separation
- source-quality effects
- sparse-profile penalties
- subgroup disparity measurement
- reviewer disagreement and uncertainty handling

### 5. Sponsor and forecasting modules are mixed into the same package

These features are valuable, but they should not define the open-source core boundary. They are downstream products built on the ranking/evidence layer, not part of the minimum reusable engine.

## Target Product Shape

Split the system into three layers.

### Layer 1: OSS core

Package name suggestion:

- `applicant-rank`
- or keep `cv-rank` temporarily and make the repo positioning generic

Responsibilities:

- canonical candidate model
- dataset ingestion and schema mapping
- evidence graph
- pluggable enrichment interfaces
- pluggable scoring strategies
- pairwise comparison engine
- combination/reranking engine
- fairness and audit pipeline
- evaluation harness
- reviewer UI/CSV exports

Non-goals for the core:

- CV-specific Supabase schema knowledge
- sponsor packet generation
- private event attendance forecasting models
- private platform SQL

### Layer 2: policy packs

A policy pack defines:

- use case: hackathon, fellowship, job applicants, conference speakers
- rubric
- criteria weights
- prompt templates
- fairness constraints
- review thresholds
- output columns

Examples:

- `policy.hackathon_builders`
- `policy.technical_fellowship`
- `policy.startup_residency`
- `policy.software_engineering_hiring`

The engine should not hardcode "what makes someone valuable." A policy pack should.

### Layer 3: private adapters

Keep CV-specific integrations in a private package/repo:

- `cv_rank_cv_private` or `cv-rank-cv-adapters`

Responsibilities:

- Supabase enrichment adapter
- platform DB adapter
- sponsor analytics
- attendance forecasting
- CV-specific policy packs

This preserves internal leverage while making the core portable.

## Target Architecture

### 1. Canonical domain model

Introduce explicit domain objects:

- `Candidate`
- `Application`
- `EvidenceItem`
- `EvidenceSource`
- `EvaluationPolicy`
- `ScoreArtifact`
- `ComparisonArtifact`
- `DecisionArtifact`
- `AuditArtifact`

Rules:

- every candidate gets a stable `candidate_id`
- all joins use `candidate_id`
- raw source keys are stored as foreign references, not primary keys
- protected attributes live in a separate restricted audit payload, never in model prompts

### 2. Evidence-first pipeline

Replace "formatted profile text from mixed fields" with two steps:

1. normalize raw inputs into structured evidence
2. render policy-specific prompt views from that evidence

Why:

- easier to compare sources fairly
- easier to hide protected/sensitive fields
- easier to support different review modes
- easier to generate audit trails

Suggested evidence types:

- `work_experience`
- `education`
- `project`
- `open_source`
- `publication`
- `community_signal`
- `application_answer`
- `portfolio_link`
- `judge_score`
- `behavioral_history`

Each `EvidenceItem` should carry:

- source
- timestamp
- confidence
- verification status
- normalization notes

### 3. Adapter interfaces

Define interfaces such as:

- `CandidateSourceAdapter`
- `EvidenceEnricher`
- `ScoreProvider`
- `PairwiseJudge`
- `AuditSink`

Concrete adapters:

- CSV adapter
- JSONL adapter
- Greenhouse/Lever adapter
- CV Supabase adapter
- GitHub adapter
- LinkedIn-like structured adapter

This is the seam that makes "any data can go in" true instead of aspirational.

### 4. Strategy-based ranking engine

Make ranking strategies composable:

- `pointwise_llm`
- `rubric_llm`
- `pairwise_swiss`
- `pairwise_full_round_robin`
- `manual_review_only`
- `hybrid_rule_plus_llm`

Outputs should be standardized so policies can swap strategy without changing export logic.

### 5. Separate packages/modules

Recommended repo structure:

```text
src/applicant_rank/
  cli/
  domain/
  ingest/
  evidence/
  adapters/
  policies/
  scoring/
  ranking/
  fairness/
  audit/
  eval/
  export/

src/cv_rank_private/
  adapters/
  policies/
  sponsor/
  attendance/
```

## Debiasing and Fairness Plan

Do not position this as "bias solved." Position it as:

- bias-aware
- auditable
- policy-controlled
- human-reviewable

### Fairness principle

For applicant selection, the right target is not "demographic parity at all costs." The right target is:

- job/program relevance
- validity
- consistency
- harmful-bias detection and mitigation
- reviewability

This matches NIST’s framing of trustworthy AI as valid, reliable, accountable, transparent, explainable, and fair with harmful bias managed.

### What to remove from model prompts

Default prompt view should exclude:

- name
- email
- exact location if not relevant
- age proxies where possible
- graduation year unless policy explicitly needs seniority inference
- protected-attribute hints where avoidable

Policy-gated exceptions should be explicit and logged.

### What to normalize before scoring

Normalize evidence so the model sees comparable units:

- years of experience as bounded ranges, not prose
- GitHub activity as normalized counts + recency windows
- company signal as optional policy feature, not universal prestige shortcut
- project evidence as verified artifacts, not self-description only
- publication signal separated by venue type and recency

This reduces over-rewarding verbose, polished, or prestige-heavy profiles.

### Debiasing mechanisms to implement

#### A. Blind review mode

Keep and strengthen anonymization:

- `candidate_id` only in prompts
- scrub names, emails, direct demographic markers
- remove exact school/company names in an optional "reduced-prestige" mode for first-pass scoring

Use two-pass review:

1. blind evidence-first pass
2. contextual pass for finalists when domain relevance requires richer context

#### B. Sparse-profile correction

Replace simple profile-length debiasing with evidence-density normalization:

- distinguish low-information from low-quality
- track missingness by source category
- estimate score uncertainty for sparse candidates
- route high-uncertainty candidates to manual review instead of simply depressing score

#### C. Source-balance controls

Prevent one source from dominating:

- cap the effect of LinkedIn polish
- cap prestige priors from employer/school
- separate self-reported evidence from verified evidence
- require at least one substantive signal for high-confidence positive decisions

#### D. Group-level fairness audits

When demographic labels are legally and operationally available for audit only:

- compute selection-rate parity
- compute score-distribution shifts by group
- compute false positive / false negative gap on labeled historical outcomes
- track subgroup performance for combined intersections, not just single attributes

These labels should never be injected into prompts. They belong in offline audit jobs.

#### E. Counterfactual and perturbation tests

Add automated audits that rerun evaluation after changing:

- name
- school prestige tokens
- employer prestige tokens
- verbosity
- writing polish
- geography markers

If rankings swing too much under irrelevant perturbations, the policy fails review.

#### F. Human escalation rules

Auto-route to manual review when:

- score uncertainty is high
- model signals disagree strongly
- candidate is near the cutline
- fairness guardrails trip
- evidence is sparse but promising

The model should not be the final decider for edge cases.

## Evaluation Plan

The open-source version needs an evaluation harness before a rewrite.

### 1. Build benchmark datasets

Maintain three datasets:

- `demo_public`: synthetic or consented examples for OSS tests
- `internal_historical`: CV data for private benchmarking
- `policy_specific_eval`: job/fellowship/hackathon labeled sets

### 2. Measure three classes of quality

#### Ranking quality

- NDCG / Kendall tau against trusted historical decisions
- cutline stability across reruns
- pairwise agreement with expert reviewers

#### Operational quality

- cost per 100 applicants
- runtime
- resume/restart reliability
- adapter coverage

#### Fairness quality

- blind vs non-blind rank deltas
- subgroup adverse-impact diagnostics
- perturbation sensitivity
- missing-data sensitivity

### 3. Ship policy cards

Each policy pack should have a `POLICY_CARD.md`:

- intended use
- not intended for
- evidence sources used
- bias risks
- audit metrics
- human review requirements

## Migration Plan

### Phase 0: Freeze semantics

Goal: preserve current behavior before refactor.

Deliverables:

- golden-run fixtures from current CV workflows
- snapshot tests for exports
- benchmark suite for score/rank drift

### Phase 1: Extract open-source core contracts

Goal: create interfaces without changing behavior.

Work:

- introduce canonical `candidate_id` everywhere
- move prompt text behind policy interfaces
- move enrichment behind adapter interfaces
- isolate sponsor and waves modules from the base CLI

Success:

- current CV workflow still runs through the new contracts

### Phase 2: Generic ingestion and policy packs

Goal: make non-CV input actually supported.

Work:

- generalized schema mapper
- JSON/CSV adapters
- generic default policy pack
- remove hard requirement that Supabase be enabled

Success:

- a user with only CSV/JSON can run end to end

### Phase 3: Fairness and audit layer

Goal: make the system responsibly reusable.

Work:

- blind review mode
- sparse-profile uncertainty handling
- audit-only demographic evaluation jobs
- perturbation test harness
- policy cards and audit reports

Success:

- every ranking run emits an audit bundle

### Phase 4: Private CV package

Goal: keep CV leverage without contaminating the OSS core.

Work:

- move Supabase/platform adapters into private package
- move sponsor tooling into private package
- move attendance forecasting into private package
- publish extension docs for private adapters

Success:

- OSS repo can be published without private infra assumptions

### Phase 5: Public release hardening

Work:

- replace secrets/docs that assume internal env
- public quickstart with local-only workflow
- demo dataset
- policy examples
- adapter authoring docs
- fairness and governance docs

## Non-Negotiable Product Decisions

1. The core must be evidence-first, not raw-profile-text-first.
2. The core must not require CV infrastructure.
3. Protected attributes belong in audit workflows, not prompts.
4. Blind review should be the default first pass for applicant selection.
5. Sparse profiles should create uncertainty and manual review, not automatic rejection.
6. Sponsor analytics and attendance forecasting are extensions, not core.
7. Every policy pack needs an explicit intended-use and fairness story.

## Biggest Risks

### Risk 1: open-sourcing the current prompt logic without guardrails

This would create a reusable ranking engine that still bakes in prestige bias and event-specific priors.

### Risk 2: trying to make Supabase "optional" without a real adapter layer

That leads to hidden coupling and fragile behavior.

### Risk 3: fairness theater

Name anonymization alone is not enough. If we claim "debiased" without audit infrastructure, the product positioning will be weak.

### Risk 4: over-generalizing too early

The right sequence is:

- extract contracts
- preserve current behavior
- then generalize

Not:

- rewrite everything around an abstract dream architecture

## Recommended Immediate Next Step

Implement an RFC in this order:

1. define canonical domain model and adapter interfaces
2. identify every CV-specific prompt/config/default
3. move sponsor and waves commands behind extension boundaries
4. replace name-keyed joins with `candidate_id`
5. design fairness/audit bundle before changing ranking math

That is the smallest path to a real open-source core.
