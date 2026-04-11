You are writing one sponsor-facing markdown section: `## Market Research Insights`.

Use only facts in `CLAIMS_JSON`.

This section is background and sharpening context for one final sponsor document.
Only include audience/project composition details that improve sponsor understanding of the event.

Hard constraints:
- No methodology/process language.
- No unsupported causal claims.
- No confidence/source/metadata terms.
- No tables.
- Do not read like a raw dump of stats; include comparative interpretation in plain English.
- Do not emit one-line stat dumps without meaning.
- Every bullet must answer "so what?" for a sponsor audience.
- Use plain language and avoid internal taxonomy jargon where possible.
- Avoid filler attendance-volume bullets unless tied to a segment-level contrast or outcome difference.

Output format:
- Return markdown only.
- Start with `## Market Research Insights`.
- Use this structure when claims allow. Only include subsections that have supporting claims:
  - `### 1) Who Showed Up`
  - `### 2) Company + Geo Profile`
  - `### 3) What Builders Used`
  - `### 4) What Got Built + What Won`
  - `### 5) Underrepresented But Strong Segments`
- Write 10-16 bullets total.
- Prefer concrete counts and percentages in each subsection.
- Include at least one comparative insight per included subsection (for example top vs next-largest segment or higher vs lower outcome group).
- Do not emit placeholder bullets like "not available".
- Do not repeat the same metric in multiple bullets or subsections.
- If only weak/partial claims exist for a subsection, omit that subsection instead of forcing filler.

Section intent:
- Give sponsors a deeper view of audience composition, stack behavior, and project themes.
- Do not dump demographics for completeness; use them only when they clarify who showed up, what they built, or why certain outcomes clustered.
