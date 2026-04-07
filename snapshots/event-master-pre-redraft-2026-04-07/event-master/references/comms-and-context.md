# Communication And Context Surfaces

Use this reference when the task touches Discord, Slack, historical reconstruction, or agent-facing retrieval over the event corpus.

## Discord

Primary scripts:
- `python3 scripts/discord_event_setup.py`
- `python3 scripts/discord_export.py`
- npm aliases in `package.json`: `discord:inspect`, `discord:apply`, `discord:export`

Known local sources:
- spec / notes dir: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/discord`
- full exports dir: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/discord-export-full`
- worked example spec: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/discord/tokyo_server_spec.json`
- worked example live-state note: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/discord/tokyo-discord-live-state.md`
- official CV server export root: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/discord-export-full/cerebral-valley__1257549042255401061`

Auth rule:
- Discord scripts require `DISCORD_BOT_TOKEN` in the shell environment.
- Do not hardcode the token into this skill, repo files, or JSON specs.

Hard safety rule:
- Treat the official Cerebral Valley Discord server as read-only by default.
- Do not mutate it unless the user explicitly asks for a live Discord change in the current turn.
- Even then: inspect first, verify guild id/name, compare against docs/spec, and only then consider an `apply`.

Useful commands:

```bash
export DISCORD_BOT_TOKEN='<bot-token>'
python3 scripts/discord_event_setup.py inspect --guild-id '1490565451468767374'
python3 scripts/discord_event_setup.py apply --guild-id '1490565451468767374' --spec .research/discord/tokyo_server_spec.json --seed-messages
python3 scripts/discord_export.py --guild-id '1490565451468767374' --out-dir .research/discord-export-full/tokyo --compact
```

Export layout:
- top-level `README.md`: human-readable channel inventory with message counts and paths
- top-level `manifest.json`: structured channel inventory
- per-channel folder:
  - `channel.json`
  - `messages.jsonl.gz` in compact mode
  - or `messages.json`, `messages.jsonl`, `messages.md` in full mode

Discord step order:
1. Identify the target event and guild.
2. Open the latest exported corpus if one exists.
3. Run `inspect` for live verification if the user is asking about current state.
4. Compare against the event planning doc, participant guide, and any Discord spec.
5. Draft doc updates or a JSON spec locally.
6. Only run `apply` on explicit user request.

Interpretation rules:
- `inspect` and `export` are read-only verification paths.
- `apply` is a live mutation. Only use it if the user explicitly asks to change the live Discord server.
- For invite links, pins, channel layout, and what is live right now, prefer Discord API verification over planning-doc assumptions.
- For the official CV server, prefer the exported corpus plus `inspect`; do not improvise mutations.

## Slack

Primary scripts:
- `python3 scripts/evidence_agent_cli.py`
- `python3 scripts/slack_agent_context.py`
- `python3 scripts/materialize_slack_channel_from_evidence.py`

Known local sources:
- archived dump: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/slack-archived-dump-full`
- checklist/context layer: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/context-layer`
- evidence DB: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/agent-evidence/evidence.db`
- raw evidence rows: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/process-node-mapping/evidence_items.jsonl`

Useful commands:

```bash
python3 scripts/evidence_agent_cli.py stats --db .research/agent-evidence/evidence.db
python3 scripts/evidence_agent_cli.py search "winner emails" --db .research/agent-evidence/evidence.db --context
python3 scripts/evidence_agent_cli.py node "17:5970" --db .research/agent-evidence/evidence.db --context --full
python3 scripts/evidence_agent_cli.py channels --db .research/agent-evidence/evidence.db
python3 scripts/slack_agent_context.py .research/slack-archived-dump-full --outdir .research/slack-archived-dump-full/agent-context
python3 scripts/materialize_slack_channel_from_evidence.py --evidence-path .research/process-node-mapping/evidence_items.jsonl --channel-id 'C0XXXXXXX' --out-dir .research/slack-archived-dump-full/events-context/example-channel
```

Interpretation rules:
- Use `evidence_agent_cli.py` first for targeted retrieval.
- Use `.research/context-layer` when the question is checklist-heavy or operator-action-heavy.
- Drop to the raw archived dump only when you need full-thread evidence or exact message ordering.
- If the relevant channel is missing from the archive but present in evidence rows, materialize it from `evidence_items.jsonl`.
- If local Slack evidence is insufficient and the missing information is public/current rather than internal/private, use Exa/web search after the local pass.

## Time Slice And Context Packs

Primary scripts:
- `python3 scripts/build_event_time_slice.py`
- `python3 scripts/event_context_pack.py`
- npm aliases in `package.json`: `event-master:time-slice`, `event-master:context-pack`

Known local sources:
- staged event roots: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/eval-staging`
- generated packs: `/Users/nickita/.superset/worktrees/start/second-handstand/.research/context-packs`

Useful commands:

```bash
python3 scripts/build_event_time_slice.py --event-root .research/eval-staging/vercel-nyc --cutoff '2026-03-09T21:06:21Z' --out-dir .research/context-packs/_time-slices/vercel-nyc/demo
python3 scripts/event_context_pack.py --event-root .research/eval-staging/vercel-nyc --cutoff '2026-03-09T21:06:21Z' --name 'vercel-nyc-precutoff-demo'
python3 scripts/event_context_pack.py --slice-dir .research/context-packs/_time-slices/vercel-nyc/demo --manifest-only
```

Use these when:
- the user asks what was known before a deadline or event moment
- you need a portable bundle for review or offline reasoning
- you need artifacts + Slack + Granola context constrained to a historical cutoff

## Local Agent Retrieval API

Doc:
- `/Users/nickita/.superset/worktrees/start/second-handstand/AGENT_API.md`

Base URL:
- `http://127.0.0.1:4173`

Useful commands:

```bash
curl http://127.0.0.1:4173/api/agent
curl 'http://127.0.0.1:4173/api/agent/nodes?search=partner%20package&limit=10'
curl 'http://127.0.0.1:4173/api/agent/artifacts?type=Google%20Doc&limit=10'
curl -X POST http://127.0.0.1:4173/api/agent/retrieve \
  -H 'Content-Type: application/json' \
  -d '{"query":"What happens after contract is signed?","retrievalMode":"hybrid","scope":"workflow-global","limit":8}'
```

Use this when:
- you want structured node/artifact retrieval without manually reading the big JSON files
- you want node-local or workflow-global retrieval with graph expansion
- you need a fast local API surface for another agent or tool chain

Preference rules:
- prefer the CLI for quick local interactive work
- prefer the local API when you want structured JSON responses or cross-tool integration

## When To Ask The User

Ask a concise clarification question instead of guessing when:
- the event is ambiguous
- more than one Discord guild/server could match
- the user might want a live Discord mutation but did not say so explicitly
- the artifact family is unclear enough that you could update the wrong doc or surface
