# Sponsor CLI Quality Plan

## Objective
Build a quality-first CLI assistant workflow for sponsor packets where enrichment is mandatory, claims are reviewed before writing, and sponsor-facing narrative stays defensible and actionable.

## Design Principles
1. Deterministic metrics first, narrative second.
2. Mandatory GitHub enrichment for sponsor mode (no fallback).
3. Hard claim lint + reviewer gates before LLM writing.
4. Human approval gate before final publish.
5. Separate sponsor mode from internal diagnostics mode.

## Target CLI
`cvsponsor run --event "<event>" --run-id <id> --mode sponsor|internal`

## Pipeline Stages
1. `preflight`
2. `extract`
3. `enrich_github` (required for sponsor mode)
4. `enrich_profiles`
5. `metrics`
6. `claim_lint`
7. `claim_curate`
8. `narrative_draft`
9. `narrative_review`
10. `headline_bank`
11. `publish`
12. `audit`

## Stage Contracts

### 1) preflight
Input:
- `.env`

Checks:
- `OPENAI_API_KEY` required.
- `GITHUB_TOKEN` required in sponsor mode.
- Required source files exist.

Output:
- `outputs/<slug>/PRECHECK.json`

Fail conditions:
- Missing required env vars.
- Missing run/event artifacts.

### 2) extract
Input:
- canonical event exports / current deterministic source script outputs

Output:
- canonical people/projects/submissions tables (csv/parquet)

Notes:
- Keep exact row IDs stable for lineage.

### 3) enrich_github (mandatory sponsor mode)
Input:
- people with github handles/urls
- `GITHUB_TOKEN`

Output:
- enriched GitHub features table with freshness timestamp

Hard rule:
- Sponsor mode must fail if this stage cannot complete above quality threshold.

Suggested quality thresholds:
- >= 90% checked-in GitHub enrichment coverage OR explicit user override.
- API error rate below configured ceiling.

### 4) enrich_profiles
Input:
- Supabase/known profile fields

Output:
- normalized role/company/experience/social fields

### 5) metrics
Input:
- extracted + enriched tables

Output:
- `sponsor_metrics_long.csv`
- `sponsor_leads_internal.csv`
- `project_tech_evidence.csv`

Rules:
- deterministic only
- every metric must carry numerator/denominator/cohort

### 6) claim_lint
Input:
- `sponsor_metrics_long.csv`

Checks:
- denominator cohort consistency
- ratio recomputation
- tiny denominator warnings
- duplicate or conflicting claims
- unsupported evidence types for endpoint/tool assertions

Output:
- `CLAIM_LINT_REPORT.json`

### 7) claim_curate
Input:
- linted claims

Output:
- `CLAIMS_APPROVED.csv`

Rules:
- sponsor mode excludes deficit-heavy internal QA metrics
- strict section-level required claims list
- removes technically-true-but-sponsor-useless lines

### 8) narrative_draft
Input:
- `CLAIMS_APPROVED.csv`
- section prompts

Model:
- `gpt-5.2`

Output:
- `NARRATIVE_DRAFT.md`

### 9) narrative_review
Input:
- `NARRATIVE_DRAFT.md`
- approved claims

Model:
- `gpt-5.4`

Rules:
- each sentence maps to claim IDs
- remove repetitive lines
- enforce sponsor-facing tone

Output:
- `NARRATIVE_REVIEWED.md`

### 10) headline_bank
Input:
- approved claims + enriched aggregates

Output:
- `SPONSOR_HEADLINE_BANK.md`

Rules:
- include 10-15 clickable, evidence-backed options
- keep one MANGO/FAANG-style line when supported
- avoid opaque vanity stats unless framed with sponsor relevance

### 11) publish
Output:
- `SPONSOR_PACKET_ONE.md`
- copied csv artifacts

### 12) audit
Output:
- `RUN_AUDIT.md`

Must include:
- model IDs used
- blocked claims counts by reason
- enrichment coverage numbers
- final warnings

## Script Plan (what to build)
1. `scripts/cvsponsor_preflight.py`
2. `scripts/cvsponsor_extract.py`
3. `scripts/cvsponsor_enrich_github.py`
4. `scripts/cvsponsor_enrich_profiles.py`
5. `scripts/cvsponsor_build_metrics.py`
6. `scripts/cvsponsor_claim_lint.py`
7. `scripts/cvsponsor_claim_curate.py`
8. `scripts/cvsponsor_narrative_draft.py`
9. `scripts/cvsponsor_narrative_review.py`
10. `scripts/cvsponsor_headline_bank.py`
11. `scripts/cvsponsor_publish.py`
12. `scripts/cvsponsor_run.py`

## Trigger Rules
- Trigger `enrich_github` whenever sponsor packet is requested.
- If `GITHUB_TOKEN` missing in sponsor mode: fail immediately.
- Trigger `narrative_review` every run in sponsor mode.
- Trigger human approval checkpoint after `claim_curate` for final sends.

## Future Judging-System Integration (architecture now, integration later)
Add a pluggable stage:
- `cvsponsor_enrich_judging_extracts.py`

Expected input contract:
- per-project extract objects:
  - project_id
  - repo_url
  - short_description
  - detailed_description
  - extracted_stack
  - extracted_endpoints
  - extracted_models
  - evidence_strength

Expected output contract:
- project-level judging features table for ranking and storyline selection.

Integration point:
- stage runs between `extract` and `metrics`
- merged into `project_tech_evidence.csv` and claim scoring features

## Prompt Architecture
Per section prompts should explicitly include:
1. section objective
2. forbidden language list
3. required evidence discipline
4. expected output shape
5. sponsor tone guard

Reviewer prompt should enforce:
1. no denominator mismatch language
2. no unsupported technical claims
3. no deficit-heavy framing unless internal mode
4. no repeated stats across sections

## Quality Gates For Release
Release only if all true:
1. preflight passes
2. sponsor mode enrichment threshold passes
3. lint has zero hard errors
4. reviewer blocked/cleaned disallowed claims
5. packet passes sponsor tone grep checks
6. human reviewer sign-off for final sponsor send
