#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import shlex
import shutil
import subprocess
import sys
import time
import uuid
from pathlib import Path
from urllib.parse import urlparse

from daytona_agent_leases import activate_launch, heartbeat_launch, release_launch, reserve_launch

ROOT = Path(__file__).resolve().parents[1]
AGENT_SCRIPT = ROOT / "claude_wafer_agent.py"
DAYTONA_RUNNER = ROOT / "daytona_claude_mini_remote.py"
WEB_AUDIT_DAYTONA_RUNNER = ROOT / "daytona_minimax_web_audit_remote.py"
E2B_RUNNER = ROOT / "e2b-claude-mini"
ATTENDEE_QUEUE_SCRIPT = Path("/Users/nickita/cv-rank/scripts/attendee_dossier_queue.py")
MINIMAX_DOSSIER_WORKERS = ROOT / "scripts" / "start_daytona_minimax_queue_workers.py"
SF_FULL_COHORT_ROOT = ROOT / "data" / "sf_full_cohort"
DEFAULT_MINIMAX_ENV = Path.home() / ".claude-wafer" / "minimax.env"
WEB_AUDIT_LIVE_ROOT = ROOT / "data" / "web_audit_live"
WEB_AUDIT_FLEET_ROOT = ROOT / "data" / "web_audit_fleets"
DEFAULT_DAYTONA_LEASE_DB = ROOT / "data" / "daytona_leases.sqlite3"


def daytona_python_bin() -> str:
    override = os.environ.get("DAYTONA_PYTHON_BIN", "").strip()
    if override:
        return override
    candidate = ROOT / ".venv-daytona" / "bin" / "python"
    if candidate.exists():
        return str(candidate)
    return "python3"


def read_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if not path.exists():
        return values
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        values[key.strip()] = value.strip().strip('"').strip("'")
    return values


def merge_env_defaults(env: dict[str, str], defaults: dict[str, str]) -> None:
    for key, value in defaults.items():
        if key not in env or not str(env.get(key, "")).strip():
            env[key] = value


def normalize_model(raw: str | None) -> str:
    value = (raw or "").strip()
    if not value:
        return "MiniMax-M2.7"
    reduced = value.lower().replace("-", "").replace("/", "").replace(".", "")
    if reduced in {"m27", "minimaxm27"}:
        return "MiniMax-M2.7"
    return value


def audit_target_sandbox_name(target_url: str, sandbox_prefix: str) -> str:
    host = (urlparse(str(target_url)).hostname or "target").lower().strip()
    safe = "".join(ch if ch.isalnum() else "-" for ch in host).strip("-") or "target"
    while "--" in safe:
        safe = safe.replace("--", "-")
    return f"{sandbox_prefix}-audit-{safe}"


