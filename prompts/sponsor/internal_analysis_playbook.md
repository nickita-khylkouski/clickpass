# Sponsor Packet Analysis Playbook (Internal)

Use this as the working prompt/process for Claude/Codex when building sponsor packets from enriched hackathon data.

The end goal is one final sponsor document.
Intermediate CSVs and analysis tables are only there to help the agent think.

## 1) Frame The Objective
- Identify audience: sponsor-facing external packet vs internal CV planning.
- Lock scope: event name/date, cohort definitions, and allowed denominator cohorts.
- Decide tone: factual, business-readable, analysis-first (not advisory).
- Lock the deliverable: `SPONSOR_PACKET_ONE.md` is the product, not the CSV bundle.

## 2) Validate Inputs First
- Confirm enriched people dataset loaded for the target event.
- Confirm submission/project dataset loaded for the same event.
- Confirm placement/judging records exist and match event IDs.
- Fail fast on mixed-event data.

## 3) Build A Claims Table Before Writing
- Produce normalized claims with: metric, segment, numerator, denominator, rate, cohort.
- Keep deterministic calculations in code; do not freehand math in prose.
- Tag low-sample claims so they can be excluded from sponsor narrative if needed.
- Keep working CSVs and analysis tables available, but assume the sponsor will only read the final markdown packet.

## 4) Run Brainstorming In Two Passes
- Pass A (inventory): list all possible story angles by section (Funnel, Hiring, Sales/BD, Research, Marketing, Adoption).
- Pass B (selection): keep only claims that are decision-relevant, externally legible, and numerically stable.
- In Pass B, explicitly choose one main event story and 4-7 supporting stories. Everything else is supporting evidence only.

## 5) Convert Stats Into Analysis (Not Advice)
- For each section, answer:
  - What is largest by volume?
  - What converts better vs worse?
  - Which segments over- or under-index?
  - What does this imply about sponsor-relevant audience quality?
- Avoid lines that only restate data collection coverage unless strategically important.
- Prefer sentences that compare:
  - segment vs segment,
  - tool vs baseline,
  - theme vs outcome,
  - observed evidence vs self-report.

## 6) Write The Packet
- Start with concise executive bullets that anchor funnel + audience quality + adoption.
- In each section, prefer comparisons over isolated percentages.
- Keep methodology out of the sponsor packet body.
- Treat sections as chapters in one memo, not standalone exports.
- Avoid repeating the same fact in multiple sections unless the interpretation is different and necessary.

## 7) Headline Bank Pass
- Generate 10-15 big-number titles with concrete numbers.
- Make headlines distinct (no near-duplicates).
- Keep "Numbers" and "Why this matters" plain-English and short.

## 8) QA Gate Before Delivery
- Recompute all ratios from numerator/denominator.
- Remove weak or low-signal claims from narrative.
- Check no forbidden phrases remain (methodology, confidence/source suffixes, sponsor implication boilerplate).
- Verify packet reads as one coherent story, not stitched sections.
- Verify each retained bullet answers: what happened, compared to what, why it matters.

## 9) Deliverables
- `SPONSOR_PACKET_ONE.md` (readable sponsor narrative + headline bank)
- `sponsor_metrics_long.csv` (full metric traceability)
- `sponsor_leads_internal.csv` (internal-only)
- `project_tech_evidence.csv` (technical evidence)
- `RUN_AUDIT.md` (pipeline trace)

The first item is the real deliverable.
The rest are support artifacts.
