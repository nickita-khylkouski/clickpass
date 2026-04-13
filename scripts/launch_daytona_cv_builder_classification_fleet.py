#!/usr/bin/env python3
from __future__ import annotations

import argparse
import copy
import json
import sqlite3
import os
import subprocess
import sys
import math
from datetime import UTC, datetime
from pathlib import Path

from daytona_cv_builder_classification_claim_queue import seed_claim_queue, seed_completed_from_launch

ROOT = Path("/Users/nickita/.superset/worktrees/start/beaded-mind")
CV_RANK_ROOT = Path("/Users/nickita/cv-rank")
REMOTE_RUNNER = ROOT / "daytona_cv_builder_classification_remote.py"
CLAIM_WORKER = ROOT / "scripts" / "daytona_cv_builder_classification_claim_worker.py"
CLAIM_POOL = ROOT / "scripts" / "daytona_cv_builder_classification_claim_pool.py"
DEFAULT_DOSSIER_DIR = CV_RANK_ROOT / "outputs" / "attendee_dossier_queue" / "nyc-unified" / "people"
DEFAULT_OUTPUT_ROOT = CV_RANK_ROOT / "outputs" / "cv_builder_classification_daytona" / "launches"
DEFAULT_SYSTEM_PROMPT = CV_RANK_ROOT / "prompts" / "cv_builder_classification_system_prompt.txt"
DEFAULT_QUESTION_PACK = CV_RANK_ROOT / "prompts" / "cv_builder_classification_question_pack_v1.json"
DEFAULT_ENV_FILE = ROOT / ".env.daytona"
def resolve_default_daytona_python() -> Path:
    override = os.environ.get("DAYTONA_PYTHON_BIN", "").strip()
    candidates = []
    if override:
        candidates.append(Path(override).expanduser())
    candidates.extend(
        [
            Path.home() / ".venvs" / "daytona" / "bin" / "python",
            ROOT / ".venv-daytona" / "bin" / "python",
            Path(sys.executable),
        ]
    )
    for candidate in candidates:
        if candidate.exists():
            return candidate.resolve()
    return Path(sys.executable)


DEFAULT_DAYTONA_PYTHON = resolve_default_daytona_python()
DEFAULT_QUEUE_ROOT = CV_RANK_ROOT / "outputs" / "attendee_dossier_queue"
DEFAULT_QUEUE_DB = DEFAULT_QUEUE_ROOT / "queue.sqlite3"
DEFAULT_QUEUE_NAME = "nyc-unified"

PROMPT_PROFILES: dict[str, dict[str, object]] = {
    "default": {
        "description": "Default evidence-first classification prompt and question pack.",
        "system_prompt": str(DEFAULT_SYSTEM_PROMPT),
        "question_pack": str(DEFAULT_QUESTION_PACK),
    },
    "wafer-v2": {
        "description": "Wafer-oriented system prompt v2 with the standard question pack.",
        "system_prompt": str(CV_RANK_ROOT / "prompts" / "cv_builder_classification_system_prompt_wafer_v2.txt"),
        "question_pack": str(DEFAULT_QUESTION_PACK),
    },
    "wafer-v3": {
        "description": "Wafer-oriented system prompt v3 with the standard question pack.",
        "system_prompt": str(CV_RANK_ROOT / "prompts" / "cv_builder_classification_system_prompt_wafer_v3.txt"),
        "question_pack": str(DEFAULT_QUESTION_PACK),
    },
}

QUEUE_PROFILES: dict[str, dict[str, object]] = {
    "people": {
        "description": "Read dossier JSONs directly from queue_name/people.",
        "queue_source": "people",
        "dispatch_mode": "claim-pool",
    },
    "completed": {
        "description": "Read completed dossier outputs from the attendee dossier queue DB.",
        "queue_source": "completed-dossiers",
        "dispatch_mode": "claim-pool",
    },
}

FLEET_PROFILES: dict[str, dict[str, object]] = {
    "balanced": {
        "description": "General mixed lane run using claim-pool and modest packing.",
        "dispatch_mode": "claim-pool",
        "claude_count": 10,
        "codex_count": 15,
        "wafer_count": 10,
        "minimax_count": 10,
        "workers_per_sandbox": 3,
        "max_sandboxes": 25,
    },
    "claude-mini-fast": {
        "description": "Cheap Claude lane run using Haiku-style model selection and heavier packing.",
        "dispatch_mode": "claim-pool",
        "claude_count": 24,
        "codex_count": 0,
        "wafer_count": 0,
        "minimax_count": 0,
        "claude_model": "haiku",
        "claude_workers_per_sandbox": 4,
        "workers_per_sandbox": 4,
        "max_sandboxes": 8,
        "timeout_seconds": 1200,
    },
    "codex-heavy": {
        "description": "Codex-forward run for higher quality reasoning with lower packing.",
        "dispatch_mode": "claim-pool",
        "claude_count": 0,
        "codex_count": 24,
        "wafer_count": 0,
        "minimax_count": 0,
        "codex_model": "gpt-5.4",
        "codex_workers_per_sandbox": 2,
        "workers_per_sandbox": 2,
        "max_sandboxes": 12,
        "effort": "low",
    },
    "minimax-heavy": {
        "description": "MiniMax-heavy run for cheap scale with denser packing.",
        "dispatch_mode": "claim-pool",
        "claude_count": 0,
        "codex_count": 0,
        "wafer_count": 0,
        "minimax_count": 32,
        "minimax_model": "MiniMax-M2.7",
        "minimax_workers_per_sandbox": 4,
        "workers_per_sandbox": 4,
        "max_sandboxes": 10,
        "timeout_seconds": 1200,
    },
    "wafer-deep": {
        "description": "Smaller Wafer-focused pass with less packing and more time per worker.",
        "dispatch_mode": "claim-pool",
        "claude_count": 0,
        "codex_count": 0,
        "wafer_count": 12,
        "minimax_count": 0,
        "wafer_workers_per_sandbox": 2,
        "workers_per_sandbox": 2,
        "max_sandboxes": 8,
        "timeout_seconds": 2400,
    },
}

