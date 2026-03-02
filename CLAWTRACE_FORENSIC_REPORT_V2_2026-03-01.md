# Clawtrace Forensic Report (V2)

Date: 2026-03-01 (America/Los_Angeles)
Scope: Public OSINT + repository forensics only (no privileged/private access)
Question: Was the project likely prebuilt before the hackathon demo, and what was added when?

## Executive conclusion

- Public evidence shows the `clawtrace` public repo was created on **2026-02-28**, then received a very large initial code dump at exactly **12:00 PM PST** and sustained heavy commits in the next 30 minutes.
- This pattern is **consistent with pre-existing code being imported quickly**, but does **not** by itself prove cheating.
- Independent signal: both accounts show **private contribution activity on February 7, 2026** (the date you cited for the hackathon). Public data cannot prove what repo that activity belonged to.

Confidence:
- `High` on timeline and code-volume facts.
- `Medium` on “prebuilt before public repo” inference.
- `Low` on attribution of private Feb 7 work to this exact project (not publicly provable).

---

## Exhibit A: Public repo creation and deployment timing

1. Repo creation time (`dibbaa-code/clawtrace`):
- `2026-02-28T21:29:56Z` (GitHub API)
- Source: https://api.github.com/repos/dibbaa-code/clawtrace

2. Site deployment identifier embedded in HTML:
- `dpl_6ySXnPDsPfnyQY6ewPGyqJn7ufPP`
- Source page: https://v0-clawtrace-landing-page.vercel.app/

3. JS chunk headers from live site show March 1 build/deploy activity:
- Example `last-modified`: `Sun, 01 Mar 2026 20:37:09 GMT`
- Example chunk URL: https://v0-clawtrace-landing-page.vercel.app/_next/static/chunks/568c68c1ffc3040a.js?dpl=dpl_6ySXnPDsPfnyQY6ewPGyqJn7ufPP

What this supports:
- Public repo + deployed site are active within the last 2 days.

---

## Exhibit B: Commit timeline around project start

Source log (chronological):
- https://github.com/dibbaa-code/clawtrace/commits/main/

First 10 commits (all by `Divi157`, `divyasaini6798@gmail.com`) are between **12:00 and 12:30 PST** on 2026-02-28:

1. `2b063e0` at `12:00:00` — “Initial project setup”
2. `fb42633` at `12:04:00`
3. `56b70b4` at `12:07:00`
4. `28ba6d3` at `12:09:00`
5. `475289e` at `12:13:00`
6. `284883f` at `12:16:00`
7. `4211ca2` at `12:21:00`
8. `a712f8a` at `12:23:00`
9. `9fedfb7` at `12:27:00`
10. `43c8d2e` at `12:30:00`

Volume in those first 30 minutes:
- First commit alone: **11,064 insertions**
- First 10 commits total: **22,215 insertions**, 0 deletions
- First 10 excluding `package-lock.json`: **15,559 insertions**

Interpretation:
- This is unusually high for brand-new handwritten-from-scratch work in 30 minutes.
- It is strongly consistent with importing/refactoring existing code/templates.

---

## Exhibit C: Contributor distribution and code ownership signal

Contributors by commit count:
- `Divi157 <divyasaini6798@gmail.com>`: 17 commits
- `Sri Laasya Nutheti <nutheti.laasya@gmail.com>`: 15 commits
- `Divya Saini <divyasaini6798@gmail.com>`: 3 commits

Net line deltas by author identity string:
- `Divi157 <divyasaini6798@gmail.com>`: `+22,381`
- `Sri Laasya Nutheti <nutheti.laasya@gmail.com>`: `+653`
- `Divya Saini <divyasaini6798@gmail.com>`: `-45`

Note:
- `Divi157` and `Divya Saini` share the same email; likely same person identity split.

Interpretation:
- Most net new code volume in public history is attributable to `divyasaini6798@gmail.com` identity.
- `Sri Laasya` commits are substantial but mostly feature refinement/UI/alert pipeline evolution later in the timeline.

---

## Exhibit D: Specific code-level evidence (quoted + linked)

### D1) Large infra drop early (`475289e`, 12:13 PM)
Commit: https://github.com/dibbaa-code/clawtrace/commit/475289efe766d9e9115456799b8edf56c682d5ea
Stat: `7 files changed, 2001 insertions(+)`

Code evidence:
- WebSocket gateway client with reconnect/timeout/auth challenge flow.
- File: https://github.com/dibbaa-code/clawtrace/blob/475289efe766d9e9115456799b8edf56c682d5ea/src/integrations/openclaw/client.ts

Short quotes:
- `"Connection timeout - is openclaw gateway running?"`
- `"if (msg.type === 'event' && msg.event === 'connect.challenge')"`
- `"if (wasConnected && code !== 1000) { this.scheduleReconnect() }"`

Why it matters:
- This is non-trivial systems code dropped very early in the session.