def audit_batch_launch_dir(launch_id: str) -> Path:
    path = WEB_AUDIT_FLEET_ROOT / launch_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def normalize_url_list(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        value = str(raw or "").strip()
        if not value or value.startswith("#"):
            continue
        if "://" not in value:
            value = f"https://{value}"
        if value in seen:
            continue
        seen.add(value)
        out.append(value)
    return out


def load_audit_targets(*, inline: list[str], file_path: str) -> list[str]:
    values = list(inline)
    if str(file_path or "").strip():
        path = Path(file_path).expanduser().resolve()
        if not path.exists():
            raise SystemExit(f"Targets file not found: {path}")
        values.extend(path.read_text(encoding="utf-8").splitlines())
    targets = normalize_url_list(values)
    if not targets:
        raise SystemExit("Provide at least one target URL or --targets-file.")
    return targets


def audit_batch_sandbox_names(*, launch_id: str, sandbox_prefix: str, count: int) -> list[str]:
    normalized_launch = "".join(ch.lower() if ch.isalnum() else "-" for ch in launch_id).strip("-")
    while "--" in normalized_launch:
        normalized_launch = normalized_launch.replace("--", "-")
    short = (normalized_launch or "audit")[-18:]
    digest = uuid.uuid5(uuid.NAMESPACE_URL, launch_id).hex[:6]
    prefix = f"{sandbox_prefix}-{short}-{digest}-audit"
    return [f"{prefix}-{idx:02d}" for idx in range(1, count + 1)]


def audit_run_id(target_url: str, *, launch_id: str, index: int) -> str:
    host = (urlparse(str(target_url)).hostname or "target").lower()
    safe_host = "".join(ch if ch.isalnum() else "-" for ch in host).strip("-") or "target"
    while "--" in safe_host:
        safe_host = safe_host.replace("--", "-")
    return f"{safe_host}-{launch_id}-t{index:03d}"


def latest_sf_deduped_people_file() -> Path:
    candidates = sorted(
        SF_FULL_COHORT_ROOT.glob("sf-full-cohort-deduped-*/top_2000_deduped.json"),
        key=lambda path: path.stat().st_mtime,
    )
    if not candidates:
        raise SystemExit("No deduped SF top_2000 file found under data/sf_full_cohort.")
    return candidates[-1]


def base_env(minimax_env_file: str, model: str) -> dict[str, str]:
    env = os.environ.copy()
    env["CLAUDE_WAFER_LAUNCHER_KIND"] = "minimax"
    # Force plain Claude CLI for MiniMax mode; avoid local wafer proxy defaults.
    env["CLAUDE_WAFER_BIN"] = env.get("CLAUDE_MINI_CLAUDE_BIN", "claude")
    env["MINIMAX_ENV_FILE"] = str(Path(minimax_env_file).expanduser().resolve())
    env["MINIMAX_MODEL"] = normalize_model(model)
    env["CLAUDE_MINI_DISABLE_EXA"] = "1"
    env["CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"] = env.get(
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "1"
    )
    env.pop("EXA_API_KEY", None)
    env.pop("EXA_API_KEYS", None)

    # Compatibility: if the provided env file is Wafer-style, map keys so
    # daytona_claude_mini_remote can still find MINIMAX_* credentials.
    env_file_values = read_env_file(Path(minimax_env_file).expanduser().resolve())
    wafer_key = env_file_values.get("WAFER_API_KEY") or env.get("WAFER_API_KEY", "")
    wafer_keys = env_file_values.get("WAFER_API_KEYS") or env.get("WAFER_API_KEYS", "")
    wafer_base_url = env_file_values.get("WAFER_BASE_URL") or env.get("WAFER_BASE_URL", "")
    minimax_key = env_file_values.get("MINIMAX_API_KEY") or env.get("MINIMAX_API_KEY", "")
    minimax_keys = env_file_values.get("MINIMAX_API_KEYS") or env.get("MINIMAX_API_KEYS", "")
    minimax_host = env_file_values.get("MINIMAX_API_HOST") or env.get("MINIMAX_API_HOST", "")

    if not minimax_key and wafer_key:
        env["MINIMAX_API_KEY"] = wafer_key
    if not minimax_keys and wafer_keys:
        env["MINIMAX_API_KEYS"] = wafer_keys
    if not minimax_host and wafer_base_url:
        env["MINIMAX_API_HOST"] = wafer_base_url

    # Also mirror back so downstream tooling that checks WAFER_* still works.
    if not env.get("WAFER_API_KEY") and minimax_key:
        env["WAFER_API_KEY"] = minimax_key
    if not env.get("WAFER_API_KEYS") and minimax_keys:
        env["WAFER_API_KEYS"] = minimax_keys
    if not env.get("WAFER_BASE_URL"):
        host = env.get("MINIMAX_API_HOST", "")
        if host:
            env["WAFER_BASE_URL"] = host
    return env


def minimax_key_seed(args: argparse.Namespace, *, mode: str, worker_index: int | None = None) -> str:
    parts = [
        mode,
        str(getattr(args, "alias", "") or ""),
        str(getattr(args, "session_id", "") or ""),
        normalize_model(getattr(args, "model", None)),
        str(getattr(args, "sandbox_name", "") or ""),
    ]
    if worker_index is not None:
        parts.append(f"worker:{worker_index}")
    else:
        parts.append(str(getattr(args, "prompt", "") or ""))
    return "|".join(parts)


def add_prompt_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--prompt-file", help="Read the prompt from a UTF-8 text file.")
    parser.add_argument("prompt", nargs="?", help="Inline prompt text.")


def resolve_prompt(args: argparse.Namespace) -> str:
    inline = str(getattr(args, "prompt", "") or "").strip()
    prompt_file = str(getattr(args, "prompt_file", "") or "").strip()
    if inline and prompt_file:
        raise SystemExit("Provide either an inline prompt or --prompt-file, not both.")
    if prompt_file:
        path = Path(prompt_file).expanduser().resolve()
        if not path.exists():
            raise SystemExit(f"Prompt file not found: {path}")
        text = path.read_text(encoding="utf-8")
        if not text.strip():
            raise SystemExit(f"Prompt file is empty: {path}")
        return text
    if inline:
        return str(getattr(args, "prompt") or "")
    raise SystemExit("You must provide a prompt or --prompt-file.")


def add_common_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--model", default="m/27")
    parser.add_argument("--minimax-env-file", default=str(DEFAULT_MINIMAX_ENV))
    # Backward-compat alias so existing Wafer docs/commands keep working.
    parser.add_argument("--wafer-env-file", dest="wafer_env_file", default="")
    parser.add_argument("--tools", default="default")
    parser.add_argument("--web-mode", choices=("minimax", "exa", "mixed", "none"), default="minimax")
    parser.add_argument("--allowed-tools", default="Read,Write,Edit")
    parser.add_argument("--disallowed-tools", default="")
    parser.add_argument(
        "--output-format", choices=("text", "json", "stream-json"), default="text"
    )
    parser.add_argument("--effort", choices=("low", "medium", "high", "max"), default="low")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--sandbox", choices=("local", "daytona", "e2b"), default="local")
    parser.add_argument("--sandbox-policy", choices=("fresh", "pool", "reuse"), default="fresh")
    parser.add_argument("--sandbox-prefix", default="claude-mini")
    parser.add_argument("--sandbox-name")
    parser.add_argument("--sandbox-id")
    parser.add_argument("--daytona-env-file", default=str(ROOT / ".env.daytona"))
    parser.add_argument("--e2b-api-key-file", default="")
    parser.add_argument("--e2b-sandbox-timeout", type=int, default=3600)
    parser.add_argument("--e2b-remote-root", default="")
    parser.add_argument("--dry-run", action="store_true")


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="claude-mini: MiniMax-backed Claude wrapper with optional Daytona sandboxing."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    ask = sub.add_parser("ask", help="Run one claude-mini prompt.")
    add_common_args(ask)
    ask.add_argument("--alias")
    ask.add_argument("--session-id")
    ask.add_argument("--create-session", action=argparse.BooleanOptionalAction, default=False)
    add_prompt_args(ask)

    start = sub.add_parser("start", help="Start a persistent claude-mini conversation.")
    add_common_args(start)
    start.add_argument("--alias")
    start.add_argument("--session-id")
    start.add_argument("--force-new", action=argparse.BooleanOptionalAction, default=False)
    add_prompt_args(start)

    send = sub.add_parser("send", help="Send a follow-up prompt to an existing conversation.")
    add_common_args(send)
    send.add_argument("--alias")
    send.add_argument("--session-id")
    add_prompt_args(send)

    spawn = sub.add_parser(
        "spawn", help="Spawn multiple claude-mini prompt runs with deterministic sandbox assignment."
    )
    add_common_args(spawn)
    spawn.add_argument("--count", type=int, default=1)
    spawn.add_argument("--max-per-sandbox", type=int, default=5)
    spawn.add_argument("--sandbox-start-index", type=int, default=1)
    spawn.add_argument("--log-dir", default="/tmp/claude-mini-spawn")
    spawn.add_argument("--detach", action="store_true")
    add_prompt_args(spawn)

    audit = sub.add_parser(
        "audit-web",
        help="Run a 3-phase MiniMax web security audit in Daytona with reverse proxy and Chrome remote debugging.",
    )
    add_common_args(audit)
    audit.set_defaults(
        sandbox="daytona",
        sandbox_policy="reuse",
        timeout_seconds=0,
        web_mode="mixed",
        allowed_tools="",
        model="MiniMax-M2.7",
    )
    audit.add_argument("target_url", help="Authorized target URL to audit.")
    audit.add_argument("--phase1-file", default=str(ROOT / "prompts" / "minimax_web_audit_phase1_placeholder.md"))
    audit.add_argument("--guide-file", default=str(ROOT / "prompts" / "minimax_web_audit_guide_placeholder.md"))
    audit.add_argument("--output-root", default=str(ROOT / "data" / "web_audit_runs"))
    audit.add_argument("--run-id", default="")
    audit.add_argument("--chrome-debug-port", type=int, default=0)
    audit.add_argument("--proxy-port", type=int, default=0)
    audit.add_argument("--detach", action="store_true")
    audit.add_argument("--_audit-detached-child", action="store_true", help=argparse.SUPPRESS)

    audit_batch = sub.add_parser(
        "audit-web-batch",
        help="Launch many MiniMax web audits across pooled Daytona sandboxes with per-run isolation.",
    )
    add_common_args(audit_batch)
    audit_batch.set_defaults(
        sandbox="daytona",
        sandbox_policy="reuse",
        timeout_seconds=0,
        web_mode="mixed",
        allowed_tools="",
        model="MiniMax-M2.7",
    )
    audit_batch.add_argument("targets", nargs="*", help="Target URLs to audit.")
    audit_batch.add_argument("--targets-file", default="", help="UTF-8 file with one target URL per line.")
    audit_batch.add_argument("--phase1-file", default=str(ROOT / "prompts" / "minimax_web_audit_phase1_placeholder.md"))
    audit_batch.add_argument("--guide-file", default=str(ROOT / "prompts" / "minimax_web_audit_guide_placeholder.md"))
    audit_batch.add_argument("--output-root", default=str(ROOT / "data" / "web_audit_runs"))
    audit_batch.add_argument("--fleet-root", default=str(WEB_AUDIT_FLEET_ROOT))
    audit_batch.add_argument("--db-path", default=str(DEFAULT_DAYTONA_LEASE_DB))
    audit_batch.add_argument("--pool-name", default="default")
    audit_batch.add_argument("--pool-limit", type=int, default=25)
    audit_batch.add_argument("--run-id", default="", help="Optional fleet launch id.")
    audit_batch.add_argument("--isolation-mode", choices=("shared", "strict"), default="shared")
    audit_batch.add_argument("--max-per-sandbox", type=int, default=4, help="Packed audits per sandbox in shared mode.")
    audit_batch.add_argument("--sandbox-count", type=int, default=0, help="Override total sandbox count. Defaults from targets/max-per-sandbox.")
    audit_batch.add_argument("--poll-seconds", type=int, default=15)
    audit_batch.add_argument("--detach", action="store_true")
    audit_batch.add_argument("--_audit-batch-detached-child", action="store_true", help=argparse.SUPPRESS)

    dossiers = sub.add_parser(
        "dossiers",
        help="Enqueue and launch per-attendee full 3-pass MiniMax dossier workers on Daytona.",
    )
    dossiers.add_argument("--queue-name", default="sf-minimax-exa")
    dossiers.add_argument("--people-file", default="", help="JSON people file. Defaults to latest deduped SF top_2000.")
    dossiers.add_argument("--limit", type=int, default=0, help="Number of people to enqueue from the people file. 0 means all rows in the file.")
    dossiers.add_argument("--offset", type=int, default=0)
    dossiers.add_argument("--enqueue-only", action="store_true")
    dossiers.add_argument("--launch-only", action="store_true")
    dossiers.add_argument("--model", default="MiniMax-M2.7")
    dossiers.add_argument("--minimax-env-file", default=str(DEFAULT_MINIMAX_ENV))
    dossiers.add_argument("--daytona-env-file", default=str(ROOT / ".env.daytona"))
    dossiers.add_argument("--sandbox-prefix", default="cv-rank-minimax")
    dossiers.add_argument("--sandbox-start-index", type=int, default=1)
    dossiers.add_argument("--sandbox-count", type=int, default=5)
    dossiers.add_argument("--workers-per-sandbox", type=int, default=5)
    dossiers.add_argument("--web-mode", choices=("exa", "mixed", "claude"), default="exa")
    dossiers.add_argument("--timeout-seconds", type=int, default=1800)
    dossiers.add_argument("--cpu", type=int, default=4)
    dossiers.add_argument("--memory", type=int, default=8)
    dossiers.add_argument("--disk", type=int, default=10)
    dossiers.add_argument("--log-dir", default="/tmp/daytona-minimax-queue-workers")
    dossiers.add_argument("--dry-run", action="store_true")

    help_cmd = sub.add_parser("help", help="Show help for claude-mini or a specific command.")
    help_cmd.add_argument("topic", nargs="?", choices=("ask", "start", "send", "spawn", "audit-web", "audit-web-batch", "dossiers"))

    return parser


def parse_args() -> argparse.Namespace:
    args = build_parser().parse_args()
    if getattr(args, "wafer_env_file", ""):
        args.minimax_env_file = str(args.wafer_env_file)
    if getattr(args, "command", "") in {"ask", "start", "send", "spawn"}:
        prompt_file = str(getattr(args, "prompt_file", "") or "").strip()
        if prompt_file:
            args.prompt_file = str(Path(prompt_file).expanduser().resolve())
        args.prompt = resolve_prompt(args)
    return args


def build_local_ask_cmd(args: argparse.Namespace) -> list[str]:
    cmd = [
        "python3",
        str(AGENT_SCRIPT),
        "ask",
        "--tools",
        args.tools,
        "--output-format",
        args.output_format,
        "--model",
        normalize_model(args.model),
        "--effort",
        args.effort,
    ]
    if args.allowed_tools:
        cmd.extend(["--allowed-tools", args.allowed_tools])
    if args.disallowed_tools:
        cmd.extend(["--disallowed-tools", args.disallowed_tools])
    if getattr(args, "alias", None):
        cmd.extend(["--alias", args.alias])
    if getattr(args, "session_id", None):
        cmd.extend(["--session-id", args.session_id])
    if getattr(args, "create_session", False):
        cmd.append("--create-session")
    cmd.append(args.prompt)
    return cmd


def build_local_start_cmd(args: argparse.Namespace) -> list[str]:
    cmd = [
        "python3",
        str(AGENT_SCRIPT),
        "start",
        "--tools",
        args.tools,
        "--output-format",
        args.output_format,
        "--model",
        normalize_model(args.model),
        "--effort",
        args.effort,
    ]
    if args.allowed_tools:
        cmd.extend(["--allowed-tools", args.allowed_tools])
    if args.disallowed_tools:
        cmd.extend(["--disallowed-tools", args.disallowed_tools])
    if getattr(args, "alias", None):
        cmd.extend(["--alias", args.alias])
    if getattr(args, "session_id", None):
        cmd.extend(["--session-id", args.session_id])
    cmd.extend(["--force-new" if getattr(args, "force_new", False) else "--no-force-new"])
    cmd.append(args.prompt)
    return cmd


def build_local_send_cmd(args: argparse.Namespace) -> list[str]:
    cmd = [
        "python3",
        str(AGENT_SCRIPT),
        "send",
        "--tools",
        args.tools,
        "--output-format",
        args.output_format,
        "--model",
        normalize_model(args.model),
        "--effort",
        args.effort,
    ]
    if args.allowed_tools:
        cmd.extend(["--allowed-tools", args.allowed_tools])
    if args.disallowed_tools:
        cmd.extend(["--disallowed-tools", args.disallowed_tools])
    if getattr(args, "alias", None):
        cmd.extend(["--alias", args.alias])
    if getattr(args, "session_id", None):
        cmd.extend(["--session-id", args.session_id])
    cmd.append(args.prompt)
    return cmd


def build_daytona_ask_cmd(
    args: argparse.Namespace,
    *,
    sandbox_name: str | None = None,
    sandbox_policy: str | None = None,
) -> list[str]:
    cmd = [
        daytona_python_bin(),
        str(DAYTONA_RUNNER),
        "--sandbox-policy",
        sandbox_policy or args.sandbox_policy,
        "--sandbox-prefix",
        args.sandbox_prefix,
        "--model",
        normalize_model(args.model),
        "--local-minimax-env",
        str(Path(args.minimax_env_file).expanduser().resolve()),
        "--tools",
        args.tools,
        "--web-mode",
        args.web_mode,
        "--allowed-tools",
        args.allowed_tools,
        "--output-format",
        args.output_format,
        "--effort",
        args.effort,
        "--timeout-seconds",
        str(args.timeout_seconds),
        "--command",
        "ask",
    ]
    if args.disallowed_tools:
        cmd.extend(["--disallowed-tools", args.disallowed_tools])
    if getattr(args, "create_session", False):
        cmd.append("--create-session")
    if getattr(args, "alias", None):
        cmd.extend(["--alias", args.alias])
    if getattr(args, "session_id", None):
        cmd.extend(["--session-id", args.session_id])
    if args.sandbox_id:
        cmd.extend(["--sandbox-id", args.sandbox_id])
    if sandbox_name:
        cmd.extend(["--sandbox-name", sandbox_name])
    elif args.sandbox_name:
        cmd.extend(["--sandbox-name", args.sandbox_name])
    if args.dry_run:
        cmd.append("--dry-run")
    if getattr(args, "prompt_file", None):
        cmd.extend(["--prompt-file", str(args.prompt_file)])
    else:
        cmd.append(args.prompt)
    return cmd


def build_daytona_start_cmd(
    args: argparse.Namespace,
    *,
    sandbox_name: str | None = None,
    sandbox_policy: str | None = None,
) -> list[str]:
    cmd = [
        daytona_python_bin(),
        str(DAYTONA_RUNNER),
        "--sandbox-policy",
        sandbox_policy or args.sandbox_policy,
        "--sandbox-prefix",
        args.sandbox_prefix,
        "--model",
        normalize_model(args.model),
        "--local-minimax-env",
        str(Path(args.minimax_env_file).expanduser().resolve()),
        "--tools",
        args.tools,
        "--web-mode",
        args.web_mode,
        "--allowed-tools",
        args.allowed_tools,
        "--output-format",
        args.output_format,
        "--effort",
        args.effort,
        "--timeout-seconds",
        str(args.timeout_seconds),
        "--command",
        "start",
    ]
    if args.disallowed_tools:
        cmd.extend(["--disallowed-tools", args.disallowed_tools])
    cmd.extend(["--force-new" if getattr(args, "force_new", False) else "--no-force-new"])
    if getattr(args, "alias", None):
        cmd.extend(["--alias", args.alias])
    if getattr(args, "session_id", None):
        cmd.extend(["--session-id", args.session_id])
    if args.sandbox_id:
        cmd.extend(["--sandbox-id", args.sandbox_id])
    if sandbox_name:
        cmd.extend(["--sandbox-name", sandbox_name])
    elif args.sandbox_name:
        cmd.extend(["--sandbox-name", args.sandbox_name])
    if args.dry_run:
        cmd.append("--dry-run")
    if getattr(args, "prompt_file", None):
        cmd.extend(["--prompt-file", str(args.prompt_file)])
    else:
        cmd.append(args.prompt)
    return cmd


def build_daytona_send_cmd(
    args: argparse.Namespace,
    *,
    sandbox_name: str | None = None,
    sandbox_policy: str | None = None,
) -> list[str]:
    cmd = [
        daytona_python_bin(),
        str(DAYTONA_RUNNER),
        "--sandbox-policy",
        sandbox_policy or args.sandbox_policy,
        "--sandbox-prefix",
        args.sandbox_prefix,
        "--model",
        normalize_model(args.model),
        "--local-minimax-env",
        str(Path(args.minimax_env_file).expanduser().resolve()),
        "--tools",
        args.tools,
        "--web-mode",
        args.web_mode,
        "--allowed-tools",
        args.allowed_tools,
        "--output-format",
        args.output_format,
        "--effort",
        args.effort,
        "--timeout-seconds",
        str(args.timeout_seconds),
        "--command",
        "send",
    ]
    if args.disallowed_tools:
        cmd.extend(["--disallowed-tools", args.disallowed_tools])
    if getattr(args, "alias", None):
        cmd.extend(["--alias", args.alias])
    if getattr(args, "session_id", None):
        cmd.extend(["--session-id", args.session_id])
    if args.sandbox_id:
        cmd.extend(["--sandbox-id", args.sandbox_id])
    if sandbox_name:
        cmd.extend(["--sandbox-name", sandbox_name])
    elif args.sandbox_name:
        cmd.extend(["--sandbox-name", args.sandbox_name])
    if args.dry_run:
        cmd.append("--dry-run")
    if getattr(args, "prompt_file", None):
        cmd.extend(["--prompt-file", str(args.prompt_file)])
    else:
        cmd.append(args.prompt)
    return cmd


def build_e2b_base_cmd(args: argparse.Namespace, *, command: str) -> list[str]:
    cmd = [
        str(E2B_RUNNER),
        command,
        "--model",
        normalize_model(args.model),
        "--minimax-env-file",
        str(Path(args.minimax_env_file).expanduser().resolve()),
        "--tools",
        args.tools,
        "--web-mode",
        args.web_mode,
        "--allowed-tools",
        args.allowed_tools,
        "--output-format",
        args.output_format,
        "--effort",
        args.effort,
        "--timeout-seconds",
        str(args.timeout_seconds),
        "--sandbox-timeout",
        str(args.e2b_sandbox_timeout),
    ]
    if args.disallowed_tools:
        cmd.extend(["--disallowed-tools", args.disallowed_tools])
    if args.e2b_api_key_file:
        cmd.extend(["--api-key-file", str(Path(args.e2b_api_key_file).expanduser().resolve())])
    if args.e2b_remote_root:
        cmd.extend(["--remote-root", args.e2b_remote_root])
    return cmd


def build_e2b_ask_cmd(
    args: argparse.Namespace,
    *,
    sandbox_name: str | None = None,
) -> list[str]:
    cmd = build_e2b_base_cmd(args, command="ask")
    if getattr(args, "create_session", False):
        cmd.append("--create-session")
    if getattr(args, "alias", None):
        cmd.extend(["--alias", args.alias])
    if getattr(args, "session_id", None):
        cmd.extend(["--session-id", args.session_id])
    if args.sandbox_id:
        cmd.extend(["--sandbox-id", args.sandbox_id])
    if sandbox_name:
        cmd.extend(["--sandbox-name", sandbox_name])
    elif args.sandbox_name:
        cmd.extend(["--sandbox-name", args.sandbox_name])
    if args.dry_run:
        cmd.append("--dry-run")
    if getattr(args, "prompt_file", None):
        cmd.extend(["--prompt-file", str(args.prompt_file)])
    else:
        cmd.append(args.prompt)
    return cmd


def build_e2b_start_cmd(
    args: argparse.Namespace,
    *,
    sandbox_name: str | None = None,
) -> list[str]:
    cmd = build_e2b_base_cmd(args, command="start")
    if getattr(args, "force_new", False):
        cmd.append("--force-new")
    if getattr(args, "alias", None):
        cmd.extend(["--alias", args.alias])
    if getattr(args, "session_id", None):
        cmd.extend(["--session-id", args.session_id])
    if args.sandbox_id:
        cmd.extend(["--sandbox-id", args.sandbox_id])
    if sandbox_name:
        cmd.extend(["--sandbox-name", sandbox_name])
    elif args.sandbox_name:
        cmd.extend(["--sandbox-name", args.sandbox_name])
    if args.dry_run:
        cmd.append("--dry-run")
    if getattr(args, "prompt_file", None):
        cmd.extend(["--prompt-file", str(args.prompt_file)])
    else:
        cmd.append(args.prompt)
    return cmd


def build_e2b_send_cmd(
    args: argparse.Namespace,
    *,
    sandbox_name: str | None = None,
) -> list[str]:
    cmd = build_e2b_base_cmd(args, command="send")
    if getattr(args, "alias", None):
        cmd.extend(["--alias", args.alias])
    if getattr(args, "session_id", None):
        cmd.extend(["--session-id", args.session_id])
    if args.sandbox_id:
        cmd.extend(["--sandbox-id", args.sandbox_id])
    if sandbox_name:
        cmd.extend(["--sandbox-name", sandbox_name])
    elif args.sandbox_name:
        cmd.extend(["--sandbox-name", args.sandbox_name])
    if args.dry_run:
        cmd.append("--dry-run")
    if getattr(args, "prompt_file", None):
        cmd.extend(["--prompt-file", str(args.prompt_file)])
    else:
        cmd.append(args.prompt)
    return cmd


def build_e2b_audit_web_cmd(args: argparse.Namespace, *, run_id: str) -> list[str]:
    cmd = build_e2b_base_cmd(args, command="audit-web")
    cmd.extend(
        [
            args.target_url,
            "--phase1-file",
            str(Path(args.phase1_file).expanduser().resolve()),
            "--guide-file",
            str(Path(args.guide_file).expanduser().resolve()),
            "--output-root",
            str(Path(args.output_root).expanduser().resolve()),
            "--run-id",
            run_id,
            "--chrome-debug-port",
            str(args.chrome_debug_port),
            "--proxy-port",
            str(args.proxy_port),
        ]
    )
    derived_sandbox_name = None
    if args.sandbox_policy == "reuse" and not args.sandbox_id and not args.sandbox_name:
        derived_sandbox_name = audit_target_sandbox_name(args.target_url, args.sandbox_prefix)
    if args.sandbox_id:
        cmd.extend(["--sandbox-id", args.sandbox_id])
    elif args.sandbox_name or derived_sandbox_name:
        cmd.extend(["--sandbox-name", args.sandbox_name or derived_sandbox_name])
    if args.dry_run:
        cmd.append("--dry-run")
    return cmd


def render_cmd(cmd: list[str]) -> str:
    return shlex.join(cmd)


def web_audit_live_dir(run_id: str) -> Path:
    path = WEB_AUDIT_LIVE_ROOT / run_id
    path.mkdir(parents=True, exist_ok=True)
    return path


def write_web_audit_state(path: Path, payload: dict[str, object]) -> None:
    path.write_text(
        json_dumps(payload) + "\n",
        encoding="utf-8",
    )


def json_dumps(payload: dict[str, object]) -> str:
    return json.dumps(payload, ensure_ascii=False, indent=2)


def render_stream_json_line(raw_line: str) -> list[str]:
    line = raw_line.strip()
    if not line or not line.startswith("{"):
        return []
    try:
        payload = json.loads(line)
    except Exception:
        return []
    if not isinstance(payload, dict):
        return []
    event_type = str(payload.get("type") or "").strip()
    rendered: list[str] = []
    if event_type == "assistant":
        message = payload.get("message")
        if not isinstance(message, dict):
            return []
        for item in message.get("content") or []:
            if not isinstance(item, dict):
                continue
            content_type = str(item.get("type") or "").strip()
            if content_type == "text":
                text = str(item.get("text") or "").strip()
                if text:
                    rendered.append(f"[assistant] {text}")
            elif content_type == "tool_use":
                name = str(item.get("name") or "tool").strip()
                rendered.append(f"[tool_use] {name}")
            elif content_type == "thinking":
                rendered.append("[thinking] hidden")
    elif event_type == "result":
        subtype = str(payload.get("subtype") or "result").strip()
        terminal_reason = str(payload.get("terminal_reason") or "").strip()
        stop_reason = str(payload.get("stop_reason") or "").strip()
        summary = f"[result] {subtype}"
        extras = [part for part in (terminal_reason, stop_reason) if part]
        if extras:
            summary += f" ({', '.join(extras)})"
        rendered.append(summary)
    elif event_type == "system":
        subtype = str(payload.get("subtype") or "system").strip()
        rendered.append(f"[system] {subtype}")
    return rendered


def assign_spawn_sandbox_name(
    args: argparse.Namespace,
    *,
    run_id: str,
    worker_index: int,
) -> tuple[str | None, str]:
    if args.sandbox == "local":
        return None, "local"
    if args.sandbox_policy == "reuse":
        if not args.sandbox_name and not args.sandbox_id:
            raise SystemExit(
                "sandbox-policy=reuse requires --sandbox-name or --sandbox-id."
            )
        return args.sandbox_name, "reuse"
    if args.sandbox_policy == "fresh":
        return f"{args.sandbox_prefix}-{run_id}-w{worker_index + 1:02d}", "fresh"
    # pool
    max_per = max(1, args.max_per_sandbox)
    sandbox_num = args.sandbox_start_index + (worker_index // max_per)
    return f"{args.sandbox_prefix}-{run_id}-{sandbox_num:02d}", "pool"


def run_ask(args: argparse.Namespace) -> int:
    env = base_env(args.minimax_env_file, args.model)
    env["MINIMAX_KEY_SEED"] = minimax_key_seed(args, mode="ask")
    if args.sandbox == "daytona":
        merge_env_defaults(env, read_env_file(Path(args.daytona_env_file).expanduser().resolve()))
    if args.sandbox == "local":
        cmd = build_local_ask_cmd(args)
    elif args.sandbox == "e2b":
        cmd = build_e2b_ask_cmd(args)
    else:
        cmd = build_daytona_ask_cmd(args)
    if args.dry_run:
        print(f"mode={args.sandbox}")
        print(f"model={normalize_model(args.model)}")
        print(f"command={render_cmd(cmd)}")
        return 0
    result = subprocess.run(cmd, env=env, check=False)
    return int(result.returncode)


def run_start(args: argparse.Namespace) -> int:
    env = base_env(args.minimax_env_file, args.model)
    env["MINIMAX_KEY_SEED"] = minimax_key_seed(args, mode="start")
    if args.sandbox == "daytona":
        merge_env_defaults(env, read_env_file(Path(args.daytona_env_file).expanduser().resolve()))
    if args.sandbox == "local":
        cmd = build_local_start_cmd(args)
    elif args.sandbox == "e2b":
        cmd = build_e2b_start_cmd(args)
    else:
        cmd = build_daytona_start_cmd(args)
    if args.dry_run:
        print(f"mode={args.sandbox}")
        print("command=start")
        print(f"model={normalize_model(args.model)}")
        print(f"command={render_cmd(cmd)}")
        return 0
    result = subprocess.run(cmd, env=env, check=False)
    return int(result.returncode)


def run_send(args: argparse.Namespace) -> int:
    env = base_env(args.minimax_env_file, args.model)
    env["MINIMAX_KEY_SEED"] = minimax_key_seed(args, mode="send")
    if args.sandbox == "daytona":
        merge_env_defaults(env, read_env_file(Path(args.daytona_env_file).expanduser().resolve()))
    if args.sandbox == "local":
        cmd = build_local_send_cmd(args)
    elif args.sandbox == "e2b":
        cmd = build_e2b_send_cmd(args)
    else:
        cmd = build_daytona_send_cmd(args)
    if args.dry_run:
        print(f"mode={args.sandbox}")
        print("command=send")
        print(f"model={normalize_model(args.model)}")
        print(f"command={render_cmd(cmd)}")
        return 0
    result = subprocess.run(cmd, env=env, check=False)
    return int(result.returncode)


def run_spawn(args: argparse.Namespace) -> int:
    base = base_env(args.minimax_env_file, args.model)
    if args.sandbox == "daytona":
        merge_env_defaults(base, read_env_file(Path(args.daytona_env_file).expanduser().resolve()))
    count = max(1, int(args.count))
    run_id = f"{time.strftime('%Y%m%d%H%M%S')}-{uuid.uuid4().hex[:6]}"
    log_root = Path(args.log_dir).expanduser().resolve() / run_id
    log_root.mkdir(parents=True, exist_ok=True)
    procs: list[tuple[int, str | None, Path, subprocess.Popen[bytes]]] = []

    for idx in range(count):
        sandbox_name, policy = assign_spawn_sandbox_name(args, run_id=run_id, worker_index=idx)
        if args.sandbox == "local":
            cmd = build_local_ask_cmd(args)
        elif args.sandbox == "e2b":
            cmd = build_e2b_ask_cmd(
                args,
                sandbox_name=sandbox_name,
            )
        else:
            cmd = build_daytona_ask_cmd(
                args,
                sandbox_name=sandbox_name,
                sandbox_policy=policy,
            )
        log_path = log_root / f"worker-{idx + 1:02d}.log"
        env = base.copy()
        env["MINIMAX_KEY_SEED"] = minimax_key_seed(args, mode="spawn", worker_index=idx + 1)
        sandbox_label = sandbox_name or args.sandbox_id or ("local" if args.sandbox == "local" else args.sandbox)
        if args.dry_run:
            print(
                f"worker={idx + 1} sandbox={sandbox_label} policy={policy} command={render_cmd(cmd)}"
            )
            continue
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
        procs.append((idx + 1, sandbox_label, log_path, proc))
        print(
            f"launched worker={idx + 1} pid={proc.pid} sandbox={sandbox_label} log={log_path}"
        )

    if args.dry_run:
        return 0
    if args.detach:
        print(f"detached=1 run_id={run_id} log_root={log_root}")
        return 0

    exit_code = 0
    for worker, sandbox_name, log_path, proc in procs:
        rc = proc.wait()
        print(
            f"finished worker={worker} rc={rc} sandbox={sandbox_name} log={log_path}"
        )
        if rc != 0:
            exit_code = rc
    print(f"run_id={run_id} workers={count} log_root={log_root}")
    return int(exit_code)


def build_daytona_audit_web_cmd(args: argparse.Namespace, *, run_id: str) -> list[str]:
    cmd = [
        daytona_python_bin(),
        str(WEB_AUDIT_DAYTONA_RUNNER),
        args.target_url,
        "--sandbox-policy",
        args.sandbox_policy,
        "--sandbox-prefix",
        args.sandbox_prefix,
        "--model",
        normalize_model(args.model),
        "--local-minimax-env",
        str(Path(args.minimax_env_file).expanduser().resolve()),
        "--tools",
        args.tools,
        "--web-mode",
        args.web_mode,
        "--allowed-tools",
        args.allowed_tools,
        "--effort",
        args.effort,
        "--timeout-seconds",
        str(args.timeout_seconds),
        "--local-phase1-file",
        str(Path(args.phase1_file).expanduser().resolve()),
        "--local-guide-file",
        str(Path(args.guide_file).expanduser().resolve()),
        "--local-output-root",
        str(Path(args.output_root).expanduser().resolve()),
        "--run-id",
        run_id,
        "--chrome-debug-port",
        str(args.chrome_debug_port),
        "--proxy-port",
        str(args.proxy_port),
    ]
    if args.disallowed_tools:
        cmd.extend(["--disallowed-tools", args.disallowed_tools])
    derived_sandbox_name = None
    if args.sandbox_policy == "reuse" and not args.sandbox_id and not args.sandbox_name:
        derived_sandbox_name = audit_target_sandbox_name(args.target_url, args.sandbox_prefix)
    if args.sandbox_id:
        cmd.extend(["--sandbox-id", args.sandbox_id])
    elif args.sandbox_name or derived_sandbox_name:
        cmd.extend(["--sandbox-name", args.sandbox_name or derived_sandbox_name])
    if args.dry_run:
        cmd.append("--dry-run")
    return cmd


def build_daytona_audit_web_batch_child_cmd(
    args: argparse.Namespace,
    *,
    target_url: str,
    run_id: str,
    sandbox_name: str,
) -> list[str]:
    cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        "audit-web",
        target_url,
        "--run-id",
        run_id,
        "--sandbox",
        "daytona",
        "--sandbox-policy",
        "reuse",
        "--sandbox-name",
        sandbox_name,
        "--model",
        normalize_model(args.model),
        "--minimax-env-file",
        str(Path(args.minimax_env_file).expanduser().resolve()),
        "--daytona-env-file",
        str(Path(args.daytona_env_file).expanduser().resolve()),
        "--tools",
        args.tools,
        "--web-mode",
        args.web_mode,
        "--allowed-tools",
        args.allowed_tools,
        "--effort",
        args.effort,
        "--timeout-seconds",
        str(args.timeout_seconds),
        "--phase1-file",
        str(Path(args.phase1_file).expanduser().resolve()),
        "--guide-file",
        str(Path(args.guide_file).expanduser().resolve()),
        "--output-root",
        str(Path(args.output_root).expanduser().resolve()),
        "--chrome-debug-port",
        "0",
        "--proxy-port",
        "0",
    ]
    if args.disallowed_tools:
        cmd.extend(["--disallowed-tools", args.disallowed_tools])
    return cmd


def write_audit_batch_state(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def run_audit_web(args: argparse.Namespace) -> int:
    if args.sandbox not in {"daytona", "e2b"}:
        raise SystemExit("audit-web currently supports only --sandbox daytona or --sandbox e2b.")
    env = base_env(args.minimax_env_file, args.model)
    env["MINIMAX_KEY_SEED"] = "|".join(
        [
            "audit-web",
            normalize_model(args.model),
            str(args.target_url),
            str(args.run_id or ""),
        ]
    )
    run_id = (str(args.run_id or "").strip() or f"web-audit-{time.strftime('%Y%m%dT%H%M%SZ')}")
    live_dir = web_audit_live_dir(run_id)
    log_path = live_dir / "launch.log"
    events_path = live_dir / "messages.log"
    state_path = live_dir / "state.json"
    if args.sandbox == "daytona":
        merge_env_defaults(env, read_env_file(Path(args.daytona_env_file).expanduser().resolve()))
        cmd = build_daytona_audit_web_cmd(args, run_id=run_id)
    else:
        cmd = build_e2b_audit_web_cmd(args, run_id=run_id)
    if args.dry_run:
        print(f"mode={args.sandbox}")
        print("command=audit-web")
        print(f"model={normalize_model(args.model)}")
        print(f"run_id={run_id}")
        print(f"command={render_cmd(cmd)}")
        return 0
    state: dict[str, object] = {
        "run_id": run_id,
        "target_url": str(args.target_url),
        "pid": None,
        "started_at_epoch": time.time(),
        "updated_at_epoch": time.time(),
        "command": cmd,
        "mode": args.sandbox,
        "status": "launching",
    }
    write_web_audit_state(state_path, state)
    if args.detach and not args._audit_detached_child:
        child_argv = [token for token in sys.argv[1:] if token != "--detach"]
        child_argv.append("--_audit-detached-child")
        child_cmd = [sys.executable, str(Path(__file__).resolve()), *child_argv]
        proc = subprocess.Popen(
            child_cmd,
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            env={**env, "PYTHONUNBUFFERED": "1"},
            start_new_session=True,
            close_fds=True,
        )
        state["pid"] = proc.pid
        state["status"] = "running"
        state["updated_at_epoch"] = time.time()
        write_web_audit_state(state_path, state)
        print(f"detached=1 run_id={run_id} pid={proc.pid} live_dir={live_dir}")
        return 0
    try:
        with log_path.open("ab") as log_file:
            with events_path.open("a", encoding="utf-8") as events_file:
                proc = subprocess.Popen(
                    cmd,
                    cwd=ROOT,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    env={**env, "PYTHONUNBUFFERED": "1"},
                    text=True,
                    bufsize=1,
                )
                state["pid"] = proc.pid
                state["status"] = "running"
                state["updated_at_epoch"] = time.time()
                write_web_audit_state(state_path, state)
                assert proc.stdout is not None
                for chunk in proc.stdout:
                    sys.stdout.write(chunk)
                    sys.stdout.flush()
                    data = chunk.encode("utf-8", errors="replace")
                    log_file.write(data)
                    log_file.flush()
                    for event_line in render_stream_json_line(chunk):
                        events_file.write(event_line + "\n")
                        events_file.flush()
                    state["updated_at_epoch"] = time.time()
                    write_web_audit_state(state_path, state)
                rc = proc.wait()
    except Exception as exc:
        state["updated_at_epoch"] = time.time()
        state["status"] = "failed"
        state["error"] = f"{type(exc).__name__}: {exc}"
        write_web_audit_state(state_path, state)
        raise
    state["updated_at_epoch"] = time.time()
    state["returncode"] = rc
    state["status"] = "completed" if rc == 0 else "failed"
    completed_path = Path(args.output_root).expanduser().resolve() / run_id
    if completed_path.exists():
        state["completed_run_path"] = str(completed_path)
        if completed_path.is_dir():
            completed_live_path = live_dir / "completed-run"
            if completed_live_path.exists() or completed_live_path.is_symlink():
                completed_live_path.unlink()
            try:
                completed_live_path.symlink_to(completed_path, target_is_directory=True)
            except OSError:
                pass
    write_web_audit_state(state_path, state)
    return int(rc)


def run_audit_web_batch(args: argparse.Namespace) -> int:
    if args.sandbox != "daytona":
        raise SystemExit("audit-web-batch currently supports only --sandbox daytona.")
    targets = load_audit_targets(inline=list(args.targets or []), file_path=str(args.targets_file or ""))
    max_per = 1 if args.isolation_mode == "strict" else max(1, int(args.max_per_sandbox or 1))
    sandbox_count = int(args.sandbox_count or 0)
    if sandbox_count <= 0:
        sandbox_count = (len(targets) + max_per - 1) // max_per
    if sandbox_count <= 0:
        raise SystemExit("sandbox_count resolved to 0.")
    capacity = sandbox_count * max_per
    if capacity < len(targets):
        raise SystemExit(
            f"Requested {len(targets)} targets but only {capacity} slots available from "
            f"sandbox_count={sandbox_count} and max_per_sandbox={max_per}."
        )
    launch_id = str(args.run_id or "").strip() or f"web-audit-batch-{time.strftime('%Y%m%dT%H%M%SZ')}"
    fleet_root = Path(args.fleet_root).expanduser().resolve()
    launch_dir = fleet_root / launch_id
    logs_dir = launch_dir / "logs"
    logs_dir.mkdir(parents=True, exist_ok=True)
    state_path = launch_dir / "state.json"
    manifest_path = launch_dir / "manifest.json"
    db_path = Path(args.db_path).expanduser().resolve()
    sandbox_names = audit_batch_sandbox_names(
        launch_id=launch_id,
        sandbox_prefix=args.sandbox_prefix,
        count=sandbox_count,
    )
    sandbox_specs = [{"sandbox_name": name, "lane": "web-audit"} for name in sandbox_names]
    manifest: dict[str, object] = {
        "launch_id": launch_id,
        "targets": [],
        "sandbox_names": sandbox_names,
        "isolation_mode": args.isolation_mode,
        "max_per_sandbox": max_per,
        "sandbox_count": sandbox_count,
        "pool_name": args.pool_name,
        "pool_limit": int(args.pool_limit),
        "created_at_epoch": time.time(),
    }
    for index, target in enumerate(targets, start=1):
        sandbox_name = sandbox_names[(index - 1) // max_per]
        run_id = audit_run_id(target, launch_id=launch_id, index=index)
        log_path = logs_dir / f"{run_id}.log"
        manifest["targets"].append(
            {
                "index": index,
                "target_url": target,
                "run_id": run_id,
                "sandbox_name": sandbox_name,
                "log_path": str(log_path),
                "pid": None,
                "returncode": None,
                "status": "pending",
            }
        )
    if args.dry_run:
        print(json.dumps(manifest, ensure_ascii=False, indent=2))
        return 0

    state: dict[str, object] = {
        "launch_id": launch_id,
        "pid": None,
        "status": "launching",
        "started_at_epoch": time.time(),
        "updated_at_epoch": time.time(),
        "fleet_dir": str(launch_dir),
        "target_count": len(targets),
        "sandbox_count": sandbox_count,
        "isolation_mode": args.isolation_mode,
        "pool_name": args.pool_name,
    }
    write_audit_batch_state(state_path, state)
    if args.detach and not args._audit_batch_detached_child:
        child_argv = [token for token in sys.argv[1:] if token != "--detach"]
        child_argv.append("--_audit-batch-detached-child")
        proc = subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), *child_argv],
            cwd=ROOT,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            start_new_session=True,
            close_fds=True,
            env={**os.environ, "PYTHONUNBUFFERED": "1"},
        )
        state["pid"] = proc.pid
        state["status"] = "running"
        state["updated_at_epoch"] = time.time()
        write_audit_batch_state(state_path, state)
        print(f"detached=1 launch_id={launch_id} pid={proc.pid} fleet_dir={launch_dir}")
        return 0

    reserve_launch(
        db_path,
        pool_name=str(args.pool_name),
        sandbox_limit=int(args.pool_limit),
        launch_id=launch_id,
        launch_kind="web-audit-batch",
        sandbox_specs=sandbox_specs,
        metadata={
            "launch_dir": str(launch_dir),
            "task_profile": "web-audit-batch",
            "target_count": len(targets),
            "isolation_mode": args.isolation_mode,
            "max_per_sandbox": max_per,
        },
        pool_metadata={"managed_by": "claude_mini.audit-web-batch"},
    )
    activate_launch(
        db_path,
        launch_id=launch_id,
        metadata_patch={
            "launch_dir": str(launch_dir),
            "task_profile": "web-audit-batch",
            "target_count": len(targets),
        },
    )
    env = os.environ.copy()
    procs: list[tuple[subprocess.Popen[bytes], dict[str, object]]] = []
    final_status = "finished"
    try:
        for item in list(manifest["targets"]):
            assert isinstance(item, dict)
            cmd = build_daytona_audit_web_batch_child_cmd(
                args,
                target_url=str(item["target_url"]),
                run_id=str(item["run_id"]),
                sandbox_name=str(item["sandbox_name"]),
            )
            log_path = Path(str(item["log_path"]))
            with log_path.open("ab") as log_file:
                proc = subprocess.Popen(
                    cmd,
                    cwd=ROOT,
                    stdin=subprocess.DEVNULL,
                    stdout=log_file,
                    stderr=log_file,
                    start_new_session=True,
                    close_fds=True,
                    env={**env, "PYTHONUNBUFFERED": "1"},
                )
            item["pid"] = proc.pid
            item["status"] = "running"
            procs.append((proc, item))
        state["pid"] = os.getpid()
        state["status"] = "running"
        state["updated_at_epoch"] = time.time()
        write_audit_batch_state(state_path, state)
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        while True:
            heartbeat_launch(db_path, launch_id=launch_id, status="running")
            all_done = True
            for proc, item in procs:
                rc = proc.poll()
                if rc is None:
                    all_done = False
                    continue
                if item.get("returncode") is None:
                    item["returncode"] = int(rc)
                    item["status"] = "completed" if int(rc) == 0 else "failed"
                    if int(rc) != 0:
                        final_status = "finished_with_failures"
            manifest["updated_at_epoch"] = time.time()
            manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            state["updated_at_epoch"] = time.time()
            state["status"] = "completed" if all_done and final_status == "finished" else "running"
            write_audit_batch_state(state_path, state)
            if all_done:
                break
            time.sleep(max(5, int(args.poll_seconds or 15)))
    except KeyboardInterrupt:
        final_status = "stopped"
        for proc, _ in procs:
            try:
                proc.terminate()
            except Exception:
                pass
        raise
    except Exception:
        final_status = "failed"
        raise
    finally:
        release_launch(db_path, launch_id=launch_id, final_status=final_status)
        state["updated_at_epoch"] = time.time()
        state["status"] = "completed" if final_status == "finished" else final_status
        state["final_status"] = final_status
        write_audit_batch_state(state_path, state)
        manifest["updated_at_epoch"] = time.time()
        manifest["final_status"] = final_status
        manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"launch_id={launch_id} fleet_dir={launch_dir}")
    return 0 if final_status == "finished" else 1


