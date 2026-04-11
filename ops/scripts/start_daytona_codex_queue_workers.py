#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path


def load_env_file(path: Path) -> dict[str, str]:
    env: dict[str, str] = {}
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        env[key.strip()] = value.strip().strip('"').strip("'")
    return env


def parse_args() -> argparse.Namespace:
    root = Path("/Users/nickita/.superset/worktrees/start/beaded-mind")
    parser = argparse.ArgumentParser()
    parser.add_argument("--env-file", default=str(root / ".env.daytona"))
    parser.add_argument("--queue-name", default="nyc-unified")
    parser.add_argument("--model", default="gpt-5.4")
    parser.add_argument("--sandbox-prefix", default="cv-rank-codex")
    parser.add_argument("--sandbox-start-index", type=int, default=1)
    parser.add_argument("--sandbox-count", type=int, default=5)
    parser.add_argument("--workers-per-sandbox", type=int, default=3)
    parser.add_argument("--web-mode", default="claude", choices=("exa", "mixed", "claude"))
    parser.add_argument("--log-dir", default="/tmp/daytona-codex-queue-workers")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    env_file = Path(args.env_file).expanduser().resolve()
    base_env = os.environ.copy()
    base_env.update(load_env_file(env_file))
    base_env.setdefault("CODEX_HOME", "/Users/nickita/.codex-seq")

    queue_script = "/Users/nickita/cv-rank/scripts/attendee_dossier_queue.py"
    runner = "/Users/nickita/.superset/worktrees/start/beaded-mind/daytona_cv_rank_remote.py"
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
                        queue_script,
                        "work",
                        "--queue-name",
                        args.queue_name,
                        "--execution-backend",
                        "daytona",
                        "--daytona-runner",
                        runner,
                        "--claude-model",
                        args.model,
                        "--wafer-web-mode",
                        args.web_mode,
                        "--wafer-timeout-seconds",
                        "1800",
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
