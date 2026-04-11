#!/usr/bin/env python3
from __future__ import annotations

import argparse
import math
import os
import subprocess
from pathlib import Path


ROOT = Path("/Users/nickita/.superset/worktrees/start/beaded-mind")
QUEUE_SCRIPT = Path("/Users/nickita/cv-rank/scripts/attendee_dossier_queue.py")

LANE_CONFIG = {
    "claude": {
        "runner": ROOT / "daytona_plain_remote.py",
        "model": "sonnet",
        "web_mode": "exa",
        "sandbox_prefix": "cv-rank-claude-pool",
        "log_dir": "/tmp/daytona-claude-pool",
    },
    "codex": {
        "runner": ROOT / "daytona_cv_rank_remote.py",
        "model": "gpt-5.4",
        "web_mode": "claude",
        "sandbox_prefix": "cv-rank-codex-pool",
        "log_dir": "/tmp/daytona-codex-pool",
    },
    "minimax": {
        "runner": ROOT / "daytona_minimax_remote.py",
        "model": "MiniMax-M2.7",
        "web_mode": "claude",
        "sandbox_prefix": "cv-rank-minimax-pool",
        "log_dir": "/tmp/daytona-minimax-pool",
    },
    "wafer": {
        "runner": ROOT / "daytona_wafer_remote.py",
        "model": "Qwen3.5-397B-A17B",
        "web_mode": "exa",
        "sandbox_prefix": "cv-rank-wafer-pool",
        "log_dir": "/tmp/daytona-wafer-pool",
    },
}


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


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Launch a pooled Daytona worker fleet with deterministic sandbox assignment.")
    parser.add_argument("--lane", choices=sorted(LANE_CONFIG), required=True)
    parser.add_argument("--workers", type=int, required=True, help="Total workers to launch.")
    parser.add_argument("--workers-per-sandbox", type=int, default=3)
    parser.add_argument("--queue-name", default="nyc-unified")
    parser.add_argument("--sandbox-prefix")
    parser.add_argument("--sandbox-start-index", type=int, default=1)
    parser.add_argument("--max-sandboxes", type=int, default=25)
    parser.add_argument("--env-file", default=str(ROOT / ".env.daytona"))
    parser.add_argument("--model")
    parser.add_argument("--web-mode", choices=("exa", "mixed", "claude"))
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--log-dir")
    parser.add_argument("--minimax-env-file", default=str(Path.home() / ".claude-wafer" / "minimax.env"))
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    lane = LANE_CONFIG[args.lane]
    workers = max(1, args.workers)
    workers_per_sandbox = max(1, args.workers_per_sandbox)
    sandbox_count = math.ceil(workers / workers_per_sandbox)
    if sandbox_count > args.max_sandboxes:
        raise SystemExit(
            f"Requested {workers} workers with {workers_per_sandbox} per sandbox needs {sandbox_count} sandboxes, "
            f"which exceeds the cap of {args.max_sandboxes}."
        )

    env_file = Path(args.env_file).expanduser().resolve()
    base_env = os.environ.copy()
    base_env.update(load_env_file(env_file))
    if args.lane == "codex":
        base_env.setdefault("CODEX_HOME", "/Users/nickita/.codex-seq")
    if args.lane == "minimax":
        base_env["MINIMAX_ENV_FILE"] = str(Path(args.minimax_env_file).expanduser().resolve())

    runner = str(lane["runner"])
    model = args.model or lane["model"]
    web_mode = args.web_mode or lane["web_mode"]
    sandbox_prefix = args.sandbox_prefix or lane["sandbox_prefix"]
    log_dir = Path(args.log_dir or lane["log_dir"]).expanduser().resolve()
    log_dir.mkdir(parents=True, exist_ok=True)

    launched = 0
    worker_index = 0
    while launched < workers:
        sandbox_number = args.sandbox_start_index + (worker_index // workers_per_sandbox)
        sandbox_name = f"{sandbox_prefix}-{sandbox_number:02d}"
        lane_slot = (worker_index % workers_per_sandbox) + 1
        env = base_env.copy()
        env["DAYTONA_SANDBOX_NAME"] = sandbox_name
        log_path = log_dir / f"{sandbox_name}-w{lane_slot}.log"
        with log_path.open("ab") as log_file:
            proc = subprocess.Popen(
                [
                    "python3",
                    "-u",
                    str(QUEUE_SCRIPT),
                    "work",
                    "--queue-name",
                    args.queue_name,
                    "--execution-backend",
                    "daytona",
                    "--daytona-runner",
                    runner,
                    "--claude-model",
                    model,
                    "--wafer-web-mode",
                    web_mode,
                    "--wafer-timeout-seconds",
                    str(args.timeout_seconds),
                    "--effort",
                    "low",
                    "--wafer-expansion-pass",
                    "--wafer-braindump-pass",
                ],
                stdin=subprocess.DEVNULL,
                stdout=log_file,
                stderr=log_file,
                env=env,
                start_new_session=True,
                close_fds=True,
            )
        print(f"{proc.pid} lane={args.lane} sandbox={sandbox_name} worker={lane_slot} log={log_path}")
        launched += 1
        worker_index += 1
    print(f"launched_workers={launched} sandboxes={sandbox_count} workers_per_sandbox={workers_per_sandbox}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
