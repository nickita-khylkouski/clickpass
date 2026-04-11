# Sponsor One-Document Workflow

This workflow is for building one final sponsor document per event.

Intermediate CSVs, evidence files, and audit tables are working memory for the agent. They are not the deliverable.

## End Goal

Produce one document:

- `SPONSOR_PACKET_ONE.md`

That document should read like a single narrative, not a stitched export of section dumps.
Target length: under 5 pages, roughly 1,000-1,400 words.

## Working Principle

Use structured data to think.
Use the final document to persuade.

That means:

- CSVs are for checking numerators, denominators, lifts, and edge cases.
- Analysis tables are for finding the strongest comparisons.
- The packet only keeps the strongest, most sponsor-meaningful findings.

## Workflow

### Stage 1: Collect and Validate Data

Required inputs:

- enriched people/event data
- project/submission data
- judging and placement data
- sponsor tool evidence

Rules:

- never mix event snapshots across runs
- fail fast on mismatched event IDs or applicant counts
- require GitHub enrichment for quality sponsor work

### Stage 2: Build the Analysis Workspace

Run:

```bash
cd /Users/nickita/cv-rank && \
PYTHONPATH=src uv run cv-rank sponsor-analyze \
  --run-id <RUN_ID> \
  --reviewer-mode strict
```

Core analyst files:

- `ANALYSIS_SUMMARY.md`
- `ANALYSIS_TASKS.md`
- `analysis_claim_review.csv`
- `analysis_segment_funnel.csv`
- `analysis_tool_summary.csv`
- `analysis_theme_summary.csv`
- `analysis_audience_summary.csv`
- `analysis_tool_outcomes.csv`
- `analysis_tool_reconciliation.csv`
- `analysis_top_projects.csv`
- `analysis_big_numbers.csv`

### Stage 3: Select the Story Before Writing

Before drafting, answer:

1. What is the main event-level story?
2. Which audience segments outperformed or underperformed?
3. Which sponsor tools were merely frequent, and which correlated with stronger outcomes?
4. Which projects prove the strongest sponsor story?
5. Which numbers are big but still meaningful?

If a metric does not help answer one of those, it probably does not belong in the packet.

### Stage 4: Draft Section Inputs

The final document may still use section blocks, but each section is only a chapter in one narrative.

Required section behavior:

- `Executive Summary` sets the story.
- `Sponsor Tech Adoption` shows real usage depth and evidence-backed quality.
- `Hiring Insights` explains who looks strongest and why.
- `Sales/BD Insights` explains where operator/buyer density sits and how it converts.
- `Marketing Insights` explains what stories are externally usable.
- `Market Research Insights` explains audience and project composition only when it sharpens the sponsor read.
- `Funnel` is supporting context, not the lead story, unless the funnel is the event's main result.

### Stage 5: Assemble One Coherent Document

The final document must:

- have one opening frame
- avoid repeating the same metric in multiple sections
- move from most important findings to supporting detail
- read like one memo, not multiple disconnected analyses

Recommended order:

1. Executive Summary
2. Sponsor Tech Adoption
3. Hiring Insights
4. Sales/BD Insights
5. Marketing Insights
6. Market Research Insights
7. Funnel
8. Sponsor Headline Options

Only include Recommendations if they are explicitly requested and numerically grounded.

### Stage 6: Final QA

Before shipping the packet:

1. Recompute every suspect ratio from CSVs.
2. Remove duplicate bullets across sections.
3. Remove weak bullets that only restate counts.
4. Keep only comparisons, rankings, lifts, or unusually strong absolute numbers.
5. Make sure the packet could be read top to bottom without needing the CSVs.

## Writing Standard

Good packet sentence:

- states the metric
- compares it to something relevant
- explains why it matters to a sponsor

Bad packet sentence:

- only restates a count
- explains methodology
- hides behind jargon
- gives a recommendation without evidence

## Example Pattern

Good:

- Students generated more project volume than founders, but founders converted more of that effort into placements, which makes students the better community story and founders the better follow-up pool.

Bad:

- Students were 79/101 and founders were 71/116.
