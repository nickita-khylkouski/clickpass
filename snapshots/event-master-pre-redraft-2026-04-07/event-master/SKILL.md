---
name: event-master
description: "Use this skill when working on Cerebral Valley event operations in this workspace: find the right event docs and templates, resolve the relevant workflow nodes and adjacent steps, verify live state in the platform DB, use local Granola context when helpful, and then actually execute the next artifact or update instead of stopping at research."
---

# Event Master

Use this as the umbrella skill for CV event work in this workspace. This skill is not only for retrieval. It is for execution.

It ties together:
- the local `Events` Drive corpus and embeddings index
- the Google-native export cache for private Google Docs / Sheets / Slides bodies
- the Google Workspace edit/create/copy/patch CLIs for Docs and Sheets
- the node graph summaries and evidence packets
- the persisted Slack archive, derived checklist/context layer, and evidence DB
- the Discord server spec / live-state docs plus Discord API helper scripts
- the pre-cutoff event time-slice and context-pack builders
- the packet-writing / execution-grade research prompt
- platform DB and Supabase verification paths
- the evidence-routing workflow
- local Granola exports and transcripts
- the local agent retrieval API over nodes / artifacts / evidence
- operator CLIs that collapse an incoming ask into docs, nodes, packets, DB checks, and next actions

## Safety Default

Unless the user explicitly asks for a live mutation in the current turn, treat this skill as:
- local draft creation
- read-only DB / Supabase verification
- read-only Discord / platform state verification
- approval-bundle preparation

Do not write to production systems, live platform rows, live docs, or live event/Discord surfaces by default.
For event-ops tasks, prefer:
- a local approval draft
- source map / approval notes
- exact paste-ready content
- explicit go-live steps for a human operator

For Discord specifically:
- treat the official Cerebral Valley server as read-only by default, no exceptions
- do not run Discord mutation paths like `discord_event_setup.py apply` against the official server unless the user explicitly says to do it in the current turn
- when a live Discord write is explicitly requested, inspect/export first, confirm the target guild/event, and proceed carefully from a spec or local draft rather than improvising

Use this skill when the user asks things like:
- decide how to respond to an inbound event email
- find the right event docs
- find templates, partner packages, proposals, contracts, recaps, or master planning docs
- figure out which workflow node matters
- figure out what came right before or right after a step
- verify whether an event is actually live / configured / taking applications / ready for post-event follow-up
- make or update the real artifact, not just describe it
- route new evidence into the right node
- use Granola context to answer event questions
- onboard an agent to CV event operations quickly
- build opening decks, finalist slides, winner/closing decks, or any event slide artifact

If the task is specifically about creating or rebuilding a slide deck, use `slide-deck-creation` alongside this skill. `event-master` should still find the event docs, nodes, and state, but the actual slide workflow should follow the slide skill.

If the task is specifically about creating, rebuilding, standardizing, or seeding an event Discord server, use `event-discord-server` alongside this skill. `event-master` should still find the event docs, event-specific truths, and live state first, but the Discord workflow should follow the Discord skill instead of improvising structure or copy.

If the task is specifically about creating, copying, patching, reviewing, or importing Google Docs / Sheets / Slides artifacts, use `event-google-workspace` alongside this skill. `event-master` should still find the event docs, templates, live context, and safest mutation target first, but the actual Google Workspace workflow should follow the dedicated skill instead of improvising tool choice.

## Google Workspace Quick Start

Even if the task is not mainly a Google Workspace task, remember the basic event-ops path:
- create a review Google Doc from local Markdown with `python3 scripts/md_to_google_doc.py`
- copy an existing Google Doc before experimenting with `python3 scripts/google_doc_copy.py`
- inspect tabs before patching with `python3 scripts/google_doc_tabs.py`
- patch only the intended tab with `python3 scripts/google_doc_patch.py`
- update operational row/cell data with `python3 scripts/google_sheet_edit.py`
- import a local `.pptx` into Google Slides with `python3 scripts/pptx_to_google_slides.py`
- when you need richer Docs verbs like comments, images, page breaks, broader read modes, or upstream MCP tools, use `node scripts/google_docs_mcp_cli.mjs` or `npm run events-index:google-docs-mcp`

