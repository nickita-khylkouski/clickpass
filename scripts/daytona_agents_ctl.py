#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import signal
import subprocess
import sys
from pathlib import Path
from typing import Any

from daytona_agent_leases import (
    activate_launch,
    connect_leases,
    heartbeat_launch,
    list_launches,
    list_sandboxes,
    pool_snapshot,
    release_launch,
    reserve_launch,
    stale_launch_ids,
)
from daytona_agent_task_queue import cancel_unfinished_tasks, queue_counts, seed_task_queue


ROOT = Path(__file__).resolve().parent
WORKSPACE_ROOT = ROOT.parent
if str(WORKSPACE_ROOT) not in sys.path:
    sys.path.insert(0, str(WORKSPACE_ROOT))
CLASSIFICATION_LAUNCH = ROOT / "launch_daytona_cv_builder_classification_fleet.py"
CLASSIFICATION_STATUS = ROOT / "monitor_daytona_cv_builder_classification_launch.py"
CLASSIFICATION_STOP = ROOT / "stop_daytona_cv_builder_classification_launch.py"
PROMPT_POOL = ROOT / "daytona_prompt_batch_claim_pool.py"

DEFAULT_LEASE_DB = WORKSPACE_ROOT / "data" / "daytona_leases.sqlite3"
DEFAULT_PROMPT_OUTPUT_ROOT = WORKSPACE_ROOT / "outputs" / "daytona_agents" / "launches"
DEFAULT_CLASSIFICATION_OUTPUT_ROOT = Path("/Users/nickita/cv-rank/outputs/cv_builder_classification_daytona/launches")
DEFAULT_DAYTONA_PYTHON = Path(os.environ.get("DAYTONA_PYTHON_BIN") or sys.executable).expanduser().resolve()
DEFAULT_ENV_FILE = WORKSPACE_ROOT / ".env.daytona"
DEFAULT_MAX_DISK_GB = int(os.environ.get("DAYTONA_MAX_DISK_GB", "10"))
DEFAULT_MAX_CPU = int(os.environ.get("DAYTONA_MAX_CPU", "0"))
DEFAULT_MAX_MEMORY_GB = int(os.environ.get("DAYTONA_MAX_MEMORY_GB", "0"))

TASK_PROFILES: dict[str, dict[str, object]] = {
    "prompt-batch": {
        "description": "Generic prompt/task batch with native local queueing and one controller per sandbox.",
        "runtime_support": ["codex", "wafer", "claude", "minimax"],
        "default_output_root": str(DEFAULT_PROMPT_OUTPUT_ROOT),
        "default_dispatch_mode": "claim-pool",
    },
    "cv-classification": {
        "description": "Adapter over the existing Daytona CV builder classification fleet launcher.",
        "runtime_support": ["claude", "codex", "wafer", "minimax"],
        "default_output_root": str(DEFAULT_CLASSIFICATION_OUTPUT_ROOT),
        "default_dispatch_mode": "claim-pool",
    },
}

RUNTIME_PROFILES: dict[str, dict[str, object]] = {
    "codex-fast": {
        "description": "Cheap Codex pool for repeated prompt work.",
        "runtime": "codex",
        "model": "gpt-5.4-mini",
        "tools": "default",
        "effort": "low",
        "web_mode": "exa",
        "workers": 12,
        "workers_per_sandbox": 3,
        "cpu": 2,
        "memory": 4,
        "disk": 10,
        "auto_stop_interval": 5,
        "auto_archive_interval": 5,
        "auto_delete_interval": 15,
    },
    "codex-quality": {
        "description": "Higher-quality Codex prompt pool with lighter packing.",
        "runtime": "codex",
        "model": "gpt-5.4",
        "tools": "default",
        "effort": "medium",
        "web_mode": "exa",
        "workers": 8,
        "workers_per_sandbox": 2,
        "cpu": 2,
        "memory": 4,
        "disk": 10,
        "auto_stop_interval": 5,
        "auto_archive_interval": 5,
        "auto_delete_interval": 15,
    },
    "wafer-fast": {
        "description": "Fast Wafer prompt pool with Exa-enabled web access.",
        "runtime": "wafer",
        "model": "Qwen3.5-397B-A17B",
        "tools": "default",
        "effort": "low",
        "web_mode": "exa",
        "workers": 10,
        "workers_per_sandbox": 1,
        "cpu": 4,
        "memory": 8,
        "disk": 10,
        "auto_stop_interval": 5,
        "auto_archive_interval": 5,
        "auto_delete_interval": 15,
        "timeout_seconds": 2700,
    },
    "claude-fast": {
        "description": "Direct Claude Code prompt pool with light packing.",
        "runtime": "claude",
        "model": "claude-sonnet-4-5",
        "tools": "default",
        "effort": "low",
        "web_mode": "claude",
        "workers": 8,
        "workers_per_sandbox": 1,
        "cpu": 2,
        "memory": 4,
        "disk": 10,
        "auto_stop_interval": 5,
        "auto_archive_interval": 5,
        "auto_delete_interval": 15,
        "timeout_seconds": 2700,
    },
    "minimax-fast": {
        "description": "MiniMax-backed Claude prompt pool for cheap repeated work.",
        "runtime": "minimax",
        "model": "MiniMax-M2.7",
        "tools": "default",
        "effort": "low",
        "web_mode": "claude",
        "workers": 12,
        "workers_per_sandbox": 1,
        "cpu": 2,
        "memory": 4,
        "disk": 10,
        "auto_stop_interval": 5,
        "auto_archive_interval": 5,
        "auto_delete_interval": 15,
        "timeout_seconds": 2700,
    },
}

POOL_PROFILES: dict[str, dict[str, object]] = {
    "default": {
        "description": "General shared Daytona pool.",
        "pool_name": "default",
        "pool_limit": 25,
    },
    "high-capacity": {
        "description": "Larger shared pool for heavy overnight runs.",
        "pool_name": "default",
        "pool_limit": 40,
    },
}


def timestamp_slug() -> str:
    from datetime import UTC, datetime

    return datetime.now(tz=UTC).strftime("%Y%m%dT%H%M%SZ")


def print_json(payload: object) -> None:
    print(json.dumps(payload, ensure_ascii=True, indent=2))