def build_dossier_enqueue_cmd(args: argparse.Namespace, *, people_file: Path) -> list[str]:
    cmd = [
        "python3",
        str(ATTENDEE_QUEUE_SCRIPT),
        "enqueue",
        "--queue-name",
        args.queue_name,
        "--people-file",
        str(people_file),
        "--offset",
        str(args.offset),
    ]
    if int(args.limit or 0) > 0:
        cmd.extend(["--limit", str(args.limit)])
    else:
        cmd.append("--all")
    return cmd


def build_dossier_launch_cmd(args: argparse.Namespace) -> list[str]:
    return [
        "python3",
        str(MINIMAX_DOSSIER_WORKERS),
        "--env-file",
        str(Path(args.daytona_env_file).expanduser().resolve()),
        "--queue-name",
        args.queue_name,
        "--model",
        normalize_model(args.model),
        "--sandbox-prefix",
        args.sandbox_prefix,
        "--sandbox-start-index",
        str(args.sandbox_start_index),
        "--sandbox-count",
        str(args.sandbox_count),
        "--workers-per-sandbox",
        str(args.workers_per_sandbox),
        "--log-dir",
        args.log_dir,
        "--web-mode",
        args.web_mode,
        "--timeout-seconds",
        str(args.timeout_seconds),
        "--minimax-env-file",
        str(Path(args.minimax_env_file).expanduser().resolve()),
        "--cpu",
        str(args.cpu),
        "--memory",
        str(args.memory),
        "--disk",
        str(args.disk),
    ]