Auth/token notes:
- first try `--auth-if-missing` on the local Google Workspace CLIs; that is the default easy path
- if you need to bootstrap Google export auth directly, run `npm run events-index:google-auth`
- default export/edit token path is usually `.research/events-google-export/auth/token.json`
- when the CV account specifically needs to read a doc, sheet, or slides file, use `.research/events-google-export/auth/token-cv.json`
- the local MCP wrapper auto-seeds auth from those same token files into a workspace-local config store; you usually do not need a separate OAuth flow

Minimal examples:

```bash
python3 scripts/md_to_google_doc.py '/abs/path/to/draft.md' --title 'Review Draft' --auth-if-missing
python3 scripts/google_doc_copy.py 'DOC_ID_OR_URL' --title 'Copy - Working Draft' --auth-if-missing
python3 scripts/google_doc_tabs.py 'DOC_ID_OR_URL' --auth-if-missing
python3 scripts/google_sheet_edit.py inspect 'SHEET_ID_OR_URL' --auth-if-missing
npm run events-index:google-docs-mcp -- doctor
npm run events-index:google-docs-mcp -- call-tool readDocument --args-json '{"documentId":"DOC_ID","format":"markdown"}'
```

Use this quick-start when the Google Workspace need is incidental to a larger task.
If the task is mainly Docs / Sheets / Slides execution, switch to `event-google-workspace`.

## Read This Skill In Layers

1. Start here for the overall workflow.
2. Read [references/drive-index.md](references/drive-index.md) for the Events Drive CLI surface.
3. Read [references/google-workspace-editing.md](references/google-workspace-editing.md) for the short Google Workspace summary, then switch to `event-google-workspace` when the task is mainly Docs / Sheets / Slides execution.
4. If the needed doc is a private Google-native file and you need its full body, run the Google-native export step before proceeding.
5. Read [references/node-knowledge.md](references/node-knowledge.md) for node summaries and per-node evidence.
6. Read [references/packet-execution.md](references/packet-execution.md) when the task needs a packet, an execution-grade workflow answer, or a concrete operator playbook.
7. Read [references/databases.md](references/databases.md) when platform state, applicant state, submissions, reminders, or blasts matter.
8. Read [references/comms-and-context.md](references/comms-and-context.md) when Discord, Slack, context packs, time-slice reconstruction, or local retrieval surfaces matter.
9. Read [references/granola.md](references/granola.md) when meeting notes or transcripts matter.
10. Read [references/routing.md](references/routing.md) when new evidence needs to be attached to graph nodes.
11. If the output is a slide deck, hand off the deck-building part to `slide-deck-creation`.

## Core Workflow

Use this exact order unless the user explicitly asks for a narrower task.

1. Find the event and canonical source documents first.
Use the local Events index before manually browsing Drive folders. Resolve:
- the event folder
- the master planning / workback doc
- the real template if one exists
- the strongest example artifact from a prior event

If the canonical source is a `.gdoc`, `.gsheet`, or `.gslides` pointer and you need the actual body text, export it before reasoning from it. Do not pretend pointer metadata is the full doc.
For Google Sheets specifically, inspect the exported tab bodies, not just the workbook title. Master checklists, run-of-show sheets, digital checklists, judge trackers, and blast calendars often contain the real operator instructions that the planning doc omits.
For Google Slides, prefer exported slide text over the pointer title. If the Slides API path is unavailable, fall back to Drive export of the presentation as `.pptx` and extract the slide text from that export instead of stopping.
If the local pointer is missing `doc_id`, stale, or unreadable, fall back to direct export by doc ID / URL instead of stopping.
If one Google account cannot read the file, retry with the alternate token path for the account that actually has event access.
If local semantic/index search still misses the right file, use Google Drive API lexical search as a fallback. It can search titles and file text, but it is not semantic; treat it as a recall fallback, not the primary ranking layer.
If the user wants you to create or edit a Google Doc / Sheet as part of the task, decide the edit strategy here instead of waiting until the end:
- create a standalone review doc from local Markdown
- copy a template / prior doc first and patch the copy
- patch an official doc in place only on explicit request
- use Sheets tools instead of Docs tools when the real artifact is row/cell data

