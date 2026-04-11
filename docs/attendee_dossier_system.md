# Attendee Dossier Queue System

This document describes the current attendee dossier production system used to generate one research packet per applicant with multiple model lanes, Daytona execution, queueing, telemetry, and a live dashboard.

It is intentionally operational. It explains:

- what the system does
- how the components fit together
- how to set it up on a new machine
- how to launch and monitor it
- what gets written to disk
- how failures are handled
- what the known weak points are

This is the system behind:

- [scripts/attendee_dossier_queue.py](/Users/nickita/cv-rank/scripts/attendee_dossier_queue.py)
- [scripts/build_attendee_dossiers.py](/Users/nickita/cv-rank/scripts/build_attendee_dossiers.py)
- Daytona runners in `/Users/nickita/.superset/worktrees/start/beaded-mind`
- the dashboard at [scripts/serve_attendee_queue_dashboard.py](/Users/nickita/.superset/worktrees/start/beaded-mind/scripts/serve_attendee_queue_dashboard.py)

## Purpose

The ranking pipeline decides who should be invited. The dossier pipeline answers a different question:

- for each applicant, what can we confidently say in a sponsor-facing or operator-facing packet right now?

Each run builds a packet around one person:

- identity anchors
- current role and company when verifiable
- public outputs
- evidence clips
- possible but unconfirmed updates
- a dense current snapshot summary
- research memo / braindump style supporting material
- per-run usage telemetry

The system is queue-based because:

- thousands of applicants need to be processed
- multiple models are useful in parallel
- failures need retries, telemetry, and auditability
- packets need deterministic output folders and a live operations view

## High-Level Architecture

The production path has 6 layers.

1. Queue storage
- SQLite queue DB at:
  - `/Users/nickita/cv-rank/outputs/attendee_dossier_queue/queue.sqlite3`
- one row per applicant per queue name
- tracks:
  - status
  - attempts
  - backend
  - requested model
  - web mode
  - worker pid/host
  - last exit code
  - failure bucket
  - run id and run dir

2. Local prefetch / seed context
- done by [scripts/attendee_dossier_queue.py](/Users/nickita/cv-rank/scripts/attendee_dossier_queue.py)
- builds a single-person `people_file` before the remote model call
- the important design choice is:
  - fetch the narrow applicant context once locally
  - reuse it on retries
  - do not let every remote attempt re-query live databases

3. Dossier builder
- [scripts/build_attendee_dossiers.py](/Users/nickita/cv-rank/scripts/build_attendee_dossiers.py)
- creates:
  - seed evidence
  - formatted context
  - prompt payload
  - final dossier JSON
  - dossier markdown
  - usage artifacts
  - run summary telemetry

4. Model lanes
- Codex
- plain Claude
- MiniMax
- Wafer

All lanes ultimately go through the same dossier builder, but use different runner wrappers and model settings.

5. Daytona remote execution
- most production work runs inside Daytona sandboxes
- one long-lived local queue worker launches one remote runner subprocess per job
- the runner uploads the needed runtime files, executes the one-person dossier build, archives the remote output, and syncs it back locally

6. Dashboard + CLI inspection
- live operational state comes from:
  - queue DB
  - worker log tails
  - run artifacts
- dashboard serves a compact black-and-white ops view
- queue CLI gives exact truth when the dashboard is ambiguous

## Repositories and Paths

The system is split across two working trees.

Core data + queue + dossier builder:
- `/Users/nickita/cv-rank`

Daytona runners + launchers + dashboard:
- `/Users/nickita/.superset/worktrees/start/beaded-mind`

Important files:

Core:
- [scripts/attendee_dossier_queue.py](/Users/nickita/cv-rank/scripts/attendee_dossier_queue.py)
- [scripts/build_attendee_dossiers.py](/Users/nickita/cv-rank/scripts/build_attendee_dossiers.py)
- [prompts/wafer_attendee_dossier_system_prompt.txt](/Users/nickita/cv-rank/prompts/wafer_attendee_dossier_system_prompt.txt)

