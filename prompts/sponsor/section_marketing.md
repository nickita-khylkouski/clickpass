You are writing one sponsor-facing markdown section: `## Marketing Insights`.

Use only facts in `CLAIMS_JSON`.

This section appears inside one final sponsor document.
Assume Executive Summary already covered the broad event snapshot; focus here on which stories are actually usable externally.

Hard constraints:
- No methodology/process language.
- No invented campaign claims.
- No confidence/source/metadata terms.
- No tables.
- Keep sponsor-positive and specific.
- Do not include low-signal technical depth bullets when evidence rates are low.
- Do not write prescriptive recommendations (avoid "prioritize", "start with", "should").
- Use plain English; avoid jargon phrases (`sponsor-adjacent`, `activation base`, `inventory channel`, `anchor`).
- If a claim references "project inventory submissions", rewrite it as "documented project write-ups" in prose.
- Do not include zero-signal bullets (`0/N`) unless paired with a meaningful comparative insight.
- Do not include tautology bullets (`N/N` generic capture metrics).
- Do not include stand-alone volume bullets that just restate attendance/submission counts without deeper interpretation.

Output format:
- Return markdown only.
- Start with `## Marketing Insights`.
- Write 5-8 bullets.
- Prioritize:
  1) cross-sponsor story inventory,
  2) tool co-usage depth,
  3) amplifiable audience/segment hooks,
  4) content-ready angles with concrete counts and analytical interpretation.
- Every bullet should include a number and explain why that number matters for sponsor messaging.
- Prefer comparative bullets (segment A vs segment B, theme A vs theme B, tool depth vs baseline) over isolated single-metric statements.
- Use short direct sentences; avoid verbose filler.

Section intent:
- Turn metrics into clear post-event sponsor story angles and activation opportunities.
- Strong bullets explain what is marketable and why, not just how many projects existed.
