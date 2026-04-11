You are writing one sponsor-facing markdown section: `## Recommendations (next 30/60 days)`.

Use only facts in `CLAIMS_JSON`.

This section is optional inside the one-document packet.
If the evidence is weak or the recommendations would just restate obvious next steps, keep this section very short or omit weak bullets.

Hard constraints:
- No methodology/how-calculated/process language.
- No unsupported recommendations; every recommendation must be grounded in at least one provided claim.
- No confidence/source suffixes or metadata labels in prose.
- No tables, no implementation runbooks, no automation notes.
- Avoid repeated recommendations with different wording.

Output format:
- Return markdown only.
- Start with `## Recommendations (next 30/60 days)`.
- Write 4-6 bullets.
- Each bullet must be action-oriented and include one concrete numeric anchor when available.

Section intent:
- Turn validated claim signals into practical, sponsor-ready actions for the next 30/60 days.
- Only keep recommendations that are clearly earned by the analysis already established in the document.
