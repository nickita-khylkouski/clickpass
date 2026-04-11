#!/usr/bin/env python3
from __future__ import annotations

import argparse
import os
import subprocess
from pathlib import Path


def parse_args() -> argparse.Namespace:
    root = Path("/Users/nickita/.superset/worktrees/start/beaded-mind")
    parser = argparse.ArgumentParser()
    parser.add_argument("--queue-name", default="nyc-unified")
    parser.add_argument("--model", default="MiniMax-M2.7")
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--log-dir", default="/tmp/local-minimax-queue-workers")
    parser.add_argument("--web-mode", default="mixed", choices=("exa", "mixed", "claude"))
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--env-file", default=str(Path.home() / ".claude-wafer" / "minimax.env"))
    parser.add_argument("--claude-bin", default="/Users/nickita/.superset/bin/claude")
    parser.add_argument("--root", default=str(root))
    return parser.parse_args()


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


def main() -> int:
    args = parse_args()
    log_dir = Path(args.log_dir).expanduser().resolve()
    log_dir.mkdir(parents=True, exist_ok=True)

    base_env = os.environ.copy()
    base_env.update(load_env_file(Path(args.env_file).expanduser().resolve()))
    base_env["CLAUDE_WAFER_LAUNCHER_KIND"] = "minimax"
    base_env["CLAUDE_WAFER_BIN"] = args.claude_bin
    base_env["MINIMAX_ENV_FILE"] = str(Path(args.env_file).expanduser().resolve())

    queue_script = "/Users/nickita/cv-rank/scripts/attendee_dossier_queue.py"

    for worker in range(1, args.workers + 1):
        log_path = log_dir / f"local-minimax-w{worker}.log"
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
                    "local",
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
                env=base_env,
                start_new_session=True,
                close_fds=True,
            )
        print(f"{proc.pid} worker={worker} {log_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