def write_json(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def read_json(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


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


def normalize_name_slug(value: str) -> str:
    out = "".join(ch.lower() if ch.isalnum() else "-" for ch in value.strip())
    out = "-".join(part for part in out.split("-") if part)
    return out or "run"


def resolve_daytona_python() -> Path:
    override = os.environ.get("DAYTONA_PYTHON_BIN", "").strip()
    candidates = []
    if override:
        candidates.append(Path(override).expanduser())
    candidates.extend(
        [
            Path.home() / ".venvs" / "daytona" / "bin" / "python",
            WORKSPACE_ROOT / ".venv-daytona" / "bin" / "python",
            DEFAULT_DAYTONA_PYTHON,
            Path(sys.executable),
        ]
    )
    for candidate in candidates:
        if not candidate.exists():
            continue
        probe = subprocess.run(
            [str(candidate), "-c", "import daytona"],  # noqa: S607
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        if probe.returncode == 0:
            return candidate.expanduser()
    return Path(sys.executable)


def load_named_profile(name: str, mapping: dict[str, dict[str, object]], kind: str) -> dict[str, object]:
    selected = str(name or "").strip()
    if not selected:
        return {}
    if selected not in mapping:
        available = ", ".join(sorted(mapping))
        raise SystemExit(f"Unknown {kind} profile '{selected}'. Available: {available}")
    return dict(mapping[selected])


def default_output_root_for(task_profile: str) -> Path:
    payload = TASK_PROFILES.get(task_profile) or {}
    return Path(str(payload.get("default_output_root") or DEFAULT_PROMPT_OUTPUT_ROOT)).expanduser().resolve()


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generic Daytona agent control plane with shared sandbox lease management.",
        epilog=(
            "Task profiles:\n"
            "  prompt-batch      Generic queue of prompts/tasks with one result file per task. Supports codex, wafer, claude, and minimax runtimes.\n"
            "  cv-classification Adapter over the existing mixed-lane classification fleet. Use this today for Claude/Codex/Wafer/MiniMax.\n"
            "\n"
            "Prompt-batch task sources:\n"
            "  --prompt-text / --prompt-file  Repeat one prompt N times via --repeat.\n"
            "  --task-file                   JSON object, JSON list, or {\"tasks\":[...]} payload.\n"
            "  --task-dir                    Directory of task JSON files.\n"
            "\n"
            "Prompt-batch task schema:\n"
            "  {\"task_id\":\"task-001\",\"prompt_text\":\"...\",\"task_type\":\"prompt\",\"workdir\":\"optional\",\"input_ref\":\"...\",\"output_ref\":\"results/task-001.txt\",\"agent_mode\":\"ask|start|send\",\"session_alias\":\"optional\",\"session_id\":\"optional\",\"create_session\":false,\"append_output\":false,\"allowed_tools\":\"optional\",\"disallowed_tools\":\"optional\",\"metadata\":{...}}\n"
            "\n"
            "Outputs:\n"
            "  prompt-batch writes per-task output text plus per-task metadata JSON under outputs/daytona_agents/launches/<launch_id>/results.\n"
            "  cv-classification writes its normal launch_meta, logs, and classification outputs under the existing cv-rank launch root.\n"
            "\n"
            "Capacity model:\n"
            "  Every launch reserves sandboxes in data/daytona_leases.sqlite3 before launch. If the pool is full, launch is refused instead of oversubscribing.\n"
            "  Codex can be densely packed. Wafer/Claude/MiniMax default to one worker per sandbox unless --allow-shared-sandbox-runtime is set.\n"
            "\n"
            "Telemetry:\n"
            "  status reports queue counts, controller liveness, failed task details, and prompt-batch health buckets like usage_exhausted/auth_error/transport_error/setup_error/model_error/runner_error based on logs and queue artifacts.\n"
            "\n"
            "Examples:\n"
            "  python3 scripts/daytona_agents_ctl.py profiles\n"
            "  python3 scripts/daytona_agents_ctl.py capacity\n"
            "  python3 scripts/daytona_agents_ctl.py dashboard --port 8793\n"
            "  python3 scripts/daytona_agents_ctl.py plan --task-profile prompt-batch --runtime-profile codex-fast --prompt-file prompts/research.txt --repeat 50\n"
            "  python3 scripts/daytona_agents_ctl.py launch --task-profile prompt-batch --runtime-profile codex-fast --prompt-file prompts/research.txt --repeat 50\n"
            "  python3 scripts/daytona_agents_ctl.py launch --task-profile prompt-batch --runtime-profile wafer-fast --prompt-file data/startup_ai_deal_snipe_prompt_v1.md --repeat 10\n"
            "  python3 scripts/daytona_agents_ctl.py launch --task-profile prompt-batch --runtime-profile codex-fast --task-file tasks/research_batch.json\n"
            "  python3 scripts/daytona_agents_ctl.py launch --task-profile prompt-batch --runtime-profile minimax-fast --task-file tasks/session_batch.json\n"
            "  python3 scripts/daytona_agents_ctl.py plan --task-profile cv-classification --pool-profile default -- --recipe balanced-backfill --print-plan-json\n"
            "  python3 scripts/daytona_agents_ctl.py launch --task-profile cv-classification --pool-profile default -- --recipe balanced-backfill\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("profiles", help="Print built-in task, runtime, and pool profiles.")
    capacity = sub.add_parser("capacity", help="Show shared sandbox pool capacity.")
    capacity.add_argument("--db-path", type=Path, default=DEFAULT_LEASE_DB)
    leases = sub.add_parser("leases", help="Show active or recent launch leases.")
    leases.add_argument("--db-path", type=Path, default=DEFAULT_LEASE_DB)

    for name in ("plan", "launch"):
        cmd = sub.add_parser(name, help=f"{name.title()} a Daytona launch.")
        add_launch_args(cmd)
        cmd.add_argument("adapter_args", nargs=argparse.REMAINDER, help="For cv-classification, pass raw adapter args after --.")

    status = sub.add_parser("status", help="Show launch status.")
    status.add_argument("--db-path", type=Path, default=DEFAULT_LEASE_DB)
    status.add_argument("--launch-id", required=True)
    status.add_argument("--json", action="store_true")

    stop = sub.add_parser("stop", help="Stop a launch and release its lease.")
    stop.add_argument("--db-path", type=Path, default=DEFAULT_LEASE_DB)
    stop.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    stop.add_argument("--launch-id", required=True)
    stop.add_argument("--delete-sandboxes", action="store_true")

    reap = sub.add_parser("reap", help="Release stale or completed launches and optionally delete sandboxes.")
    reap.add_argument("--db-path", type=Path, default=DEFAULT_LEASE_DB)
    reap.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE)
    reap.add_argument("--stale-after-seconds", type=int, default=3600)
    reap.add_argument("--delete-sandboxes", action="store_true")

    logs = sub.add_parser("logs", help="Print log paths for a launch.")
    logs.add_argument("--db-path", type=Path, default=DEFAULT_LEASE_DB)
    logs.add_argument("--launch-id", required=True)
    logs.add_argument("--tail", action="store_true")

    dashboard = sub.add_parser("dashboard", help="Serve the generic Daytona agents dashboard.")
    dashboard.add_argument("--db-path", type=Path, default=DEFAULT_LEASE_DB)
    dashboard.add_argument("--port", type=int, default=8793)
    dashboard.add_argument("--launch-limit", type=int, default=40)
    dashboard.add_argument("--recent-file-limit", type=int, default=30)
    dashboard.add_argument("--active-only", action="store_true")
    return parser


def add_launch_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--db-path", type=Path, default=DEFAULT_LEASE_DB)
    parser.add_argument("--task-profile", choices=sorted(TASK_PROFILES), default="prompt-batch", help="Top-level task family. prompt-batch is generic queued prompts; cv-classification wraps the existing mixed-lane classifier.")
    parser.add_argument("--runtime-profile", default="", help="Named runtime defaults. Currently used by prompt-batch.")
    parser.add_argument("--pool-profile", default="default", help="Named pool/capacity defaults.")
    parser.add_argument("--launch-id", default="", help="Optional launch id. Auto-generated if omitted.")
    parser.add_argument("--pool-name", default="", help="Shared sandbox pool name. Defaults from --pool-profile.")
    parser.add_argument("--pool-limit", type=int, default=0, help="Max concurrently reserved sandboxes in the pool.")
    parser.add_argument("--sandbox-prefix", default="daytona-agent", help="Prefix used when generating sandbox names.")
    parser.add_argument("--output-root", type=Path, default=None, help="Launch output root. Defaults per task profile.")
    parser.add_argument("--env-file", type=Path, default=DEFAULT_ENV_FILE, help="Env file merged into launch env for prompt-batch runs.")
    parser.add_argument("--runtime", default="", help="Runtime for prompt-batch: codex, wafer, claude, or minimax.")
    parser.add_argument("--model", default="", help="Model name for the runtime.")
    parser.add_argument("--tools", default="", help="Tool preset passed to the runtime worker.")
    parser.add_argument("--effort", default="", help="Reasoning effort passed to the runtime worker.")
    parser.add_argument("--web-mode", default="", help="Web mode for runtime workers. Typical values: exa, mixed, claude.")
    parser.add_argument("--workers", type=int, default=0, help="Total logical workers.")
    parser.add_argument("--workers-per-sandbox", type=int, default=0, help="How many workers to pack into each sandbox.")
    parser.add_argument("--allow-shared-sandbox-runtime", action="store_true", help="Allow packing multiple prompt workers into one sandbox for shared-state runtimes like wafer/claude/minimax.")
    parser.add_argument("--cpu", type=int, default=0, help="CPU per sandbox.")
    parser.add_argument("--memory", type=int, default=0, help="Memory per sandbox in GB.")
    parser.add_argument("--disk", type=int, default=0, help="Disk per sandbox in GB.")
    parser.add_argument("--auto-stop-interval", type=int, default=0, help="Daytona auto-stop interval in minutes.")
    parser.add_argument("--auto-archive-interval", type=int, default=0, help="Daytona auto-archive interval in minutes.")
    parser.add_argument("--auto-delete-interval", type=int, default=0, help="Daytona auto-delete interval in minutes.")
    parser.add_argument("--timeout-seconds", type=int, default=0, help="Per-task remote runtime timeout.")
    parser.add_argument("--prompt-file", type=Path, default=None, help="Prompt file for repeated prompt-batch launches.")
    parser.add_argument("--prompt-text", default="", help="Inline prompt text for repeated prompt-batch launches.")
    parser.add_argument("--task-file", type=Path, default=None, help="JSON file containing one task, a task list, or {tasks:[...]}.")
    parser.add_argument("--task-dir", type=Path, default=None, help="Directory of task JSON files.")
    parser.add_argument("--repeat", type=int, default=1, help="Repeat count when using --prompt-file/--prompt-text.")
    parser.add_argument("--workdir", default="", help="Remote workdir override for prompt-batch tasks. Defaults to a runtime-safe workspace.")
    parser.add_argument("--print-plan-json", action="store_true")


def apply_profile_defaults(args: argparse.Namespace) -> argparse.Namespace:
    runtime_profile = load_named_profile(args.runtime_profile, RUNTIME_PROFILES, "runtime")
    pool_profile = load_named_profile(args.pool_profile, POOL_PROFILES, "pool")
    for key, value in runtime_profile.items():
        if key == "description":
            continue
        current = getattr(args, key, None)
        if isinstance(current, int) and current == 0:
            setattr(args, key, value)
        elif isinstance(current, str) and current == "":
            setattr(args, key, value)
    for key, value in pool_profile.items():
        if key == "description":
            continue
        current = getattr(args, key, None)
        if isinstance(current, int) and current == 0:
            setattr(args, key, value)
        elif isinstance(current, str) and current == "":
            setattr(args, key, value)
    if args.output_root is None:
        args.output_root = default_output_root_for(str(args.task_profile))
    else:
        args.output_root = args.output_root.expanduser().resolve()
    if not str(args.launch_id or "").strip():
        args.launch_id = f"{normalize_name_slug(str(args.task_profile))}-{timestamp_slug()}"
    return args


def plan_sandbox_names(*, launch_id: str, sandbox_prefix: str, runtime_or_lane: str, count: int) -> list[str]:
    normalized_launch = normalize_name_slug(launch_id)
    short = normalized_launch[-18:]
    digest = hashlib.sha1(normalized_launch.encode("utf-8")).hexdigest()[:6]
    lane = normalize_name_slug(runtime_or_lane)
    prefix = f"{normalize_name_slug(sandbox_prefix)}-{short}-{digest}-{lane}"
    return [f"{prefix}-{idx:02d}" for idx in range(1, count + 1)]


def classification_adapter_command(args: argparse.Namespace, *, for_plan: bool) -> list[str]:
    auto_delete = args.auto_delete_interval or 15
    env_file = args.env_file.expanduser().resolve()
    env_payload = load_env_file(env_file)
    daytona_python = str(env_payload.get("DAYTONA_PYTHON_BIN") or "").strip()
    if not daytona_python:
        daytona_python = str(resolve_daytona_python())
    cmd = [
        sys.executable,
        str(CLASSIFICATION_LAUNCH),
        "--launch-id",
        str(args.launch_id),
        "--sandbox-prefix",
        f"{normalize_name_slug(str(args.sandbox_prefix))}-{normalize_name_slug(str(args.launch_id))[-12:]}",
        "--auto-delete-interval",
        str(int(auto_delete)),
        "--env-file",
        str(env_file),
        "--daytona-python",
        daytona_python,
    ]
    if args.adapter_args and args.adapter_args[0] == "--":
        cmd.extend(args.adapter_args[1:])
    else:
        cmd.extend(args.adapter_args)
    if for_plan:
        cmd.append("--print-plan-json")
    return cmd


def build_classification_plan(args: argparse.Namespace) -> dict[str, object]:
    cmd = classification_adapter_command(args, for_plan=True)
    proc = subprocess.run(
        cmd,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(proc.stderr.strip() or proc.stdout.strip() or "classification plan failed")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"classification plan returned invalid JSON: {exc}\n{proc.stdout}") from exc
    sandbox_specs: list[dict[str, object]] = []
    for lane_summary in list(payload.get("lane_summaries") or []):
        lane = str(lane_summary.get("lane") or "")
        for sandbox_name in list(lane_summary.get("sandbox_names") or []):
            sandbox_specs.append({"sandbox_name": str(sandbox_name), "lane": lane})
    return {
        "task_profile": "cv-classification",
        "launch_id": str(payload["launch_id"]),
        "launch_dir": str(payload["launch_dir"]),
        "output_root": str(Path(str(payload["launch_dir"])).parent),
        "total_tasks": int(payload.get("total_dossiers") or 0),
        "total_workers": int(payload.get("total_shards") or 0),
        "workers_per_sandbox": dict(payload.get("lane_workers_per_sandbox") or {}),
        "sandbox_specs": sandbox_specs,
        "adapter_plan": payload,
        "adapter_cmd": cmd,
        "pool_name": str(args.pool_name),
        "pool_limit": int(args.pool_limit),
    }


def _load_task_file_payload(path: Path) -> list[dict[str, object]]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    if isinstance(payload, dict):
        if isinstance(payload.get("tasks"), list):
            return [dict(item) for item in payload["tasks"] if isinstance(item, dict)]
        return [dict(payload)]
    if isinstance(payload, list):
        return [dict(item) for item in payload if isinstance(item, dict)]
    raise SystemExit(f"Unsupported task payload in {path}")


def resolve_prompt_tasks(args: argparse.Namespace) -> list[dict[str, object]]:
    raw_tasks: list[dict[str, object]] = []
    if args.task_file is not None:
        raw_tasks.extend(_load_task_file_payload(args.task_file.expanduser().resolve()))
    elif args.task_dir is not None:
        for path in sorted(args.task_dir.expanduser().resolve().glob("*.json")):
            raw_tasks.extend(_load_task_file_payload(path))
    else:
        prompt_text = str(args.prompt_text or "")
        prompt_ref = ""
        if args.prompt_file is not None:
            prompt_path = args.prompt_file.expanduser().resolve()
            prompt_text = prompt_path.read_text(encoding="utf-8")
            prompt_ref = str(prompt_path)
        if not prompt_text.strip():
            raise SystemExit("prompt-batch requires --prompt-file, --prompt-text, --task-file, or --task-dir")
        for index in range(1, max(1, int(args.repeat)) + 1):
            raw_tasks.append(
                {
                    "task_id": f"task-{index:04d}",
                    "task_type": "prompt",
                    "prompt_text": prompt_text,
                    "prompt_ref": prompt_ref,
                    "workdir": str(args.workdir or ""),
                    "metadata": {"repeat_index": index},
                }
            )
    if not raw_tasks:
        raise SystemExit("No tasks resolved for prompt-batch")
    tasks: list[dict[str, object]] = []
    for index, raw in enumerate(raw_tasks, start=1):
        prompt_text = str(raw.get("prompt_text") or "").strip()
        prompt_ref = str(raw.get("prompt_ref") or "")
        if not prompt_text and prompt_ref:
            prompt_text = Path(prompt_ref).expanduser().resolve().read_text(encoding="utf-8").strip()
        if not prompt_text:
            raise SystemExit(f"Task {index} is missing prompt_text/prompt_ref")
        task_id = str(raw.get("task_id") or f"task-{index:04d}")
        agent_mode = str(raw.get("agent_mode") or "ask").strip() or "ask"
        session_alias = str(raw.get("session_alias") or "").strip()
        session_id = str(raw.get("session_id") or "").strip()
        create_session = bool(raw.get("create_session") or False)
        append_output = bool(raw.get("append_output") or False)
        allowed_tools = str(raw.get("allowed_tools") or "").strip()
        disallowed_tools = str(raw.get("disallowed_tools") or "").strip()
        if agent_mode not in {"ask", "start", "send"}:
            raise SystemExit(f"Task {task_id} has invalid agent_mode={agent_mode!r}; expected ask|start|send")
        tasks.append(
            {
                "task_id": task_id,
                "task_type": str(raw.get("task_type") or "prompt"),
                "prompt_text": prompt_text,
                "prompt_ref": prompt_ref,
                "input_ref": str(raw.get("input_ref") or ""),
                "output_ref": str(raw.get("output_ref") or f"results/{task_id}.txt"),
                "workdir": str(raw.get("workdir") or args.workdir or ""),
                "agent_mode": agent_mode,
                "session_alias": session_alias,
                "session_id": session_id,
                "create_session": create_session,
                "append_output": append_output,
                "allowed_tools": allowed_tools,
                "disallowed_tools": disallowed_tools,
                "metadata": dict(raw.get("metadata") or {}),
            }
        )
    return tasks


def build_prompt_batch_plan(args: argparse.Namespace) -> dict[str, object]:
    tasks = resolve_prompt_tasks(args)
    runtime = str(args.runtime or "codex")
    if runtime not in {"codex", "wafer", "claude", "minimax"}:
        raise SystemExit("prompt-batch currently supports runtime=codex, wafer, claude, or minimax")
    sessionful_tasks = [
        task for task in tasks
        if str(task.get("agent_mode") or "ask") in {"start", "send"}
        or bool(task.get("create_session") or False)
        or str(task.get("session_alias") or "").strip()
        or str(task.get("session_id") or "").strip()
    ]
    workers = min(max(1, int(args.workers or 1)), len(tasks))
    workers_per_sandbox = max(1, int(args.workers_per_sandbox or 1))
    if sessionful_tasks and workers != 1:
        raise SystemExit(
            "Sessionful prompt-batch tasks currently require --workers 1 so follow-up prompts stay on the same sandbox-local session store. "
            "Add queue affinity before scaling sessionful work beyond one worker."
        )
    if runtime in {"wafer", "claude", "minimax"} and workers_per_sandbox > 1 and not bool(args.allow_shared_sandbox_runtime):
        raise SystemExit(
            f"Runtime '{runtime}' defaults to one worker per sandbox because shared packing can race sandbox bootstrap/auth state. "
            "Use --allow-shared-sandbox-runtime if you really want multiple workers in the same sandbox."
        )
    sandbox_count = math.ceil(workers / workers_per_sandbox)
    default_workdir = str(args.workdir or ("/root" if runtime == "codex" else ""))
    normalized_tasks: list[dict[str, object]] = []
    for task in tasks:
        item = dict(task)
        item["workdir"] = str(item.get("workdir") or default_workdir)
        normalized_tasks.append(item)
    tasks = normalized_tasks
    sandbox_names = plan_sandbox_names(
        launch_id=str(args.launch_id),
        sandbox_prefix=str(args.sandbox_prefix),
        runtime_or_lane=runtime,
        count=sandbox_count,
    )
    launch_dir = args.output_root.expanduser().resolve() / str(args.launch_id)
    queue_db = launch_dir / "task_queue.sqlite3"
    logs_dir = launch_dir / "logs"
    controller_specs_dir = launch_dir / "controller_specs"
    controllers: list[dict[str, object]] = []
    worker_specs: list[dict[str, object]] = []
    for worker_index in range(workers):
        sandbox_name = sandbox_names[worker_index // workers_per_sandbox]
        lane_slot = (worker_index % workers_per_sandbox) + 1
        run_id = f"{args.launch_id}-{runtime}-w{worker_index:03d}"
        log_path = logs_dir / f"{run_id}.log"
        spec = {
            "runtime": runtime,
            "model": str(args.model),
            "sandbox_name": sandbox_name,
            "lane_slot": lane_slot,
            "run_id": run_id,
            "worker_id": f"{runtime}-worker-{worker_index:03d}",
            "log": str(log_path),
        }
        worker_specs.append(spec)
    for sandbox_name in sandbox_names:
        grouped = [spec for spec in worker_specs if str(spec["sandbox_name"]) == sandbox_name]
        controller_id = f"{args.launch_id}-{runtime}-{sandbox_name}"
        worker_specs_path = controller_specs_dir / f"{normalize_name_slug(controller_id)}.json"
        controllers.append(
            {
                "controller_id": controller_id,
                "runtime": runtime,
                "sandbox_name": sandbox_name,
                "worker_specs_file": str(worker_specs_path),
                "worker_count": len(grouped),
                "run_ids": [str(spec["run_id"]) for spec in grouped],
                "log": str(logs_dir / f"{normalize_name_slug(controller_id)}.log"),
                "worker_specs": grouped,
            }
        )
    cpu = int(args.cpu or 2)
    memory = int(args.memory or 4)
    disk = int(args.disk or 10)
    if DEFAULT_MAX_CPU > 0 and cpu > DEFAULT_MAX_CPU:
        raise SystemExit(
            f"Requested cpu={cpu} exceeds configured Daytona limit {DEFAULT_MAX_CPU}. "
            "Override with --cpu or set DAYTONA_MAX_CPU if the account limit changed."
        )
    if DEFAULT_MAX_MEMORY_GB > 0 and memory > DEFAULT_MAX_MEMORY_GB:
        raise SystemExit(
            f"Requested memory={memory}GB exceeds configured Daytona limit {DEFAULT_MAX_MEMORY_GB}GB. "
            "Override with --memory or set DAYTONA_MAX_MEMORY_GB if the account limit changed."
        )
    if DEFAULT_MAX_DISK_GB > 0 and disk > DEFAULT_MAX_DISK_GB:
        raise SystemExit(
            f"Requested disk={disk}GB exceeds configured Daytona limit {DEFAULT_MAX_DISK_GB}GB. "
            "Override with --disk or set DAYTONA_MAX_DISK_GB if the account limit changed."
        )
    return {
        "task_profile": "prompt-batch",
        "launch_id": str(args.launch_id),
        "launch_dir": str(launch_dir),
        "output_root": str(args.output_root.expanduser().resolve()),
        "queue_db": str(queue_db),
        "total_tasks": len(tasks),
        "tasks": tasks,
        "sessionful": bool(sessionful_tasks),
        "runtime": runtime,
        "model": str(args.model),
        "tools": str(args.tools or "default"),
        "effort": str(args.effort or "low"),
        "web_mode": str(args.web_mode or "exa"),
        "workers": workers,
        "workers_per_sandbox": workers_per_sandbox,
        "sandbox_specs": [{"sandbox_name": name, "lane": runtime} for name in sandbox_names],
        "controllers": controllers,
        "pool_name": str(args.pool_name),
        "pool_limit": int(args.pool_limit),
        "cpu": cpu,
        "memory": memory,
        "disk": disk,
        "auto_stop_interval": int(args.auto_stop_interval or 5),
        "auto_archive_interval": int(args.auto_archive_interval or 5),
        "auto_delete_interval": int(args.auto_delete_interval or 15),
        "timeout_seconds": int(args.timeout_seconds or 1800),
        "workdir": default_workdir,
    }


def build_launch_plan(args: argparse.Namespace) -> dict[str, object]:
    args = apply_profile_defaults(args)
    if not str(args.pool_name or "").strip():
        args.pool_name = "default"
    if int(args.pool_limit or 0) <= 0:
        args.pool_limit = 25
    if args.task_profile == "cv-classification":
        return build_classification_plan(args)
    if args.task_profile == "prompt-batch":
        return build_prompt_batch_plan(args)
    raise SystemExit(f"Unsupported task profile: {args.task_profile}")


def is_pid_alive(pid: int | None) -> bool:
    if not pid or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def delete_sandboxes_by_name(names: list[str], *, env_file: Path | None = None) -> list[dict[str, object]]:
    if not names:
        return []

    env = os.environ.copy()
    if env_file is not None:
        env.update(load_env_file(env_file.expanduser().resolve()))
    api_key = str(env.get("DAYTONA_API_KEY") or "").strip()
    api_url = str(env.get("DAYTONA_API_URL") or "https://app.daytona.io/api").strip()
    if not api_key:
        raise SystemExit("Missing DAYTONA_API_KEY for sandbox deletion.")
    helper = (
        "import json, os, sys\n"
        "from daytona import Daytona, DaytonaConfig\n"
        "names=json.loads(sys.argv[1])\n"
        "client=Daytona(DaytonaConfig(api_key=os.environ['DAYTONA_API_KEY'], api_url=os.environ.get('DAYTONA_API_URL','https://app.daytona.io/api')))\n"
        "items=getattr(client.list(),'items',[]) or []\n"
        "by_name={str(getattr(item,'name','') or ''): item for item in items}\n"
        "out=[]\n"
        "for name in names:\n"
        "    item=by_name.get(name)\n"
        "    if item is None:\n"
        "        out.append({'sandbox_name': name, 'status': 'missing'})\n"
        "        continue\n"
        "    try:\n"
        "        client.delete(item)\n"
        "        out.append({'sandbox_name': name, 'status': 'deleted'})\n"
        "    except Exception as exc:\n"
        "        out.append({'sandbox_name': name, 'status': 'error', 'error': str(exc)})\n"
        "print(json.dumps(out, ensure_ascii=True))\n"
    )
    proc = subprocess.run(
        [str(resolve_daytona_python()), "-c", helper, json.dumps(names, ensure_ascii=True)],
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        env=env,
    )
    if proc.returncode != 0:
        raise SystemExit(proc.stderr.strip() or proc.stdout.strip() or "sandbox deletion failed")
    try:
        payload = json.loads(proc.stdout)
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Invalid sandbox deletion payload: {exc}\n{proc.stdout}") from exc
    if not isinstance(payload, list):
        raise SystemExit(f"Unexpected sandbox deletion payload: {payload!r}")
    return [dict(item) for item in payload if isinstance(item, dict)]


def launch_prompt_batch(plan: dict[str, object], *, db_path: Path, env: dict[str, str]) -> dict[str, object]:
    launch_dir = Path(str(plan["launch_dir"]))
    logs_dir = launch_dir / "logs"
    controller_specs_dir = launch_dir / "controller_specs"
    launch_dir.mkdir(parents=True, exist_ok=True)
    logs_dir.mkdir(parents=True, exist_ok=True)
    controller_specs_dir.mkdir(parents=True, exist_ok=True)
    seed_task_queue(Path(str(plan["queue_db"])), list(plan["tasks"]))
    controllers_out: list[dict[str, object]] = []
    for controller in list(plan["controllers"]):
        worker_specs_path = Path(str(controller["worker_specs_file"]))
        write_json(worker_specs_path, list(controller["worker_specs"]))
        log_path = Path(str(controller["log"]))
        cmd = [
            sys.executable,
            str(PROMPT_POOL),
            "--queue-db",
            str(plan["queue_db"]),
            "--leases-db",
            str(db_path),
            "--launch-id",
            str(plan["launch_id"]),
            "--controller-id",
            str(controller["controller_id"]),
            "--runtime",
            str(controller["runtime"]),
            "--sandbox-name",
            str(controller["sandbox_name"]),
            "--worker-specs-file",
            str(worker_specs_path),
            "--output-root",
            str(plan["output_root"]),
            "--model",
            str(plan["model"]),
            "--tools",
            str(plan["tools"]),
            "--effort",
            str(plan["effort"]),
            "--web-mode",
            str(plan["web_mode"]),
            "--workdir",
            str(plan["workdir"]),
            "--cpu",
            str(plan["cpu"]),
            "--memory",
            str(plan["memory"]),
            "--disk",
            str(plan["disk"]),
            "--auto-stop-interval",
            str(plan["auto_stop_interval"]),
            "--auto-archive-interval",
            str(plan["auto_archive_interval"]),
            "--auto-delete-interval",
            str(plan["auto_delete_interval"]),
            "--timeout-seconds",
            str(plan["timeout_seconds"]),
        ]
        with log_path.open("ab") as log_file:
            proc = subprocess.Popen(
                cmd,
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=log_file,
                start_new_session=True,
                close_fds=True,
                env=env,
            )
        controllers_out.append(
            {
                **{k: v for k, v in controller.items() if k != "worker_specs"},
                "pid": proc.pid,
            }
        )
    meta = {
        **{k: v for k, v in plan.items() if k != "tasks"},
        "controllers": controllers_out,
        "created_at_utc": __import__("datetime").datetime.now(__import__("datetime").UTC).isoformat(),
    }
    write_json(launch_dir / "launch_meta.json", meta)
    return meta


def launch_classification(plan: dict[str, object]) -> dict[str, object]:
    cmd = list(plan["adapter_cmd"])
    proc = subprocess.run(
        cmd[:-1] if cmd and cmd[-1] == "--print-plan-json" else cmd,
        check=False,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    if proc.returncode != 0:
        raise SystemExit(proc.stderr.strip() or proc.stdout.strip() or "classification launch failed")
    launch_meta_path = Path(str(plan["launch_dir"])) / "launch_meta.json"
    return read_json(launch_meta_path)


def launch_meta_path_from_record(record: dict[str, object]) -> Path:
    metadata = dict(record.get("metadata") or {})
    launch_dir = str(metadata.get("launch_dir") or "").strip()
    if not launch_dir:
        raise SystemExit(f"Launch {record['launch_id']} is missing launch_dir metadata.")
    return Path(launch_dir) / "launch_meta.json"


def print_profiles() -> None:
    print_json(
        {
            "task_profiles": TASK_PROFILES,
            "runtime_profiles": RUNTIME_PROFILES,
            "pool_profiles": POOL_PROFILES,
            "notes": {
                "prompt_batch_runtime_support": "prompt-batch now supports codex, wafer, claude, and minimax runtimes with the same queue and lease model",
                "multi_model_support_today": "cv-classification still runs through the adapter, but generic prompt-batch can now launch its own codex/wafer/claude/minimax fleets",
            },
        }
    )


def render_capacity(db_path: Path) -> list[dict[str, object]]:
    return pool_snapshot(db_path)


def render_leases(db_path: Path) -> list[dict[str, object]]:
    return list_launches(db_path, only_active=False)


def classification_status_from_meta(record: dict[str, object]) -> dict[str, object]:
    metadata = dict(record.get("metadata") or {})
    output_root = Path(str(metadata.get("output_root") or default_output_root_for("cv-classification")))
    cmd = [
        sys.executable,
        str(CLASSIFICATION_STATUS),
        "--launch-id",
        str(record["launch_id"]),
        "--output-root",
        str(output_root),
    ]
    proc = subprocess.run(cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    parsed = parse_classification_status_stdout(proc.stdout)
    return {
        "launch_id": record["launch_id"],
        "task_profile": "cv-classification",
        "lease_status": record["status"],
        "launch_dir": str(metadata.get("launch_dir") or ""),
        "raw_status_stdout": proc.stdout,
        "raw_status_stderr": proc.stderr,
        "status_rc": proc.returncode,
        **parsed,
    }


def parse_classification_status_stdout(text: str) -> dict[str, object]:
    payload: dict[str, object] = {
        "total_dossiers": None,
        "completed_items": None,
        "failed_items": None,
        "eta": "",
        "lane_summaries": [],
        "health_flags": [],
        "terminal": False,
        "derived_final_status": "",
    }
    if not text.strip():
        return payload
    total_match = re.search(r"^total_dossiers=(\d+)$", text, flags=re.MULTILINE)
    completed_match = re.search(r"^completed_items=(\d+)$", text, flags=re.MULTILINE)
    failed_match = re.search(r"^failed_items=(\d+)$", text, flags=re.MULTILINE)
    eta_match = re.search(r"^eta=(.+)$", text, flags=re.MULTILINE)
    if total_match:
        payload["total_dossiers"] = int(total_match.group(1))
    if completed_match:
        payload["completed_items"] = int(completed_match.group(1))
    if failed_match:
        payload["failed_items"] = int(failed_match.group(1))
    if eta_match:
        payload["eta"] = eta_match.group(1).strip()

    lane_summaries: list[dict[str, object]] = []
    health_flags: list[str] = []
    for match in re.finditer(
        r"^(?P<lane>[a-z0-9_-]+): shards=(?P<shards>\d+) "
        r"finished=(?P<finished>\d+) running=(?P<running>\d+) "
        r"started=(?P<started>\d+) pending=(?P<pending>\d+) "
        r"completed_items=(?P<completed>\d+) failed_items=(?P<failed>\d+)$",
        text,
        flags=re.MULTILINE,
    ):
        lane_summaries.append(
            {
                "lane": match.group("lane"),
                "shards": int(match.group("shards")),
                "finished": int(match.group("finished")),
                "running": int(match.group("running")),
                "started": int(match.group("started")),
                "pending": int(match.group("pending")),
                "completed_items": int(match.group("completed")),
                "failed_items": int(match.group("failed")),
            }
        )
    for health_match in re.finditer(r"^\s+health:\s+(.+)$", text, flags=re.MULTILINE):
        for part in health_match.group(1).split():
            name = part.split("=", 1)[0].strip()
            if name and name not in health_flags:
                health_flags.append(name)
    payload["lane_summaries"] = lane_summaries
    payload["health_flags"] = health_flags
    if lane_summaries and all(
        int(row["running"]) == 0 and int(row["started"]) == 0 and int(row["pending"]) == 0
        for row in lane_summaries
    ):
        payload["terminal"] = True
        payload["derived_final_status"] = (
            "finished_with_failures" if int(payload.get("failed_items") or 0) > 0 else "finished"
        )
    return payload


def detect_health_flags_from_text(text: str) -> list[str]:
    hay = text.lower()
    flags: list[str] = []
    patterns = {
        "resource_error": [
            "exceeds maximum allowed per sandbox",
            "requested cpu",
            "requested memory",
            "requested disk",
            "insufficient resources",
            "insufficient capacity",
        ],
        "usage_exhausted": [
            "usage limit",
            "limit reached",
            "quota exceeded",
            "insufficient credits",
            "credit balance is too low",
            "too many requests",
            "rate limit",
        ],
        "auth_error": [
            "authentication failed",
            "auth failed",
            "invalid api key",
            "unauthorized",
            "forbidden",
            "missing daytona_api_key",
            "missing local codex auth file",
            "missing claude auth material",
            "login required",
        ],
        "transport_error": [
            "connection error",
            "transport error",
            "timed out",
            "timeout",
            "502 bad gateway",
            "503 service unavailable",
            "websocket 500",
        ],
        "setup_error": [
            "failed to install",
            "failed to prepare",
            "missing required runtime path",
            "missing required env values",
            "no such file or directory",
            "command not found",
            "permission denied",
        ],
        "model_error": [
            "model not found",
            "unsupported runtime",
            "unsupported model",
            "invalid model",
            "content policy",
            "policy violation",
        ],
        "runner_error": [
            "daytonanotfounderror",
            "failed to download file",
            "file not found: /tmp/codex-last-message.txt",
            "traceback (most recent call last)",
        ],
    }
    for flag, needles in patterns.items():
        if any(needle in hay for needle in needles):
            flags.append(flag)
    return flags


def summarize_prompt_batch_health(meta: dict[str, object]) -> dict[str, object]:
    logs_dir = Path(str(meta["launch_dir"])) / "logs"
    health_counts = {
        "resource_error": 0,
        "usage_exhausted": 0,
        "auth_error": 0,
        "transport_error": 0,
        "setup_error": 0,
        "model_error": 0,
        "runner_error": 0,
    }
    error_logs: list[dict[str, object]] = []
    for log_path in sorted(logs_dir.glob("*.log")):
        try:
            text = log_path.read_text(encoding="utf-8", errors="replace")
        except Exception:
            continue
        flags = detect_health_flags_from_text(text)
        if not flags:
            continue
        for flag in flags:
            health_counts[flag] += 1
        error_logs.append({"log": str(log_path), "flags": flags})
    return {
        "health_counts": health_counts,
        "flagged_logs": error_logs[:12],
    }


def prompt_batch_failed_items(meta: dict[str, object]) -> list[dict[str, object]]:
    import sqlite3

    queue_db = Path(str(meta["queue_db"]))
    conn = sqlite3.connect(str(queue_db))
    try:
        rows = conn.execute(
            """
            SELECT task_id, status, attempts, last_error, last_run_id
            FROM items
            WHERE status = 'failed'
            ORDER BY ordinal ASC, id ASC
            """
        ).fetchall()
        out: list[dict[str, object]] = []
        for task_id, status, attempts, last_error, last_run_id in rows:
            out.append(
                {
                    "task_id": str(task_id),
                    "status": str(status),
                    "attempts": int(attempts),
                    "last_error": str(last_error or ""),
                    "last_run_id": str(last_run_id or ""),
                }
            )
        return out
    finally:
        conn.close()


def prompt_batch_status_from_meta(record: dict[str, object]) -> dict[str, object]:
    meta_path = launch_meta_path_from_record(record)
    if not meta_path.exists():
        metadata = dict(record.get("metadata") or {})
        return {
            "launch_id": record["launch_id"],
            "task_profile": "prompt-batch",
            "lease_status": str(record["status"]),
            "queue_counts": {},
            "alive_controllers": 0,
            "controllers": [],
            "launch_dir": str(metadata.get("launch_dir") or ""),
            "result_root": str(Path(str(metadata.get("launch_dir") or "")) / "results"),
            "failed_items": [],
            "telemetry": {"health_counts": {}, "flagged_logs": []},
            "status_note": "launch_meta_missing",
        }
    meta = read_json(meta_path)
    queue_db = Path(str(meta["queue_db"]))
    counts = queue_counts(queue_db)
    controllers: list[dict[str, object]] = []
    alive = 0
    for controller in list(meta.get("controllers") or []):
        pid = int(controller.get("pid") or 0)
        running = is_pid_alive(pid)
        if running:
            alive += 1
        controllers.append(
            {
                "controller_id": str(controller["controller_id"]),
                "sandbox_name": str(controller["sandbox_name"]),
                "pid": pid,
                "alive": running,
                "log": str(controller["log"]),
            }
        )
    done = counts.get("pending", 0) == 0 and counts.get("running", 0) == 0 and alive == 0
    lease_status = str(record["status"])
    if done and not str(record.get("released_at") or "").strip():
        release_launch(Path(str(record.get("_db_path") or DEFAULT_LEASE_DB)), launch_id=str(record["launch_id"]), final_status="finished")
        lease_status = "finished"
    telemetry = summarize_prompt_batch_health(meta)
    return {
        "launch_id": record["launch_id"],
        "task_profile": "prompt-batch",
        "lease_status": lease_status,
        "queue_counts": counts,
        "alive_controllers": alive,
        "controllers": controllers,
        "launch_dir": str(meta["launch_dir"]),
        "result_root": str(Path(str(meta["launch_dir"])) / "results"),
        "failed_items": prompt_batch_failed_items(meta),
        "telemetry": telemetry,
    }


def find_launch_record(db_path: Path, launch_id: str) -> dict[str, object]:
    for record in list_launches(db_path, only_active=False):
        if str(record["launch_id"]) == launch_id:
            record["_db_path"] = str(db_path)
            return record
    raise SystemExit(f"Unknown launch_id: {launch_id}")


def stop_prompt_batch(record: dict[str, object]) -> dict[str, object]:
    meta = read_json(launch_meta_path_from_record(record))
    stopped: list[dict[str, object]] = []
    for controller in list(meta.get("controllers") or []):
        pid = int(controller.get("pid") or 0)
        if is_pid_alive(pid):
            os.killpg(pid, signal.SIGTERM)
            stopped.append({"controller_id": controller["controller_id"], "pid": pid, "signal": "TERM"})
    cancelled = cancel_unfinished_tasks(
        Path(str(meta["queue_db"])),
        reason="stopped_by_user",
        run_id=str(record["launch_id"]),
    )
    release_launch(Path(str(record["_db_path"])), launch_id=str(record["launch_id"]), final_status="stopped")
    return {"launch_id": record["launch_id"], "stopped": stopped, "queue_cancelled": cancelled}


def stop_classification(record: dict[str, object]) -> dict[str, object]:
    metadata = dict(record.get("metadata") or {})
    output_root = Path(str(metadata.get("output_root") or default_output_root_for("cv-classification")))
    env_file = Path(str(metadata.get("env_file") or DEFAULT_ENV_FILE)).expanduser().resolve()
    env_payload = load_env_file(env_file)
    daytona_python = str(env_payload.get("DAYTONA_PYTHON_BIN") or "").strip()
    if not daytona_python:
        daytona_python = str(resolve_daytona_python())
    cmd = [
        daytona_python,
        str(CLASSIFICATION_STOP),
        "--launch-id",
        str(record["launch_id"]),
        "--output-root",
        str(output_root),
        "--env-file",
        str(env_file),
    ]
    proc = subprocess.run(cmd, check=False, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    release_launch(Path(str(record["_db_path"])), launch_id=str(record["launch_id"]), final_status="stopped")
    return {"launch_id": record["launch_id"], "stdout": proc.stdout, "stderr": proc.stderr, "rc": proc.returncode}


def cmd_profiles(_args: argparse.Namespace) -> int:
    print_profiles()
    return 0


def cmd_capacity(args: argparse.Namespace) -> int:
    print_json(render_capacity(args.db_path.expanduser().resolve()))
    return 0


def cmd_leases(args: argparse.Namespace) -> int:
    print_json(render_leases(args.db_path.expanduser().resolve()))
    return 0


def cmd_plan(args: argparse.Namespace) -> int:
    plan = build_launch_plan(args)
    print_json(plan)
    return 0


def cmd_launch(args: argparse.Namespace) -> int:
    plan = build_launch_plan(args)
    db_path = args.db_path.expanduser().resolve()
    reserve_launch(
        db_path,
        pool_name=str(plan["pool_name"]),
        sandbox_limit=int(plan["pool_limit"]),
        launch_id=str(plan["launch_id"]),
        launch_kind=str(plan["task_profile"]),
        sandbox_specs=list(plan["sandbox_specs"]),
        metadata={
            "task_profile": str(plan["task_profile"]),
            "launch_dir": str(plan["launch_dir"]),
            "output_root": str(plan["output_root"]),
            "env_file": str(args.env_file.expanduser().resolve()),
        },
        pool_metadata={"managed_by": "daytona_agents_ctl"},
    )
    try:
        if str(plan["task_profile"]) == "cv-classification":
            meta = launch_classification(plan)
        else:
            launch_env = os.environ.copy()
            launch_env.update(load_env_file(args.env_file.expanduser().resolve()))
            meta = launch_prompt_batch(plan, db_path=db_path, env=launch_env)
        activate_launch(
            db_path,
            launch_id=str(plan["launch_id"]),
            metadata_patch={
                "task_profile": str(plan["task_profile"]),
                "launch_dir": str(plan["launch_dir"]),
                "output_root": str(plan["output_root"]),
                "env_file": str(args.env_file.expanduser().resolve()),
                "total_tasks": int(plan.get("total_tasks") or 0),
                "total_workers": int(plan.get("total_workers") or plan.get("workers") or 0),
            },
        )
    except Exception:
        release_launch(db_path, launch_id=str(plan["launch_id"]), final_status="failed")
        raise
    print_json(
        {
            "launch_id": str(plan["launch_id"]),
            "task_profile": str(plan["task_profile"]),
            "launch_dir": str(plan["launch_dir"]),
            "pool_name": str(plan["pool_name"]),
            "reserved_sandboxes": len(list(plan["sandbox_specs"])),
            "meta_path": str(Path(str(plan["launch_dir"])) / "launch_meta.json"),
            "adapter_result": meta if str(plan["task_profile"]) == "prompt-batch" else {"launch_meta_path": str(Path(str(plan["launch_dir"])) / "launch_meta.json")},
        }
    )
    return 0


def cmd_status(args: argparse.Namespace) -> int:
    db_path = args.db_path.expanduser().resolve()
    record = find_launch_record(db_path, args.launch_id)
    heartbeat_launch(db_path, launch_id=str(record["launch_id"]))
    task_profile = str(dict(record.get("metadata") or {}).get("task_profile") or record.get("launch_kind") or "")
    if task_profile == "cv-classification":
        payload = classification_status_from_meta(record)
        if payload.get("status_rc") == 0 and payload.get("terminal") and str(record.get("status") or "") == "active":
            final_status = str(payload.get("derived_final_status") or "finished")
            release_launch(db_path, launch_id=str(record["launch_id"]), final_status=final_status)
            payload["lease_status"] = final_status
    else:
        record["_db_path"] = str(db_path)
        payload = prompt_batch_status_from_meta(record)
    if args.json:
        print_json(payload)
    else:
        print_json(payload)
    return 0


def cmd_stop(args: argparse.Namespace) -> int:
    db_path = args.db_path.expanduser().resolve()
    record = find_launch_record(db_path, args.launch_id)
    task_profile = str(dict(record.get("metadata") or {}).get("task_profile") or record.get("launch_kind") or "")
    record["_db_path"] = str(db_path)
    payload = stop_classification(record) if task_profile == "cv-classification" else stop_prompt_batch(record)
    if args.delete_sandboxes:
        names = [item["sandbox_name"] for item in list_sandboxes(db_path, only_active=False) if str(item["launch_id"]) == args.launch_id]
        payload["deleted_sandboxes"] = delete_sandboxes_by_name(names, env_file=args.env_file)
    print_json(payload)
    return 0


def cmd_reap(args: argparse.Namespace) -> int:
    db_path = args.db_path.expanduser().resolve()
    stale_ids = stale_launch_ids(db_path, stale_after_seconds=int(args.stale_after_seconds))
    reaped: list[dict[str, object]] = []
    for launch_id in stale_ids:
        record = find_launch_record(db_path, launch_id)
        release_launch(db_path, launch_id=launch_id, final_status="reaped")
        payload: dict[str, object] = {"launch_id": launch_id, "status": "reaped"}
        if args.delete_sandboxes:
            names = [item["sandbox_name"] for item in list_sandboxes(db_path, only_active=False) if str(item["launch_id"]) == launch_id]
            payload["deleted_sandboxes"] = delete_sandboxes_by_name(names, env_file=args.env_file)
        reaped.append(payload)
    print_json({"stale_launch_ids": stale_ids, "reaped": reaped})
    return 0


def cmd_logs(args: argparse.Namespace) -> int:
    record = find_launch_record(args.db_path.expanduser().resolve(), args.launch_id)
    meta_path = launch_meta_path_from_record(record)
    meta = read_json(meta_path)
    logs: list[str] = []
    for controller in list(meta.get("controllers") or []):
        logs.append(str(controller.get("log") or ""))
    for run in list(meta.get("runs") or []):
        if str(run.get("log") or ""):
            logs.append(str(run["log"]))
    if args.tail and logs:
        raise SystemExit(subprocess.call(["tail", "-n", "80", *logs[:8]]))
    print_json({"launch_id": args.launch_id, "logs": logs})
    return 0


def cmd_dashboard(args: argparse.Namespace) -> int:
    cmd = [
        sys.executable,
        str(ROOT / "serve_daytona_agents_dashboard.py"),
        "--db-path",
        str(args.db_path.expanduser().resolve()),
        "--port",
        str(int(args.port)),
        "--launch-limit",
        str(int(args.launch_limit)),
        "--recent-file-limit",
        str(int(args.recent_file_limit)),
    ]
    if args.active_only:
        cmd.append("--active-only")
    raise SystemExit(subprocess.call(cmd))


def main() -> int:
    argv = sys.argv[1:]
    if argv and argv[0] == "-help":
        argv = ["--help"]
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.command == "profiles":
        return cmd_profiles(args)
    if args.command == "capacity":
        return cmd_capacity(args)
    if args.command == "leases":
        return cmd_leases(args)
    if args.command == "plan":
        return cmd_plan(args)
    if args.command == "launch":
        return cmd_launch(args)
    if args.command == "status":
        return cmd_status(args)
    if args.command == "stop":
        return cmd_stop(args)
    if args.command == "reap":
        return cmd_reap(args)
    if args.command == "logs":
        return cmd_logs(args)
    if args.command == "dashboard":
        return cmd_dashboard(args)
    raise SystemExit(f"Unknown command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