2. Find the relevant workflow node and adjacent nodes second.
Do not stop at the direct node. Also identify:
- the strongest owning node
- what usually happens immediately before
- what usually happens immediately after
- whether this is a doc-only step or a DB/product-backed step

3. Verify product / DB state third when the step touches platform state.
If the step involves event pages, applications, reminders, blasts, waivers, attendance, submissions, public gallery, or judging, verify live state in the platform DB instead of relying only on docs or Slack.

4. Check communication/runtime surfaces when the step touches participant comms, staff coordination, or live Discord state.
If the ask involves:
- Discord setup, live channels, invite links, pinned posts, or server verification
- Slack checklists, assignment threads, or "what did ops say/do"
- reconstructing what was true before a specific cutoff

then inspect the comms/context surfaces before guessing:
- Discord spec / live-state docs and Discord API helpers
- Slack archive, checklist/context layer, and evidence DB
- event time-slice snapshots and context packs

5. Pull supporting Granola context when ownership, rationale, recent decisions, or meeting context are unclear.
Granola is useful for meeting context, constraints, ownership hints, rationale, and "why did we decide this?" recovery. It is not the main system of record, but it should be checked earlier when the docs explain what to do and Granola is more likely to explain why or who.

6. Execute the task.
Based on docs, nodes, examples, and live state:
- update the document
- draft the artifact
- produce the checklist
- identify the missing inputs
- or route the new evidence

If the artifact is a slide deck:
- use this skill to resolve the content and source-of-truth docs
- then switch to `slide-deck-creation` for precedent lookup, asset sourcing, local PPTX build, preview QA, and Google Slides import/share

For event copy / participant resources / emails / platform details pages, default to building the local artifact first unless the user explicitly says to push the live change now.
If the user wants the local artifact turned into a shareable Google Doc for review, create a standalone Google Doc from the local Markdown draft instead of editing an official event doc in place.
If the user wants an existing Google Doc updated:
- inspect whether it is multi-tab
- target the correct tab with `google_doc_tabs.py` plus `--tab-title` / `--tab-id`
- copy the doc first unless they explicitly want the official source doc edited in place
If the artifact is operational checklist / tracker / run-of-show data, prefer `google_sheet_edit.py` over trying to stuff row data into a doc.

For participant-facing details pages or hacker resources, do this in order:
- search the event's exported planning doc body for embedded sections like `Participant Resources`, `Details Page`, `Event Details`, `Discord`, `1st Blast`, `2nd Blast`, `Problem Statements`, `Provided Resources`, `Judging`, and `Prizes`
- treat that embedded event-specific draft as higher priority than a generic prior-event example
- then compare against one finished prior hacker-resources example to normalize structure and catch missing sections
- then inspect one or two shipped live `PlatformEvent.details` examples from the platform DB for the same artifact family, because the live event page often has a more accurate final section order and attendee-facing tone than the raw planning doc
- if the event has a checklist or run-of-show sheet, inspect the exported tabs too because they often carry the most current owner notes, due dates, placeholders, and missing-link callouts
- remove nonessential internal/public-risk content from the participant-facing version unless the source explicitly makes it attendee-facing
- produce three local artifacts by default:
  - source-backed working draft
  - paste-ready participant-facing version
  - approval notes with source-backed vs normalized distinctions

