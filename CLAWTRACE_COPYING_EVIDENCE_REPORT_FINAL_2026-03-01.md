# Final Forensic Report: Clawtrace vs Crabwalk (Directionality + Copying Evidence)

Date: 2026-03-01
Prepared for: Hackathon integrity review

## Executive conclusion

Public evidence strongly supports this direction:
- `crabwalk` existed first and had mature releases before Feb 7.
- `clawtrace` appeared later and is highly overlapping.
- Early `clawtrace` matches specific `crabwalk` file states from Feb 19 (after Feb 7), weakening the reverse hypothesis.

This is strong evidence of substantial reuse/rebrand. Whether it is “wrong” in hackathon terms depends on event rules (originality window + disclosure requirements).

---

## Exhibit 1: Chronology (who came first)

- `crabwalk` history starts January 2026 (165 commits).
- `crabwalk` tags before Feb 7:
  - `v1.0.1` at `2026-01-26T16:56:05-05:00`
  - `v1.0.11` at `2026-02-05T14:08:13-05:00`
- `clawtrace` first public commit:
  - `2b063e0` at `2026-02-28T12:00:00-08:00`

Links:
- https://github.com/luccast/crabwalk
- https://github.com/luccast/crabwalk/tags
- https://github.com/dibbaa-code/clawtrace/commit/2b063e0bd428bdf5fdfb915b9fda6c771b6e0df5

---

## Exhibit 2: Similarity metrics (src ts/tsx)

- `clawtrace main` vs `crabwalk main`: `shared=48 identical=29 changed=19`
- `clawtrace @43c8d2e` vs `crabwalk main`: `shared=48 identical=41 changed=7`
- `clawtrace @43c8d2e` vs `crabwalk v1.0.11`: `shared=47 identical=38 changed=9`

Interpretation:
- Early `clawtrace` is more similar than later `clawtrace` (base import, then edits).

---

## Exhibit 3: Decisive reverse-hypothesis check (new)

Hypothesis tested:
- “Could crabwalk have copied from private clawtrace around Feb 7 instead?”

Result:
- Early `clawtrace` (`43c8d2e`) exactly matches three file states in `crabwalk` commit `ea99ca93` dated **2026-02-19T18:35:52Z**.
- That date is after Feb 7, and these states are in crabwalk public history.

Commit link:
- https://github.com/luccast/crabwalk/commit/ea99ca93fd36b4aa2991a0b1401974bb0e881c15

Files:
- `src/integrations/openclaw/device.ts`
- `src/integrations/trpc/router.ts`
- `src/routes/monitor/index.tsx`

Verification details:

```text
src/integrations/openclaw/device.ts
- clawtrace@43c8d2e hash == crabwalk@ea99ca93 hash
- file is MISSING in crabwalk@v1.0.11 (Feb 5)

src/integrations/trpc/router.ts
- clawtrace@43c8d2e hash == crabwalk@ea99ca93 hash
- differs from crabwalk@v1.0.11

src/routes/monitor/index.tsx
- clawtrace@43c8d2e hash == crabwalk@ea99ca93 hash
- differs from crabwalk@v1.0.11
```

Observed `v1.0.11 -> ea99ca93` file deltas in crabwalk:

```text
src/integrations/openclaw/device.ts: 1 file changed, 177 insertions(+)
src/integrations/trpc/router.ts: 1 file changed, 27 insertions(+), 3 deletions(-)
src/routes/monitor/index.tsx: 1 file changed, 81 insertions(+), 12 deletions(-)
```

Interpretation:
- Reverse-direction theory is less consistent with public evidence.
- Direction still points to `crabwalk -> clawtrace` reuse path.

---

## Exhibit 4: Rebrand-style code evidence

Representative CLI diff pattern (crabwalk -> clawtrace):

```diff
-# 🦀 Crabwalk CLI
+# 🦀 Clawtrace CLI

-CRABWALK_HOME="${CRABWALK_HOME:-$HOME/.crabwalk}"
+CLAWTRACE_HOME="${CLAWTRACE_HOME:-$HOME/.clawtrace}"

-echo "Usage: crabwalk <command> [options]"
+echo "Usage: clawtrace <command> [options]"

-https://api.github.com/repos/luccast/crabwalk/releases/latest
+https://api.github.com/repos/dibbaa-code/clawtrace/releases/latest
```

Interpretation:
- Consistent with adaptation/rebrand of an existing CLI and release wiring.

---

## Exhibit 5: License provenance

Both repositories carry MIT LICENSE naming the same copyright owner.

Links:
- Clawtrace LICENSE: https://github.com/dibbaa-code/clawtrace/blob/main/LICENSE
- Crabwalk LICENSE: https://github.com/luccast/crabwalk/blob/main/LICENSE

Shared text:

```text
MIT License

Copyright (c) 2026 Luciano Castillo Vega
```

Interpretation:
- Reuse may be legally allowed under MIT.
- Hackathon fairness/originality is a separate policy question.

---

## Exhibit 6: Early clawtrace high-volume import pattern

Within 30 minutes of first public commit (Feb 28, 12:00–12:30 PST):
- 10 commits
- 22,215 insertions total
- 15,559 non-lockfile insertions
- First commit alone: 11,064 insertions

Large early drops:
- `475289e` +2001
- `4211ca2` +2846
- `43c8d2e` +1275

Interpretation:
- Pattern is strongly consistent with importing/modifying a pre-existing codebase.

---

## Hackathon policy impact (practical reading)

Likely problematic if rules required:
- coding primarily during event,
- disclosure of pre-existing code,
- originality-based judging without major prior-code allowance.

Not automatically a copyright violation (MIT), but can still be a competition-rule violation if undisclosed.

---

## Final assessment

Most evidence-consistent explanation:
1. `crabwalk` existed and evolved publicly before Feb 7.
2. `clawtrace` was built by adapting/rebranding a substantial crabwalk base.
3. Additional later edits/customizations were made in clawtrace.

Recommended organizer wording:
- “Strong evidence of substantial prior code reuse from crabwalk; please evaluate compliance with originality and disclosure rules for this event.”

