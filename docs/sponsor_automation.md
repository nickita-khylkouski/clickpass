# Sponsor Automation

This runbook covers the sponsor delivery automation centered on:

- `scripts/build_sponsor_delivery_bundle.py`
- `sponsor_report_pii_safe.md`
- `sponsor_metrics_long.csv` / `sponsor_metrics_long_full.csv`
- `sponsor_leads_internal.csv`
- `project_tech_evidence.csv`

## Setup

1. Install dependencies:
   - `cd /Users/nickita/cv-rank`
   - `uv sync`
2. Confirm input artifacts exist:
   - `marketing_master.csv`
   - latest enriched JSON (configured in script constants)
   - `subagent_outputs/sectionC_agent5_q18.md`

## Required Environment Variables

The bundle script itself is deterministic and does not call OpenAI directly.

For the full sponsor automation workflow (enrichment + optional narrative synthesis), use:

| Variable | Required | Why |
|---|---|---|
| `SUPABASE_URL` | Yes (for enrichment refresh) | Pull latest event/applicant/submission context |
| `SUPABASE_KEY` | Yes (for enrichment refresh) | Auth for Supabase reads |
| `OPENAI_API_KEY` | Yes (for narrative automation steps only) | `responses.create` synthesis/QA jobs |
| `OPENAI_MODEL` | Recommended | Pin model used by automation wrappers |
| `GITHUB_TOKEN` | Optional | Enable GitHub enrichment where needed |

## Command Examples

Refresh enriched source data:

```bash
cd /Users/nickita/cv-rank
uv run cv-rank run --event "Gemini 3 NYC Hackathon" --accept 1 --enrich-only --supabase --no-github
```

Generate sponsor delivery bundle:

```bash
cd /Users/nickita/cv-rank
uv run python scripts/build_sponsor_delivery_bundle.py
```

Run offline automation contract tests:

```bash
cd /Users/nickita/cv-rank
uv run pytest -q tests/test_sponsor_automation.py
```

## Troubleshooting

- `ModuleNotFoundError: cv_rank`: run tests via `uv run pytest ...` (not bare `pytest`) so package paths resolve.
- Missing input file errors: verify script constants point to existing files for the active run.
- Denominator cohort issues: allowed cohorts are `all_applicants`, `approved`, `checked_in`, `submitters`, `placed`.
- Schema drift in `sponsor_metrics_long.csv`: enforce exact columns in this order:
  `section, metric_name, segment, value, numerator, denominator, denominator_cohort, coverage_pct, source_table_or_file, calculation_note, confidence`.
- Packet/order regressions: validate `sponsor_report_pii_safe.md` section order with `tests/test_sponsor_automation.py`.