RECIPE_PROFILES: dict[str, dict[str, object]] = {
    "balanced-backfill": {
        "description": "Default completed-dossier backfill using the balanced mixed fleet.",
        "profile": "balanced",
        "prompt_profile": "default",
        "queue_profile": "completed",
        "queue_name": DEFAULT_QUEUE_NAME,
    },
    "claude-mini-cheap": {
        "description": "Cheap high-throughput completed-dossier pass using Claude mini style workers.",
        "profile": "claude-mini-fast",
        "prompt_profile": "default",
        "queue_profile": "completed",
        "queue_name": DEFAULT_QUEUE_NAME,
    },
    "partnership-scan": {
        "description": "Balanced mixed run aimed at partnership review. Override --partnership-target with the real org.",
        "profile": "balanced",
        "prompt_profile": "default",
        "queue_profile": "completed",
        "queue_name": DEFAULT_QUEUE_NAME,
        "partnership_target": "Target partner org",
    },
    "hiring-scan": {
        "description": "Balanced mixed run aimed at hire-fit review. Override --hiring-role with the role you care about.",
        "profile": "balanced",
        "prompt_profile": "default",
        "queue_profile": "completed",
        "queue_name": DEFAULT_QUEUE_NAME,
        "hiring_role": "Role to evaluate",
    },
    "wafer-audit": {
        "description": "Deeper Wafer-oriented pass over completed dossiers using the wafer-v3 prompt.",
        "profile": "wafer-deep",
        "prompt_profile": "wafer-v3",
        "queue_profile": "completed",
        "queue_name": DEFAULT_QUEUE_NAME,
    },
    "people-queue-fast": {
        "description": "Run directly from the live people queue instead of completed dossier outputs.",
        "profile": "claude-mini-fast",
        "prompt_profile": "default",
        "queue_profile": "people",
        "queue_name": DEFAULT_QUEUE_NAME,
    },
}


def utc_now() -> str:
    return datetime.now(tz=UTC).isoformat()


def timestamp_slug() -> str:
    return datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")


def load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    if not path.exists():
        return env
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def load_json_file(path: Path) -> dict[str, object]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise SystemExit(f"Config file not found: {path}") from exc
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid JSON in config file {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"Config file must contain a JSON object: {path}")
    return payload


def available_profiles_payload() -> dict[str, object]:
    return {
        "recipes": copy.deepcopy(RECIPE_PROFILES),
        "fleet_profiles": copy.deepcopy(FLEET_PROFILES),
        "prompt_profiles": copy.deepcopy(PROMPT_PROFILES),
        "queue_profiles": copy.deepcopy(QUEUE_PROFILES),
    }


def print_profiles() -> None:
    payload = available_profiles_payload()
    print(json.dumps(payload, ensure_ascii=True, indent=2))


def default_config_template() -> dict[str, object]:
    return {
        "recipe": "balanced-backfill",
        "workers_per_sandbox": 3,
        "max_sandboxes": 25,
        "partnership_target": "XYZ",
        "hiring_role": "XYZ role",
    }


