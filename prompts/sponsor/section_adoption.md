You are writing one sponsor-facing markdown section: `## Sponsor Tech Adoption`.

Use only facts in `CLAIMS_JSON`.

This section is a core chapter in one final sponsor document.
Use it to explain not just usage volume, but usage quality, stack patterns, and which tools appear in stronger projects.

Hard constraints:
- No methodology/process language.
- No unsupported endpoint/API/tool usage claims.
- No confidence/source suffixes or metadata labels.
- No tables.
- Keep sponsor-facing, concrete, and technical.
- Use plain English; avoid jargon like `sponsor-adjacent`, `activation base`, `cohort`, `inventory channel`, `story anchor`.
- Do not include tautology/placeholder bullets such as:
  - `0/N` multi-tool usage lines with no positive comparison.
  - `N/N external base URL` lines.
  - `single channel inventory` phrasing.
- Every bullet must contain: metric + interpretation + sponsor relevance in one sentence.

Output format:
- Return markdown only.
- Start with `## Sponsor Tech Adoption`.
- Use this structure when claims exist:
  - `### Submitter-Level Adoption`
  - `### Team/Project-Level Adoption`
  - `### Placement + Co-Usage`
- Add `### Model + Technical Depth` only when model/SDK evidence claims are strong enough to be sponsor-positive.
- Use only subsections with real supporting claims; omit empty/weak subsections.
- Write 4-8 bullets total. If strong claims are sparse, keep it short instead of filling with weak bullets.
- Include exact numerators/denominators and percentages where available.

Section intent:
- Analyze sponsor-tool usage quality and depth, not just list counts.
- Prefer sentences like "tool X was common but tool Y overperformed on outcomes" over flat adoption bullets.
