# Ranking V2 Execution Plan

## Goal

Rebuild the applicant-ranking part of `cv-rank` so it preserves the current ranking behavior while becoming:

- stable for datasets up to 15,000 applicants
- easier to reason about and test
- modular enough to extend without another `cli.py` god object
- deterministic enough to debug and compare against v1

This plan is intentionally scoped to applicant ranking only.

## Scope

In scope:

- applicant ingest from CSV and event sources
- normalization into one canonical applicant model
- enrichment needed for applicant evaluation
- profile rendering for model evaluation
- pointwise scoring
- pairwise / Swiss / Bradley-Terry ranking
- signal combination
- cutline-focused borderline reranking
- quality review and decision exports
- checkpoint/resume, metrics, deterministic replay

Out of scope for this rewrite:

- sponsor packet generation
- sponsor analytics and publishing
- most of `waves` except optional `p_show` integration later
- repo-root ad hoc scripts
- marketing/reporting documents

## Non-Negotiable Invariants

These are the behaviors to preserve even if the implementation is rewritten from scratch:

- same core ranking semantics: pointwise + pairwise + combine + borderline
- same ability to export ranked outputs, clean outputs, failures, and needs-review rows
- same ability to resume interrupted runs
- same ability to run incrementally after the core pipeline is stable
- same ability to explain why someone was ranked highly or poorly

These are the implementation choices to reject:

- name-keyed joins
- direct business logic inside CLI handlers
- raw SQL embedded in orchestration code
- script-first internal pipelines
- repo-root scratch artifacts as runtime dependencies
- ad hoc checkpoint JSON without schema or atomicity

## What To Copy From V1

Copy the concepts, not the code shape:

- overall flow: ingest -> enrich -> profile -> pointwise -> Swiss -> BT -> combine -> borderline -> quality -> export
- pointwise scoring intent from [src/cv_rank/scoring/pointwise.py](/Users/nickita/cv-rank/src/cv_rank/scoring/pointwise.py)
- Swiss comparison approach from [src/cv_rank/scoring/swiss.py](/Users/nickita/cv-rank/src/cv_rank/scoring/swiss.py)
- Bradley-Terry strength fitting from [src/cv_rank/scoring/bradley_terry.py](/Users/nickita/cv-rank/src/cv_rank/scoring/bradley_terry.py)
- blend logic from [src/cv_rank/scoring/combine.py](/Users/nickita/cv-rank/src/cv_rank/scoring/combine.py)
- cutline reranking idea from [src/cv_rank/scoring/borderline.py](/Users/nickita/cv-rank/src/cv_rank/scoring/borderline.py)
- profile section intent from [src/cv_rank/profile.py](/Users/nickita/cv-rank/src/cv_rank/profile.py)
- enrichment source coverage from [/Users/nickita/cv-rank/src/cv_rank/enrichment](/Users/nickita/cv-rank/src/cv_rank/enrichment)
- review artifact expectations from [src/cv_rank/quality.py](/Users/nickita/cv-rank/src/cv_rank/quality.py)
- export artifact expectations from [src/cv_rank/csv_io.py](/Users/nickita/cv-rank/src/cv_rank/csv_io.py)

## What Not To Port

Do not reuse these patterns from v1:

- the orchestration shape in [src/cv_rank/cli.py](/Users/nickita/cv-rank/src/cv_rank/cli.py)
- partial identity by `name` or `email`
- duplicated field aliasing across modules
- direct filesystem and env lookups inside scoring functions
- ranking logic entangled with OpenAI client lifecycle
- hidden heuristics with no typed config surface

## V2 System Shape

```text
src/cv_rank_v2/
  domain/
    models.py
    enums.py
    schemas.py
  ingest/
    csv_loader.py
    event_loader.py
    normalize.py
    validate.py
  enrichment/
    base.py
    supabase.py
    github.py
    platform_db.py
    local_csv.py
    merge.py
  profile/
    render.py
    sections.py
  scoring/
    pointwise.py
    pairwise.py
    swiss.py
    bradley_terry.py
    combine.py
    borderline.py
    uncertainty.py
  runtime/
    config.py
    checkpoints.py
    clients.py
    metrics.py
    cache.py
  export/
    ranked_csv.py
    review_csv.py
    failures_csv.py
  cli/
    run.py
    validate.py
    incremental.py
```

