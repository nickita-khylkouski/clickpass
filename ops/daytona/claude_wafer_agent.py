#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import sqlite3
import subprocess
import sys
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

from minimax_key_selection import (
    build_minimax_runtime_values,
    cooldown_matcher as minimax_cooldown_matcher,
    mark_key_cooldown,
)


CLAUDE_WAFER_BIN = os.environ.get(
    "CLAUDE_WAFER_BIN", "/Users/nickita/.superset/bin/claude-wafer"
)
STATE_ROOT = Path(os.environ.get("CLAUDE_WAFER_ROOT", Path.home() / ".claude-wafer"))
MINIMAX_ENV_FILE = Path(
    os.environ.get("MINIMAX_ENV_FILE", STATE_ROOT / "minimax.env")
)
QUEUE_DB = Path(
    os.environ.get("CLAUDE_WAFER_AGENT_DB", STATE_ROOT / "agent-queue.sqlite3")
)
LOOP_ROOT = Path(
    os.environ.get("CLAUDE_WAFER_LOOP_DIR", STATE_ROOT / "loops")
)


def launcher_kind() -> str:
    explicit = os.environ.get("CLAUDE_WAFER_LAUNCHER_KIND", "").strip().lower()
    if explicit in {"wafer", "plain", "codex", "minimax"}:
        return explicit
    launcher_name = Path(CLAUDE_WAFER_BIN).name.lower()
    if launcher_name == "claude":
        return "plain"
    if launcher_name == "codex":
        return "codex"
    return "wafer"


def _read_simple_env_file(path: Path) -> dict[str, str]:
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


def minimax_settings() -> dict[str, str]:
    model = os.environ.get("MINIMAX_MODEL", "").strip() or "MiniMax-M2.7"
    return build_minimax_runtime_values(
        model=model,
        env_file=MINIMAX_ENV_FILE,
        environ=os.environ,
        seed=os.environ.get("MINIMAX_KEY_SEED", "").strip() or None,
    )


def _split_exa_key_blob(raw: str) -> list[str]:
    keys: list[str] = []
    for line in re.split(r"[\n,]+", raw or ""):
        value = str(line or "").strip().strip("'\"")
        if not value or value.startswith("#"):
            continue
        if re.match(r"^EXA_API_KEY(?:S|_\d+)?=", value):
            value = value.split("=", 1)[1].strip().strip("'\"")
        if value and value not in keys:
            keys.append(value)
    return keys


def discover_local_exa_keys() -> list[str]:
    key_file = Path.home() / ".claude-wafer" / "exa_keys.env"
    if key_file.exists():
        keys: list[str] = []
        for key in _split_exa_key_blob(key_file.read_text(encoding="utf-8")):
            if key not in keys:
                keys.append(key)
        if keys:
            return keys
    keys: list[str] = []
    for env_name in ("EXA_API_KEYS", "EXA_API_KEY"):
        for key in _split_exa_key_blob(os.environ.get(env_name, "")):
            if key not in keys:
                keys.append(key)
    indexed_env = sorted(name for name in os.environ if re.fullmatch(r"EXA_API_KEY_\d+", name))
    for env_name in indexed_env:
        for key in _split_exa_key_blob(os.environ.get(env_name, "")):
            if key not in keys:
                keys.append(key)
    return keys


def choose_local_exa_key(seed: str | None = None) -> str | None:
    keys = discover_local_exa_keys()
    if not keys:
        return None
    if len(keys) == 1:
        return keys[0]
    selector = seed or uuid.uuid4().hex
    index = int(selector, 16) % len(keys) if all(c in "0123456789abcdef" for c in selector.lower()) else hash(selector) % len(keys)
    return keys[index]


def build_process_env() -> dict[str, str]:
    env = os.environ.copy()
    exa_keys = discover_local_exa_keys()
    if exa_keys:
        env["EXA_API_KEYS"] = "\n".join(exa_keys)
    exa_key = choose_local_exa_key(env.get("CLAUDE_WAFER_EXA_SEED"))
    if exa_key:
        env["EXA_API_KEY"] = exa_key
    if launcher_kind() != "minimax":
        return env
    settings = minimax_settings()
    api_key = settings.get("MINIMAX_API_KEY", "").strip()
    if not api_key:
        raise SystemExit(
            f"Missing MINIMAX_API_KEY in {MINIMAX_ENV_FILE} or environment."
        )
    api_host = settings["MINIMAX_API_HOST"].rstrip("/")
    model = settings["MINIMAX_MODEL"].strip()
    env.update(
        {
            "MINIMAX_API_KEY": api_key,
            "MINIMAX_API_HOST": api_host,
            "ANTHROPIC_AUTH_TOKEN": api_key,
            "ANTHROPIC_API_KEY": api_key,
            "ANTHROPIC_BASE_URL": f"{api_host}/anthropic",
            "API_TIMEOUT_MS": settings["API_TIMEOUT_MS"],
            "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": settings[
                "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"
            ],
            "ANTHROPIC_MODEL": model,
            "ANTHROPIC_SMALL_FAST_MODEL": model,
            "ANTHROPIC_DEFAULT_SONNET_MODEL": model,
            "ANTHROPIC_DEFAULT_OPUS_MODEL": model,
            "ANTHROPIC_DEFAULT_HAIKU_MODEL": model,
        }
    )
    return env


def maybe_mark_minimax_cooldown(process_env: dict[str, str], *texts: str) -> None:
    if launcher_kind() != "minimax":
        return
    combined = "\n".join(text for text in texts if text).strip()
    if not combined or not minimax_cooldown_matcher(combined):
        return
    raw_index = str(process_env.get("MINIMAX_KEY_INDEX", "")).strip()
    if not raw_index.isdigit():
        return
    try:
        mark_key_cooldown(
            env_file=MINIMAX_ENV_FILE,
            key_index=int(raw_index),
            reason="rate_limit",
            observed_text=combined,
        )
    except Exception:
        return


