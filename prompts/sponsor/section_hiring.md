You are writing one sponsor-facing markdown section: `## Hiring Insights`.

Use only facts in `CLAIMS_JSON`.

This section is one chapter in a single sponsor document.
Do not reuse executive bullets unless you add hiring-specific interpretation.

Hard constraints:
- No methodology/process language.
- No unsupported quality judgments.
- No confidence/source/metadata terms.
- Avoid leading with deficits (`missing`, `blocker`, `gap`) unless unavoidable.
- If a caveat is needed, keep it to one short bullet.
- Do not include profile-completeness/coverage bullets (GitHub+LinkedIn coverage, language coverage, judging coverage).
- Do not include Top-25/Top-50 as a percentage of all applicants.

Output format:
- Return markdown only.
- Start with `## Hiring Insights`.
- Write 8-12 bullets.
- Prefer this structure:
  - hiring-ready pool and scale,
  - segment comparisons that show who builds faster vs who places better,
  - top technical/output cohorts by count,
  - practical ranking takeaways stated as analysis (not recommendations).
- Every bullet should carry at least one metric.

Section intent:
- Provide sponsors with a clear and actionable hiring-readout, not an internal QA report.
- Good bullets compare segments and explain candidate quality, not just list pool sizes.
