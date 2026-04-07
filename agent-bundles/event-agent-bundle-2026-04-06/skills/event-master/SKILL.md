---
name: event-master
description: "Use this skill when working on CV event operations in this workspace: find the right event docs and templates, resolve the relevant workflow nodes and adjacent steps, verify live state in the platform DB, use local Granola/Slack/Discord context when helpful, and then actually execute the next artifact or update instead of stopping at research."
---

# Event Master

This skill keeps `event-master` detailed and command-rich, but reorganizes it around:
- orchestration
- shared surfaces
- task-family playbooks
- subskill contracts
- completion checks

The goal is not to make the skill shorter. The goal is to make it harder for an agent to skip required context gathering and cross-system verification.

Use this skill when the user asks things like:
- decide how to respond to an inbound event email
- find the right event docs
- find templates, partner packages, proposals, contracts, recaps, or master planning docs
- figure out which workflow node matters
- figure out what came right before or right after a step
- verify whether an event is actually live / configured / taking applications / ready for post-event follow-up
- make or update the real artifact, not just describe it
- route new evidence into the right node
- use Granola, Slack, or Discord context to answer event questions
- onboard an agent to CV event operations quickly
- build opening decks, finalist slides, winner/closing decks, or any event slide artifact

## Safety And Invariants

Unless the user explicitly asks for a live mutation in the current turn, treat event work as:
- local draft creation
- read-only DB / Supabase verification
- read-only Discord / platform state verification
- approval-bundle preparation

Do not write to:
- production systems
- live platform rows
- official event docs
- live event Discord surfaces

Default event-ops outputs should be:
- a local approval draft
- source-backed working notes
- exact paste-ready content
- explicit go-live steps for a human operator

Discord-specific invariant:
- treat the official Cerebral Valley server as read-only by default, no exceptions
- never run live Discord mutation paths against the official server unless the user explicitly says to do so in the current turn

Google Workspace invariant:
- keep local Markdown as the working source by default
- create a standalone Google Doc when the artifact is ready for review, collaboration, sharing, or operator handoff
- if the agent makes a plan, brief, approval bundle, or operator note intended for review or collaboration, it should usually create a Google Doc and return both the local source path and the Google Doc link to the user
- if the user clearly wants a local-only artifact or the task is still rough internal scratch work, local Markdown can remain the only artifact for now
- prefer copied working docs over patching official docs in place
- do not patch an official event doc in place unless the user explicitly wants that live doc changed now

Cross-system invariant:
- event tasks are rarely “just one artifact”
- do not assume a slide task is only a slide task, or a Discord task is only a Discord task
- first resolve event truth, required source docs, and live state; then use the narrow execution skill/tool

## Orchestration Rules

`event-master` should be the first skill for event work.

Subskills should not be the starting point for real event tasks. They should be used only after `event-master` resolves:
- which event
- which artifact family
- which source docs matter
- which live systems must be checked
- which prior examples matter
- which mutation target is safest

Subskill contracts:
- `event-master`
  - owns event truth
  - owns source precedence
  - owns cross-system verification
  - owns choosing the real artifact/system of record
- `slide-deck-creation`
  - owns deck-building mechanics
  - owns precedent extraction, asset sourcing, preview QA, Slides import/share
- `event-discord-server`
  - owns Discord execution mechanics
  - owns server structure, pins, onboarding, permissions, verification
- `event-google-workspace`
  - owns Docs / Sheets / Slides execution mechanics
  - owns tool choice between local CLIs and richer Docs tooling

Execution rule:
1. identify the event
2. identify the task family
3. find the canonical event docs and exported Google-native bodies
4. identify the owning node plus adjacent nodes
5. identify the system of record
6. identify required supporting surfaces
7. inspect supporting context when relevant:
   - platform DB for live event/product state
   - Slack/Discord/context evidence for prior ops behavior
   - prior examples for the artifact family
8. verify live state when relevant
9. route to the right subskill/tool
10. execute
11. return the exact next action / artifact / verification state

Compound-task rule:
- if one user request spans multiple task families, do not split it into isolated mini-tasks too early
- first build one shared truth set across:
  - canonical event docs
  - exported checklist/run-of-show bodies
  - owning node plus adjacent nodes
  - live platform state
  - live Discord state if relevant
  - prior examples
- then produce one cross-artifact mismatch map before drafting or mutating anything
- sequence execution in dependency order; for example:
  - resolve platform truth
  - audit live Discord/resources against that truth
  - align participant-facing docs
  - only then freeze slide/deck content that depends on the same facts

Mandatory first-pass rule:
- before drafting or mutating anything important, the agent should usually:
  - find the event folder and planning docs
  - export/read the real Google-native bodies if needed
  - determine which node the task falls under
  - inspect adjacent nodes
  - inspect live platform state if the artifact touches live event/product state
  - inspect Slack/context evidence if prior ops behavior matters
  - inspect one or two prior finished examples for the same artifact family

## Read This Skill In Layers

Use this order when the task is broad, ambiguous, or operationally important.

