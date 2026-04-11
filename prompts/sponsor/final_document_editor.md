You are rewriting a sponsor packet draft into one final sponsor-facing markdown document.

Use only facts in `CLAIMS_JSON` and `DRAFT_MARKDOWN`.

This is the final memo the sponsor reads.
The CSVs and intermediate sections are not the product.

Hard constraints:
- The document must read like one memo, not stitched section exports.
- Keep all numbers consistent with `CLAIMS_JSON`.
- Do not add new claims.
- Remove repeated metrics across sections unless the interpretation is materially different.
- Keep the document concise enough to fit under 5 pages.
- Target `MAX_WORDS`; do not exceed it unless absolutely necessary.
- Prefer fewer, stronger findings over broad coverage.
- No methodology/process language.
- No confidence/source/metadata labels.
- No filler recommendations.
- Do not include weak sections just for completeness.
- If a section has weak claims, compress it to 1-2 bullets or omit it.

Writing standard:
- Every good bullet or paragraph must contain:
  1. a metric,
  2. a comparison or interpretation,
  3. why it matters to the sponsor.

Preferred document order:
1. `## Executive Summary`
2. `## Sponsor Tech Adoption`
3. `## Hiring Insights`
4. `## Sales/BD Insights`
5. `## Marketing Insights`
6. `## Market Research Insights`
7. `## Funnel`

Allowed compression moves:
- Merge repeated points across sections.
- Drop weak subsections.
- Convert long bullet lists into shorter, denser bullets.
- Keep only the strongest 2-4 findings per section.

Output:
- Return markdown only.
- Return one final document body.
- Do not include title metadata or file lists.
