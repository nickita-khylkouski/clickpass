#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_DAYTONA_ENV = REPO_ROOT / ".env.daytona"
QUEUE_SCRIPT = REPO_ROOT / "scripts" / "attendee_dossier_queue.py"
DAYTONA_RUNNER = REPO_ROOT / "ops" / "daytona" / "daytona_minimax_remote.py"
APP_ENV_NAMES = ("SUPABASE_URL", "SUPABASE_KEY", "PLATFORM_DATABASE_URL")


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


def load_app_env() -> dict[str, str]:
    merged: dict[str, str] = {}
    candidates = [
        REPO_ROOT / ".env",
        Path.home() / "cv-rank" / ".env",
    ]
    for path in candidates:
        values = load_env_file(path)
        for key in APP_ENV_NAMES:
            value = str(values.get(key, "")).strip()
            if value and key not in merged:
                merged[key] = value
    for key in APP_ENV_NAMES:
        value = str(os.environ.get(key, "")).strip()
        if value:
            merged[key] = value
    return merged


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=str(DEFAULT_DAYTONA_ENV))
    parser.add_argument("--queue-name", default="sf-minimax-exa")
    parser.add_argument("--model", default="MiniMax-M2.7")
    parser.add_argument("--sandbox-prefix", default="cv-rank-minimax")
    parser.add_argument("--sandbox-start-index", type=int, default=1)
    parser.add_argument("--sandbox-count", type=int, default=5)
    parser.add_argument("--workers-per-sandbox", type=int, default=5)
    parser.add_argument("--log-dir", default="/tmp/daytona-minimax-queue-workers")
    parser.add_argument("--web-mode", default="exa", choices=("exa", "mixed", "claude"))
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--minimax-env-file", default=str(Path.home() / ".claude-wafer" / "minimax.env"))
    parser.add_argument("--cpu", type=int, default=4)
    parser.add_argument("--memory", type=int, default=8)
    parser.add_argument("--disk", type=int, default=10)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    env_file = Path(args.env_file).expanduser().resolve()
    base_env = os.environ.copy()
    base_env.update(load_env_file(env_file))
    base_env.update(load_app_env())
    base_env["MINIMAX_ENV_FILE"] = str(Path(args.minimax_env_file).expanduser().resolve())
    base_env["DAYTONA_CPU"] = str(args.cpu)
    base_env["DAYTONA_MEMORY"] = str(args.memory)
    base_env["DAYTONA_DISK"] = str(args.disk)

    log_dir = Path(args.log_dir).expanduser().resolve()
    log_dir.mkdir(parents=True, exist_ok=True)

    sandbox_end_index = args.sandbox_start_index + args.sandbox_count - 1
    for box in range(args.sandbox_start_index, sandbox_end_index + 1):
        sandbox_name = f"{args.sandbox_prefix}-{box:02d}"
        for lane in range(1, args.workers_per_sandbox + 1):
            env = base_env.copy()
            env["DAYTONA_SANDBOX_NAME"] = sandbox_name
            log_path = log_dir / f"{sandbox_name}-w{lane}.log"
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
                        str(DAYTONA_RUNNER),
                        "--claude-model",
                        args.model,
                        "--wafer-web-mode",
                        args.web_mode,
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
            print(f"{proc.pid} {sandbox_name} worker={lane} {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
