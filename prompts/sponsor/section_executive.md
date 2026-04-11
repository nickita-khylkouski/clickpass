You are writing one sponsor-facing markdown section: `## Executive Summary`.

Use only facts in `CLAIMS_JSON`.

This section will appear inside one final sponsor document.
It sets the main event story, so avoid spending bullets on details that belong later.

Hard constraints:
- Do not mention methodology, extraction process, formulas, confidence labels, source labels, or metadata keys.
- Do not invent numbers, interpretations, or external context.
- Keep tone sponsor-ready and outcome-forward.
- Avoid deficit-heavy framing unless unavoidable.
- Do not include coverage-only or profile-completeness bullets.
- Do not include "Top-25/Top-50 as % of all applicants" bullets.
- Do not include tautology bullets (`N/N generic capture`, `0/N story anchor`, `0/N multi-tool`) in summary.
- Use plain, direct language (no internal analytics jargon).
- If a claim references "project inventory submissions", rewrite it as "documented project write-ups".

Output format:
- Return markdown only.
- Start with `## Executive Summary`.
- Write 6-9 bullets.
- Every bullet must include at least one concrete number.
- Include these bullet types when claims exist:
  1) end-to-end event funnel snapshot,
  2) participation/build intensity snapshot,
  3) sponsor adoption snapshot,
  4) hiring/readiness snapshot.

Section intent:
- Give sponsors a high-density, at-a-glance summary with clear business relevance.
- Establish the 3-5 findings the rest of the document will unpack.