Runner / infra:
- [daytona_cv_rank_remote.py](/Users/nickita/.superset/worktrees/start/beaded-mind/daytona_cv_rank_remote.py)
- [daytona_plain_remote.py](/Users/nickita/.superset/worktrees/start/beaded-mind/daytona_plain_remote.py)
- [daytona_minimax_remote.py](/Users/nickita/.superset/worktrees/start/beaded-mind/daytona_minimax_remote.py)
- [daytona_wafer_remote.py](/Users/nickita/.superset/worktrees/start/beaded-mind/daytona_wafer_remote.py)
- [scripts/start_daytona_queue_pool.py](/Users/nickita/.superset/worktrees/start/beaded-mind/scripts/start_daytona_queue_pool.py)
- [scripts/serve_attendee_queue_dashboard.py](/Users/nickita/.superset/worktrees/start/beaded-mind/scripts/serve_attendee_queue_dashboard.py)
- [claude_wafer_agent.py](/Users/nickita/.superset/worktrees/start/beaded-mind/claude_wafer_agent.py)

State / credentials:
- Daytona env:
  - `/Users/nickita/.superset/worktrees/start/beaded-mind/.env.daytona`
- Exa key pool:
  - `~/.claude-wafer/exa_keys.env`
- MiniMax env:
  - `~/.claude-wafer/minimax.env`
- Claude long-lived token:
  - `~/.claude/oauth_token`

## What Each Lane Is For

### Codex

Default model:
- `gpt-5.4`

Current default Daytona lane config:
- runner: `daytona_cv_rank_remote.py`
- web mode: native-web / Claude-style search mode by default in launcher config, but can be run in `exa`

Strengths:
- strong structured outputs
- conservative claims
- reliable current-role summaries when evidence is good

Weaknesses:
- can be too cautious
- may under-fill public outputs or confirmed updates on weaker profiles

### Plain Claude

Default model:
- `sonnet`

Runner:
- `daytona_plain_remote.py`

Strengths:
- good synthesis quality
- strong narrative summaries
- now authenticated via `CLAUDE_CODE_OAUTH_TOKEN`

Weaknesses:
- historically broke on expired local auth
- can be slower than Codex on similar packet quality

### MiniMax

Default model:
- `MiniMax-M2.7`

Runner:
- `daytona_minimax_remote.py`

Strengths:
- can produce rich packets when identity is clear
- good breadth on public outputs and evidence clips

Weaknesses:
- quality is more variable than the other lanes
- historically prone to thin packets when web evidence is sparse
- highest average cost per applicant of the current lanes

### Wafer

Default model:
- `Qwen3.5-397B-A17B`

Runner:
- `daytona_wafer_remote.py`

Strengths:
- strongest rich-packet lane today
- high evidence density
- high number of useful public outputs

Weaknesses:
- can generate a lot of “possible updates”
- sometimes benefits from stricter filtering if the packet should stay very sponsor-safe

## Why the System Works

The system is robust because it does not depend on a single fragile step.

### 1. Local prefetch isolates the DB

Before the model runs, the queue worker constructs a narrow one-person context file.

That file is the `people_file` for the job and includes:
- queue row / applicant identity
- Postgres platform data
- GitHub enrichment when available
- precomputed seed evidence

The important consequence:
- retries reuse the local cached person payload
- remote sandboxes do not keep hitting Supabase/Postgres for the same row

### 2. One run == one directory

Each run gets a stable folder:
- `outputs/attendee_dossier_queue/<queue_name>/runs/<run_id>`

That gives:
- reproducibility
- easy reinspection
- deterministic telemetry
- artifact diffing across attempts

### 3. Validation is conservative

A run is not considered successful just because a JSON file exists.

The queue validates:
- missing dossier dir
- malformed or too-short JSON
- too-short markdown
- zero-signal outputs
- thin outputs
- rate-limit/auth shell responses

This prevents empty wrapper files from being counted as “completed.”

### 4. Lane diversity helps

