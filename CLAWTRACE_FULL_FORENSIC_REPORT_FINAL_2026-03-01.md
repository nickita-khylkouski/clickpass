# Clawtrace Full Forensic Report

Date: 2026-03-01
Prepared for: Hackathon integrity review
Scope: Public OSINT + repository/code comparison

## Executive summary

This report finds strong evidence that `dibbaa-code/clawtrace` is substantially derived from `luccast/crabwalk`, with additional features and rebranding layered on top.

Most evidence-consistent direction:
- `crabwalk` existed first and had public release history before Feb 7.
- `clawtrace` appeared later with a large initial code drop and high structural/code overlap.
- `clawtrace` also added meaningful incremental work (threat analysis, Discord alerting pipeline, setup script, UI/theme changes).

Conclusion for hackathon context:
- This is likely a major adaptation/fork, not a fully from-scratch build during event window.
- Whether that is disallowed depends on specific hackathon rules (originality + disclosure clauses).

---

## 1. Repositories and key links

- Clawtrace: https://github.com/dibbaa-code/clawtrace
- Crabwalk: https://github.com/luccast/crabwalk
- Clawtrace first commit: https://github.com/dibbaa-code/clawtrace/commit/2b063e0bd428bdf5fdfb915b9fda6c771b6e0df5
- Crabwalk tags: https://github.com/luccast/crabwalk/tags
- Crabwalk Feb 19 commit: https://github.com/luccast/crabwalk/commit/ea99ca93fd36b4aa2991a0b1401974bb0e881c15
- Clawtrace LICENSE: https://github.com/dibbaa-code/clawtrace/blob/main/LICENSE
- Crabwalk LICENSE: https://github.com/luccast/crabwalk/blob/main/LICENSE

---

## 2. Timeline and directionality evidence

### 2.1 Crabwalk existed first

Public crabwalk history shows:
- Early commits in January 2026
- 165 total commits in history
- Tags before Feb 7:
  - `v1.0.1` at `2026-01-26T16:56:05-05:00`
  - `v1.0.11` at `2026-02-05T14:08:13-05:00`

### 2.2 Clawtrace appears later

Clawtrace public history shows:
- First commit: `2b063e0` at `2026-02-28T12:00:00-08:00`
- First 30 minutes include 10 rapid commits and very large insertions

Interpretation:
- Public chronology strongly supports `crabwalk -> clawtrace`, not reverse.

---

## 3. Similarity metrics (src ts/tsx)

Comparison (`clawtrace@c925655` vs `crabwalk@main`):
- Shared files: `48`
- Identical files: `29`
- Changed files: `19`

Early snapshot comparison (`clawtrace@43c8d2e`):
- vs `crabwalk@main`: `shared=48 identical=41 changed=7`
- vs `crabwalk@v1.0.11`: `shared=47 identical=38 changed=9`

Interpretation:
- Early clawtrace is even closer to crabwalk than current clawtrace is, consistent with base reuse followed by edits.

---

## 4. Decisive reverse-hypothesis check (Feb 7 theory)

Hypothesis tested:
- “Could crabwalk have copied from private clawtrace around Feb 7?”

Result:
- `clawtrace@43c8d2e` exactly matches `crabwalk@ea99ca93` (2026-02-19) for these files:
  - `src/integrations/openclaw/device.ts`
  - `src/integrations/trpc/router.ts`
  - `src/routes/monitor/index.tsx`

Additional key point:
- `src/integrations/openclaw/device.ts` is missing in `crabwalk@v1.0.11` (Feb 5) and appears in later crabwalk history.

Interpretation:
- Public evidence makes reverse-direction theory less consistent.

---

## 5. What clawtrace added on top of crabwalk

### 5.1 Net-new source files in clawtrace (not in crabwalk main)

- `src/components/effects/PixelWaves.tsx` (`114` lines)
- `src/lib/threat-analyzer.ts` (`106` lines)
- `src/lib/alert-service.ts` (`54` lines)
- `src/components/effects/CrabTrails.tsx` (`43` lines)
- `src/components/effects/SandGradient.tsx` (`19` lines)

Net-new `src` lines from these files: `336`

### 5.2 Script/tooling additions outside src

- `setup.sh` (`163` lines)
- `bin/clawtrace` (`297` lines)

### 5.3 Largest modifications in shared files