def write_config_template(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(default_config_template(), ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Launch a full Daytona CV builder classification fleet.",
        epilog=(
            "Quick starts:\n"
            "  python3 scripts/launch_daytona_cv_builder_classification_fleet.py --list-profiles\n"
            "  python3 scripts/launch_daytona_cv_builder_classification_fleet.py --recipe balanced-backfill --print-plan-only\n"
            "  python3 scripts/launch_daytona_cv_builder_classification_fleet.py --profile balanced --prompt-profile default --queue-profile completed --queue-name nyc-unified --print-plan-only\n"
            "  python3 scripts/launch_daytona_cv_builder_classification_fleet.py --profile claude-mini-fast --queue-profile completed --queue-name nyc-unified --claude-count 32 --print-plan-only\n"
            "  python3 scripts/launch_daytona_cv_builder_classification_fleet.py --write-config-template scripts/daytona_classification_launch.json\n"
            "  python3 scripts/launch_daytona_cv_builder_classification_fleet.py --config-file scripts/daytona_classification_launch.json --print-plan-json"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--config-file", type=Path, default=None, help="Optional JSON config with launch defaults.")
    parser.add_argument("--write-config-template", type=Path, default=None, help="Write a starter JSON config file and exit.")
    parser.add_argument("--list-profiles", action="store_true", help="Print built-in fleet, prompt, and queue profiles, then exit.")
    parser.add_argument("--recipe", default="", help="High-level launch recipe name.")
    parser.add_argument("--profile", default="", help="Built-in fleet profile name.")
    parser.add_argument("--prompt-profile", default="", help="Built-in prompt profile name.")
    parser.add_argument("--queue-profile", default="", help="Built-in queue profile name.")
    parser.add_argument("--launch-id", default=None)
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    parser.add_argument("--daytona-python", type=Path, default=DEFAULT_DAYTONA_PYTHON)
    parser.add_argument("--local-repo-root", type=Path, default=CV_RANK_ROOT)
    parser.add_argument("--dossier-dir", type=Path, default=DEFAULT_DOSSIER_DIR)
    parser.add_argument("--failed-from-launch-id", default="", help="Optional prior launch_id to pull only failed dossiers from that launch's claim queue.")
    parser.add_argument("--queue-name", default="", help="Optional attendee_dossier_queue queue name; resolves dossier input automatically.")
    parser.add_argument("--queue-root", type=Path, default=DEFAULT_QUEUE_ROOT)
    parser.add_argument("--queue-db", type=Path, default=DEFAULT_QUEUE_DB)
    parser.add_argument(
        "--queue-source",
        choices=("people", "completed-dossiers"),
        default="people",
        help="Queue-native dossier source. 'people' uses queue_root/queue_name/people; 'completed-dossiers' reads completed job run_dirs from queue DB.",
    )
    parser.add_argument("--system-prompt", type=Path, default=DEFAULT_SYSTEM_PROMPT)
    parser.add_argument("--question-pack", type=Path, default=DEFAULT_QUESTION_PACK)
    parser.add_argument("--output-root", type=Path, default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--dispatch-mode", choices=("snapshot", "claim-queue", "claim-pool"), default="snapshot")
    parser.add_argument("--cpu", type=int, default=2)
    parser.add_argument("--memory", type=int, default=4)
    parser.add_argument("--disk", type=int, default=10)
    parser.add_argument("--auto-stop-interval", type=int, default=5)
    parser.add_argument("--auto-archive-interval", type=int, default=5)
    parser.add_argument("--auto-delete-interval", type=int, default=0)
    parser.add_argument("--claude-count", type=int, default=10)
    parser.add_argument("--codex-count", type=int, default=15)
    parser.add_argument("--wafer-count", type=int, default=10)
    parser.add_argument("--minimax-count", type=int, default=10)
    parser.add_argument("--claude-model", default="claude-sonnet-4-5")
    parser.add_argument("--codex-model", default="gpt-5.4")
    parser.add_argument("--wafer-model", default="google/gemini-2.5-pro")
    parser.add_argument("--minimax-model", default="MiniMax-M2.7")
    parser.add_argument("--tools", default="default")
    parser.add_argument("--effort", default="low")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--max-attempts", type=int, default=4)
    parser.add_argument("--retry-backoff-seconds", type=float, default=3.0)
    parser.add_argument("--partnership-target", default="XYZ")
    parser.add_argument("--hiring-role", default="XYZ role")
    parser.add_argument("--workers-per-sandbox", type=int, default=3)
    parser.add_argument("--claude-workers-per-sandbox", type=int, default=0)
    parser.add_argument("--codex-workers-per-sandbox", type=int, default=0)
    parser.add_argument("--wafer-workers-per-sandbox", type=int, default=0)
    parser.add_argument("--minimax-workers-per-sandbox", type=int, default=0)
    parser.add_argument("--max-sandboxes", type=int, default=25)
    parser.add_argument("--sandbox-prefix", default="cv-rank-classifier")
    parser.add_argument("--sandbox-start-index", type=int, default=1)
    parser.add_argument("--remote-repo-root-base", default="/home/daytona/tools/cv-rank")
    parser.add_argument("--remote-wafer-root-base", default="/home/daytona/tools/claude-wafer-share")
    parser.add_argument("--delete-stale-sandboxes", action="store_true")
    parser.add_argument("--stale-sandbox-prefix", default="cv-rank-classifier-")
    parser.add_argument("--kill-existing-controllers", action="store_true")
    parser.add_argument("--print-plan-only", action="store_true")
    parser.add_argument("--print-plan-json", action="store_true", help="Print the resolved plan as JSON and exit.")
    parser.add_argument("--claim-stale-after-seconds", type=int, default=1800)
    parser.add_argument("--claim-max-attempts", type=int, default=2)
    parser.add_argument("--seed-from-launch-id", default="")
    return parser


def provided_dests(parser: argparse.ArgumentParser, argv: list[str]) -> set[str]:
    option_to_dest: dict[str, str] = {}
    for action in parser._actions:
        for option in action.option_strings:
            option_to_dest[option] = action.dest
    seen: set[str] = set()
    for token in argv:
        if not token.startswith("--"):
            continue
        option = token.split("=", 1)[0]
        dest = option_to_dest.get(option)
        if dest:
            seen.add(dest)
    return seen


def apply_overrides(args: argparse.Namespace, overrides: dict[str, object], protected: set[str]) -> None:
    for key, value in overrides.items():
        if not hasattr(args, key) or key in protected:
            continue
        current = getattr(args, key)
        if isinstance(current, Path) and value not in ("", None):
            setattr(args, key, Path(str(value)).expanduser())
        else:
            setattr(args, key, value)


def resolve_named_profile(name: str, mapping: dict[str, dict[str, object]], *, kind: str) -> dict[str, object]:
    selected = str(name or "").strip()
    if not selected:
        return {}
    if selected not in mapping:
        available = ", ".join(sorted(mapping))
        raise SystemExit(f"Unknown {kind} profile '{selected}'. Available: {available}")
    return dict(mapping[selected])


def resolve_recipe(name: str) -> dict[str, object]:
    return resolve_named_profile(name, RECIPE_PROFILES, kind="recipe")


def parse_args() -> argparse.Namespace:
    parser = build_parser()
    args = parser.parse_args()
    if args.list_profiles:
        print_profiles()
        raise SystemExit(0)
    if args.write_config_template is not None:
        path = args.write_config_template.expanduser().resolve()
        write_config_template(path)
        print(f"wrote_config_template={path}")
        raise SystemExit(0)
    protected = provided_dests(parser, sys.argv[1:])
    config_payload: dict[str, object] = {}
    if args.config_file is not None:
        config_payload = load_json_file(args.config_file.expanduser().resolve())
        apply_overrides(args, config_payload, protected)
    apply_overrides(args, resolve_recipe(str(args.recipe or "")), protected)
    apply_overrides(args, resolve_named_profile(str(args.profile or ""), FLEET_PROFILES, kind="fleet"), protected)
    prompt_profile_payload = resolve_named_profile(str(args.prompt_profile or ""), PROMPT_PROFILES, kind="prompt")
    queue_profile_payload = resolve_named_profile(str(args.queue_profile or ""), QUEUE_PROFILES, kind="queue")
    apply_overrides(args, prompt_profile_payload, protected)
    apply_overrides(args, queue_profile_payload, protected)
    if str(args.queue_profile or "").strip() and not str(args.queue_name or "").strip():
        args.queue_name = DEFAULT_QUEUE_NAME
    setattr(args, "_resolved_config_file", str(args.config_file.expanduser().resolve()) if args.config_file is not None else "")
    setattr(args, "_resolved_config_payload", config_payload)
    return args


def collect_dossiers(dossier_dir: Path) -> list[Path]:
    paths = sorted(dossier_dir.glob("*.json"))
    if not paths:
        raise SystemExit(f"No dossier JSON files found in {dossier_dir}")
    return paths


def collect_queue_people_dossiers(*, queue_root: Path, queue_name: str) -> list[Path]:
    people_dir = queue_root / queue_name / "people"
    return collect_dossiers(people_dir)


def collect_queue_completed_dossiers(*, queue_db: Path, queue_name: str) -> list[Path]:
    if not queue_db.exists():
        raise SystemExit(f"Queue DB not found: {queue_db}")
    conn = sqlite3.connect(str(queue_db))
    try:
        rows = conn.execute(
            """
            SELECT run_dir
            FROM jobs
            WHERE queue_name = ? AND status = 'completed' AND run_dir IS NOT NULL AND run_dir <> ''
            ORDER BY priority_rank ASC, id ASC
            """,
            (queue_name,),
        ).fetchall()
    finally:
        conn.close()
    paths: list[Path] = []
    for (run_dir_value,) in rows:
        run_dir = Path(str(run_dir_value))
        dossier_dir = run_dir / "dossiers"
        if not dossier_dir.exists():
            continue
        paths.extend(sorted(dossier_dir.glob("*.json")))
    if not paths:
        raise SystemExit(f"No completed dossier JSON files found for queue={queue_name} via {queue_db}")
    deduped: dict[Path, None] = {}
    for path in paths:
        deduped[path] = None
    return sorted(deduped)


def collect_failed_dossiers_from_launch(*, output_root: Path, launch_id: str) -> list[Path]:
    launch_dir = output_root / launch_id
    queue_db = launch_dir / "claim_queue.sqlite3"
    if not queue_db.exists():
        raise SystemExit(f"Failed-launch claim queue DB not found: {queue_db}")
    conn = sqlite3.connect(str(queue_db))
    try:
        rows = conn.execute(
            """
            SELECT dossier_path
            FROM items
            WHERE status = 'failed' AND dossier_path IS NOT NULL AND dossier_path <> ''
            ORDER BY ordinal ASC, id ASC
            """
        ).fetchall()
    finally:
        conn.close()
    paths: list[Path] = []
    for (dossier_path_value,) in rows:
        path = Path(str(dossier_path_value)).expanduser().resolve()
        if path.exists():
            paths.append(path)
    if not paths:
        raise SystemExit(f"No failed dossier JSON files found for launch={launch_id} via {queue_db}")
    deduped: dict[Path, None] = {}
    for path in paths:
        deduped[path] = None
    return sorted(deduped)


def resolve_dossiers(args: argparse.Namespace) -> tuple[list[Path], dict[str, object]]:
    failed_from_launch_id = str(args.failed_from_launch_id or "").strip()
    if failed_from_launch_id:
        output_root = args.output_root.expanduser().resolve()
        dossiers = collect_failed_dossiers_from_launch(output_root=output_root, launch_id=failed_from_launch_id)
        return dossiers, {
            "mode": "failed-launch",
            "failed_from_launch_id": failed_from_launch_id,
            "output_root": str(output_root),
        }
    queue_name = str(args.queue_name or "").strip()
    if queue_name:
        queue_root = args.queue_root.expanduser().resolve()
        queue_db = args.queue_db.expanduser().resolve()
        if args.queue_source == "completed-dossiers":
            dossiers = collect_queue_completed_dossiers(queue_db=queue_db, queue_name=queue_name)
        else:
            dossiers = collect_queue_people_dossiers(queue_root=queue_root, queue_name=queue_name)
        return dossiers, {
            "mode": "queue",
            "queue_name": queue_name,
            "queue_source": str(args.queue_source),
            "queue_root": str(queue_root),
            "queue_db": str(queue_db),
        }
    dossier_dir = args.dossier_dir.expanduser().resolve()
    dossiers = collect_dossiers(dossier_dir)
    return dossiers, {
        "mode": "directory",
        "dossier_dir": str(dossier_dir),
    }


def build_lane_plan(args: argparse.Namespace) -> list[dict[str, str | int]]:
    plan: list[dict[str, str | int]] = []
    lane_specs = [
        ("claude", int(args.claude_count), str(args.claude_model)),
        ("codex", int(args.codex_count), str(args.codex_model)),
        ("wafer", int(args.wafer_count), str(args.wafer_model)),
        ("minimax", int(args.minimax_count), str(args.minimax_model)),
    ]
    for lane, count, model in lane_specs:
        if count <= 0:
            continue
        plan.append({"lane": lane, "count": count, "model": model})
    if not plan:
        raise SystemExit("At least one lane count must be > 0")
    return plan


def expand_shards(lane_plan: list[dict[str, str | int]]) -> list[tuple[str, str]]:
    shards: list[tuple[str, str]] = []
    for spec in lane_plan:
        lane = str(spec["lane"])
        model = str(spec["model"])
        count = int(spec["count"])
        for _ in range(count):
            shards.append((lane, model))
    return shards


def _lane_workers_per_sandbox(args: argparse.Namespace, lane: str) -> int:
    lane_value = int(getattr(args, f"{lane}-workers-per-sandbox".replace("-", "_")))
    if lane_value > 0:
        return max(1, lane_value)
    return max(1, int(args.workers_per_sandbox))


def compute_sandbox_plan(
    *,
    lane_plan: list[dict[str, str | int]],
    lane_workers_per_sandbox: dict[str, int],
    sandbox_prefix: str,
    sandbox_start_index: int,
) -> dict[str, list[str]]:
    plan: dict[str, list[str]] = {}
    for spec in lane_plan:
        lane = str(spec["lane"])
        count = int(spec["count"])
        lane_wps = int(lane_workers_per_sandbox[lane])
        lane_sandbox_count = max(1, math.ceil(count / lane_wps))
        names = [
            f"{sandbox_prefix}-{lane}-{sandbox_start_index + idx:02d}"
            for idx in range(lane_sandbox_count)
        ]
        plan[lane] = names
    return plan


def recommend_dispatch_mode(args: argparse.Namespace, *, input_source: dict[str, object]) -> str:
    mode = str(input_source.get("mode") or "")
    if mode in {"queue", "failed-launch"}:
        return "claim-pool"
    return "snapshot"


def build_plan_payload(
    *,
    args: argparse.Namespace,
    launch_id: str,
    launch_dir: Path,
    lane_plan: list[dict[str, str | int]],
    lane_workers_per_sandbox: dict[str, int],
    sandbox_plan: dict[str, list[str]],
    total_sandboxes: int,
    total_shards: int,
    dossiers: list[Path],
    input_source: dict[str, object],
    deleted_sandboxes: list[dict[str, str]],
) -> dict[str, object]:
    lane_summaries: list[dict[str, object]] = []
    for spec in lane_plan:
        lane = str(spec["lane"])
        count = int(spec["count"])
        lane_summaries.append(
            {
                "lane": lane,
                "model": str(spec["model"]),
                "workers": count,
                "workers_per_sandbox": int(lane_workers_per_sandbox[lane]),
                "sandbox_count": len(sandbox_plan.get(lane, [])),
                "sandbox_names": list(sandbox_plan.get(lane, [])),
            }
        )
    prompt_profile_name = str(args.prompt_profile or "").strip() or "custom"
    queue_profile_name = str(args.queue_profile or "").strip() or ""
    profile_name = str(args.profile or "").strip() or ""
    recipe_name = str(args.recipe or "").strip() or ""
    return {
        "launch_id": launch_id,
        "launch_dir": str(launch_dir),
        "dispatch_mode": str(args.dispatch_mode),
        "recommended_dispatch_mode": recommend_dispatch_mode(args, input_source=input_source),
        "recipe": recipe_name,
        "profile": profile_name,
        "prompt_profile": prompt_profile_name,
        "queue_profile": queue_profile_name,
        "system_prompt": str(args.system_prompt.expanduser().resolve()),
        "question_pack": str(args.question_pack.expanduser().resolve()),
        "total_dossiers": len(dossiers),
        "input_source": input_source,
        "total_shards": total_shards,
        "lane_workers_per_sandbox": lane_workers_per_sandbox,
        "total_sandboxes": total_sandboxes,
        "lane_summaries": lane_summaries,
        "config_file": str(getattr(args, "_resolved_config_file", "") or ""),
        "deleted_stale_sandboxes": deleted_sandboxes,
    }


def delete_stale_sandboxes(*, env: dict[str, str], prefix: str) -> list[dict[str, str]]:
    from daytona import Daytona, DaytonaConfig

    api_key = env.get("DAYTONA_API_KEY", "").strip()
    api_url = env.get("DAYTONA_API_URL", "").strip()
    if not api_key or not api_url:
        raise SystemExit("Missing DAYTONA_API_KEY or DAYTONA_API_URL while deleting stale sandboxes.")
    client = Daytona(DaytonaConfig(api_key=api_key, api_url=api_url))
    deleted: list[dict[str, str]] = []
    items = getattr(client.list(), "items", []) or []
    for item in items:
        name = str(getattr(item, "name", "") or "")
        sandbox_id = str(getattr(item, "id", "") or "")
        if not name.startswith(prefix):
            continue
        try:
            client.delete(item)
            deleted.append({"id": sandbox_id, "name": name})
        except Exception as exc:  # noqa: BLE001
            deleted.append({"id": sandbox_id, "name": name, "error": str(exc)})
    return deleted


def build_snapshot_command(
    *,
    args: argparse.Namespace,
    manifest_path: Path,
    run_id: str,
    lane: str,
    model: str,
    sandbox_name: str,
    remote_repo_root: str,
    remote_wafer_root: str,
) -> list[str]:
    daytona_python = str(args.daytona_python.expanduser())
    return [
        daytona_python,
        "-u",
        str(REMOTE_RUNNER),
        "--sandbox-name",
        sandbox_name,
        "--local-repo-root",
        str(args.local_repo_root.expanduser().resolve()),
        "--local-dossier-manifest",
        str(manifest_path),
        "--local-system-prompt",
        str(args.system_prompt.expanduser().resolve()),
        "--local-question-pack",
        str(args.question_pack.expanduser().resolve()),
        "--local-output-root",
        str(args.output_root.expanduser().resolve()),
        "--remote-repo-root",
        remote_repo_root,
        "--remote-wafer-root",
        remote_wafer_root,
        "--run-id",
        run_id,
        "--lane",
        lane,
        "--model",
        model,
        "--cpu",
        str(int(args.cpu)),
        "--memory",
        str(int(args.memory)),
        "--disk",
        str(int(args.disk)),
        "--auto-stop-interval",
        str(int(args.auto_stop_interval)),
        "--auto-archive-interval",
        str(int(args.auto_archive_interval)),
        "--auto-delete-interval",
        str(int(args.auto_delete_interval)),
        "--tools",
        str(args.tools),
        "--effort",
        str(args.effort),
        "--timeout-seconds",
        str(int(args.timeout_seconds)),
        "--max-attempts",
        str(int(args.max_attempts)),
        "--retry-backoff-seconds",
        str(float(args.retry_backoff_seconds)),
        "--partnership-target",
        str(args.partnership_target),
        "--hiring-role",
        str(args.hiring_role),
    ]


def build_claim_worker_command(
    *,
    args: argparse.Namespace,
    claim_queue_db: Path,
    launch_id: str,
    run_id: str,
    worker_id: str,
    lane: str,
    model: str,
    sandbox_name: str,
    lane_slot: int,
    remote_repo_root: str,
    remote_wafer_root: str,
) -> list[str]:
    return [
        sys.executable,
        "-u",
        str(CLAIM_WORKER),
        "--queue-db",
        str(claim_queue_db),
        "--launch-id",
        launch_id,
        "--run-id",
        run_id,
        "--worker-id",
        worker_id,
        "--lane",
        lane,
        "--model",
        model,
        "--sandbox-name",
        sandbox_name,
        "--lane-slot",
        str(int(lane_slot)),
        "--daytona-python",
        str(args.daytona_python.expanduser()),
        "--local-repo-root",
        str(args.local_repo_root.expanduser().resolve()),
        "--local-system-prompt",
        str(args.system_prompt.expanduser().resolve()),
        "--local-question-pack",
        str(args.question_pack.expanduser().resolve()),
        "--local-output-root",
        str(args.output_root.expanduser().resolve()),
        "--remote-repo-root",
        remote_repo_root,
        "--remote-wafer-root",
        remote_wafer_root,
        "--cpu",
        str(int(args.cpu)),
        "--memory",
        str(int(args.memory)),
        "--disk",
        str(int(args.disk)),
        "--auto-stop-interval",
        str(int(args.auto_stop_interval)),
        "--auto-archive-interval",
        str(int(args.auto_archive_interval)),
        "--auto-delete-interval",
        str(int(args.auto_delete_interval)),
        "--tools",
        str(args.tools),
        "--effort",
        str(args.effort),
        "--timeout-seconds",
        str(int(args.timeout_seconds)),
        "--max-attempts",
        str(int(args.max_attempts)),
        "--retry-backoff-seconds",
        str(float(args.retry_backoff_seconds)),
        "--partnership-target",
        str(args.partnership_target),
        "--hiring-role",
        str(args.hiring_role),
        "--claim-stale-after-seconds",
        str(int(args.claim_stale_after_seconds)),
        "--claim-max-attempts",
        str(int(args.claim_max_attempts)),
    ]


def build_claim_pool_command(
    *,
    args: argparse.Namespace,
    claim_queue_db: Path,
    launch_id: str,
    controller_id: str,
    lane: str,
    sandbox_name: str,
    worker_specs_path: Path,
) -> list[str]:
    return [
        str(args.daytona_python.expanduser()),
        "-u",
        str(CLAIM_POOL),
        "--queue-db",
        str(claim_queue_db),
        "--launch-id",
        launch_id,
        "--controller-id",
        controller_id,
        "--lane",
        lane,
        "--sandbox-name",
        sandbox_name,
        "--worker-specs-file",
        str(worker_specs_path),
        "--daytona-python",
        str(args.daytona_python.expanduser()),
        "--local-repo-root",
        str(args.local_repo_root.expanduser().resolve()),
        "--local-system-prompt",
        str(args.system_prompt.expanduser().resolve()),
        "--local-question-pack",
        str(args.question_pack.expanduser().resolve()),
        "--local-output-root",
        str(args.output_root.expanduser().resolve()),
        "--cpu",
        str(int(args.cpu)),
        "--memory",
        str(int(args.memory)),
        "--disk",
        str(int(args.disk)),
        "--auto-stop-interval",
        str(int(args.auto_stop_interval)),
        "--auto-archive-interval",
        str(int(args.auto_archive_interval)),
        "--auto-delete-interval",
        str(int(args.auto_delete_interval)),
        "--tools",
        str(args.tools),
        "--effort",
        str(args.effort),
        "--timeout-seconds",
        str(int(args.timeout_seconds)),
        "--max-attempts",
        str(int(args.max_attempts)),
        "--retry-backoff-seconds",
        str(float(args.retry_backoff_seconds)),
        "--partnership-target",
        str(args.partnership_target),
        "--hiring-role",
        str(args.hiring_role),
        "--claim-stale-after-seconds",
        str(int(args.claim_stale_after_seconds)),
        "--claim-max-attempts",
        str(int(args.claim_max_attempts)),
    ]


def main() -> int:
    args = parse_args()
    env_file = args.env_file.expanduser().resolve()
    base_env = os.environ.copy()
    base_env.update(load_env_file(env_file))

    launch_id = args.launch_id or f"classify-daytona-full-{timestamp_slug()}"
    launch_dir = args.output_root.expanduser().resolve() / launch_id
    manifests_dir = launch_dir / "manifests"
    logs_dir = launch_dir / "logs"
    launch_dir.mkdir(parents=True, exist_ok=True)
    manifests_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)

    lane_plan = build_lane_plan(args)
    lane_workers_per_sandbox = {
        str(spec["lane"]): _lane_workers_per_sandbox(args, str(spec["lane"]))
        for spec in lane_plan
    }
    sandbox_plan = compute_sandbox_plan(
        lane_plan=lane_plan,
        lane_workers_per_sandbox=lane_workers_per_sandbox,
        sandbox_prefix=str(args.sandbox_prefix),
        sandbox_start_index=int(args.sandbox_start_index),
    )
    total_sandboxes = sum(len(v) for v in sandbox_plan.values())
    if total_sandboxes > int(args.max_sandboxes):
        raise SystemExit(
            f"Requested fleet needs {total_sandboxes} sandboxes with lane workers-per-sandbox={lane_workers_per_sandbox}, "
            f"which exceeds max-sandboxes={int(args.max_sandboxes)}."
        )
    shard_specs = expand_shards(lane_plan)
    total_shards = len(shard_specs)
    dossiers, input_source = resolve_dossiers(args)

    deleted_sandboxes: list[dict[str, str]] = []
    if args.delete_stale_sandboxes:
        deleted_sandboxes = delete_stale_sandboxes(env=base_env, prefix=str(args.stale_sandbox_prefix))

    plan_payload = build_plan_payload(
        args=args,
        launch_id=launch_id,
        launch_dir=launch_dir,
        lane_plan=lane_plan,
        lane_workers_per_sandbox=lane_workers_per_sandbox,
        sandbox_plan=sandbox_plan,
        total_sandboxes=total_sandboxes,
        total_shards=total_shards,
        dossiers=dossiers,
        input_source=input_source,
        deleted_sandboxes=deleted_sandboxes,
    )

    if args.print_plan_only:
        print(f"launch_id={plan_payload['launch_id']}")
        print(f"launch_dir={plan_payload['launch_dir']}")
        if str(plan_payload["recipe"]):
            print(f"recipe={plan_payload['recipe']}")
        print(f"profile={plan_payload['profile']}")
        print(f"prompt_profile={plan_payload['prompt_profile']}")
        if str(plan_payload["queue_profile"]):
            print(f"queue_profile={plan_payload['queue_profile']}")
        print(f"dispatch_mode={plan_payload['dispatch_mode']}")
        print(f"recommended_dispatch_mode={plan_payload['recommended_dispatch_mode']}")
        if str(args.seed_from_launch_id).strip():
            print(f"seed_from_launch_id={str(args.seed_from_launch_id).strip()}")
        print(f"system_prompt={plan_payload['system_prompt']}")
        print(f"question_pack={plan_payload['question_pack']}")
        print(f"total_dossiers={plan_payload['total_dossiers']}")
        print(f"input_source={json.dumps(input_source, ensure_ascii=True, sort_keys=True)}")
        print(f"total_shards={plan_payload['total_shards']}")
        print(f"lane_workers_per_sandbox={json.dumps(lane_workers_per_sandbox, ensure_ascii=True)}")
        print(f"total_sandboxes={plan_payload['total_sandboxes']}")
        for lane_summary in plan_payload["lane_summaries"]:
            names = ",".join(lane_summary["sandbox_names"])
            print(
                f"lane={lane_summary['lane']} model={lane_summary['model']} workers={lane_summary['workers']} "
                f"workers_per_sandbox={lane_summary['workers_per_sandbox']} sandboxes={lane_summary['sandbox_count']} names={names}"
            )
        return 0

    if args.print_plan_json:
        print(json.dumps(plan_payload, ensure_ascii=True, indent=2))
        return 0

    if args.kill_existing_controllers:
        subprocess.run(
            ["pkill", "-f", "daytona_cv_builder_classification_remote.py"],
            check=False,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        for pattern in (
            "daytona_cv_builder_classification_claim_worker.py",
            "daytona_cv_builder_classification_claim_pool.py",
        ):
            subprocess.run(
                ["pkill", "-f", pattern],
                check=False,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
    claim_queue_db = launch_dir / "claim_queue.sqlite3"
    seed_result: dict[str, int] = {"seeded_completed": 0, "scanned_outputs": 0}
    manifest_paths: list[Path] = []
    if args.dispatch_mode == "snapshot":
        for shard_index, (lane, _model) in enumerate(shard_specs):
            shard_paths = [str(p) for idx, p in enumerate(dossiers) if idx % total_shards == shard_index]
            manifest_path = manifests_dir / f"shard-{shard_index:02d}-{lane}.json"
            manifest_path.write_text(json.dumps(shard_paths, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
            manifest_paths.append(manifest_path)
    else:
        seed_claim_queue(claim_queue_db, dossiers)
        source_launch_id = str(args.seed_from_launch_id or "").strip()
        if source_launch_id:
            seed_result = seed_completed_from_launch(
                claim_queue_db,
                source_output_root=args.output_root.expanduser().resolve(),
                source_launch_id=source_launch_id,
                dossier_paths=dossiers,
            )

    runs: list[dict[str, object]] = []
    controllers: list[dict[str, object]] = []
    controller_specs_dir = launch_dir / "controller_specs"
    controller_specs_dir.mkdir(parents=True, exist_ok=True)
    lane_worker_index: dict[str, int] = {}
    pool_groups: dict[tuple[str, str], list[dict[str, object]]] = {}
    for shard_index, (lane, model) in enumerate(shard_specs):
        run_id = f"{launch_id}-{lane}-s{shard_index:02d}"
        log_path = logs_dir / f"{run_id}.log"
        lane_index = lane_worker_index.get(lane, 0)
        lane_wps = int(lane_workers_per_sandbox[lane])
        sandbox_name = sandbox_plan[lane][lane_index // lane_wps]
        lane_slot = (lane_index % lane_wps) + 1
        lane_worker_index[lane] = lane_index + 1
        remote_repo_root = f"{str(args.remote_repo_root_base).rstrip('/')}/{run_id}"
        remote_wafer_root = f"{str(args.remote_wafer_root_base).rstrip('/')}/{run_id}"
        manifest_path: Path | None = None
        if args.dispatch_mode == "snapshot":
            manifest_path = manifest_paths[shard_index]
            cmd = build_snapshot_command(
                args=args,
                manifest_path=manifest_path,
                run_id=run_id,
                lane=lane,
                model=model,
                sandbox_name=sandbox_name,
                remote_repo_root=remote_repo_root,
                remote_wafer_root=remote_wafer_root,
            )
            env = base_env.copy()
            env["DAYTONA_PYTHON_BIN"] = str(args.daytona_python.expanduser())
            if lane == "codex":
                env.setdefault("CODEX_HOME", str(Path.home() / ".codex"))

            with log_path.open("ab") as log_file:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=log_file,
                    env=env,
                    start_new_session=True,
                    close_fds=True,
                )
            pid = proc.pid
            print(
                f"{proc.pid} lane={lane} shard={shard_index:02d} run_id={run_id} "
                f"sandbox={sandbox_name} slot={lane_slot} log={log_path}"
            )
        elif args.dispatch_mode == "claim-queue":
            cmd = build_claim_worker_command(
                args=args,
                claim_queue_db=claim_queue_db,
                launch_id=launch_id,
                run_id=run_id,
                worker_id=f"{lane}-worker-{shard_index:02d}",
                lane=lane,
                model=model,
                sandbox_name=sandbox_name,
                lane_slot=lane_slot,
                remote_repo_root=remote_repo_root,
                remote_wafer_root=remote_wafer_root,
            )
            env = base_env.copy()
            env["DAYTONA_PYTHON_BIN"] = str(args.daytona_python.expanduser())
            if lane == "codex":
                env.setdefault("CODEX_HOME", str(Path.home() / ".codex"))

            with log_path.open("ab") as log_file:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=log_file,
                    env=env,
                    start_new_session=True,
                    close_fds=True,
                )
            pid = proc.pid
            print(
                f"{proc.pid} lane={lane} shard={shard_index:02d} run_id={run_id} "
                f"sandbox={sandbox_name} slot={lane_slot} log={log_path}"
            )
        else:
            controller_id = f"{launch_id}-{lane}-{sandbox_name}"
            controller_log = logs_dir / f"{controller_id}.log"
            pool_groups.setdefault((lane, sandbox_name), []).append(
                {
                    "shard": shard_index,
                    "lane": lane,
                    "model": model,
                    "lane_worker_index": lane_index,
                    "lane_slot": lane_slot,
                    "sandbox_name": sandbox_name,
                    "run_id": run_id,
                    "worker_id": f"{lane}-worker-{shard_index:02d}",
                    "dispatch_mode": str(args.dispatch_mode),
                    "manifest": "",
                    "manifest_count": 0,
                    "claim_queue_db": str(claim_queue_db),
                    "log": str(log_path),
                    "controller_id": controller_id,
                    "controller_log": str(controller_log),
                    "remote_repo_root": remote_repo_root,
                    "remote_wafer_root": remote_wafer_root,
                }
            )
            pid = None

        runs.append(
            {
                "shard": shard_index,
                "lane": lane,
                "model": model,
                "lane_worker_index": lane_index,
                "lane_slot": lane_slot,
                "sandbox_name": sandbox_name,
                "run_id": run_id,
                "dispatch_mode": str(args.dispatch_mode),
                "manifest": str(manifest_path) if manifest_path is not None else "",
                "manifest_count": len(json.loads(manifest_path.read_text(encoding="utf-8"))) if manifest_path is not None else 0,
                "claim_queue_db": str(claim_queue_db) if args.dispatch_mode != "snapshot" else "",
                "log": str(log_path),
                "pid": pid,
                "launched_at_utc": utc_now(),
                **({"controller_id": f"{launch_id}-{lane}-{sandbox_name}"} if args.dispatch_mode == "claim-pool" else {}),
            }
        )

    if args.dispatch_mode == "claim-pool":
        for (lane, sandbox_name), worker_specs in sorted(pool_groups.items()):
            controller_id = str(worker_specs[0]["controller_id"])
            worker_specs_path = controller_specs_dir / f"{controller_id}.json"
            worker_specs_path.write_text(json.dumps(worker_specs, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
            controller_log = Path(str(worker_specs[0]["controller_log"]))
            cmd = build_claim_pool_command(
                args=args,
                claim_queue_db=claim_queue_db,
                launch_id=launch_id,
                controller_id=controller_id,
                lane=lane,
                sandbox_name=sandbox_name,
                worker_specs_path=worker_specs_path,
            )
            env = base_env.copy()
            env["DAYTONA_PYTHON_BIN"] = str(args.daytona_python.expanduser())
            if lane == "codex":
                env.setdefault("CODEX_HOME", str(Path.home() / ".codex"))
            with controller_log.open("ab") as log_file:
                proc = subprocess.Popen(
                    cmd,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=log_file,
                    env=env,
                    start_new_session=True,
                    close_fds=True,
                )
            controllers.append(
                {
                    "controller_id": controller_id,
                    "lane": lane,
                    "sandbox_name": sandbox_name,
                    "worker_specs_file": str(worker_specs_path),
                    "worker_count": len(worker_specs),
                    "run_ids": [str(spec["run_id"]) for spec in worker_specs],
                    "log": str(controller_log),
                    "pid": proc.pid,
                    "launched_at_utc": utc_now(),
                }
            )
            for spec in worker_specs:
                print(
                    f"{proc.pid} lane={lane} sandbox={sandbox_name} controller={controller_id} "
                    f"slot={int(spec['lane_slot'])} run_id={spec['run_id']} log={controller_log}"
                )

    launch_meta = {
        "launch_id": launch_id,
        "created_at_utc": utc_now(),
        "dispatch_mode": str(args.dispatch_mode),
        "total_dossiers": len(dossiers),
        "total_shards": total_shards,
        "workers_per_sandbox": int(args.workers_per_sandbox),
        "lane_workers_per_sandbox": lane_workers_per_sandbox,
        "total_sandboxes": total_sandboxes,
        "input_source": input_source,
        "claim_queue_db": str(claim_queue_db) if args.dispatch_mode != "snapshot" else "",
        "seed_from_launch_id": str(args.seed_from_launch_id or "").strip(),
        "seed_result": seed_result if args.dispatch_mode in {"claim-queue", "claim-pool"} else {},
        "config": {
            "env_file": str(env_file),
            "config_file": str(getattr(args, "_resolved_config_file", "") or ""),
            "config_payload": getattr(args, "_resolved_config_payload", {}),
            "recipe": str(args.recipe or "").strip(),
            "profile": str(args.profile or "").strip(),
            "prompt_profile": str(args.prompt_profile or "").strip(),
            "queue_profile": str(args.queue_profile or "").strip(),
            "daytona_python": str(args.daytona_python.expanduser()),
            "local_repo_root": str(args.local_repo_root.expanduser().resolve()),
            "system_prompt": str(args.system_prompt.expanduser().resolve()),
            "question_pack": str(args.question_pack.expanduser().resolve()),
            "output_root": str(args.output_root.expanduser().resolve()),
            "remote_repo_root_base": str(args.remote_repo_root_base),
            "remote_wafer_root_base": str(args.remote_wafer_root_base),
            "cpu": int(args.cpu),
            "memory": int(args.memory),
            "disk": int(args.disk),
            "auto_stop_interval": int(args.auto_stop_interval),
            "auto_archive_interval": int(args.auto_archive_interval),
            "auto_delete_interval": int(args.auto_delete_interval),
            "tools": str(args.tools),
            "effort": str(args.effort),
            "timeout_seconds": int(args.timeout_seconds),
            "max_attempts": int(args.max_attempts),
            "retry_backoff_seconds": float(args.retry_backoff_seconds),
            "partnership_target": str(args.partnership_target),
            "hiring_role": str(args.hiring_role),
            "dispatch_mode": str(args.dispatch_mode),
            "claim_stale_after_seconds": int(args.claim_stale_after_seconds),
            "claim_max_attempts": int(args.claim_max_attempts),
            "seed_from_launch_id": str(args.seed_from_launch_id or "").strip(),
        },
        "sandbox_plan": sandbox_plan,
        "lane_plan": lane_plan,
        "controllers": controllers,
        "deleted_stale_sandboxes": deleted_sandboxes,
        "plan": plan_payload,
        "runs": runs,
    }
    (launch_dir / "launch_meta.json").write_text(json.dumps(launch_meta, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")
    (args.output_root.expanduser().resolve() / "LATEST_CLASSIFY_LAUNCH.txt").write_text(launch_id + "\n", encoding="utf-8")
    print(f"launch_dir={launch_dir}")
    print(f"launch_meta={launch_dir / 'launch_meta.json'}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