7. Return actionable output.
Prefer:
- exact docs and templates to open
- exact node packet(s) used
- exact DB state verified
- exact next actions
- exact missing artifacts / blockers

## Packet-Grade Answer Style

When the user wants a workflow answer, packet, or operator note, copy the style of the best old packet prompts:
- execution-grade, not generic
- grounded in concrete docs, packets, Slack, code, and DB state
- explicit about before / in-progress / done / completion verification
- explicit about uncertainty and missing artifacts

Default answer expectations:
- commit to one best interpretation of the workflow step
- say what usually must already be true
- explain how it is normally done with concrete artifacts
- tell the operator exactly what to do next
- name the system of record
- name the completion signal
- cite the supporting file path / doc / packet inline
- if product state matters, verify it

If you are building a packet or execution note, use [references/packet-execution.md](references/packet-execution.md).

## Operator CLIs

Use these when the user gives you an incoming ask and you need to quickly collapse it into the real operating workflow.

- `event_ops_brief.py`
  - best for: freeform tasks like "respond to this email", "what should we do next", "make the partner package", "is this event page live"
  - output: likely task family, template, best examples, likely owning node, adjacent steps, packet hits, recommended DB checks, suggested next actions
- `event_packet_search.py`
  - best for: finding the strongest prior packets, quotes, and node evidence for a topic
  - output: matching final packets plus matching node evidence excerpts
- `event_platform_snapshot.py`
  - best for: checking whether a live event is actually configured / taking applicants / has reminders / has blasts / has submissions
  - output: event row plus applicant/reminder/blast/submission summaries

## Default Commands

Events index:

```bash
npm run events-index:stats
npm run events-index:tree -- --max-depth 3 --sort count
npm run events-index:search -- "partner package"
npm run events-index:search -- "master planning document" --semantic
python3 scripts/events_drive_index.py show --file-id 'file:...'
```

Google-native export:

```bash
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
npm run events-index:google-create-doc -- '/abs/path/to/local.md' --title 'Standalone Draft' --auth-if-missing
python3 scripts/google_doc_patch.py replace-body 'DOC_ID_OR_URL' '/abs/path/to/local.md' --tab-title 'Prizes' --auth-if-missing
python3 scripts/google_doc_patch.py append-body 'DOC_ID_OR_URL' '/abs/path/to/appendix.md' --tab-title 'Event Page Copy' --auth-if-missing
python3 scripts/google_doc_patch.py replace-text 'DOC_ID_OR_URL' '/abs/path/to/replacements.json' --tab-title 'Prizes' --auth-if-missing
python3 scripts/google_sheet_edit.py inspect 'SHEET_ID_OR_URL' --auth-if-missing
python3 scripts/google_sheet_edit.py set-range 'SHEET_ID_OR_URL' "'Checklist'!A1:C3" '/abs/path/to/values.json' --auth-if-missing
python3 scripts/google_sheet_edit.py upsert-rows 'SHEET_ID_OR_URL' 'Checklist' '/abs/path/to/rows.json' --key-column 'Task' --extend-header --auth-if-missing
```

For approval workflows:
- keep the Markdown file as source of truth in-repo
- create a standalone Google Doc only after the local draft is ready
- return both the local file path and the Google Doc URL

Node search:

```bash
python3 /Users/nickita/.codex/skills/event-master/scripts/search_event_nodes.py "partner package"
python3 /Users/nickita/.codex/skills/event-master/scripts/search_event_nodes.py "pricing proposal" --include-sections --limit 8
```

Workspace status:

```bash
python3 /Users/nickita/.codex/skills/event-master/scripts/event_master_status.py
npm run event-master:status
```

Node search:

```bash
python3 /Users/nickita/.codex/skills/event-master/scripts/search_event_nodes.py "partner package"
python3 /Users/nickita/.codex/skills/event-master/scripts/search_event_nodes.py "publish event page" --include-sections --limit 8
npm run event-master:nodes -- "partner package"
```

Platform DB snapshot:

```bash
python3 /Users/nickita/.codex/skills/event-master/scripts/event_platform_snapshot.py --slug "gemini-3-nyc-hackathon"
python3 /Users/nickita/.codex/skills/event-master/scripts/event_platform_snapshot.py --title-like "Anthropic"
python3 /Users/nickita/.codex/skills/event-master/scripts/event_platform_snapshot.py --recent 10
npm run event-master:platform -- --recent 10
```

Ops brief:

```bash
python3 /Users/nickita/.codex/skills/event-master/scripts/event_ops_brief.py \
  "Need to send the partner package after the event" --event "OpenAI Codex"
npm run event-master:brief -- "Need to respond to client asking for proposal update" --event "Google GCP"
```

Packet / quote lookup:

```bash
python3 /Users/nickita/.codex/skills/event-master/scripts/event_packet_search.py "partner package"
npm run event-master:packets -- "publish event page"
```

Discord / Slack / context reconstruction:

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

Granola:

```bash
python3 /Users/nickita/.codex/skills/granola-local-export/scripts/search_granola_cache.py \
  --cache-path "$HOME/Library/Application Support/Granola/cache-v6.json" \
  --query "event owner handoff"
```

## What This Skill Knows Exists

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
- Prefer exported Google-native bodies over `.gdoc` / `.gsheet` / `.gslides` pointer metadata when the task requires exact doc contents.
- Prefer `node-catalog.json` for quick workflow orientation.
- Prefer `node-evidence.json` or final packets for detailed operational reasoning.
- Prefer packet-grade execution notes over generic summaries.
- Prefer local approval bundles over live writes unless the user explicitly asks for the live mutation in the current turn.
- Prefer the actual template plus one or two completed examples over a blank generic answer.
- Prefer platform DB verification over Slack claims when the node touches live event state.
- Prefer Discord API inspect/export over static docs when the user asks what is live in Discord right now.
- Prefer event-specific Slack/context-pack slices over broad corpus browsing when the user asks what was known before a cutoff.
- Prefer the strongest owning node plus adjacent nodes over one isolated node hit.
- Prefer local Granola cache/export over Granola MCP.
- Prefer local event corpus, exported Discord/Slack evidence, and platform state first; use Exa/web research only when the needed context is outside the local CV operating corpus or clearly stale/missing.
- Prefer generic artifact-family searches and placeholders in examples; do not hardcode one event's naming unless the user is explicitly asking about that event.
- Route evidence by direct proof, not vague relevance.
- Do not hardcode Discord bot tokens in the skill, repo, or example specs; use `DISCORD_BOT_TOKEN` from the shell environment.
- If the event, guild, region, or live-mutation target is unclear and the risk of guessing is real, ask the user for the missing context instead of silently picking the wrong surface.

## Common Task Patterns

If the user asks "how do we do X event task?":
- search Events docs
- search nodes
- read the best one to three final node packets
- verify DB/product state if relevant
- answer with the real workflow, the best docs, the template, and the next operator actions

If the user asks for an operational communication artifact like a judge email, participant blast, reminder, or resource note:
- start with the event's exported master planning doc body before broad search
- search within that export for sections like `Judge Comms`, `Participant Comms`, `Judge Tracking`, `Participant Resources`, `Resource Blast`, or `Reminder`
- prefer the event-specific embedded draft over a generic prior-event example
- use prior-event examples only to fill real gaps, not to override the event's own template
- call out any related tracker fields that should be updated after send, such as `Confirmed`, `Judges Logistics Sent`, or reminder status

For judge logistics / judge onboarding asks:
- pull the event's `Judge Tracking`, `Judge Comms`, and `Final Round Judging Rubric` sections before drafting
- identify the owning judge node and at least one adjacent judge node
- compare against one or two prior finished judge comms examples
- verify live platform state in read-only mode using:
  - `EventInvitation`
  - `HackathonJudge`