### Core Design Rules

- `applicant_id` is the only cross-stage key.
- Every stage has typed inputs and outputs.
- Engine modules are pure where practical.
- Runtime modules own filesystem, env, network, and checkpoint concerns.
- CLI modules only parse args and call runners.

## Applicant Model

V2 should normalize all sources into one internal model before ranking starts.

```text
Applicant
  applicant_id
  source_refs[]
  basic
  application
  evidence
  event_history
  enrichment_status
  provenance
```

Minimum required sections:

- `basic`: display name, email, timezone, links
- `application`: raw answers, timestamps, event metadata
- `evidence`: work, education, projects, OSS, publications, social, judging, submissions
- `event_history`: prior CV attendance and outcomes
- `enrichment_status`: which enrichers ran, failed, or were skipped
- `provenance`: source-level trace for debugging and audit

## 15k Applicant Scaling Model

The ranking system should use a funnel instead of treating every applicant as a full-cost candidate.

### Tier 0: Ingest And Deterministic Enrichment For Everyone

Run on all applicants:

- header mapping
- schema validation
- canonical normalization
- deterministic enrichment from local / DB / Supabase / GitHub where available
- feature extraction
- profile assembly inputs

No pairwise ranking happens here.

### Tier 1: Broad Pointwise Signal

Run on all or nearly all applicants:

- compact pointwise evaluation
- deterministic heuristics where strong evidence already exists
- optional low-cost summary for sparse applications

This produces a broad ordering and uncertainty estimate.

### Tier 2: Selective Pairwise Resolution

Only run pairwise / Swiss where it changes outcomes:

- likely accepted tranche
- cutline band
- disagreement cases between heuristics and pointwise
- sparse or ambiguous profiles

This is the core scale unlock for 15k. We do not do deep Swiss over the entire population.

### Tier 3: Borderline Reranking

Run higher-accuracy, lower-throughput logic only for:

- people near the acceptance boundary
- strong disagreement cases
- flagged quality-review rows

## Workstreams

Each workstream below should be executed as an independent, reviewable unit with tests.

### Workstream 0: Baseline Freeze And Oracle

Purpose:
Capture what v1 currently does so the rewrite has a target.

Tasks:

- [ ] identify one fixed sample dataset for parity testing
- [ ] capture current v1 outputs for ranked CSV, clean CSV, failures, and needs-review
- [ ] record current config defaults used for the sample dataset
- [ ] fix or explicitly document the two currently failing baseline tests before using v1 as oracle
- [ ] add a golden-output harness so v2 can be compared against v1 phase by phase

Acceptance criteria:

- one reproducible sample run exists as the parity oracle
- baseline test status is explicit and trusted

### Workstream 1: Domain Model And Config

Purpose:
Create stable internal contracts.

Tasks:

- [ ] define `Applicant`, `EnrichedApplicant`, `PointwiseScore`, `PairwiseMatch`, `CombinedRank`, `QualityDecision`
- [ ] define `ResolvedConfig` with all runtime defaults in one place
- [ ] define stage result envelopes with status, timing, and provenance
- [ ] define deterministic-mode settings and RNG seed handling
- [ ] define checkpoint schemas and schema-versioning rules

Acceptance criteria:

- no stage depends on raw CSV row dicts after normalization
- no stage reads env vars directly
- all runtime defaults come from `ResolvedConfig`

### Workstream 2: Ingest And Normalization

Purpose:
Make applicant input reliable and deterministic.

Tasks:

- [ ] rewrite CSV header mapping into a dedicated ingest module
- [ ] normalize raw application fields into one `Applicant`
- [ ] assign stable `applicant_id` on ingest
- [ ] move alias logic into one mapping registry
- [ ] produce structured validation errors instead of loose logging
- [ ] handle duplicates, missing keys, and malformed rows explicitly