Different models fail differently:
- one can be cautious
- one can be verbose
- one can be evidence-heavy

Running multiple lanes in parallel reduces dependence on one model family.

### 5. Telemetry is first-class

Each run now writes usage telemetry, attempt telemetry, and a run summary.

That lets operators answer:
- what model produced this packet
- how many tokens did it use
- what did it cost
- why did it fail
- was the failure terminal or retryable

## Data Flow

### Step 1: Enqueue people

You can enqueue from:
- the ranked Postgres-backed applicant list
- or an explicit JSON `people_file`

Command:

```bash
python3 /Users/nickita/cv-rank/scripts/attendee_dossier_queue.py \
  --db /Users/nickita/cv-rank/outputs/attendee_dossier_queue/queue.sqlite3 \
  enqueue \
  --queue-name nyc-unified \
  --all
```

### Step 2: Queue claims a job

The worker:
- selects the next `pending` row
- increments attempts
- writes run metadata to the queue row
- writes a local cached `people_file`

### Step 3: Remote runner executes

For Daytona execution:
- the worker launches a runner such as `daytona_plain_remote.py`
- the runner:
  - ensures the sandbox exists
  - uploads runtime bundle
  - uploads env files
  - runs `build_attendee_dossiers.py`
  - archives the remote output
  - syncs it to the local run dir

### Step 4: Validation

If the run exits `0`, the queue still validates the artifacts.

Outcomes:
- valid -> `completed`
- retryable failure -> `pending` again with incremented attempt count
- terminal failure or attempts exhausted -> `failed`

### Step 5: Dashboard and telemetry

Once synced locally:
- the dashboard can inspect the run
- CLI tools can inspect the run
- the packet can be consumed by downstream sponsor or operator workflows

## What Gets Prefetched

Queue prefetch is intentionally narrow.

Current default prefetch includes:
- platform DB
- GitHub, when token/config allows it
- no Supabase by default
- no Exa by default

The prefetched marker in the person payload records that decision:

```json
{
  "_prefetched_enrichment": {
    "supabase": false,
    "github": true,
    "platform_db": true
  }
}
```

This matters because:
- the dossier builder should respect that marker
- remote jobs should not “helpfully” turn providers back on

That exact bug existed before and has been fixed.

## Setup

### 1. Install Python environments

Core repo:

```bash
cd /Users/nickita/cv-rank
uv pip install -e ".[full]"
```

Daytona Python environment:

Use the Daytona Python referenced in:
- `/Users/nickita/.superset/worktrees/start/beaded-mind/.env.daytona`

### 2. Configure core repo env

Core env file:
- `/Users/nickita/cv-rank/.env`

At minimum, the dossier system expects:
- `PLATFORM_DATABASE_URL`
- `SUPABASE_URL`
- `SUPABASE_KEY`

Even when Supabase is disabled for queue prefetch, those env values may still be required by some runner paths or older code paths. In practice, keep them configured.

### 3. Configure Daytona

File:
- `/Users/nickita/.superset/worktrees/start/beaded-mind/.env.daytona`

Needed values:
- `DAYTONA_API_KEY`
- `DAYTONA_API_URL`
- `DAYTONA_PYTHON_BIN`

### 4. Configure Exa

The production setup uses a pool of Exa keys for load distribution.

File:
- `~/.claude-wafer/exa_keys.env`

The code reads keys from:
- `EXA_API_KEYS`
- `EXA_API_KEY`
- `EXA_API_KEY_1`, `EXA_API_KEY_2`, ...
- `~/.claude-wafer/exa_keys.env`

Current behavior:
- a key is assigned per run
- assignment is deterministic per seed/run id
- this spreads cost without making retries bounce between keys unpredictably

### 5. Configure MiniMax

File:
- `~/.claude-wafer/minimax.env`

Expected values:
- `MINIMAX_API_KEY`
- `MINIMAX_API_HOST`
- optional timeout / traffic controls

Current default model:
- `MiniMax-M2.7`

### 6. Configure plain Claude auth

Recommended source:
- `~/.claude/oauth_token`

