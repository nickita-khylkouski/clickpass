# Node Knowledge

There are two main node surfaces in this workspace.

## Fast summary surface

File:
- `/Users/nickita/.superset/worktrees/start/second-handstand/web/src/data/node-catalog.json`

Use this for:
- quick node lookup
- node labels
- short summaries
- key bullets
- neighbor labels / ids
- top references

## Detailed evidence surface

File:
- `/Users/nickita/.superset/worktrees/start/second-handstand/web/src/data/node-evidence.json`

Use this for:
- detailed node evidence
- bullets and quotes
- packet sections
- references
- graph neighbors

Detailed packet directory:
- `/Users/nickita/.superset/worktrees/start/second-handstand/.research/agent-evidence/final`

Use final packets when:
- you need the real workflow
- you need canonical quotes
- you need examples or operational detail

Node CLI added by this skill:

```bash
python3 /Users/nickita/.codex/skills/event-master/scripts/search_event_nodes.py "partner package"
python3 /Users/nickita/.codex/skills/event-master/scripts/search_event_nodes.py "pricing proposal" --include-sections
```

Workflow for node reasoning:
1. search nodes
2. identify the best owning node
3. identify the immediate upstream and downstream nodes from neighbors / connected labels
4. open the best one to three hits
5. read the final packet if the user needs process detail
6. only then map docs/evidence into the workflow

Interpretation rule:
- do not treat one node hit as enough when the user needs execution
- figure out:
  - what usually came before
  - what usually comes after
  - what artifact proves the step is done
  - whether the step is docs-only or platform-backed

For packet-grade answers:
- use the final packet plus [packet-execution.md](packet-execution.md)
- use [databases.md](databases.md) if the node touches live platform state

When the user gives a freeform ask rather than a node name:
- use `event_ops_brief.py` first
- then confirm the owning node with `search_event_nodes.py`
- then open the strongest packet for the owning node plus any critical adjacent node
