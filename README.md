# Attendee Dossier System Private

This is a private export of the useful code and docs behind the Cerebral Valley applicant ranking and attendee dossier production system.

It contains:

- the `cv-rank` ranking pipeline
- the attendee dossier queue
- the dossier builder and prompts
- Exa research helpers
- tests and internal docs
- Daytona runner and dashboard ops code

It does not contain:

- runtime outputs
- generated dossier artifacts
- logs
- local credentials
- `.env` files
- cached model data

## Layout

- [README.md](README.md): this file
- [docs/attendee_dossier_system.md](docs/attendee_dossier_system.md): detailed architecture and operator guide
- [src/](src): core Python package code
- [scripts/](scripts): ranking, queue, dossier, Exa, and sponsor scripts
- [tests/](tests): regression and pipeline tests
- [prompts/](prompts): ranking and dossier prompts
- [ops/daytona/](ops/daytona): Daytona remote runners and shared agent code
- [ops/scripts/](ops/scripts): fleet launchers and dashboard server

## Main entry points

Ranking:

- [src/cv_rank/cli.py](src/cv_rank/cli.py)
- [README.md](README.md)

Dossier queue:

- [scripts/attendee_dossier_queue.py](scripts/attendee_dossier_queue.py)
- [scripts/build_attendee_dossiers.py](scripts/build_attendee_dossiers.py)
- [docs/attendee_dossier_system.md](docs/attendee_dossier_system.md)

Daytona ops:

- [ops/daytona/daytona_cv_rank_remote.py](ops/daytona/daytona_cv_rank_remote.py)
- [ops/daytona/daytona_plain_remote.py](ops/daytona/daytona_plain_remote.py)
- [ops/daytona/daytona_minimax_remote.py](ops/daytona/daytona_minimax_remote.py)
- [ops/daytona/daytona_wafer_remote.py](ops/daytona/daytona_wafer_remote.py)
- [ops/scripts/start_daytona_queue_pool.py](ops/scripts/start_daytona_queue_pool.py)
- [ops/scripts/serve_attendee_queue_dashboard.py](ops/scripts/serve_attendee_queue_dashboard.py)

## Setup

Install:

```bash
uv pip install -e ".[full]"
```

You will need credentials and config outside the repo for any live run:

- OpenAI-compatible model credentials
- platform Postgres access
- Supabase access if you want the legacy enrichment path
- GitHub token for profile enrichment
- Exa API keys if using Exa-backed web mode
- Claude / MiniMax / Wafer credentials if running those lanes
- Daytona credentials if using remote execution

Keep those in local env files or shell exports, not in git.

## Notes

- This export is meant to preserve the useful operational code, not every local artifact from the original worktrees.
- Some paths inside the copied ops code still reference absolute local paths from the original machine. Those should be normalized if this repo becomes the new source of truth.