Fallback source:
- `~/.claude/.credentials.json`

Current runner behavior:
- if `oauth_token` exists, the runner prefers it
- stale `.credentials.json` is not uploaded into the sandbox when token auth is available

This matters because the old plain-Claude 401 failures came from expired credential precedence.

## Running the System

### Inspect queue status

```bash
python3 /Users/nickita/cv-rank/scripts/attendee_dossier_queue.py \
  --db /Users/nickita/cv-rank/outputs/attendee_dossier_queue/queue.sqlite3 \
  status \
  --queue-name nyc-unified \
  --limit 8
```

### Inspect live running jobs

```bash
python3 /Users/nickita/cv-rank/scripts/attendee_dossier_queue.py \
  --db /Users/nickita/cv-rank/outputs/attendee_dossier_queue/queue.sqlite3 \
  running \
  --queue-name nyc-unified \
  --limit 24
```

### Inspect failures

```bash
python3 /Users/nickita/cv-rank/scripts/attendee_dossier_queue.py \
  --db /Users/nickita/cv-rank/outputs/attendee_dossier_queue/queue.sqlite3 \
  failures \
  --queue-name nyc-unified \
  --limit 20
```

### Inspect a single run

```bash
python3 /Users/nickita/cv-rank/scripts/attendee_dossier_queue.py \
  --db /Users/nickita/cv-rank/outputs/attendee_dossier_queue/queue.sqlite3 \
  inspect \
  --queue-name nyc-unified \
  --run-id nyc-unified-00903
```

### Launch a pooled Daytona fleet

The main fleet launcher is:
- [scripts/start_daytona_queue_pool.py](/Users/nickita/.superset/worktrees/start/beaded-mind/scripts/start_daytona_queue_pool.py)

Examples:

```bash
python3 /Users/nickita/.superset/worktrees/start/beaded-mind/scripts/start_daytona_queue_pool.py \
  --lane codex \
  --workers 20

python3 /Users/nickita/.superset/worktrees/start/beaded-mind/scripts/start_daytona_queue_pool.py \
  --lane claude \
  --workers 10

python3 /Users/nickita/.superset/worktrees/start/beaded-mind/scripts/start_daytona_queue_pool.py \
  --lane minimax \
  --workers 10

python3 /Users/nickita/.superset/worktrees/start/beaded-mind/scripts/start_daytona_queue_pool.py \
  --lane wafer \
  --workers 10
```

Defaults today:
- `3` workers per sandbox
- sandbox cap protection
- lane-specific default model and runner

### Serve the dashboard

```bash
python3 /Users/nickita/.superset/worktrees/start/beaded-mind/scripts/serve_attendee_queue_dashboard.py \
  --port 8788
```

Open:
- `http://127.0.0.1:8788`

## Dashboard Semantics

The dashboard shows several different counts that mean different things.

### Claimed

`claimed` is the real number of running queue jobs for that lane.

Source of truth:
- live queue DB rows with `status='running'`

This is the number to trust for:
- “How many jobs are assigned to Codex right now?”

### Active

`active` is process count, not job count.

It includes:
- local queue worker parents
- Daytona runner children

This is why it often looks roughly doubled versus `claimed`.

### Fresh logs

`fresh logs` means:
- worker log files modified in the last `300` seconds

It is a liveness signal, not a concurrency count.

Low `fresh logs` does not necessarily mean a worker is dead. It can simply mean:
- the worker is still running but quiet
- the file has not flushed recently

### Throughput metrics

Dashboard now computes:
- average seconds per applicant
- median seconds per applicant
- average cost per applicant
- average input tokens per applicant
- average output tokens per applicant

These are computed from:
- queue timestamps
- `usage/*.json` files inside completed run dirs

## Run Artifacts

Each run directory looks like:

```text
outputs/attendee_dossier_queue/<queue_name>/runs/<run_id>/
  contexts/
  seed_evidence/
  raw_wafer/
  dossiers/
  research_memos/
  braindumps/            # when enabled
  usage/
  telemetry/
  index.json
  daytona-runner.stdout.txt
```