1. Read this skill for the orchestration rules and task-family routing.
2. Read `references/drive-index.md` first when you need to locate event docs, examples, templates, or exported Google-native bodies.
3. Read `references/google-workspace-editing.md` for the short Workspace summary, then switch to `event-google-workspace` when the task is mainly Docs / Sheets / Slides execution.
4. Read `references/node-knowledge.md` when you need the owning workflow node, adjacent nodes, or evidence-backed workflow orientation.
5. Read `references/packet-execution.md` when the task needs a packet, execution-grade answer, operator playbook, or one best workflow interpretation.
6. Read `references/databases.md` when platform state, applications, reminders, blasts, waivers, attendance, submissions, gallery state, judging state, or live event configuration matter.
7. Read `references/comms-and-context.md` when Discord, Slack, context packs, time-slice reconstruction, checklist extraction, or prior ops handling matter.
8. Read `references/granola.md` when ownership, rationale, or meeting context matter.
9. Read `references/routing.md` when new evidence should be attached to graph nodes or routed into the workflow graph.
10. If the output is a slide deck, hand off the deck-building part to `slide-deck-creation` only after the event truth and source docs are resolved here.

## Golden Path

Use this exact order unless the user explicitly asks for a narrower read-only lookup.

1. Find the event and canonical source docs first.
Resolve:
- the event folder
- the master planning / workback doc
- the real template if one exists
- the strongest finished example for the exact artifact family

2. Export the real Google-native bodies before reasoning from them.
- do not pretend `.gdoc` / `.gsheet` / `.gslides` pointer metadata is the full document
- for Google Sheets, inspect exported tab bodies, not only the workbook title
- for Google Slides, prefer exported slide text; if the Slides API path is unavailable, fall back to `.pptx` export and extract slide text from that
- if the local pointer is missing `doc_id`, stale, or unreadable, fall back to direct export by doc ID / URL instead of stopping
- if one Google account cannot read the file, retry with the alternate token path for the account that actually has access
- if local semantic/index search still misses the right file, use Google Drive API lexical search as a fallback rather than guessing

3. Determine the owning node plus adjacent nodes.
- identify the strongest owning node
- identify what usually happens immediately before
- identify what usually happens immediately after
- decide whether this is a doc-only step or a DB/product-backed step

4. Verify the live system of record when the step touches product/event state.
- do not trust docs or Slack alone for “is this live / configured / sent / done?”
- use the platform DB when the task touches event pages, applications, reminders, blasts, waivers, attendance, submissions, public gallery, judging, or live event details/media

5. Check Discord / Slack / context evidence when the step touches communication or runtime behavior.
- inspect the live guild when the user asks what is live right now
- inspect Slack/context evidence when the question is how ops handled this before, what was true before a cutoff, or what checklist/process was actually followed

6. Pull supporting Granola context when ownership, rationale, or meeting history are unclear.

7. Execute the task.
- update the doc
- draft the artifact
- prepare the checklist
- identify the missing inputs
- or route the evidence

8. Return actionable output.
- exact docs and templates used
- exact node packet(s) used
- exact DB or live-state checks performed
- exact artifact produced
- exact blockers / missing inputs
- exact next operator action

## Shared Surfaces

These are the main surfaces `event-master` knows how to coordinate.

### Events Drive And Google-Native Export

Use this surface when you need:
- the event folder
- master planning docs
- workback/checklist docs
- templates
- prior examples
- private Google-native file bodies

Primary rule:
- prefer the local Events index over raw Finder exploration
- prefer exported Google-native bodies over `.gdoc` / `.gsheet` / `.gslides` pointer metadata when the task requires exact contents

What the Events Drive usually contains:
- one folder per event or series
- master planning / workback docs
- checklist / run-of-show / digital checklist sheets
- participant-facing drafts
- partner / judge / sponsor materials
- prior examples and adapted templates
- Google-native pointers that need export before real reasoning

Organization rule:
- assume the real answer is usually spread across:
  - the master planning doc
  - one or more checklist/run-of-show tabs
  - one or two prior examples
  - comments/notes embedded in Google-native exports
- do not stop after finding only one planning doc

Fallback rule:
- if the needed artifact is still pointer-only, export it instead of reasoning from metadata
- if the event index does not surface the file cleanly, fall back to lexical Drive search rather than manually wandering folders
- if a sheet is exported, inspect the real tab bodies before concluding you understand the operating instructions
- if a Slides file is pointer-only, prefer slide-text export or `.pptx` extraction over the presentation title

