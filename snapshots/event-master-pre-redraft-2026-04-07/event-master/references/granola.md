# Granola

Use local Granola files, not Granola MCP.

Primary cache:
- `~/Library/Application Support/Granola/cache-v6.json`

Primary local exporter skill:
- `/Users/nickita/.codex/skills/granola-local-export/SKILL.md`

Useful commands:

```bash
python3 /Users/nickita/.codex/skills/granola-local-export/scripts/inspect_granola_cache.py \
  --cache-path "$HOME/Library/Application Support/Granola/cache-v6.json"

python3 /Users/nickita/.codex/skills/granola-local-export/scripts/search_granola_cache.py \
  --cache-path "$HOME/Library/Application Support/Granola/cache-v6.json" \
  --query "Anthropic"

python3 /Users/nickita/.codex/skills/granola-local-export/scripts/export_granola_cache.py \
  --cache-path "$HOME/Library/Application Support/Granola/cache-v6.json" \
  --out-dir ".research/granola-export" \
  --include-transcripts \
  --transcript-bucket-seconds 15
```

What Granola is good for here:
- meeting titles
- folder membership
- attendees
- cached transcripts
- per-meeting markdown exports

What Granola is not guaranteed to have:
- full note body for every meeting
- full assistant/cloud answers

Use Granola as supporting event context. Do not treat it as the main system of record for event operations.