Top changed shared files (added+deleted line estimates):
- `src/routes/workspace/index.tsx` (`280` touched)
- `src/routes/index.tsx` (`165` touched)
- `src/components/monitor/ActionGraph.tsx` (`137` touched)
- `src/integrations/trpc/router.ts` (`117` touched)
- `src/components/monitor/ExecNode.tsx` (`98` touched)
- `src/components/monitor/SettingsPanel.tsx` (`98` touched)
- `src/components/monitor/ActionNode.tsx` (`96` touched)
- `src/routes/monitor/index.tsx` (`83` touched)

Across all 19 changed shared `src` files:
- Added: `702`
- Deleted: `576`
- Total touched: `1,278`

### 5.4 Aggregate “work on top” estimate

Using this comparison method:
- `src` work on top = `336` (net-new files) + `1,278` (touched shared files) = `1,614`
- Including setup/CLI scripts: `1,614 + 163 + 297 = 2,074`

Note: “touched lines” is a change-volume metric, not guaranteed net-new unique logic.

---

## 6. README, branding, and asset evidence

### 6.1 README status

README is not byte-identical, but heavily derived.
- `crabwalk` README lines: `165`
- `clawtrace` README lines: `121`
- Diff changed lines: `60` (`+16 / -44`)

Pattern:
- Same overall structure/sections in many places
- Rebranding substitutions (`Crabwalk` -> `Clawtrace`, repo/image links swapped)

### 6.2 Rebrand-style diff behavior

Representative behavior in CLI/docs:
- command names changed (`crabwalk` -> `clawtrace`)
- home paths changed (`.crabwalk` -> `.clawtrace`)
- release/update URLs changed from `luccast/crabwalk` to `dibbaa-code/clawtrace`

Interpretation:
- Strongly consistent with adaptation/rebrand workflow.

---

## 7. License provenance

Both projects include MIT license naming same copyright holder:

```text
MIT License

Copyright (c) 2026 Luciano Castillo Vega
```

Implication:
- Code reuse may be legally allowed under MIT.
- Hackathon originality/disclosure compliance is a separate issue.

---

## 8. High-velocity initial clawtrace import pattern

In first 30 minutes after first commit (Feb 28, 12:00–12:30 PST):
- 10 commits
- 22,215 additions total
- 15,559 additions excluding lockfile
- first commit alone: 11,064 additions

Interpretation:
- Pattern is consistent with importing and adapting an existing codebase rapidly.

---

## 9. Private contribution context

Public contribution calendar signals around Feb 7:
- `srilaasya`: 7 contributions on Feb 7
- `dibbaa-code`: 10 contributions on Feb 7

Interpretation:
- Suggests private activity around date in question.
- Does not prove which private repo those contributions belong to.

---

## 10. Counterargument and rebuttal

### 10.1 Strongest counterargument (defense side)

- Project uses MIT-licensed upstream; reuse is lawful.
- Clawtrace added meaningful functionality:
  - Threat analysis module
  - Discord alert service
  - Event/trace alert wiring
  - Setup script and CLI adjustments
  - UI/theme/effects and workflow refinements
- Therefore this can be framed as a legitimate fork/customization project.

### 10.2 Rebuttal (integrity/originality side)

- Legal permissibility != hackathon originality compliance.
- Evidence indicates substantial pre-existing base reuse.
- Timeline and similarity argue core architecture/code was not mostly created during event window.
- If rules required disclosure of prior code or event-time coding, this is likely non-compliant unless clearly disclosed/approved.

---

## 11. Final assessment language for organizers

Recommended neutral wording:
- “There is strong evidence that this project substantially reuses the pre-existing crabwalk codebase and was adapted/rebranded, with additional new features layered on top. Please evaluate this against originality and disclosure rules for the event.”

---

## 12. Evidence artifacts generated locally

- `CLAWTRACE_COPYING_EVIDENCE_REPORT_FINAL_2026-03-01.md`
- `CLAWTRACE_COPYING_EVIDENCE_REPORT_2026-03-01.md`
- `CLAWTRACE_FORENSIC_REPORT_V2_2026-03-01.md`
- repository backups:
  - `/Users/nickita/.superset/worktrees/start/sigma/clawtrace_backup_2b063e0`
  - `/Users/nickita/.superset/worktrees/start/sigma/crabwalk_osint_20260301`