def utc_now() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def ensure_parent(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)


def connect_db() -> sqlite3.Connection:
    ensure_parent(QUEUE_DB)
    conn = sqlite3.connect(str(QUEUE_DB))
    conn.row_factory = sqlite3.Row
    # WAL is ideal, but it can fail on nearly full disks. Fall back instead of aborting.
    for journal_mode in ("WAL", "TRUNCATE", "DELETE", "MEMORY"):
        try:
            conn.execute(f"PRAGMA journal_mode={journal_mode}")
            break
        except sqlite3.OperationalError:
            continue
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            alias TEXT PRIMARY KEY,
            session_id TEXT NOT NULL UNIQUE,
            created_at TEXT NOT NULL,
            updated_at TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS jobs (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            alias TEXT,
            session_id TEXT,
            mode TEXT NOT NULL,
            prompt TEXT NOT NULL,
            tools TEXT NOT NULL,
            output_format TEXT NOT NULL,
            status TEXT NOT NULL,
            created_at TEXT NOT NULL,
            started_at TEXT,
            finished_at TEXT,
            result TEXT,
            error TEXT
        )
        """
    )
    conn.commit()
    return conn


@dataclass
class SessionTarget:
    session_id: str | None
    alias: str | None
    mode: str


def get_session_by_alias(conn: sqlite3.Connection, alias: str) -> str | None:
    row = conn.execute(
        "SELECT session_id FROM sessions WHERE alias = ?",
        (alias,),
    ).fetchone()
    return str(row["session_id"]) if row else None


def upsert_session(conn: sqlite3.Connection, alias: str, session_id: str) -> None:
    now = utc_now()
    conn.execute(
        """
        INSERT INTO sessions (alias, session_id, created_at, updated_at)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(alias) DO UPDATE
        SET session_id = excluded.session_id,
            updated_at = excluded.updated_at
        """,
        (alias, session_id, now, now),
    )
    conn.commit()


def touch_session(conn: sqlite3.Connection, alias: str) -> None:
    conn.execute(
        "UPDATE sessions SET updated_at = ? WHERE alias = ?",
        (utc_now(), alias),
    )
    conn.commit()


def resolve_target(
    conn: sqlite3.Connection,
    alias: str | None,
    session_id: str | None,
    create_if_missing: bool,
) -> SessionTarget:
    if alias:
        existing_session_id = get_session_by_alias(conn, alias)
        if existing_session_id:
            return SessionTarget(existing_session_id, alias, "resume")
        if not create_if_missing:
            raise SystemExit(f"Unknown alias: {alias}")
        new_session_id = str(uuid.uuid4())
        upsert_session(conn, alias, new_session_id)
        return SessionTarget(new_session_id, alias, "start")

    if session_id:
        return SessionTarget(session_id, None, "resume")

    if create_if_missing:
        return SessionTarget(str(uuid.uuid4()), None, "start")

    return SessionTarget(None, None, "ask")


def build_claude_command(
    prompt: str,
    mode: str,
    session_id: str | None,
    tools: str,
    output_format: str,
    *,
    launcher_args: list[str] | None = None,
    claude_args: list[str] | None = None,
    no_session_persistence: bool = False,
) -> list[str]:
    if launcher_kind() == "codex":
        raise RuntimeError("build_claude_command does not support codex; use run_codex directly")
    cmd = [CLAUDE_WAFER_BIN]
    if launcher_args:
        cmd.extend(launcher_args)
    if launcher_kind() not in {"plain", "minimax"}:
        cmd.append("--bare")
    cmd.extend(
        [
            "-p",
            "--output-format",
            output_format,
            "--tools",
            tools,
        ]
    )
    if claude_args:
        cmd.extend(claude_args)
    if no_session_persistence:
        cmd.append("--no-session-persistence")
    if mode == "start" and session_id:
        cmd.extend(["--session-id", session_id])
    elif mode == "resume" and session_id:
        cmd.extend(["-r", session_id])
    cmd.extend(["--", prompt])
    return cmd


def finalize_command(cmd: list[str]) -> list[str]:
    if launcher_kind() in {"plain", "minimax"}:
        return [os.environ.get("SHELL", "/bin/zsh"), "-lc", shlex.join(cmd)]
    return cmd


def build_codex_prompt(prompt: str, claude_args: list[str] | None) -> str:
    if not claude_args:
        return prompt
    args = list(claude_args)
    system_prompt: str | None = None
    while args:
        arg = args.pop(0)
        if arg == "--append-system-prompt" and args:
            system_prompt = args.pop(0)
            break
    if not system_prompt:
        return prompt
    return f"{system_prompt.rstrip()}\n\n---\n\n{prompt}"


def daytona_enabled_for_codex() -> bool:
    return launcher_kind() == "codex" and bool(os.environ.get("DAYTONA_API_KEY"))


def extract_codex_model_arg(claude_args: list[str] | None) -> str | None:
    if not claude_args:
        return None
    args = list(claude_args)
    while args:
        arg = args.pop(0)
        if arg == "--model" and args:
            return args.pop(0)
    return None


def build_daytona_codex_command(
    prompt: str,
    *,
    claude_args: list[str] | None = None,
    output_file: Path,
) -> list[str]:
    helper_python = os.environ.get(
        "DAYTONA_PYTHON_BIN",
        str(Path(__file__).resolve().parent / ".venv-daytona" / "bin" / "python"),
    )
    helper_script = Path(__file__).resolve().parent / "daytona_codex_remote.py"
    cmd = [
        helper_python,
        str(helper_script),
        "--output-file",
        str(output_file),
    ]
    local_exa_root = os.environ.get("CV_RANK_ROOT", str(Path.home() / "cv-rank"))
    if local_exa_root:
        cmd.extend(["--local-exa-root", local_exa_root])
    model = extract_codex_model_arg(claude_args)
    if model:
        cmd.extend(["--model", model])
    cmd.append(build_codex_prompt(prompt, claude_args))
    return cmd


def build_codex_command(
    prompt: str,
    *,
    claude_args: list[str] | None = None,
    output_file: Path,
) -> list[str]:
    if daytona_enabled_for_codex():
        return build_daytona_codex_command(
            prompt,
            claude_args=claude_args,
            output_file=output_file,
        )
    cmd = [
        CLAUDE_WAFER_BIN,
        "exec",
        "--skip-git-repo-check",
        "--dangerously-bypass-approvals-and-sandbox",
        "--json",
        "-o",
        str(output_file),
    ]
    if claude_args:
        passthrough = list(claude_args)
        while passthrough:
            arg = passthrough.pop(0)
            if arg == "--append-system-prompt":
                if passthrough:
                    passthrough.pop(0)
                continue
            cmd.append(arg)
    cmd.append(build_codex_prompt(prompt, claude_args))
    return cmd


def run_codex(
    prompt: str,
    *,
    claude_args: list[str] | None = None,
) -> subprocess.CompletedProcess[str]:
    output_dir = Path(os.environ.get("TMPDIR", "/tmp"))
    output_dir.mkdir(parents=True, exist_ok=True)
    output_file = output_dir / f"codex-last-message-{uuid.uuid4()}.txt"
    cmd = build_codex_command(prompt, claude_args=claude_args, output_file=output_file)
    process_env = build_process_env()
    result = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        check=False,
        env=process_env,
    )
    final_output = output_file.read_text(encoding="utf-8") if output_file.exists() else ""
    maybe_mark_minimax_cooldown(process_env, result.stderr, final_output or result.stdout)
    try:
        output_file.unlink(missing_ok=True)
    except OSError:
        pass
    return subprocess.CompletedProcess(
        args=cmd,
        returncode=result.returncode,
        stdout=final_output or result.stdout,
        stderr=result.stderr,
    )


def run_claude(
    prompt: str,
    mode: str,
    session_id: str | None,
    tools: str,
    output_format: str,
    *,
    launcher_args: list[str] | None = None,
    claude_args: list[str] | None = None,
    no_session_persistence: bool = False,
) -> subprocess.CompletedProcess[str]:
    if launcher_kind() == "codex":
        return run_codex(prompt, claude_args=claude_args)
    cmd = build_claude_command(
        prompt,
        mode,
        session_id,
        tools,
        output_format,
        launcher_args=launcher_args,
        claude_args=claude_args,
        no_session_persistence=no_session_persistence,
    )
    cmd = finalize_command(cmd)
    process_env = build_process_env()
    result = subprocess.run(
        cmd,
        text=True,
        capture_output=True,
        check=False,
        env=process_env,
    )
    maybe_mark_minimax_cooldown(process_env, result.stderr, result.stdout)
    return result


def print_result_block(
    *,
    mode: str,
    alias: str | None,
    session_id: str | None,
    output: str,
) -> None:
    if alias:
        print(f"alias: {alias}")
    if session_id:
        print(f"session_id: {session_id}")
    print(f"mode: {mode}")
    print()
    sys.stdout.write(output.rstrip())
    if output and not output.endswith("\n"):
        print()


def get_prompt_text(args: argparse.Namespace) -> str:
    prompt = getattr(args, "prompt", None)
    prompt_file = getattr(args, "prompt_file", None)
    if bool(prompt) == bool(prompt_file):
        raise SystemExit("provide exactly one of: prompt or --prompt-file")
    if prompt_file:
        return Path(prompt_file).read_text(encoding="utf-8")
    return str(prompt)


def command_ask(args: argparse.Namespace) -> int:
    conn = connect_db()
    prompt_text = get_prompt_text(args)
    target = resolve_target(conn, args.alias, args.session_id, args.create_session)
    launcher_args = build_launcher_args(args)
    claude_args = build_claude_args(args)
    if args.stdout_file or args.stderr_file or args.combined_file:
        stdout_path = Path(args.stdout_file or Path.cwd() / "claude-agent.stdout.txt")
        stderr_path = Path(args.stderr_file or Path.cwd() / "claude-agent.stderr.txt")
        combined_path = Path(args.combined_file or Path.cwd() / "claude-agent.combined.txt")
        result = run_claude_live(
            prompt=prompt_text,
            mode=target.mode,
            session_id=target.session_id,
            tools=args.tools,
            output_format=args.output_format,
            launcher_args=launcher_args,
            claude_args=claude_args,
            stdout_path=stdout_path,
            stderr_path=stderr_path,
            combined_path=combined_path,
            echo=args.echo_live_output,
        )
    else:
        result = run_claude(
            prompt=prompt_text,
            mode=target.mode,
            session_id=target.session_id,
            tools=args.tools,
            output_format=args.output_format,
            launcher_args=launcher_args,
            claude_args=claude_args,
        )
    if target.alias and result.returncode == 0:
        touch_session(conn, target.alias)
    if result.returncode != 0:
        sys.stderr.write(result.stderr or result.stdout or "claude-wafer-agent: request failed\n")
        return result.returncode
    print_result_block(
        mode=target.mode,
        alias=target.alias,
        session_id=target.session_id,
        output=result.stdout,
    )
    return 0


def command_start(args: argparse.Namespace) -> int:
    conn = connect_db()
    prompt_text = get_prompt_text(args)
    if not args.alias and not args.session_id:
        args.session_id = str(uuid.uuid4())
    if args.alias:
        existing = get_session_by_alias(conn, args.alias)
        if existing and not args.force_new:
            raise SystemExit(
                f"Alias already exists: {args.alias}. Use send or pass --force-new."
            )
        if existing and args.force_new:
            args.session_id = str(uuid.uuid4())
    target = SessionTarget(args.session_id or str(uuid.uuid4()), args.alias, "start")
    if args.alias:
        upsert_session(conn, args.alias, target.session_id)
    launcher_args = build_launcher_args(args)
    claude_args = build_claude_args(args)
    result = run_claude(
        prompt=prompt_text,
        mode="start",
        session_id=target.session_id,
        tools=args.tools,
        output_format=args.output_format,
        launcher_args=launcher_args,
        claude_args=claude_args,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr or result.stdout or "claude-wafer-agent: request failed\n")
        return result.returncode
    print_result_block(
        mode="start",
        alias=target.alias,
        session_id=target.session_id,
        output=result.stdout,
    )
    return 0


def command_send(args: argparse.Namespace) -> int:
    conn = connect_db()
    prompt_text = get_prompt_text(args)
    target = resolve_target(conn, args.alias, args.session_id, create_if_missing=False)
    if target.mode == "ask":
        raise SystemExit("send requires --alias or --session-id")
    launcher_args = build_launcher_args(args)
    claude_args = build_claude_args(args)
    result = run_claude(
        prompt=prompt_text,
        mode="resume",
        session_id=target.session_id,
        tools=args.tools,
        output_format=args.output_format,
        launcher_args=launcher_args,
        claude_args=claude_args,
    )
    if result.returncode != 0:
        sys.stderr.write(result.stderr or result.stdout or "claude-wafer-agent: request failed\n")
        return result.returncode
    if target.alias:
        touch_session(conn, target.alias)
    print_result_block(
        mode="resume",
        alias=target.alias,
        session_id=target.session_id,
        output=result.stdout,
    )
    return 0


def ensure_dir(path: Path) -> None:
    path.mkdir(parents=True, exist_ok=True)


def write_text(path: Path, content: str) -> None:
    ensure_dir(path.parent)
    path.write_text(content, encoding="utf-8")


def write_json(path: Path, payload: object) -> None:
    write_text(path, json.dumps(payload, indent=2) + "\n")


def latest_loop_dir() -> Path | None:
    if not LOOP_ROOT.exists():
        return None
    candidates = [path for path in LOOP_ROOT.iterdir() if path.is_dir()]
    if not candidates:
        return None
    return sorted(candidates)[-1]


def resolve_loop_dir(loop_id: str | None) -> Path:
    if loop_id and loop_id != "latest":
        path = LOOP_ROOT / loop_id
    else:
        path = latest_loop_dir()
        if path is None:
            raise SystemExit("no loop runs found")
    if not path.exists():
        raise SystemExit(f"unknown loop: {loop_id}")
    return path


def tee_stream(
    stream,
    file_path: Path,
    combined_path: Path,
    prefix: str,
    echo_stream,
    capture: list[str],
    *,
    echo: bool,
) -> None:
    ensure_dir(file_path.parent)
    with file_path.open("w", encoding="utf-8") as out_file, combined_path.open(
        "a", encoding="utf-8"
    ) as combined_file:
        for line in stream:
            out_file.write(line)
            out_file.flush()
            combined_file.write(line if prefix == "" else f"{prefix}{line}")
            combined_file.flush()
            capture.append(line)
            if echo:
                echo_stream.write(line)
                echo_stream.flush()


def run_claude_live(
    prompt: str,
    mode: str,
    session_id: str | None,
    tools: str,
    output_format: str,
    *,
    launcher_args: list[str] | None = None,
    claude_args: list[str] | None = None,
    no_session_persistence: bool = False,
    stdout_path: Path,
    stderr_path: Path,
    combined_path: Path,
    echo: bool,
) -> subprocess.CompletedProcess[str]:
    if launcher_kind() == "codex":
        output_file = combined_path.parent / f"{combined_path.stem}.last-message.txt"
        cmd = build_codex_command(prompt, claude_args=claude_args, output_file=output_file)
        process_env = build_process_env()
        proc = subprocess.Popen(
            cmd,
            text=True,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            bufsize=1,
            env=process_env,
        )

        stdout_capture: list[str] = []
        stderr_capture: list[str] = []

        stdout_thread = threading.Thread(
            target=tee_stream,
            args=(
                proc.stdout,
                stdout_path,
                combined_path,
                "",
                sys.stdout,
                stdout_capture,
            ),
            kwargs={"echo": echo},
            daemon=True,
        )
        stderr_thread = threading.Thread(
            target=tee_stream,
            args=(
                proc.stderr,
                stderr_path,
                combined_path,
                "[stderr] ",
                sys.stderr,
                stderr_capture,
            ),
            kwargs={"echo": echo},
            daemon=True,
        )
        stdout_thread.start()
        stderr_thread.start()
        returncode = proc.wait()
        stdout_thread.join()
        stderr_thread.join()
        final_output = output_file.read_text(encoding="utf-8") if output_file.exists() else ""
        final_result = subprocess.CompletedProcess(
            args=cmd,
            returncode=returncode,
            stdout=final_output or "".join(stdout_capture),
            stderr="".join(stderr_capture),
        )
        maybe_mark_minimax_cooldown(process_env, final_result.stderr, final_result.stdout)
        return final_result
    cmd = build_claude_command(
        prompt,
        mode,
        session_id,
        tools,
        output_format,
        launcher_args=launcher_args,
        claude_args=claude_args,
        no_session_persistence=no_session_persistence,
    )
    cmd = finalize_command(cmd)
    process_env = build_process_env()
    proc = subprocess.Popen(
        cmd,
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=1,
        env=process_env,
    )

    stdout_capture: list[str] = []
    stderr_capture: list[str] = []

    stdout_thread = threading.Thread(
        target=tee_stream,
        args=(
            proc.stdout,
            stdout_path,
            combined_path,
            "",
            sys.stdout,
            stdout_capture,
        ),
        kwargs={"echo": echo},
        daemon=True,
    )
    stderr_thread = threading.Thread(
        target=tee_stream,
        args=(
            proc.stderr,
            stderr_path,
            combined_path,
            "[stderr] ",
            sys.stderr,
            stderr_capture,
        ),
        kwargs={"echo": echo},
        daemon=True,
    )
    stdout_thread.start()
    stderr_thread.start()
    returncode = proc.wait()
    stdout_thread.join()
    stderr_thread.join()

    final_result = subprocess.CompletedProcess(
        args=cmd,
        returncode=returncode,
        stdout="".join(stdout_capture),
        stderr="".join(stderr_capture),
    )
    maybe_mark_minimax_cooldown(process_env, final_result.stderr, final_result.stdout)
    return final_result


def command_loop(args: argparse.Namespace) -> int:
    if args.repetitions <= 0:
        raise SystemExit("--repetitions must be >= 1")
    prompt_text = get_prompt_text(args)
    launcher_args = build_launcher_args(args)
    claude_args = build_claude_args(args)

    loop_id = time.strftime("%Y%m%d-%H%M%S", time.localtime())
    loop_dir = LOOP_ROOT / loop_id
    ensure_dir(loop_dir)
    write_text(loop_dir / "prompt.txt", prompt_text)
    meta_path = loop_dir / "loop.json"
    combined_log = loop_dir / "combined.log"
    metadata = {
        "loop_id": loop_id,
        "status": "running",
        "prompt_file": str(loop_dir / "prompt.txt"),
        "repetitions": args.repetitions,
        "completed": 0,
        "current_iteration": None,
        "current_session_id": None,
        "started_at": utc_now(),
        "updated_at": utc_now(),
        "tools": args.tools,
        "output_format": args.output_format,
    }
    write_json(meta_path, metadata)

    overall_exit = 0
    for iteration in range(1, args.repetitions + 1):
        session_id = str(uuid.uuid4())
        metadata["current_iteration"] = iteration
        metadata["current_session_id"] = session_id
        metadata["updated_at"] = utc_now()
        write_json(meta_path, metadata)

        header = f"== iteration {iteration}/{args.repetitions} ==\nsession_id: {session_id}\n"
        with combined_log.open("a", encoding="utf-8") as combined:
            combined.write(header)
        if args.follow:
            sys.stdout.write(header)
            sys.stdout.flush()

        result = run_claude_live(
            prompt=prompt_text,
            mode="start",
            session_id=session_id,
            tools=args.tools,
            output_format=args.output_format,
            launcher_args=launcher_args,
            claude_args=claude_args,
            no_session_persistence=True,
            stdout_path=loop_dir / f"{iteration:04d}.stdout.txt",
            stderr_path=loop_dir / f"{iteration:04d}.stderr.txt",
            combined_path=combined_log,
            echo=args.follow,
        )

        if result.returncode != 0:
            overall_exit = result.returncode
            status_line = f"[iteration {iteration}] exit={result.returncode}\n"
            if args.follow:
                sys.stdout.write(status_line)
                sys.stdout.flush()
            with combined_log.open("a", encoding="utf-8") as combined:
                combined.write(status_line)
            if args.stop_on_error:
                break
        else:
            status_line = f"[iteration {iteration}] done\n"
            if args.follow:
                sys.stdout.write(status_line)
                sys.stdout.flush()
            with combined_log.open("a", encoding="utf-8") as combined:
                combined.write(status_line)

        metadata["completed"] = iteration
        metadata["updated_at"] = utc_now()
        write_json(meta_path, metadata)

        if iteration < args.repetitions and args.delay > 0:
            time.sleep(args.delay)

    metadata["status"] = "done" if overall_exit == 0 else "error"
    metadata["current_iteration"] = None
    metadata["current_session_id"] = None
    metadata["updated_at"] = utc_now()
    write_json(meta_path, metadata)

    print()
    print(f"log_dir: {loop_dir}")
    return overall_exit


def command_loop_status(args: argparse.Namespace) -> int:
    loop_dir = resolve_loop_dir(args.loop_id)
    meta_path = loop_dir / "loop.json"
    if not meta_path.exists():
        raise SystemExit(f"missing metadata: {meta_path}")
    payload = json.loads(meta_path.read_text(encoding="utf-8"))
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    for key in (
        "loop_id",
        "status",
        "repetitions",
        "completed",
        "current_iteration",
        "current_session_id",
        "started_at",
        "updated_at",
    ):
        print(f"{key}: {payload.get(key)}")
    print(f"log_dir: {loop_dir}")
    print(f"combined_log: {loop_dir / 'combined.log'}")
    return 0


def command_loop_tail(args: argparse.Namespace) -> int:
    loop_dir = resolve_loop_dir(args.loop_id)
    target = loop_dir / "combined.log"
    if not target.exists():
        raise SystemExit(f"missing log: {target}")
    cmd = ["tail"]
    if args.follow:
        cmd.append("-f")
    cmd.extend(["-n", str(args.lines), str(target)])
    return subprocess.call(cmd)


def queue_job(
    conn: sqlite3.Connection,
    *,
    prompt: str,
    alias: str | None,
    session_id: str | None,
    mode: str,
    tools: str,
    output_format: str,
) -> int:
    cur = conn.execute(
        """
        INSERT INTO jobs (alias, session_id, mode, prompt, tools, output_format, status, created_at)
        VALUES (?, ?, ?, ?, ?, ?, 'queued', ?)
        """,
        (alias, session_id, mode, prompt, tools, output_format, utc_now()),
    )
    conn.commit()
    return int(cur.lastrowid)


def command_enqueue(args: argparse.Namespace) -> int:
    conn = connect_db()
    prompt_text = get_prompt_text(args)
    target = resolve_target(conn, args.alias, args.session_id, args.create_session)
    job_id = queue_job(
        conn,
        prompt=prompt_text,
        alias=target.alias,
        session_id=target.session_id,
        mode=target.mode,
        tools=args.tools,
        output_format=args.output_format,
    )
    print(f"queued job {job_id}")
    if target.alias:
        print(f"alias: {target.alias}")
    if target.session_id:
        print(f"session_id: {target.session_id}")
    print(f"mode: {target.mode}")
    return 0


def claim_next_job(conn: sqlite3.Connection) -> sqlite3.Row | None:
    row = conn.execute(
        """
        SELECT *
        FROM jobs
        WHERE status = 'queued'
        ORDER BY id ASC
        LIMIT 1
        """
    ).fetchone()
    if not row:
        return None
    cur = conn.execute(
        "UPDATE jobs SET status = 'running', started_at = ? WHERE id = ? AND status = 'queued'",
        (utc_now(), row["id"]),
    )
    conn.commit()
    if cur.rowcount != 1:
        return None
    claimed = conn.execute("SELECT * FROM jobs WHERE id = ?", (row["id"],)).fetchone()
    if claimed and claimed["status"] == "running":
        return claimed
    return None


def finish_job(
    conn: sqlite3.Connection,
    job_id: int,
    *,
    status: str,
    result: str | None,
    error: str | None,
) -> None:
    conn.execute(
        """
        UPDATE jobs
        SET status = ?, finished_at = ?, result = ?, error = ?
        WHERE id = ?
        """,
        (status, utc_now(), result, error, job_id),
    )
    conn.commit()


def process_one_job(
    conn: sqlite3.Connection,
    announce: bool,
    launcher_args: list[str] | None = None,
    claude_args: list[str] | None = None,
) -> bool:
    job = claim_next_job(conn)
    if not job:
        return False
    if announce:
        print(
            f"[running] job {job['id']} "
            f"mode={job['mode']} "
            f"alias={job['alias'] or '-'} "
            f"session_id={job['session_id'] or '-'}"
        )
    result = run_claude(
        prompt=str(job["prompt"]),
        mode=str(job["mode"]),
        session_id=str(job["session_id"]) if job["session_id"] else None,
        tools=str(job["tools"]),
        output_format=str(job["output_format"]),
        launcher_args=launcher_args,
        claude_args=claude_args,
    )
    if result.returncode == 0:
        finish_job(conn, int(job["id"]), status="done", result=result.stdout, error=None)
        if job["alias"]:
            touch_session(conn, str(job["alias"]))
        if announce:
            print(f"[done] job {job['id']}")
        return True

    error_text = result.stderr or result.stdout or f"exit {result.returncode}"
    finish_job(conn, int(job["id"]), status="error", result=result.stdout, error=error_text)
    if announce:
        print(f"[error] job {job['id']}: {error_text.strip()}")
    return True


def command_drain(args: argparse.Namespace) -> int:
    conn = connect_db()
    launcher_args = build_launcher_args(args)
    claude_args = build_claude_args(args)
    processed_any = False
    while True:
        processed = process_one_job(
            conn,
            announce=True,
            launcher_args=launcher_args,
            claude_args=claude_args,
        )
        if not processed:
            break
        processed_any = True
    if not processed_any:
        print("queue is empty")
    return 0


def command_worker(args: argparse.Namespace) -> int:
    conn = connect_db()
    launcher_args = build_launcher_args(args)
    claude_args = build_claude_args(args)
    print(f"worker watching {QUEUE_DB}")
    while True:
        processed = process_one_job(
            conn,
            announce=True,
            launcher_args=launcher_args,
            claude_args=claude_args,
        )
        if args.once:
            return 0 if processed else 1
        if not processed:
            time.sleep(args.poll_interval)


def iter_job_rows(rows: Iterable[sqlite3.Row]) -> Iterable[str]:
    for row in rows:
        yield (
            f"{row['id']:>4}  {row['status']:<7}  {row['mode']:<6}  "
            f"{(row['alias'] or '-'): <14}  "
            f"{(row['session_id'] or '-'): <36}  "
            f"{row['created_at']}"
        )


def command_status(args: argparse.Namespace) -> int:
    conn = connect_db()
    rows = conn.execute(
        """
        SELECT id, status, mode, alias, session_id, created_at
        FROM jobs
        ORDER BY id DESC
        LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    if not rows:
        print("no jobs")
        return 0
    print("  id  status   mode    alias           session_id                            created_at")
    for line in iter_job_rows(rows):
        print(line)
    return 0