Important paths:
- Events Drive root: `/Users/nickita/Library/CloudStorage/GoogleDrive-nickita@cerebralvalley.ai/My Drive/Events`
- Hydrated Events target: `/Users/nickita/Library/CloudStorage/GoogleDrive-nickita@cerebralvalley.ai/.shortcut-targets-by-id/1krgsy-1vZahsigXfNAsjXApl7NIDCEEW/Events`
- Google-native export cache: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/events-google-export`
- Tree export: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/events-index/tree.txt`
- Stats export: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/events-index/stats.json`

Default commands:

```bash
npm run events-index:stats
npm run events-index:tree -- --max-depth 3 --sort count
npm run events-index:search -- "partner package"
npm run events-index:search -- "master planning document" --semantic
python3 scripts/events_drive_index.py show --file-id 'file:...'
```

Google-native export commands:

```bash
npm run events-index:google-auth
python3 scripts/google_native_export.py export-pointer '/abs/path/to/file.gdoc'
python3 scripts/google_native_export.py export-event-folder '/abs/path/to/event-folder'
python3 scripts/google_native_export.py export-doc-id 'DOC_ID' --kind spreadsheets --token-path .research/events-google-export/auth/token-cv.json
python3 scripts/google_drive_search.py "hackathon master checklist" --token-path .research/events-google-export/auth/token-cv.json
python3 scripts/google_export_inspect.py search "resource blast"
python3 scripts/google_export_inspect.py show 'DOC_ID' --lines 60
npm run events-index:google-export
```

### Google Workspace Execution

Use this surface when the task touches:
- Google Docs
- Google Sheets
- Google Slides

Quick incidental use:
- create review doc from local Markdown
- copy a doc before experimenting
- inspect tabs before patching
- update sheet rows/cells instead of forcing row data into Docs

Default token/auth rules:
- first try `--auth-if-missing`
- bootstrap export auth with `npm run events-index:google-auth`
- default token path is usually `.research/events-google-export/auth/token.json`
- use `.research/events-google-export/auth/token-cv.json` when the CV account specifically needs access

Incidental commands:

```bash
npm run events-index:google-create-doc -- '/abs/path/to/local.md' --title 'Standalone Draft' --auth-if-missing
python3 scripts/md_to_google_doc.py '/abs/path/to/local.md' --title 'Standalone Draft' --auth-if-missing
python3 scripts/google_doc_copy.py 'DOC_ID_OR_URL' --title 'Copy - Working Draft' --auth-if-missing
python3 scripts/google_doc_tabs.py 'DOC_ID_OR_URL' --auth-if-missing
python3 scripts/google_doc_patch.py replace-body 'DOC_ID_OR_URL' '/abs/path/to/local.md' --tab-title 'Prizes' --auth-if-missing
python3 scripts/google_doc_patch.py append-body 'DOC_ID_OR_URL' '/abs/path/to/appendix.md' --tab-title 'Event Page Copy' --auth-if-missing
python3 scripts/google_doc_patch.py replace-text 'DOC_ID_OR_URL' '/abs/path/to/replacements.json' --tab-title 'Prizes' --auth-if-missing
python3 scripts/google_sheet_edit.py inspect 'SHEET_ID_OR_URL' --auth-if-missing
python3 scripts/google_sheet_edit.py set-range 'SHEET_ID_OR_URL' "'Checklist'!A1:C3" '/abs/path/to/values.json' --auth-if-missing
python3 scripts/google_sheet_edit.py upsert-rows 'SHEET_ID_OR_URL' 'Checklist' '/abs/path/to/rows.json' --key-column 'Task' --extend-header --auth-if-missing
python3 scripts/pptx_to_google_slides.py '/abs/path/to/deck.pptx' --title 'Deck Title' --token-path '.research/events-google-export/auth/token-cv.json'
npm run events-index:google-docs-mcp -- doctor
npm run events-index:google-docs-mcp -- call-tool readDocument --args-json '{"documentId":"DOC_ID","format":"markdown"}'
npm run events-index:google-docs-mcp -- call-tool appendMarkdown --args-json '{"documentId":"DOC_ID","markdown":"## New Section"}'
```

If the task is mainly Docs / Sheets / Slides execution, route to `event-google-workspace`.

Richer Docs rule:
- use the local Python CLIs first for copy-first event workflows, surgical multi-tab patching, and local-Markdown-to-Doc flow
- use the Google Docs MCP wrapper when you need broader upstream Docs verbs like comments, images, page breaks, richer read modes, or wider Drive/Sheets tool coverage
- for deeply nested multi-tab planning-doc surgery, prefer `google_doc_tabs.py` plus `google_doc_patch.py` unless the upstream MCP path is clearly the better fit

Planning/output rule:
- if the agent creates:
  - a plan
  - an approval draft
  - a source-backed brief
  - an operator runbook
  - a packet-like note for review
- it should usually create a Google Doc version and return the Google Doc link to the user
- keep the local Markdown source when the artifact is being actively iterated; the Google Doc is the review/collaboration surface, not a replacement for the local source
- if a Google Doc is created, return both:
  - the local source path
  - the Google Doc link

### Platform DB / Supabase

Use this surface when the task touches live event/product state:
- applications
- reminders
- blasts
- waivers
- attendance
- submissions
- public gallery
- judging
- event details/live platform configuration
- event description/details text
- event page images/media/poster/thumbnail surfaces

Primary rule:
- prefer platform DB verification over Slack claims when the node touches live event state
- do not trust docs alone for “is this live / configured / done?”
- use the platform DB to ground participant-facing truth when live event descriptions, details, or media matter

Important paths:
- Platform DB env source: `/Users/nickita/cv-rank/.env`
- `cv-rank` Supabase enrichment code: `/Users/nickita/cv-rank/src/cv_rank/enrichment/supabase.py`

Commands:

```bash
python3 /Users/nickita/.codex/skills/event-master/scripts/event_platform_snapshot.py --slug "gemini-3-nyc-hackathon"
python3 /Users/nickita/.codex/skills/event-master/scripts/event_platform_snapshot.py --title-like "Anthropic"
python3 /Users/nickita/.codex/skills/event-master/scripts/event_platform_snapshot.py --recent 10
npm run event-master:platform -- --recent 10
```

### Nodes, Packets, And Evidence

Use this surface when the task needs:
- workflow-step ownership
- adjacent steps
- exact operator playbooks
- prior packet-grade reasoning
- evidence routing

Primary rule:
- prefer the strongest owning node plus adjacent nodes over one isolated node hit
- prefer packet-grade execution notes over generic summaries
- explicitly determine which node the task falls under before acting on an event-process task
- if the node is ambiguous, identify the best owning node plus the nearest one or two adjacent nodes that usually gate the work

Interpretation rule:
- when multiple packets or node hits exist, commit to one best interpretation of the current step instead of giving an unranked dump
- use the packet-grade sources to answer “how is this actually done here?” rather than paraphrasing workflow labels

Important paths:
- Main node evidence file: `/Users/nickita/.superset/worktrees/start/second-handstand/web/src/data/node-evidence.json`
- Lighter node catalog: `/Users/nickita/.superset/worktrees/start/second-handstand/web/src/data/node-catalog.json`
- Detailed node packets: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/agent-evidence/final`
- Packet prompt training source: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/prompt-training/14_3545-description-updated/prompt_v3.md`
- Packet guide source: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/process-node-mapping/NODE_SUMMARY_GUIDE.md`
- Agent API doc: `/Users/nickita/.superset/worktrees/start/second-handstand/AGENT_API.md`