### D2) Large graph/visualization drop early (`4211ca2`, 12:21 PM)
Commit: https://github.com/dibbaa-code/clawtrace/commit/4211ca253794e0a4af1f9caeefe711ac77bd3eb6
Stat: `12 files changed, 2846 insertions(+)`

Code evidence:
- Full ReactFlow monitor graph with node/edge modeling, layout, and behavior constants.
- File: https://github.com/dibbaa-code/clawtrace/blob/4211ca253794e0a4af1f9caeefe711ac77bd3eb6/src/components/monitor/ActionGraph.tsx

Short quotes:
- `"const STEP_INTERVAL = 100"`
- `"const [layoutDirection, setLayoutDirection] = useState<'LR' | 'TB'>('LR')"`
- `"const rawEdges = useMemo(() => {"`

Why it matters:
- Suggests a mature UI subsystem added in one shot.

### D3) Workspace + ops scaffolding early (`43c8d2e`, 12:30 PM)
Commit: https://github.com/dibbaa-code/clawtrace/commit/43c8d2e291e8242895ce8303d91cc2797bd37b02
Stat: `7 files changed, 1275 insertions(+)`

Code evidence:
- Workspace route includes path validation, local caching, directory listing, file read/write flow.
- File: https://github.com/dibbaa-code/clawtrace/blob/43c8d2e291e8242895ce8303d91cc2797bd37b02/src/routes/workspace/index.tsx

Short quotes:
- `"validatePathAndSet"`
- `"localStorage.setItem('crabcrawl:workspacePath', result.expandedPath)"`
- `"const result = await trpc.workspace.readFile.query"`

Why it matters:
- Indicates broad product scope (monitor + workspace + infra) within first 30 minutes.

### D4) Later threat/alerts feature by Sri Laasya (`bf057f6`, 5:28 PM)
Commit: https://github.com/dibbaa-code/clawtrace/commit/bf057f6083ca91d7ae38c87df092abb9ea302302
Stat: `13 files changed, 486 insertions(+), 94 deletions(-)`

Code evidence (LLM threat analysis + Discord alerting + trace IDs):
- Threat analyzer: https://github.com/dibbaa-code/clawtrace/blob/bf057f6083ca91d7ae38c87df092abb9ea302302/src/lib/threat-analyzer.ts
- Alert service: https://github.com/dibbaa-code/clawtrace/blob/bf057f6083ca91d7ae38c87df092abb9ea302302/src/lib/alert-service.ts
- Router event pipeline: https://github.com/dibbaa-code/clawtrace/blob/bf057f6083ca91d7ae38c87df092abb9ea302302/src/integrations/trpc/router.ts

Short quotes:
- `"model: 'gpt-4o-mini'"`
- `"const link = `${BASE_URL}/monitor?trace=${payload.traceId}`"`
- `"const traceId = nanoid(12)"`
- `"if (threat.malicious) { sendDiscordAlert(...) }"`

Why it matters:
- Demonstrates real feature work by `Sri Laasya` after initial bulk code import phase.

---

## Exhibit E: February 7 private-contribution signal

Contribution-calendar endpoints (publicly visible counts):
- `srilaasya` Feb 2026: https://github.com/users/srilaasya/contributions?from=2026-02-01&to=2026-02-28
  - includes: `7 contributions on February 7`
- `dibbaa-code` Feb 2026: https://github.com/users/dibbaa-code/contributions?from=2026-02-01&to=2026-02-28
  - includes: `10 contributions on February 7`

Interpretation:
- Both users had private/non-public contribution activity on Feb 7.
- This is compatible with “work happened before public repo launch.”
- It is not direct proof those contributions were this exact project.

---

## What can and cannot be proven from public OSINT

Can prove:
- Public repo creation/push times.
- Exact commit chronology and code volume.
- Which public commits introduced which features.
- Live deployment artifacts and deployment-id continuity.

Cannot prove (without private access):
- Exact content of private Feb 7 contributions.
- Whether code was authored from scratch vs copied from private/internal/template source.
- Whether hackathon rules were violated (requires official rule text + organizer adjudication).

---

## For adjudication: strongest evidence packet to submit

1. Timeline chart of first 30 minutes (10 commits, 22k+ insertions).
2. Three early “large subsystem” commit links (`475289e`, `4211ca2`, `43c8d2e`).
3. Feb 7 private-contribution count screenshots/exports for both users.
4. Repo creation timestamp and deployment timestamp evidence.
5. Neutral language: “pattern strongly consistent with pre-existing code import” (not absolute accusation).

---

## Chain-of-custody / preservation

- Checked out snapshot commit locally:
  - `/Users/nickita/.superset/worktrees/start/sigma/clawtrace_backup_2b063e0` at `2b063e0bd428bdf5fdfb915b9fda6c771b6e0df5`
- Full backup bundle created:
  - `/Users/nickita/.superset/worktrees/start/sigma/clawtrace_backup_full.bundle`