def command_show(args: argparse.Namespace) -> int:
    conn = connect_db()
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (args.job_id,)).fetchone()
    if not row:
        raise SystemExit(f"Unknown job: {args.job_id}")
    payload = {key: row[key] for key in row.keys()}
    if args.json:
        print(json.dumps(payload, indent=2))
        return 0
    for key in (
        "id",
        "status",
        "mode",
        "alias",
        "session_id",
        "created_at",
        "started_at",
        "finished_at",
        "error",
    ):
        print(f"{key}: {payload[key]}")
    print()
    print("prompt:")
    print(payload["prompt"])
    print()
    print("result:")
    print(payload["result"] or "")
    return 0


def command_sessions(args: argparse.Namespace) -> int:
    conn = connect_db()
    rows = conn.execute(
        """
        SELECT alias, session_id, created_at, updated_at
        FROM sessions
        ORDER BY updated_at DESC, alias ASC
        """
    ).fetchall()
    if not rows:
        print("no sessions")
        return 0
    print("alias           session_id                            updated_at")
    for row in rows:
        print(f"{row['alias']:<14}  {row['session_id']}  {row['updated_at']}")
    return 0


def add_common_prompt_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("prompt", nargs="?", help="Prompt text to send")
    parser.add_argument(
        "--prompt-file",
        help="Read prompt text from a file",
    )
    parser.add_argument(
        "--tools",
        default="",
        help='Tool mode for Claude ("", "default", or a tool list). Default disables tools.',
    )
    parser.add_argument(
        "--output-format",
        default="text",
        choices=("text", "json", "stream-json"),
        help="Claude print output format",
    )
    parser.add_argument(
        "--append-system-prompt",
        help="Append a system prompt to the default Claude Code system prompt.",
    )
    parser.add_argument(
        "--append-system-prompt-file",
        help="Read appended system prompt text from a file.",
    )
    parser.add_argument(
        "--allowed-tools",
        help="Comma or space-separated list of tool names to allow.",
    )
    parser.add_argument(
        "--disallowed-tools",
        help="Comma or space-separated list of tool names to deny.",
    )
    parser.add_argument(
        "--fallback-model",
        help="Claude CLI fallback model to use when the primary model is overloaded.",
    )
    parser.add_argument(
        "--model",
        help="Claude CLI model override (for example: haiku or claude-haiku-4-5-20251001).",
    )
    parser.add_argument(
        "--effort",
        choices=("low", "medium", "high", "max"),
        help="Claude CLI effort level.",
    )
    parser.add_argument(
        "--stdout-file",
        help="Optional file path to tee Claude stdout to while the request is running.",
    )
    parser.add_argument(
        "--stderr-file",
        help="Optional file path to tee Claude stderr to while the request is running.",
    )
    parser.add_argument(
        "--combined-file",
        help="Optional file path to tee combined stdout/stderr to while the request is running.",
    )
    parser.add_argument(
        "--echo-live-output",
        action="store_true",
        help="Echo live Claude output to the terminal when using tee files.",
    )