- treat `judge logistics sent` and `invite first round judges to platform` as separate completion checks
- default to a local bundle with:
  - source-backed judge ops plan
  - send-ready first/final email package
  - first-round platform-onboarding checklist

If the user asks for a participant-facing details page or attendee resources page:
- first identify the owning node, usually `Hacker Resources Finalized` or the nearest adjacent event-page node
- search the planning export for the exact participant-resource block before drafting anything
- inspect exported HTML/text comments for unresolved operator notes like translation requirements, placeholder links, naming conventions, or schedule discrepancies
- compare against at least one finished prior participant-resource artifact
- default to a local approval bundle, not a live paste

If the user asks about venue Wi-Fi, AV, LED, translators, or similar day-of logistics:
- treat it as a vendor lane first, not just event-page copy
- look for explicit vendor/venue confirmation requirements in planning docs and comments
- search node packets around contractor outreach, quote availability, quote confirmation, and payout
- check whether a separate invoice / install lead time is likely from prior-event examples
- produce:
  - a local execution plan
  - send-ready venue/internal messages
  - explicit completion criteria for when the row is actually done

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

If the user asks about Discord setup, invite links, or what is actually live in the server:
- inspect the live guild with `discord_event_setup.py inspect`
- inspect the latest exported Discord corpus under `.research/discord-export-full/<guild>`, especially `README.md`, `manifest.json`, and the relevant channel folders
- compare against the event's Discord spec / planning doc / participant resources
- export the server with `discord_export.py` if you need message, pin, or thread evidence
- treat `discord_event_setup.py apply` as live mutation and only use it on explicit request
- treat the official CV Discord server as read-only unless the user explicitly requests a live change
- return:
  - the live invite if verified
  - the channel/category structure
  - missing roles, pins, or stale copy
  - the exact docs/blasts that must be updated to match

If the user asks what happened in Slack, what ops said, or what checklist existed:
- use `evidence_agent_cli.py` or the local Agent API before opening huge JSON blobs manually
- use `.research/context-layer` for extracted checklist / operational context
- use `.research/slack-archived-dump-full` when you need full message-level evidence
- materialize a single channel from evidence if the relevant conversation is only in `evidence_items.jsonl`

If the user needs a portable or historical event bundle:
- build a time slice from `.research/eval-staging/<event>`
- build a context pack from that slice
- use the pack when the question is "what would an operator have known at this time?"

If the user asks for opening decks, finalist decks, winner decks, closing decks, or MC materials:
- resolve the event, source docs, and canonical workflow node here first
- then use `slide-deck-creation` for precedent lookup, asset sourcing, local deck build, preview QA, and Google Slides import/share
- if the deck needs public/vendor/current context not present in the local corpus, use Exa/web research after the local search pass instead of guessing

If the user asks "find the source doc / template":
- search the Events index
- return both the local file path and Google URL if present
- also return the best prior completed example if one exists

If the user asks "which node does this belong to?":
- search nodes first
- then use `$graph-evidence-routing`

If the user asks "make/update the artifact":
- find the canonical template
- find one or two prior examples
- find the owning node and adjacent nodes
- verify any live product/DB state
- then draft or update the real artifact using the evidence, not a generic template

If the user asks "how should we respond?":
- identify whether the ask is:
  - proposal/quote
  - event page / launch
  - contract / invoice
  - partner package / post-event
  - social copy
  - judge / hacker comms
- find the exact docs and packets for that family
- tell the operator what artifact to send or update next
- if possible, draft the response grounded in the real docs/examples

If the user asks "what should I do next?":
- identify the current workflow node
- identify what is already done
- identify what completion looks like
- produce an exact checklist with named docs, systems, and people

If the user asks "is this actually live / configured / done?":
- do not trust docs alone
- check platform DB/product state using [references/databases.md](references/databases.md)

If the user asks "what happened in the meeting?":
- search/export Granola
- use transcript exports as support
- do not over-claim if only metadata is cached

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