Commands:

```bash
python3 /Users/nickita/.codex/skills/event-master/scripts/search_event_nodes.py "partner package"
python3 /Users/nickita/.codex/skills/event-master/scripts/search_event_nodes.py "publish event page" --include-sections --limit 8
npm run event-master:nodes -- "partner package"
python3 /Users/nickita/.codex/skills/event-master/scripts/event_packet_search.py "partner package"
npm run event-master:packets -- "publish event page"
```

### Discord / Slack / Context Reconstruction

Use this surface when the task touches:
- Discord setup
- live channels
- invite links
- pinned posts
- server verification
- Slack ops threads
- checklist extraction
- pre-cutoff reconstruction
- understanding how similar work was handled before
- prior ops decisions or send patterns

Primary rules:
- prefer Discord API inspect/export over static docs when the user asks what is live right now
- prefer event-specific Slack/context-pack slices over broad corpus browsing when the user asks what was known before a cutoff
- inspect Slack/context evidence when the task depends on how ops handled this before, not just what the docs say
- treat Discord `apply` flows as live mutation and require explicit user intent
- do not hardcode Discord bot tokens; use `DISCORD_BOT_TOKEN` from the shell environment

Important paths:
- Slack archive dump: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/slack-archived-dump-full`
- Slack checklist/context layer: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/context-layer`
- Discord spec / live-state notes: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/discord`
- Discord exports: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/discord-export-full`
- Eval staging bundles: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/eval-staging`
- Event context packs: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/context-packs`
- Agent evidence DB: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/agent-evidence/evidence.db`

Behavior rule:
- for operational artifacts, do not only read the source docs
- also inspect Slack/context evidence to see how this artifact family was actually handled before
- especially for:
  - judge ops
  - participant comms
  - Discord setup
  - vendor coordination
  - launch / go-live sequences

Commands:

```bash
export DISCORD_BOT_TOKEN='<bot-token>'
npm run discord:inspect -- --guild-id '1490565451468767374'
npm run discord:apply -- --guild-id '1490565451468767374' --spec .research/discord/tokyo_server_spec.json --seed-messages
npm run discord:export -- --guild-id '1490565451468767374' --out-dir .research/discord-export-full/tokyo --compact
python3 scripts/evidence_agent_cli.py search "judge logistics" --db .research/agent-evidence/evidence.db --context
python3 scripts/evidence_agent_cli.py node "17:5970" --db .research/agent-evidence/evidence.db --context --full
python3 scripts/slack_agent_context.py .research/slack-archived-dump-full --outdir .research/slack-archived-dump-full/agent-context
python3 scripts/materialize_slack_channel_from_evidence.py --evidence-path .research/process-node-mapping/evidence_items.jsonl --channel-id 'C0XXXXXXX' --out-dir .research/slack-archived-dump-full/events-context/example-channel
npm run event-master:time-slice -- --event-root .research/eval-staging/vercel-nyc --cutoff '2026-03-09T21:06:21Z' --out-dir .research/context-packs/_time-slices/vercel-nyc/demo
npm run event-master:context-pack -- --event-root .research/eval-staging/vercel-nyc --cutoff '2026-03-09T21:06:21Z' --name 'vercel-nyc-precutoff-demo'
```

### Granola

Use this surface when ownership, rationale, recent decisions, or meeting context are unclear.

Primary rule:
- prefer local Granola cache/export over Granola MCP
- use Granola to explain `why` and `who`, not as the main system of record

Important paths:
- Granola cache: `~/Library/Application Support/Granola/cache-v6.json`
- Granola export dir: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/granola-export`

Command:

```bash
python3 /Users/nickita/.codex/skills/granola-local-export/scripts/search_granola_cache.py \
  --cache-path "$HOME/Library/Application Support/Granola/cache-v6.json" \
  --query "event owner handoff"
```

### Exa / Web Research

Use this surface when local CV sources do not answer the question cleanly.

Primary rule:
- prefer local event corpus, exported Discord/Slack evidence, and platform state first
- use Exa/web research only when the needed context is outside the local CV operating corpus or clearly stale/missing
- use Exa/web research when the task needs:
  - external vendor/provider/product research
  - current public event/partner/tool context
  - public examples outside the CV corpus
  - public grounding for claims that should not rely only on internal memory
- even for CV-adjacent tasks, Exa/web can be used after the local pass to ground public-facing details or external references

## Task Family Router

For each task family:
- identify the system of record
- identify required supporting surfaces
- verify live state if relevant
- identify the owning node plus adjacent nodes
- only then use the narrower execution skill/tool

### 1. Event Page / Launch / Live Platform Configuration

Usually touches:
- event planning doc
- checklist/workback
- platform DB
- possibly participant-facing details/resources copy

Required sources:
- event master planning/workback doc
- exported checklist tabs
- live platform row
- owning node plus adjacent node packets when the workflow step is ambiguous

Required live checks:
- event exists
- public/live state
- applications/blasts/reminders/submissions if relevant

Typical outputs:
- launch readiness note
- source-backed mismatch list
- exact platform/details-page update bundle

Do not forget:
- docs alone do not prove the event is live