def add_wafer_override_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--wafer-api-key",
        help="Override the Wafer API key for this run. Visible in process lists; prefer --wafer-api-key-file when possible.",
    )
    parser.add_argument(
        "--wafer-api-key-file",
        help="Read the Wafer API key override from a file.",
    )
    parser.add_argument(
        "--wafer-env-file",
        help="Use an alternate wafer.env-style file for this run.",
    )
    parser.add_argument(
        "--wafer-model",
        help="Override the Wafer model ID for this run.",
    )
    parser.add_argument(
        "--wafer-base-url",
        help="Override the Wafer base URL for this run.",
    )
    parser.add_argument(
        "--wafer-web-mode",
        choices=("exa", "mixed", "claude"),
        help="Web tool mode for claude-wafer: exa (default), mixed (Exa + Claude WebSearch), or claude (Claude defaults only).",
    )


def build_launcher_args(args: argparse.Namespace) -> list[str]:
    launcher_args: list[str] = []
    if launcher_kind() in {"plain", "minimax"}:
        return launcher_args
    if getattr(args, "wafer_api_key", None):
        launcher_args.extend(["--wafer-api-key", str(args.wafer_api_key)])
    if getattr(args, "wafer_api_key_file", None):
        launcher_args.extend(["--wafer-api-key-file", str(args.wafer_api_key_file)])
    if getattr(args, "wafer_env_file", None):
        launcher_args.extend(["--wafer-env-file", str(args.wafer_env_file)])
    if getattr(args, "wafer_model", None):
        launcher_args.extend(["--wafer-model", str(args.wafer_model)])
    if getattr(args, "wafer_base_url", None):
        launcher_args.extend(["--wafer-base-url", str(args.wafer_base_url)])
    if getattr(args, "wafer_web_mode", None):
        launcher_args.extend(["--wafer-web-mode", str(args.wafer_web_mode)])
    return launcher_args


