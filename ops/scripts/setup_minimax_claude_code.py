#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path


DEFAULT_ENV_FILE = Path.home() / ".claude-wafer" / "minimax.env"
DEFAULT_SETTINGS_FILE = Path.home() / ".claude" / "settings.json"


def read_simple_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip("'").strip('"')
    return values


def ensure_settings(settings_path: Path, *, model: str) -> None:
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    if settings_path.exists():
        data = json.loads(settings_path.read_text(encoding="utf-8"))
    else:
        data = {}
    data["env"] = data.get("env") or {}
    data["env"]["ANTHROPIC_BASE_URL"] = "https://api.minimax.io/anthropic"
    data["env"]["API_TIMEOUT_MS"] = "3000000"
    data["env"]["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = "1"
    data["model"] = model
    settings_path.write_text(json.dumps(data, indent=2) + "\n", encoding="utf-8")


def run(cmd: list[str], *, env: dict[str, str], allow_failure: bool = False) -> None:
    result = subprocess.run(cmd, text=True, capture_output=True, env=env, check=False)
    if result.returncode != 0 and not allow_failure:
        sys.stderr.write(result.stderr or result.stdout)
        raise SystemExit(result.returncode)


def main() -> int:
    parser = argparse.ArgumentParser(description="Configure Claude Code to use MiniMax locally.")
    parser.add_argument("--env-file", default=str(DEFAULT_ENV_FILE))
    parser.add_argument("--settings-file", default=str(DEFAULT_SETTINGS_FILE))
    parser.add_argument("--model", default="MiniMax-M2.7")
    parser.add_argument("--mcp-name", default="MiniMax")
    args = parser.parse_args()

    env_file = Path(args.env_file)
    values = read_simple_env(env_file)
    api_key = values.get("MINIMAX_API_KEY") or os.environ.get("MINIMAX_API_KEY", "").strip()
    if not api_key:
        raise SystemExit(f"Missing MINIMAX_API_KEY in {env_file} or environment.")
    api_host = values.get("MINIMAX_API_HOST") or os.environ.get("MINIMAX_API_HOST", "").strip() or "https://api.minimax.io"

    ensure_settings(Path(args.settings_file), model=args.model)

    env = os.environ.copy()
    env.update(
        {
            "MINIMAX_API_KEY": api_key,
            "MINIMAX_API_HOST": api_host,
            "ANTHROPIC_AUTH_TOKEN": api_key,
            "ANTHROPIC_BASE_URL": f"{api_host.rstrip('/')}/anthropic",
            "API_TIMEOUT_MS": values.get("API_TIMEOUT_MS", "3000000"),
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": values.get(
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1"
            ),
        }
    )

    run(
        [
            "claude",
            "mcp",
            "remove",
            "-s",
            "user",
            args.mcp_name,
        ],
        env=env,
        allow_failure=True,
    )
    run(
        [
            "claude",
            "mcp",
            "add",
            "-s",
            "user",
            args.mcp_name,
            "--env",
            f"MINIMAX_API_KEY={api_key}",
            "--env",
            f"MINIMAX_API_HOST={api_host}",
            "--",
            "uvx",
            "minimax-coding-plan-mcp",
            "-y",
        ],
        env=env,
    )

    print(f"settings: {args.settings_file}")
    print(f"env_file: {env_file}")
    print(f"mcp: {args.mcp_name}")
    print(f"model: {args.model}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