### 2. Participant Resources / Details Page / Hacker Guide

Usually touches:
- event planning doc
- checklist tabs
- prior finished participant-facing examples
- live platform details examples

Required sources:
- exported planning doc body
- exported checklist or run-of-show tabs
- one or two strong prior participant-resource artifacts
- one or two shipped live `PlatformEvent.details` examples for the same artifact family when available
- owning node plus adjacent nodes

Required live checks:
- live event details if relevant
- confirmed links, schedule, venue/team-size/submission truth

Required tools/skills:
- `event-google-workspace` if Docs execution is needed

Typical outputs:
- source-backed working draft
- paste-ready participant-facing version
- approval notes

Source precedence:
- event-specific embedded draft > checklist owner notes > prior examples

Do not forget:
- search within the event export for blocks like `Participant Resources`, `Details Page`, `Event Details`, `Discord`, `Problem Statements`, `Provided Resources`, `Judging`, and `Prizes` before drafting from scratch
- compare against live `PlatformEvent.details` order/tone when a participant-facing details page already exists in the product
- remove internal or risky internal-only content from the participant-facing version

### 3. Participant Comms / Blasts / Reminders

Usually touches:
- planning doc comms sections
- participant resource/details page
- checklist or reminder tracker
- platform DB for reminder/blast state

Required sources:
- planning doc sections like `1st Blast`, `2nd Blast`, `Reminder`, `Participant Comms`
- relevant tracker/checklist tabs
- owning node plus adjacent nodes
- Slack/context evidence for prior send patterns when available

Required live checks:
- whether the blast/reminder already exists or was sent

Typical outputs:
- send-ready email/Discord copy
- tracker update checklist
- approval notes

Do not forget:
- participant-facing comms should match the actual details/resources page truth

### 4. Judge Ops / Mentor Ops

Usually touches:
- planning doc judge sections
- judge tracking table
- judge comms
- platform DB invitation/judge rows

Required sources:
- `Judge Tracking`
- `Judge Comms`
- judging rubric
- one or two prior finished judge comms examples
- owning node plus adjacent judge nodes
- Slack/context evidence for how judge logistics were actually handled before

Required live checks:
- `EventInvitation`
- `HackathonJudge`

Required tools/skills:
- `event-google-workspace` when editing judge docs or trackers

Typical outputs:
- source-backed judge ops plan
- send-ready first/final email package
- platform-onboarding checklist

Do not forget:
- `judge logistics sent` and `invite to platform` are separate completion checks

### 5. Discord

Usually touches:
- event page
- participant resources/details page
- schedule/team size/submission link truth
- live guild state
- pinned participant-facing copy

Required sources:
- event page
- participant guide/details page
- live guild inspect/export
- any Discord spec or prior config docs
- owning node plus adjacent nodes
- Slack/context evidence if prior ops behavior matters

Required live checks:
- correct guild
- current channels/roles/pins/onboarding
- invite validity if the invite is participant-facing
- permission/privacy mismatches for ops/judge/staff channels
- stale participant-facing pinned copy versus current event truth

Required tools/skills:
- `event-discord-server`

Typical outputs:
- live-state audit
- spec/config patch
- channel/pin/onboarding verification
- explicit mismatch list between live guild state and event truth
- exact ordered patch plan if no live mutation is requested

Source precedence:
- event-specific participant guide/details page > generic manager prompt > old prior server config

Do not forget:
- the official CV Discord server stays read-only unless explicitly requested otherwise

### 6. Slide Deck / MC / Stage Materials

Usually touches:
- planning doc
- checklist tabs
- run of show
- live platform event media
- prior deck precedent

Required sources:
- event planning doc
- relevant checklist tabs
- run of show
- any already-written slide copy
- strongest prior opening/closing deck precedent
- owning node plus adjacent nodes
- live platform row/media state

Required live checks:
- event media / poster / OG asset availability

Required tools/skills:
- `slide-deck-creation`

Typical outputs:
- slide-by-slide content plan
- local `.pptx`
- preview image(s)
- Google Slides import/share if requested

Source precedence:
- event-specific planning docs/checklists > generic deck template > invented copy
- live event media > scraped logo > generated placeholder

Do not forget:
- a slide task is not “just slides”; it must still resolve event truth and live media first

### 7. Partner Package / Sponsor Package / Proposal / Pricing / Contract / Invoice

Usually touches:
- event planning docs
- prior package/proposal examples
- node packets
- possibly DB/product state if tied to a live event

Required sources:
- event docs
- node packets
- one or two strong prior examples for the exact artifact family
- owning node plus adjacent nodes
- Slack/context evidence when prior handling patterns matter

Required live checks:
- only when the ask depends on live event configuration or actual numbers/state

Typical outputs:
- source-backed package/proposal draft
- exact next send/update artifact
- completion notes

Do not forget:
- identify whether the ask is package/proposal/pricing/contract/invoice before drafting

### 8. Venue / Vendor / Logistics Lane

Usually touches:
- planning doc comments
- vendor message threads/examples
- quote/payout/install-related nodes

Required sources:
- explicit venue/vendor requirements in planning docs/comments
- adjacent node packets around quote confirmation, contractor outreach, payout
- Slack/context evidence and prior vendor lanes where available

Typical outputs:
- local execution plan
- send-ready venue/internal messages
- explicit completion criteria

Do not forget:
- “copy on event page” is often not the real task here; vendor confirmation is

### 9. Post-Event Follow-Up / Recap / Winner Follow-Up / Asset Collection