def build_claude_args(args: argparse.Namespace) -> list[str]:
    claude_args: list[str] = []
    if launcher_kind() in {"plain", "minimax"}:
        claude_args.extend(["--permission-mode", "bypassPermissions"])
        if getattr(args, "output_format", None) in {"json", "stream-json"}:
            claude_args.append("--verbose")
    elif launcher_kind() == "codex":
        claude_args.extend(["-C", str(Path.cwd())])
    append_system_prompt = getattr(args, "append_system_prompt", None)
    append_system_prompt_file = getattr(args, "append_system_prompt_file", None)
    if append_system_prompt and append_system_prompt_file:
        raise SystemExit(
            "use only one of --append-system-prompt or --append-system-prompt-file"
        )
    if append_system_prompt_file:
        append_system_prompt = Path(append_system_prompt_file).read_text(encoding="utf-8")
    if append_system_prompt:
        claude_args.extend(["--append-system-prompt", str(append_system_prompt)])
    if launcher_kind() != "codex" and getattr(args, "allowed_tools", None):
        claude_args.extend(["--allowedTools", str(args.allowed_tools)])
    if launcher_kind() != "codex" and getattr(args, "disallowed_tools", None):
        claude_args.extend(["--disallowedTools", str(args.disallowed_tools)])
    if getattr(args, "model", None):
        claude_args.extend(["--model", str(args.model)])
    if launcher_kind() != "codex" and getattr(args, "fallback_model", None):
        claude_args.extend(["--fallback-model", str(args.fallback_model)])
    if launcher_kind() != "codex" and getattr(args, "effort", None):
        claude_args.extend(["--effort", str(args.effort)])
    return claude_args


