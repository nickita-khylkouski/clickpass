# Packet Execution

Use this reference when the user wants more than retrieval:
- a packet
- an execution note
- an onboarding brief
- a “how do we actually do this?” answer
- an actionable artifact update plan

This reference is based on the best prior packet-making prompt and guide in this workspace:
- `/Users/nickita/.superset/worktrees/start/second-handstand/.research/prompt-training/14_3545-description-updated/prompt_v3.md`
- `/Users/nickita/.superset/worktrees/start/second-handstand/.research/process-node-mapping/NODE_SUMMARY_GUIDE.md`
- `/Users/nickita/.superset/worktrees/start/second-handstand/web/server/nodeChat.ts`

## Core Principle

The goal is not to prove that a step exists.

The goal is to let another operator or agent read one note and understand:
- what the step actually means in practice
- what usually has to already be true
- how it is normally done
- what the before / in-progress state looks like
- what done actually looks like
- what system of record should change
- how to verify completion
- what to do next without guessing

## Packet Rules To Copy

Always:
- commit to one best interpretation of the node or task
- ground the answer in concrete docs, packets, Slack, code, DB state, and templates
- prefer literal examples over clean-but-fake abstraction
- keep uncertainty explicit
- say when a referenced doc is missing locally
- say when product verification is repo-backed rather than live-backed

Do not:
- invent a cleaner workflow than the evidence supports
- write a generic best-practices memo
- assume a template exists if you have not found it
- assume a state is live if you have not checked product or DB

## Required Execution Questions

Before writing the answer, determine:
1. What is the actual artifact or state change?
2. Which source doc or template is canonical?
3. Which prior completed example is closest?
4. Which node owns this step?
5. What usually happens right before it?
6. What usually happens right after it?
7. Is this a docs-only step or a platform-backed step?
8. What is the visible done signal?

For participant-facing artifacts, also determine:
9. Is there an event-specific embedded draft already living inside the planning doc or comms section?
10. Which parts are safe and necessary for attendees, and which are internal-only even if present in source docs?
11. Are there source comments/placeholders that reveal unresolved issues like translation, stale links, or naming conventions?

## Standard Retrieval Order

1. Event docs and templates
- master planning docs
- workback docs
- partner handbooks
- proposals
- contracts
- run of show
- partner package templates
- social templates

2. Node surfaces
- `web/src/data/node-catalog.json`
- `web/src/data/node-evidence.json`
- final packets under `.research/agent-evidence/final`

3. Product / DB verification
- platform DB rows
- checked-in schema
- implementation code

4. Slack / Granola support
- strong operational threads
- meeting rationale
- owner hints

5. Finished participant-facing example, if the artifact is attendee-facing
- participant resources
- details page
- reminder blast
- judge email

## Expected Output Shape

When the task is packet-like, prefer these sections:
1. `What This Is`
2. `What Usually Needs To Already Be True`
3. `How This Is Normally Done`
4. `Examples Before / In Progress`
5. `What To Do`
6. `Inputs Required`
7. `Output Artifact`
8. `System Of Record`
9. `Verification Steps`
10. `Escalation / Who To Ask`
11. `Examples Done / Completed`
12. `Completion Criteria`
13. `Common Failure Modes`
14. `Sources, Artifacts, And People To Ask`

For shorter user answers, compress the shape, but keep the logic:
- current state
- real workflow
- template/example docs
- live verification
- next actions

## How This Is Normally Done

For each step, include:
- actor
- input artifact
- action
- reviewer
- output artifact
- `Proved by:` with exact path(s) when useful

That is the main thing the old good prompt enforced well.

## Before / In-Progress / Done

Good answers should distinguish:

Before / in-progress:
- the open ask
- the request thread
- the draft or missing artifact
- the unresolved dependency

Done:
- final artifact
- review signal
- platform signal if relevant
- downstream handoff

## Product / Platform Requirement

If the task touches anything user-facing or admin-facing:
- do not rely on docs or Slack alone
- verify where the field is stored
- verify where it renders
- verify which table / field actually changes

Use [databases.md](databases.md) for the DB access path.

## Default Operator Loop

Use this loop when the user wants real execution help:

1. Find the event folder and canonical docs.
2. Find the template and the nearest completed example.
3. Find the owning node and adjacent nodes.
4. Verify live DB/product state if relevant.
5. Write or update the artifact.
6. Call out anything still missing.
7. Return the exact next actions.

For participant-facing docs, the default local output bundle should be:
1. working draft with sourcing context
2. paste-ready participant-facing version
3. approval notes that separate:
- fully source-backed statements
- normalized wording
- inferred or withheld public-facing content

## Best Prompt Fragments To Reuse

These are the strongest reusable prompt ideas from the older packet system:

- “Produce a report that another agent could use as a working note.”
- “Commit to one best interpretation.”
- “Make `How This Is Normally Done` source-backed.”
- “Write `What To Do` so another agent/operator could follow it without guessing the next artifact.”
- “Keep the report rich and literal. Do not over-compress.”
- “If live deployed proof is not recoverable locally, say that explicitly, but still show the strongest repo-backed verification path.”