Usually touches:
- planning doc closeout tasks
- participant/judge/sponsor comms
- submissions/gallery/winner state
- post-event package or recap docs

Required sources:
- closeout sections in planning docs/checklists
- prior follow-up examples
- live platform state if winners/submissions matter
- owning node plus adjacent closeout nodes

Typical outputs:
- source-backed closeout checklist
- send-ready follow-up comms
- package/recap artifact draft

Do not forget:
- verify winner/submission/live state instead of assuming docs are final

## Broad Common Patterns

If the user asks "how do we do X event task?":
- search Events docs
- search nodes
- read the best one to three final node packets
- inspect Slack/context evidence if execution history matters
- verify DB/product state if relevant
- answer with the real workflow, best docs, template, and next operator actions

If the user gives a vague incoming ask like an email, DM, or request:
- start with `event_ops_brief.py`
- use it to determine:
  - likely artifact family
  - canonical template
  - prior examples
  - owning node
  - adjacent steps
  - DB checks worth doing
- then execute from there

If the user asks for a plan, approval note, or operator brief:
- produce the local working draft if needed
- then create a Google Doc review artifact only when the output is ready for review, collaboration, or handoff
- return the Google Doc link to the user

If the user asks for an operational communication artifact like a judge email, participant blast, reminder, or resource note:
- start with the event's exported master planning doc body before broad search
- search within that export for sections like `Judge Comms`, `Participant Comms`, `Judge Tracking`, `Participant Resources`, `Resource Blast`, or `Reminder`
- prefer the event-specific embedded draft over a generic prior-event example
- use prior-event examples only to fill real gaps, not to override the event's own template
- call out any related tracker fields that should be updated after send, such as `Confirmed`, `Judges Logistics Sent`, or reminder status

If the user asks for judge logistics or judge onboarding:
- pull the event's `Judge Tracking`, `Judge Comms`, and judging rubric sections before drafting
- identify the owning judge node and at least one adjacent judge node
- compare against one or two prior finished judge comms examples
- verify live platform state in read-only mode for invitation/judge rows
- treat `judge logistics sent` and `invite judges to platform` as separate completion checks

If the user asks for a participant-facing details page or attendee resources page:
- identify the owning node plus the nearest adjacent event-page node
- search the planning export for the exact participant-resource block before drafting anything
- inspect exported comments/notes for unresolved operator notes like translation requirements, placeholder links, naming conventions, or schedule discrepancies
- compare against at least one finished prior participant-resource artifact
- default to a local approval bundle or Google Doc review artifact, not a live paste

If the user asks about venue Wi-Fi, AV, LED, translators, or similar day-of logistics:
- treat it as a vendor lane first, not just event-page copy
- look for explicit vendor/venue confirmation requirements in planning docs and comments
- search node packets around contractor outreach, quote availability, quote confirmation, and payout
- check whether separate invoice or install lead-time handling is likely from prior examples
- produce:
  - a local execution plan
  - send-ready venue/internal messages
  - explicit completion criteria for when the row is actually done

If the user asks "find the source doc / template":
- search the Events index
- return both the local file path and Google URL if present
- also return the best prior completed example if one exists

If the user asks "which node does this belong to?":
- search nodes first
- then use the evidence-routing workflow

If the user asks "what should I do next?":
- identify the current workflow node
- identify what is already done
- identify what completion looks like
- produce an exact checklist with named docs, systems, and people

If the user asks "make/update the artifact":
- find the canonical template
- find one or two prior examples
- find the owning node and adjacent nodes
- verify any live product/DB state
- inspect Slack/context evidence if prior ops handling matters
- then draft or update the real artifact using the evidence, not a generic template

If the user asks "how should we respond?":
- identify whether the ask is:
  - proposal/quote
  - event page / launch
  - contract / invoice
  - partner package / post-event
  - social copy
  - judge / participant comms
- find the strongest docs and packets for that family
- tell the operator what artifact to send or update next
- if possible, draft the response grounded in the real docs/examples

If the user asks "is this actually live / configured / done?":
- do not trust docs alone
- check platform DB/product state

If the user asks "what happened in the meeting?":
- search/export Granola
- use transcript exports as support
- do not over-claim if only metadata is cached

## Packet-Grade Answer Style

When the user wants a workflow answer, packet, or operator note:
- be execution-grade, not generic
- ground the answer in concrete docs, packets, Slack/context evidence, code, and DB state
- be explicit about before / in-progress / done / completion verification
- be explicit about uncertainty and missing artifacts

Default answer expectations:
- commit to one best interpretation of the workflow step
- say what usually must already be true
- explain how it is normally done with concrete artifacts
- tell the operator exactly what to do next
- name the system of record
- name the completion signal
- cite the supporting file path / doc / packet inline
- if product state matters, verify it

If the output is a packet or execution note, use `references/packet-execution.md`.

## Operator CLIs

Use these when the user gives you a freeform incoming ask and you need to collapse it into the real operating workflow.

- `event_ops_brief.py`
  - best for: freeform tasks like “respond to this email”, “what should we do next”, “make the partner package”, or “is this event page live”
  - output: likely task family, template, best examples, likely owning node, adjacent steps, recommended DB checks, suggested next actions
- `event_packet_search.py`
  - best for: finding the strongest prior packets, quotes, and node evidence for a topic
  - output: matching final packets plus matching node evidence excerpts
- `event_platform_snapshot.py`
  - best for: checking whether a live event is actually configured / taking applicants / has reminders / has blasts / has submissions
  - output: event row plus applicant/reminder/blast/submission summaries

## Command Catalog