def add_session_selector_args(parser: argparse.ArgumentParser, *, allow_create: bool) -> None:
    parser.add_argument("--alias", help="Named session alias")
    parser.add_argument("--session-id", help="Explicit Claude session ID")
    if allow_create:
        parser.add_argument(
            "--create-session",
            action="store_true",
            help="Create a new persistent session when alias or session is missing",
        )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude-wafer-agent",
        description="Thin CLI for direct and queued Claude Code requests over claude-wafer.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    ask_parser = subparsers.add_parser("ask", help="Run a direct request")
    add_common_prompt_args(ask_parser)
    add_session_selector_args(ask_parser, allow_create=True)
    add_wafer_override_args(ask_parser)
    ask_parser.set_defaults(func=command_ask)

    start_parser = subparsers.add_parser("start", help="Start a new persistent conversation")
    add_common_prompt_args(start_parser)
    add_wafer_override_args(start_parser)
    start_parser.add_argument("--alias", help="Named session alias to create")
    start_parser.add_argument("--session-id", help="Specific session ID to start with")
    start_parser.add_argument(
        "--force-new",
        action="store_true",
        help="Replace an existing alias with a new session ID",
    )
    start_parser.set_defaults(func=command_start)

    send_parser = subparsers.add_parser("send", help="Send a follow-up message to a saved conversation")
    add_common_prompt_args(send_parser)
    add_wafer_override_args(send_parser)
    send_parser.add_argument("--alias", help="Named session alias")
    send_parser.add_argument("--session-id", help="Explicit Claude session ID")
    send_parser.set_defaults(func=command_send)

    loop_parser = subparsers.add_parser(
        "loop",
        help="Run the same prompt in fresh Claude chats repeatedly",
    )
    add_common_prompt_args(loop_parser)
    add_wafer_override_args(loop_parser)
    loop_parser.add_argument(
        "--repetitions",
        type=int,
        required=True,
        help="Number of fresh chats to run",
    )
    loop_parser.add_argument(
        "--delay",
        type=float,
        default=0.0,
        help="Seconds to sleep between repetitions",
    )
    loop_parser.add_argument(
        "--stop-on-error",
        action="store_true",
        help="Stop the loop on the first Claude failure",
    )
    loop_parser.add_argument(
        "--follow",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Stream Claude output live while the loop runs",
    )
    loop_parser.set_defaults(func=command_loop)

    loop_status_parser = subparsers.add_parser(
        "loop-status",
        help="Show metadata for a loop run",
    )
    loop_status_parser.add_argument("loop_id", nargs="?", help='Loop ID or "latest"')
    loop_status_parser.add_argument("--json", action="store_true", help="Print raw JSON")
    loop_status_parser.set_defaults(func=command_loop_status)

    loop_tail_parser = subparsers.add_parser(
        "loop-tail",
        help="Tail the combined log for a loop run",
    )
    loop_tail_parser.add_argument("loop_id", nargs="?", help='Loop ID or "latest"')
    loop_tail_parser.add_argument(
        "-n",
        "--lines",
        type=int,
        default=40,
        help="Number of lines to show",
    )
    loop_tail_parser.add_argument(
        "-f",
        "--follow",
        action="store_true",
        help="Follow the log",
    )
    loop_tail_parser.set_defaults(func=command_loop_tail)

    enqueue_parser = subparsers.add_parser("enqueue", help="Queue a request for later processing")
    add_common_prompt_args(enqueue_parser)
    add_session_selector_args(enqueue_parser, allow_create=True)
    enqueue_parser.set_defaults(func=command_enqueue)

    drain_parser = subparsers.add_parser("drain", help="Process all queued jobs")
    add_wafer_override_args(drain_parser)
    drain_parser.set_defaults(func=command_drain)

    worker_parser = subparsers.add_parser("worker", help="Run a polling queue worker")
    add_wafer_override_args(worker_parser)
    worker_parser.add_argument(
        "--poll-interval",
        type=float,
        default=2.0,
        help="Seconds to sleep between queue polls",
    )
    worker_parser.add_argument(
        "--once",
        action="store_true",
        help="Process at most one queued job, then exit",
    )
    worker_parser.set_defaults(func=command_worker)

    status_parser = subparsers.add_parser("status", help="Show recent queue jobs")
    status_parser.add_argument("--limit", type=int, default=20, help="Number of jobs to show")
    status_parser.set_defaults(func=command_status)

    show_parser = subparsers.add_parser("show", help="Show one queued job in detail")
    show_parser.add_argument("job_id", type=int, help="Queued job ID")
    show_parser.add_argument("--json", action="store_true", help="Print raw job JSON")
    show_parser.set_defaults(func=command_show)

    sessions_parser = subparsers.add_parser("sessions", help="List saved session aliases")
    sessions_parser.set_defaults(func=command_sessions)

    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    return int(args.func(args))


if __name__ == "__main__":
    raise SystemExit(main())
