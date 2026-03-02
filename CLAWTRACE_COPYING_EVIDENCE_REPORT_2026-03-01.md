# Clawtrace vs Crabwalk Forensic Evidence Report

Date: 2026-03-01 (America/Los_Angeles)
Prepared for: Hackathon integrity review
Scope: Public OSINT + local forensic comparison of public repositories

## 1. Executive summary

This report finds strong technical evidence that `dibbaa-code/clawtrace` is largely a rebrand/adaptation of `luccast/crabwalk`, not a project built fully from scratch at hackathon start.

Core findings:
- `crabwalk` predates `clawtrace` by weeks (first commits in January 2026; tagged releases before February 7).
- `clawtrace` appears publicly on February 28, 2026 with a very large immediate code drop.
- Cross-repo overlap is high, including exact file-hash matches and rename-style diffs.
- Both repos contain the same MIT copyright holder: **Luciano Castillo Vega**.

This does not by itself prove rule violation unless hackathon rules required event-time original coding or mandatory disclosure. But the evidence is strongly consistent with substantial pre-existing code reuse.

---

## 2. Main claim and confidence

Claim: `clawtrace` substantially reuses `crabwalk` code and branding was changed.

Confidence:
- High: repository chronology and similarity metrics.
- High: directionality (`crabwalk` existed first).
- Medium: whether this violated a specific hackathon’s rules (depends on official rule text and disclosure requirements).

---

## 3. Timeline evidence (who existed first)

### 3.1 Crabwalk existed first

Repository: https://github.com/luccast/crabwalk

Observed chronology:
- First commit history starts: **2026-01-25**
- Tags present before Feb 7:
  - `v1.0.1` at `2026-01-26T16:56:05-05:00`
  - `v1.0.11` at `2026-02-05T14:08:13-05:00`

### 3.2 Clawtrace appears later

Repository: https://github.com/dibbaa-code/clawtrace

Observed chronology:
- First public commit: `2b063e0` at `2026-02-28T12:00:00-08:00`
  - https://github.com/dibbaa-code/clawtrace/commit/2b063e0bd428bdf5fdfb915b9fda6c771b6e0df5
- Repo API creation timestamp:
  - `2026-02-28T21:29:56Z`

Interpretation:
- Publicly observable chronology supports `crabwalk` -> `clawtrace`, not the reverse.

---

## 4. Similarity metrics (quantitative overlap)

Compared `src/*.ts` + `src/*.tsx` snapshots:

1. `clawtrace main` vs `crabwalk main`
- `shared=48`
- `identical=29`
- `changed=19`

2. Early `clawtrace` (`43c8d2e`) vs `crabwalk main`
- `shared=48`
- `identical=41`
- `changed=7`

3. Early `clawtrace` (`43c8d2e`) vs `crabwalk v1.0.11`
- `shared=47`
- `identical=38`
- `changed=9`

Interpretation:
- Early `clawtrace` is *more similar* to `crabwalk` than later `clawtrace` is, which matches a base-import then customization pattern.

---

## 5. File-level overlap evidence

Cross-repo tree comparison showed:
- Common file paths: `94`
- Exact SHA-256 file matches: `60`

Example identical source files across repos include:
- `src/components/ani/CrabAnimations.tsx`
- `src/components/workspace/FileEditor.tsx`
- `src/integrations/openclaw/parser.ts`
- `src/lib/workspace-fs.ts`
- `src/router.tsx`

Interpretation:
- This is substantial direct reuse, not just idea-level similarity.

---

## 6. Rename/rebrand diff evidence (direct code block)

A representative diff of CLI script (`crabwalk` vs `clawtrace`) shows mostly brand/repo renaming:

```diff
-# 🦀 Crabwalk CLI
+# 🦀 Clawtrace CLI

-CRABWALK_HOME="${CRABWALK_HOME:-$HOME/.crabwalk}"
+CLAWTRACE_HOME="${CLAWTRACE_HOME:-$HOME/.clawtrace}"

-echo "Usage: crabwalk <command> [options]"
+echo "Usage: clawtrace <command> [options]"

-local latest=$(curl -s https://api.github.com/repos/luccast/crabwalk/releases/latest ...)
+local latest=$(curl -s https://api.github.com/repos/dibbaa-code/clawtrace/releases/latest ...)
```