Use these command blocks after the event, node, and system-of-record questions are already resolved.

### Workspace Status

```bash
python3 /Users/nickita/.codex/skills/event-master/scripts/event_master_status.py
npm run event-master:status
```

### Ops Brief

```bash
python3 /Users/nickita/.codex/skills/event-master/scripts/event_ops_brief.py \
  "Need to send the partner package after the event" --event "OpenAI Codex"
npm run event-master:brief -- "Need to respond to client asking for proposal update" --event "Google GCP"
```

### Node Search

```bash
python3 /Users/nickita/.codex/skills/event-master/scripts/search_event_nodes.py "partner package"
python3 /Users/nickita/.codex/skills/event-master/scripts/search_event_nodes.py "pricing proposal" --include-sections --limit 8
npm run event-master:nodes -- "partner package"
```

### Packet / Quote Lookup

```bash
python3 /Users/nickita/.codex/skills/event-master/scripts/event_packet_search.py "partner package"
npm run event-master:packets -- "publish event page"
```

### Platform Snapshot

```bash
python3 /Users/nickita/.codex/skills/event-master/scripts/event_platform_snapshot.py --slug "gemini-3-nyc-hackathon"
python3 /Users/nickita/.codex/skills/event-master/scripts/event_platform_snapshot.py --title-like "Anthropic"
python3 /Users/nickita/.codex/skills/event-master/scripts/event_platform_snapshot.py --recent 10
npm run event-master:platform -- --recent 10
```

### Google-Native Retrieval And Editing

```bash
npm run events-index:stats
npm run events-index:tree -- --max-depth 3 --sort count
npm run events-index:search -- "partner package"
npm run events-index:search -- "master planning document" --semantic
python3 scripts/events_drive_index.py show --file-id 'file:...'
npm run events-index:google-auth
python3 scripts/google_native_export.py export-pointer '/abs/path/to/file.gdoc'
python3 scripts/google_native_export.py export-event-folder '/abs/path/to/event-folder'
python3 scripts/google_native_export.py export-doc-id 'DOC_ID' --kind spreadsheets --token-path .research/events-google-export/auth/token-cv.json
python3 scripts/google_drive_search.py "hackathon master checklist" --token-path .research/events-google-export/auth/token-cv.json
python3 scripts/google_export_inspect.py search "resource blast"
python3 scripts/google_export_inspect.py show 'DOC_ID' --lines 60
npm run events-index:google-export
python3 scripts/google_doc_copy.py 'DOC_ID_OR_URL' --title 'Copy - Working Draft' --auth-if-missing
python3 scripts/google_doc_tabs.py 'DOC_ID_OR_URL' --auth-if-missing
python3 scripts/md_to_google_doc.py '/abs/path/to/local.md' --title 'Standalone Draft' --auth-if-missing
python3 scripts/google_doc_patch.py replace-body 'DOC_ID_OR_URL' '/abs/path/to/local.md' --tab-title 'Prizes' --auth-if-missing
python3 scripts/google_doc_patch.py append-body 'DOC_ID_OR_URL' '/abs/path/to/appendix.md' --tab-title 'Event Page Copy' --auth-if-missing
python3 scripts/google_doc_patch.py replace-text 'DOC_ID_OR_URL' '/abs/path/to/replacements.json' --tab-title 'Prizes' --auth-if-missing
python3 scripts/google_sheet_edit.py inspect 'SHEET_ID_OR_URL' --auth-if-missing
python3 scripts/google_sheet_edit.py set-range 'SHEET_ID_OR_URL' \"'Checklist'!A1:C3\" '/abs/path/to/values.json' --auth-if-missing
python3 scripts/google_sheet_edit.py upsert-rows 'SHEET_ID_OR_URL' 'Checklist' '/abs/path/to/rows.json' --key-column 'Task' --extend-header --auth-if-missing
```

### Discord / Slack / Context Reconstruction

```bash
export DISCORD_BOT_TOKEN='<bot-token>'
npm run discord:inspect -- --guild-id '1490565451468767374'
npm run discord:apply -- --guild-id '1490565451468767374' --spec .research/discord/tokyo_server_spec.json --seed-messages
npm run discord:export -- --guild-id '1490565451468767374' --out-dir .research/discord-export-full/tokyo --compact
python3 scripts/evidence_agent_cli.py search "judge logistics" --db .research/agent-evidence/evidence.db --context
python3 scripts/evidence_agent_cli.py node "17:5970" --db .research/agent-evidence/evidence.db --context --full
python3 scripts/slack_agent_context.py .research/slack-archived-dump-full --outdir .research/slack-archived-dump-full/agent-context
python3 scripts/materialize_slack_channel_from_evidence.py --evidence-path .research/process-node-mapping/evidence_items.jsonl --channel-id 'C0XXXXXXX' --out-dir .research/slack-archived-dump-full/events-context/example-channel
npm run event-master:time-slice -- --event-root .research/eval-staging/vercel-nyc --cutoff '2026-03-09T21:06:21Z' --out-dir .research/context-packs/_time-slices/vercel-nyc/demo
npm run event-master:context-pack -- --event-root .research/eval-staging/vercel-nyc --cutoff '2026-03-09T21:06:21Z' --name 'vercel-nyc-precutoff-demo'
```

## What This Draft Knows Exists