### `contexts/`

Markdown context files per attendee.

Purpose:
- human-readable input summary
- debugging what the model saw before research

### `seed_evidence/`

JSON snapshots of deterministic presearch and internal anchors.

Purpose:
- identity anchoring
- evidence budget grounding
- reproducibility

### `raw_wafer/`

Raw model / transcript outputs per pass.

Purpose:
- debugging
- usage extraction
- evidence of what the agent actually returned

### `dossiers/`

Primary output folder.

Contains:
- canonical JSON dossier
- markdown dossier
- sometimes `.agent.txt` helper output

This is the most important artifact directory.

### `research_memos/`

Rendered memo-style research output derived from the dossier.

### `braindumps/`

Optional evidence-heavy markdown dumps when braindump mode is enabled.

### `usage/`

Per-attendee usage JSON.

Contains:
- requested model
- phase usage
- aggregate usage
- costs
- token counts

### `telemetry/queue_attempt.json`

Per-attempt telemetry from the queue worker.

Includes:
- backend
- requested model
- web mode
- return code
- failure bucket
- validation error
- terminal failure flag
- stdout/stderr lengths and hashes
- artifact counts

### `telemetry/run_summary.json`

Run-level summary generated by the dossier builder.

Includes:
- run metadata
- usage payloads
- index-like summary information

### `index.json`

One row per attendee processed in the run.

Contains relative paths to:
- dossier JSON
- dossier markdown
- usage JSON
- memo files

## Queue States

Each job can be:
- `pending`
- `running`
- `completed`
- `failed`

### Pending

Ready to be claimed.

### Running

Claimed by a worker and currently executing.

### Completed

Remote run succeeded and local validation passed.

### Failed

Either:
- attempts exhausted
- or a terminal failure bucket occurred

## Retries and Terminal Failures

Current default:
- `max_attempts = 2`

Retry behavior:
- retryable failures are requeued until max attempts
- terminal provider/auth failures stop immediately

Examples of terminal buckets:
- Codex usage limit shell
- Exa credits exhausted
- Exa invalid API key
- Claude authentication failure
- Provider quota / rate limit exhausted

This prevents the system from wasting retries on failures that cannot succeed without operator intervention.

## What Failure Buckets Mean

Current important buckets include:

- `Codex usage limit shell`
- `Exa credits exhausted`
- `Exa invalid API key`
- `Claude authentication failure`
- `Provider quota / rate limit exhausted`
- `Daytona apt/dpkg lock during bootstrap`
- `Remote output archive missing`
- `MiniMax native web-search parameter error`
- `Supabase enrichment HTTP 400`
- `Missing dossier directory`
- `Thin dossier output`
- `Thin unverified dossier output`
- `Zero-signal dossier output`

Interpretation:

### Infrastructure failures

Examples:
- apt lock
- archive missing
- missing shell/runtime path

These mean the runner or sandbox failed, not the model reasoning.

### Provider failures

Examples:
- auth failure
- quota exhausted
- rate limit

These should now fail cleanly and explicitly.

### Quality failures

Examples:
- zero-signal dossier
- thin dossier

These mean the model returned something structurally present but not substantively useful.

## Why Old Failed Rows May Still Mention Supabase

This is a subtle but important historical note.

Queue prefetch had already been writing:

```json
"supabase": false
```

But the dossier builder used to only skip Supabase when that value was truthy.

That meant:
- `false` got treated as “not decided”
- the remote run tried Supabase anyway
- old failed rows got error text like:
  - `Supabase enrichment HTTP 400`

That logic has been fixed.

So when you see old failed rows with Supabase in the error:
- it does not automatically mean the current fleet is still doing live Supabase enrichment
- many of those rows are just stale failures from the pre-fix window

## Monitoring Run Health

When checking the fleet, use this order:

1. Queue DB
- source of truth for:
  - counts
  - running jobs
  - failed jobs
  - per-backend live claims