Interpretation:
- This is consistent with rebranding an existing CLI rather than independently authored net-new code.

---

## 7. Copyright and license provenance evidence

Both repos have MIT LICENSE with the same copyright holder:

`clawtrace`:
- https://github.com/dibbaa-code/clawtrace/blob/main/LICENSE

`crabwalk`:
- https://github.com/luccast/crabwalk/blob/main/LICENSE

Relevant text from both:

```text
MIT License

Copyright (c) 2026 Luciano Castillo Vega
```

Important nuance:
- MIT allows reuse/copy with attribution.
- So this is not automatically a copyright violation.
- But for hackathons, undeclared heavy reuse can still violate competition originality/disclosure rules.

---

## 8. High-velocity import pattern in early clawtrace

First 10 commits in first 30 minutes (all by same identity/email):
- window: `2026-02-28 12:00:00` to `12:30:00` PST
- total additions: `22,215`
- non-lockfile additions: `15,559`
- first commit alone: `11,064` insertions

Examples:
- `475289e` “Add OpenClaw gateway integration” -> `2001` insertions
- `4211ca2` “Add monitor components...” -> `2846` insertions
- `43c8d2e` “workspace route, Docker, CI/CD” -> `1275` insertions

Interpretation:
- This pattern strongly matches importing a large pre-existing codebase then adjusting it.

---

## 9. Private contribution context (Feb 7 signal)

Public contribution calendars show both users had private activity on Feb 7, 2026:
- `srilaasya`: `7 contributions on February 7`
- `dibbaa-code`: `10 contributions on February 7`

What this means:
- It supports pre-existing private work around hackathon date.
- It does not prove which private repo those contributions came from.

---

## 10. Why this is likely wrong in hackathon context

This is likely problematic if rules required any of the following:
- Code primarily written during event window.
- Mandatory declaration of pre-existing code.
- Limits on reused external OSS boilerplate/frameworks.
- Originality scoring tied to implementation authored during event.

Given evidence, the project appears to rely heavily on pre-existing public codebase structure and files. If that was not explicitly disclosed and permitted, this is likely against typical hackathon fairness standards.

---

## 11. What can be concluded vs not concluded

Can conclude confidently:
- `crabwalk` existed first and had mature releases before `clawtrace` public start.
- `clawtrace` has high structural and code-level overlap with `crabwalk`.
- Rebrand-style edits are present.

Cannot conclude from public data alone:
- Exact private repo contents on Feb 7.
- Intent (copying with permission/disclosure vs nondisclosure).
- Final rule violation without official rule text and organizer interpretation.

---

## 12. Recommended submission package for organizers

Submit these as exhibits:
1. Repo timelines (with timestamps and tags).
2. Similarity metrics table (`shared/identical/changed`).
3. CLI rename diff block.
4. LICENSE comparison block.
5. Early 30-minute insertion-volume table.
6. Links to representative matching files.

Use neutral wording:
- “Strong evidence of substantial prior code reuse from crabwalk.”
- “Please verify against originality/disclosure rules in effect for this event.”

---

## 13. Evidence links

- Clawtrace main: https://github.com/dibbaa-code/clawtrace
- Crabwalk main: https://github.com/luccast/crabwalk
- Clawtrace first commit: https://github.com/dibbaa-code/clawtrace/commit/2b063e0bd428bdf5fdfb915b9fda6c771b6e0df5
- Clawtrace LICENSE: https://github.com/dibbaa-code/clawtrace/blob/main/LICENSE
- Crabwalk LICENSE: https://github.com/luccast/crabwalk/blob/main/LICENSE
- Crabwalk tags: https://github.com/luccast/crabwalk/tags
- Clawtrace commits: https://github.com/dibbaa-code/clawtrace/commits/main/