def run_dossiers(args: argparse.Namespace) -> int:
    if args.enqueue_only and args.launch_only:
        raise SystemExit("Use at most one of --enqueue-only or --launch-only.")
    people_file = Path(args.people_file).expanduser().resolve() if str(args.people_file).strip() else latest_sf_deduped_people_file()
    enqueue_cmd = build_dossier_enqueue_cmd(args, people_file=people_file)
    launch_cmd = build_dossier_launch_cmd(args)
    if args.dry_run:
        print(f"people_file={people_file}")
        print(f"enqueue_command={render_cmd(enqueue_cmd)}")
        print(f"launch_command={render_cmd(launch_cmd)}")
        return 0

    if not args.launch_only:
        enqueue_result = subprocess.run(enqueue_cmd, cwd=ROOT, check=False)
        if enqueue_result.returncode != 0:
            return int(enqueue_result.returncode)
    if not args.enqueue_only:
        launch_result = subprocess.run(launch_cmd, cwd=ROOT, check=False)
        return int(launch_result.returncode)
    return 0


def run_help(args: argparse.Namespace) -> int:
    parser = build_parser()
    topic = getattr(args, "topic", None)
    if not topic:
        parser.print_help()
        return 0
    for action in parser._actions:  # noqa: SLF001 - argparse internals are stable for this lookup.
        if isinstance(action, argparse._SubParsersAction):  # noqa: SLF001
            sub = action.choices.get(topic)
            if sub is None:
                break
            sub.print_help()
            return 0
    parser.print_help()
    return 1


def main() -> int:
    args = parse_args()
    if args.command == "ask":
        return run_ask(args)
    if args.command == "start":
        return run_start(args)
    if args.command == "send":
        return run_send(args)
    if args.command == "spawn":
        return run_spawn(args)
    if args.command == "audit-web":
        return run_audit_web(args)
    if args.command == "audit-web-batch":
        return run_audit_web_batch(args)
    if args.command == "dossiers":
        return run_dossiers(args)
    if args.command == "help":
        return run_help(args)
    raise SystemExit(f"Unsupported command: {args.command}")


if __name__ == "__main__":
    raise SystemExit(main())