Acceptance criteria:

- duplicate names never collide
- ingest output is deterministic
- malformed rows are isolated without breaking the whole run

### Workstream 3: Enrichment Adapters

Purpose:
Keep all enrichers behind one shared contract.

Tasks:

- [ ] define shared enricher interface: `fetch_raw -> normalize -> merge`
- [ ] port Supabase enrichment behind paginated fetch helpers
- [ ] port GitHub enrichment behind shared retry / rate-limit handling
- [ ] port platform DB enrichment with stable event/submission/judging models
- [ ] port local CSV enrichment with the same merge contract
- [ ] record enrichment provenance and failure reasons per applicant

Acceptance criteria:

- enrichers emit one shared internal schema
- partial enricher failure does not corrupt the applicant object
- large batches can be resumed without redoing successful work

### Workstream 4: Profile Rendering

Purpose:
Make model inputs predictable and auditable.

Tasks:

- [ ] split profile rendering into section builders
- [ ] render from structured evidence only
- [ ] make anonymization an optional render mode, not a text regex pass bolted on later
- [ ] cache rendered profiles by applicant and render mode
- [ ] define compact and full profile variants for cost control

Acceptance criteria:

- pointwise, Swiss, and quality phases can request the exact render they need
- rendered profiles are reproducible for the same applicant state

### Workstream 5: Pointwise Scoring

Purpose:
Rebuild the broad ranking signal.

Tasks:

- [ ] port pointwise prompt intent into a clean scorer API
- [ ] separate prompt construction from transport and parsing
- [ ] define typed score outputs including confidence and rationale
- [ ] add cache keys based on applicant render + prompt version + model
- [ ] add deterministic replay for stored pointwise outputs
- [ ] surface model errors and parse failures as structured artifacts

Acceptance criteria:

- pointwise scoring can run across the full dataset with bounded concurrency
- repeat runs can reuse prior results when inputs are unchanged

### Workstream 6: Pairwise / Swiss / Bradley-Terry

Purpose:
Rebuild the expensive ranking layer without the current runtime entanglement.

Tasks:

- [ ] split pairing policy, match execution, and strength fitting into separate modules
- [ ] make Swiss pairing deterministic under fixed seed
- [ ] keep side-swapping and anti-position-bias protections
- [ ] port Bradley-Terry fitting behind a pure interface
- [ ] add uncertainty estimation and stop conditions based on cutline impact
- [ ] support candidate subset ranking without population-identity bugs

Acceptance criteria:

- pairwise behavior is reproducible in deterministic mode
- pairwise execution is subset-aware and keyed only by `applicant_id`

### Workstream 7: Signal Combination And Borderline Rerank

Purpose:
Preserve current final-decision semantics.

Tasks:

- [ ] port combine logic into a typed module with explicit inputs
- [ ] preserve entropy-style weighting behavior where desired
- [ ] formalize disagreement detection as a separate function
- [ ] formalize cutline band selection as config, not hidden heuristics
- [ ] rebuild borderline reranking as a stage rather than a patch on top

Acceptance criteria:

- final rank can explain which signals were used and how strongly
- cutline reranking is targeted, testable, and replayable

### Workstream 8: Quality Review And Export

Purpose:
Finish the ranking run with actionable outputs.

Tasks:

- [ ] rebuild quality review as prompt builder + runner + parser + artifact writer
- [ ] preserve verdicts and short reasons expected by operators
- [ ] split export writers by artifact type
- [ ] make output schemas explicit and versioned
- [ ] write review, failures, and ranked outputs from typed stage results

Acceptance criteria:

- outputs are consistent even when some applicants fail earlier stages
- exports no longer need implicit knowledge of raw source fields

### Workstream 9: Runtime, Checkpoints, And Resume

Purpose:
Make the pipeline operable on long runs.

Tasks:

- [ ] build atomic checkpoint writes
- [ ] version checkpoint schemas
- [ ] add per-stage progress manifests
- [ ] add idempotent resume behavior
- [ ] track tokens, cost, phase timing, and cache hit rates centrally
- [ ] provide crash-safe recovery for long-running jobs

Acceptance criteria:

- interrupted runs resume without data corruption
- checkpoint state is inspectable and schema-versioned

### Workstream 10: Incremental Ranking

Purpose:
Reintroduce incremental mode only after the core system is stable.

Tasks:

- [ ] define how prior applicants and new applicants share scale
- [ ] preserve prior pointwise artifacts when unchanged
- [ ] restrict pairwise work to new-vs-anchor and boundary-impact matches
- [ ] calibrate BT alignment explicitly instead of implicit heuristics
- [ ] export delta artifacts for operator review

Acceptance criteria:

- incremental mode matches full rerun decisions closely on held-out tests
- scale alignment is explainable and tested

## Testing Strategy

V2 should not ship without all three test layers.

### 1. Unit Tests

Required areas:

- config resolution
- ID assignment and duplicate handling
- normalization and field alias mapping
- profile section rendering
- Swiss pairing and side swapping
- Bradley-Terry fitting
- combine and cutline selection
- checkpoint serialization

### 2. Contract Tests

Required areas:

- Supabase adapter normalization
- GitHub adapter normalization
- platform DB enrichment
- local CSV enrichment
- pointwise parse contracts
- quality-review parse contracts

### 3. Golden Parity Tests

Required areas:

- fixed sample v1 vs v2 pointwise ordering comparison
- fixed sample v1 vs v2 top-N overlap
- fixed sample v1 vs v2 cutline decisions
- fixed sample output artifact schema comparison

### 4. Scale Tests

Required areas:

- 15k synthetic ingest test
- checkpoint/resume under mid-run interruption
- deterministic replay test with fixed seed
- duplicate-name stress test
- sparse-profile and missing-enrichment stress test

## Definition Of Done

The ranking rewrite is not done until all of the following are true:

- all ranking functionality in scope is implemented in v2
- all existing ranking-relevant tests are green or deliberately retired with replacements
- new unit, contract, golden, and scale tests are green
- a fixed sample dataset shows acceptable parity with v1
- 15k synthetic run completes with checkpoint/resume and no identity collisions
- CLI remains thin and orchestration does not leak back into engine modules

## Suggested Milestones

### Milestone 0: Baseline And Oracle

- baseline sample dataset
- current-output capture
- test baseline clarified

### Milestone 1: Foundation

- domain models
- resolved config
- checkpoint schemas

### Milestone 2: Ingest And Enrichment

- ingest rewrite
- shared enricher contracts
- deterministic applicant assembly

### Milestone 3: Broad Ranking

- profile rendering
- pointwise scoring
- cache and replay

### Milestone 4: Pairwise Ranking

- Swiss rewrite
- BT rewrite
- uncertainty and stop logic

### Milestone 5: Final Decisioning

- combine
- borderline
- quality review
- export

### Milestone 6: Long-Run Operability

- checkpoint/resume hardening
- metrics and cost tracking
- scale tests

### Milestone 7: Incremental

- incremental scoring and decision deltas

## Recommended Build Order

Execute in this order:

1. baseline oracle
2. canonical model and config
3. ingest and normalization
4. enrichment
5. profile rendering
6. pointwise scoring
7. pairwise / Swiss / BT
8. combine and borderline
9. quality and export
10. runtime hardening
11. incremental mode

## Immediate Next Tasks

If work starts now, do these first:

- [ ] create the parity sample dataset and save v1 outputs
- [ ] fix or lock down the current two failing tests in `tests/test_config.py` and `tests/test_profile.py`
- [ ] define the v2 applicant/domain models
- [ ] define `ResolvedConfig`
- [ ] create the v2 package skeleton under `src/cv_rank_v2/`

That is the shortest path to a rewrite that stays behaviorally anchored instead of drifting into a new product.