2. Dashboard
- best for quick visual state
- good for:
  - lane throughput
  - cost averages
  - recent completions
  - worker freshness

3. Worker logs
- best for:
  - proving workers are actually rolling
  - spotting retries and completion cadence

4. Actual dossier files
- best for:
  - quality checks
  - hallucination suspicion
  - sponsor packet readiness

## Quality Expectations by Lane

Current rough operator expectations:

- Wafer:
  - strongest rich packets
  - highest evidence density
- Claude:
  - strong summaries and synthesis
  - good sponsor-ready prose
- Codex:
  - cautious, cleaner, sometimes thinner
- MiniMax:
  - variable quality
  - can be great on strong-identity rows
  - weakest lane on consistency today

This is not a permanent truth; it is the current observed operating reality.

## Known Weak Points

### 1. Historical `unknown` lane metrics

Older completed rows may lack backend tagging, so dashboard historical metrics may show an `unknown` bucket.

### 2. MiniMax variance

MiniMax can still return packets heavy on outputs/clips with weaker confirmed updates.

### 3. Process counts are noisy

`active` process count includes parent workers and runner children.

Use:
- `claimed` for actual lane concurrency

### 4. Detached dashboard startup can be flaky

Foreground server execution is the most reliable mode in this environment.

## Recommended Operator Workflow

### Standard launch

1. Enqueue or verify queue contents
2. Start dashboard
3. Launch lanes with `start_daytona_queue_pool.py`
4. Verify:
   - queue counts
   - lane `claimed` counts
   - first completions
5. Spot-check 3-5 dossiers
6. Let fleet continue

### If failures spike

1. Check queue DB, not just the dashboard
2. Look at `failures` CLI output
3. Open one failed run dir
4. Read:
   - `telemetry/queue_attempt.json`
   - `daytona-runner.stdout.txt`
   - `dossiers/*.agent.txt` if present
5. Decide if the problem is:
   - provider
   - infrastructure
   - validation
   - prompt quality

### If outputs are weak but not crashing

Do not change 10 things at once.

Usually the right order is:
1. validate a weak specimen
2. adjust prompt or validation
3. run a small canary
4. only then scale

## Useful Commands

Status:

```bash
python3 /Users/nickita/cv-rank/scripts/attendee_dossier_queue.py \
  --db /Users/nickita/cv-rank/outputs/attendee_dossier_queue/queue.sqlite3 \
  status --queue-name nyc-unified --limit 10
```

Running:

```bash
python3 /Users/nickita/cv-rank/scripts/attendee_dossier_queue.py \
  --db /Users/nickita/cv-rank/outputs/attendee_dossier_queue/queue.sqlite3 \
  running --queue-name nyc-unified --limit 20
```

Failures:

```bash
python3 /Users/nickita/cv-rank/scripts/attendee_dossier_queue.py \
  --db /Users/nickita/cv-rank/outputs/attendee_dossier_queue/queue.sqlite3 \
  failures --queue-name nyc-unified --limit 20
```

Inspect one run:

```bash
python3 /Users/nickita/cv-rank/scripts/attendee_dossier_queue.py \
  --db /Users/nickita/cv-rank/outputs/attendee_dossier_queue/queue.sqlite3 \
  inspect --queue-name nyc-unified --run-id nyc-unified-00903
```

Launch a lane:

```bash
python3 /Users/nickita/.superset/worktrees/start/beaded-mind/scripts/start_daytona_queue_pool.py \
  --lane codex --workers 20
```

Dashboard:

```bash
python3 /Users/nickita/.superset/worktrees/start/beaded-mind/scripts/serve_attendee_queue_dashboard.py \
  --port 8788
```

## Summary

This system works because it combines:

- deterministic local prefetch
- queue-based retry control
- isolated per-run artifact directories
- model diversity
- explicit validation
- per-run telemetry
- operational visibility through DB + dashboard

If you are debugging it, trust this order:

1. queue DB
2. run artifacts
3. worker logs
4. dashboard

If those disagree, the queue DB and the run directory win.