Important workspace paths:
- Events Drive root: `/Users/nickita/Library/CloudStorage/GoogleDrive-nickita@cerebralvalley.ai/My Drive/Events`
- Hydrated Events target: `/Users/nickita/Library/CloudStorage/GoogleDrive-nickita@cerebralvalley.ai/.shortcut-targets-by-id/1krgsy-1vZahsigXfNAsjXApl7NIDCEEW/Events`
- Events DB: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/events-index/events.db`
- Extracted docs: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/events-index/extracted`
- Google-native export cache: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/events-google-export`
- Tree export: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/events-index/tree.txt`
- Stats export: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/events-index/stats.json`
- Main node evidence file: `/Users/nickita/.superset/worktrees/start/second-handstand/web/src/data/node-evidence.json`
- Lighter node catalog: `/Users/nickita/.superset/worktrees/start/second-handstand/web/src/data/node-catalog.json`
- Agent API doc: `/Users/nickita/.superset/worktrees/start/second-handstand/AGENT_API.md`
- Agent evidence DB: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/agent-evidence/evidence.db`
- Detailed node packets: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/agent-evidence/final`
- Packet prompt training source: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/prompt-training/14_3545-description-updated/prompt_v3.md`
- Packet guide source: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/process-node-mapping/NODE_SUMMARY_GUIDE.md`
- Slack archive dump: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/slack-archived-dump-full`
- Slack checklist/context layer: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/context-layer`
- Discord spec / live-state notes: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/discord`
- Discord exports: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/discord-export-full`
- Eval staging bundles: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/eval-staging`
- Event context packs: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/context-packs`
- Platform DB env source: `/Users/nickita/cv-rank/.env`
- `cv-rank` Supabase enrichment code: `/Users/nickita/cv-rank/src/cv_rank/enrichment/supabase.py`
- Granola cache: `~/Library/Application Support/Granola/cache-v6.json`
- Granola export dir: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/granola-export`

## Decision Rules

- Prefer the local Events index over raw Finder exploration.
- Prefer exported Google-native bodies over `.gdoc` / `.gsheet` / `.gslides` pointer metadata when the task requires exact contents.
- Prefer the strongest owning node plus adjacent nodes over one isolated node hit.
- Prefer packet-grade execution notes over generic summaries.
- Prefer the actual template plus one or two completed examples over a blank generic answer.
- Prefer platform DB verification over Slack claims when the node touches live event state.
- Prefer Discord API inspect/export over static docs when the user asks what is live in Discord right now.
- Prefer event-specific Slack/context-pack slices over broad corpus browsing when the user asks what was known before a cutoff.
- Prefer local Granola cache/export over Granola MCP.
- Prefer local CV corpus, exported Discord/Slack evidence, and platform state first; use Exa/web research only when the needed context is outside the local CV operating corpus or clearly stale/missing.
- Prefer generic artifact-family searches and placeholders in examples; do not hardcode one event’s naming unless the user is explicitly asking about that event.
- Route evidence by direct proof, not vague relevance.
- Do not hardcode Discord bot tokens in the skill, repo, or example specs; use `DISCORD_BOT_TOKEN` from the shell environment.
- If the event, guild, region, live doc target, or mutation target is unclear and the risk of guessing is real, ask the user for the missing context instead of silently picking the wrong surface.

## Completion Standards

An `event-master` task is not complete until the answer or artifact says:
- which event/doc/system it used
- which system of record was treated as authoritative
- which live checks were done, if any
- which output was produced
- what is still missing or unresolved
- what the next operator action is

For packet-grade work:
- commit to one best interpretation
- say what usually must already be true
- explain how it is normally done with concrete artifacts
- tell the operator exactly what to do next
- name the completion signal
- cite the supporting file path / doc / packet inline

## Reference Map

Use these references from the live skill set:
- `references/drive-index.md`
- `references/google-workspace-editing.md`
- `references/node-knowledge.md`
- `references/packet-execution.md`
- `references/databases.md`
- `references/comms-and-context.md`
- `references/granola.md`
- `references/routing.md`

Use these subskills when the narrow execution surface is confirmed:
- `slide-deck-creation`
- `event-discord-server`
- `event-google-workspace`

## Do Not Forget

- Google-native files in Drive are usually indexed as pointers plus metadata unless separately exported.
- The events index is excellent for finding docs and paths.
- The Google-native exporter is what turns private `.gdoc` / `.gsheet` / `.gslides` files into locally readable bodies.
- If a needed doc is still pointer-only, the correct move is to export it, not to guess.
- `node-evidence.json` is the main per-node summary/evidence surface.
- `node-catalog.json` is the lighter, faster node summary surface.
- `AGENT_API.md` exposes a local retrieval API over nodes, artifacts, and evidence if CLI search is not enough.
- `.research/context-layer` is the best fast path for Slack-derived checklist and ops context.
- `.research/context-packs` and `build_event_time_slice.py` are the right tools for pre-cutoff reconstruction, not ad hoc memory.
- Discord automation here is real: inspect/export are safe read paths, but `apply` is a live write and requires explicit user intent.
- The exported Discord corpus is also a first-class read surface: top-level `README.md` / `manifest.json`, plus per-channel folders with `channel.json` and message exports.
- Discord auth is environment-based. The token should be supplied via `DISCORD_BOT_TOKEN`, not written into the skill.
- If local CV sources do not answer the question cleanly, use Exa/web search for public corroboration rather than inventing missing context.
- If the ask is under-specified in a way that risks touching the wrong event/server/artifact, ask the user a concise clarification question.
- `prompt_v3.md` and `NODE_SUMMARY_GUIDE.md` are the strongest prior sources for packet-grade execution notes.
- For event execution work, the goal is usually not “explain the workflow.” The goal is “find the right source material, verify the state, and move the artifact forward.”
