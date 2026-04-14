#!/usr/bin/env python3

from __future__ import annotations

import argparse
import os
import shlex
import shutil
import sys
import tarfile
import tempfile
import uuid
from pathlib import Path

from daytona_codex_remote import ensure_sandbox, load_local_exa_key

REMOTE_USER = "daytona"
REMOTE_HOME = f"/home/{REMOTE_USER}"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run the cv-rank dossier pipeline inside a Daytona sandbox using the MiniMax Claude-compatible runtime.")
    parser.add_argument("--api-key", default=os.environ.get("DAYTONA_API_KEY"))
    parser.add_argument("--api-url", default=os.environ.get("DAYTONA_API_URL", "https://app.daytona.io/api"))
    parser.add_argument("--sandbox-id", default=os.environ.get("DAYTONA_SANDBOX_ID"))
    parser.add_argument("--sandbox-name", default=os.environ.get("DAYTONA_SANDBOX_NAME", "cv-rank-minimax"))
    parser.add_argument("--cpu", type=int, default=int(os.environ.get("DAYTONA_CPU", "4")))
    parser.add_argument("--memory", type=int, default=int(os.environ.get("DAYTONA_MEMORY", "8")))
    parser.add_argument("--disk", type=int, default=int(os.environ.get("DAYTONA_DISK", "10")))
    parser.add_argument("--auto-stop-interval", type=int, default=int(os.environ.get("DAYTONA_AUTO_STOP_INTERVAL", "5")))
    parser.add_argument("--auto-archive-interval", type=int, default=int(os.environ.get("DAYTONA_AUTO_ARCHIVE_INTERVAL", "5")))
    parser.add_argument("--auto-delete-interval", type=int, default=int(os.environ.get("DAYTONA_AUTO_DELETE_INTERVAL", "0")))
    parser.add_argument("--local-repo-root", required=True)
    parser.add_argument("--remote-repo-root", default=os.environ.get("DAYTONA_REMOTE_CV_RANK_ROOT", f"{REMOTE_HOME}/tools/cv-rank"))
    parser.add_argument("--local-people-file", required=True)
    parser.add_argument("--local-config")
    parser.add_argument("--local-system-prompt", required=True)
    parser.add_argument("--local-output-root", required=True)
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--limit", type=int, default=1)
    parser.add_argument("--model", default="MiniMax-M2.7")
    parser.add_argument("--web-mode", default="mixed")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--effort", default="low")
    parser.add_argument("--local-minimax-env", default=os.environ.get("MINIMAX_ENV_FILE") or str(Path.home() / ".claude-wafer" / "minimax.env"))
    parser.add_argument("--wafer-synthesis-only", action=argparse.BooleanOptionalAction, default=False)
    parser.add_argument("--wafer-expansion-pass", action=argparse.BooleanOptionalAction, default=True)
    parser.add_argument("--wafer-braindump-pass", action=argparse.BooleanOptionalAction, default=True)
    return parser.parse_args()


def build_runtime_bundle(local_repo_root: Path, people_file: Path, config_path: Path | None, system_prompt_path: Path) -> bytes:
    wanted = [
        local_repo_root / "scripts" / "build_attendee_dossiers.py",
        local_repo_root / "scripts" / "exa",
        local_repo_root / "scripts" / "exa_cli.py",
        local_repo_root / "src" / "cv_rank",
        system_prompt_path,
        people_file,
    ]
    if config_path is not None:
        wanted.append(config_path)
    with tempfile.NamedTemporaryFile(suffix=".tar.gz") as tmp:
        with tarfile.open(tmp.name, "w:gz") as tar:
            for path in wanted:
                if not path.exists():
                    raise SystemExit(f"Missing required runtime path: {path}")
                if path.is_dir():
                    tar.add(path, arcname=str(path.relative_to(local_repo_root)))
                else:
                    arc = str(path.relative_to(local_repo_root)) if path.is_relative_to(local_repo_root) else path.name
                    tar.add(path, arcname=arc)
        return Path(tmp.name).read_bytes()


def collect_env_text(local_repo_root: Path, *, require_exa: bool = False, exa_seed: str | None = None) -> str:
    env_names = ["SUPABASE_URL", "SUPABASE_KEY", "PLATFORM_DATABASE_URL"]
    env_map: dict[str, str] = {}
    for name in env_names:
        value = os.environ.get(name, "").strip()
        if value:
            env_map[name] = value
    env_path = local_repo_root / ".env"
    if env_path.exists():
        for raw in env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key in env_names and value and key not in env_map:
                env_map[key] = value
    exa_key = load_local_exa_key(exa_seed)
    if exa_key:
        env_map["EXA_API_KEY"] = exa_key
    required = ["SUPABASE_URL", "SUPABASE_KEY", "PLATFORM_DATABASE_URL"]
    if require_exa:
        required.append("EXA_API_KEY")
    missing = [name for name in required if not env_map.get(name)]
    if missing:
        raise SystemExit(f"Missing required env values for remote run: {', '.join(missing)}")
    return "".join(f"{key}={shlex.quote(value)}\n" for key, value in env_map.items())


def collect_minimax_env(model: str, env_file: Path) -> str:
    values: dict[str, str] = {}
    if env_file.exists():
        for raw in env_file.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            values[key.strip()] = value.strip().strip('"').strip("'")
    for key in ("MINIMAX_API_KEY", "MINIMAX_API_HOST", "API_TIMEOUT_MS", "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC"):
        override = os.environ.get(key, "").strip()
        if override:
            values[key] = override
    api_key = values.get("MINIMAX_API_KEY", "").strip()
    if not api_key:
        raise SystemExit(f"Missing MINIMAX_API_KEY in {env_file} or environment.")
    api_host = values.get("MINIMAX_API_HOST", "").strip() or "https://api.minimax.io"
    timeout_ms = values.get("API_TIMEOUT_MS", "").strip() or "3000000"
    disable_traffic = values.get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "").strip() or "1"
    return (
        f"MINIMAX_API_KEY={shlex.quote(api_key)}\n"
        f"MINIMAX_API_HOST={shlex.quote(api_host)}\n"
        f"MINIMAX_MODEL={shlex.quote(model)}\n"
        f"API_TIMEOUT_MS={shlex.quote(timeout_ms)}\n"
        f"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC={shlex.quote(disable_traffic)}\n"
    )


def ensure_remote_runtime(sandbox, args: argparse.Namespace) -> None:
    local_repo_root = Path(args.local_repo_root).expanduser().resolve()
    people_file = Path(args.local_people_file).expanduser().resolve()
    config_path = Path(args.local_config).expanduser().resolve() if args.local_config else None
    system_prompt_path = Path(args.local_system_prompt).expanduser().resolve()
    minimax_env_path = Path(args.local_minimax_env).expanduser().resolve()
    bundle = build_runtime_bundle(local_repo_root, people_file, config_path, system_prompt_path)
    env_text = collect_env_text(
        local_repo_root,
        require_exa=False,
        exa_seed=args.run_id or args.sandbox_name,
    )
    minimax_env_text = collect_minimax_env(args.model, minimax_env_path)
    remote_root = args.remote_repo_root.rstrip("/")
    remote_bundle = f"/tmp/cv-rank-minimax-runtime-{args.run_id}.tar.gz"
    lock_dir = "/tmp/cv-rank-minimax-install.lock"
    marker = f"{remote_root}/.cv_rank_minimax_ready"
    sandbox.process.exec(
        (
            f"id -u {shlex.quote(REMOTE_USER)} >/dev/null 2>&1 || "
            f"useradd -m -s /bin/bash {shlex.quote(REMOTE_USER)}; "
            f"mkdir -p {shlex.quote(remote_root)} {shlex.quote(REMOTE_HOME + '/tools/orchestrator')} {shlex.quote(REMOTE_HOME + '/.claude')}; "
            f"chown -R {shlex.quote(REMOTE_USER)}:{shlex.quote(REMOTE_USER)} {shlex.quote(REMOTE_HOME)}"
        ),
        timeout=60,
    )
    sandbox.fs.upload_file(bundle, remote_bundle)
    sandbox.fs.upload_file(env_text.encode("utf-8"), f"{remote_root}/.env.remote")
    sandbox.fs.upload_file(minimax_env_text.encode("utf-8"), f"{remote_root}/.minimax.remote")
    agent_dir = Path(__file__).resolve().parent
    agent_path = agent_dir / "claude_wafer_agent.py"
    key_selector_path = agent_dir / "minimax_key_selection.py"
    sandbox.fs.upload_file(agent_path.read_bytes(), f"{REMOTE_HOME}/tools/orchestrator/claude_wafer_agent.py")
    sandbox.fs.upload_file(key_selector_path.read_bytes(), f"{REMOTE_HOME}/tools/orchestrator/minimax_key_selection.py")
    setup_cmd = (
        f"lock={shlex.quote(lock_dir)}; "
        "while ! mkdir \"$lock\" 2>/dev/null; do sleep 1; done; "
        "trap 'rmdir \"$lock\"' EXIT; "
        f"cd {shlex.quote(remote_root)} && tar --overwrite -xzf {shlex.quote(remote_bundle)} && "
        f"if [ ! -f {shlex.quote(marker)} ]; then "
        "python3 -m ensurepip --upgrade >/dev/null 2>&1 || true; "
        "python3 -m pip install --quiet pyyaml python-dotenv psycopg2-binary exa-py openai && "
        "dpkg --configure -a >/dev/null 2>&1 || true; "
        "DEBIAN_FRONTEND=noninteractive apt-get install -f -y >/dev/null 2>&1 || true; "
        "if ! command -v rg >/dev/null 2>&1; then "
        "apt-get update >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ripgrep >/dev/null; "
        "fi && "
        "if ! command -v node >/dev/null 2>&1 || ! command -v npm >/dev/null 2>&1; then "
        "apt-get update >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends nodejs npm >/dev/null; "
        "fi && "
        "if ! command -v claude >/dev/null 2>&1; then npm install -g @anthropic-ai/claude-code >/dev/null; fi && "
        f"touch {shlex.quote(marker)}; "
        "fi"
    )
    result = sandbox.process.exec(setup_cmd, timeout=1800)
    if result.exit_code != 0:
        raise SystemExit(f"Failed to prepare Daytona MiniMax runtime:\n{result.result}")
    sandbox.process.exec(
        f"chown -R {shlex.quote(REMOTE_USER)}:{shlex.quote(REMOTE_USER)} {shlex.quote(remote_root)} {shlex.quote(REMOTE_HOME + '/tools/orchestrator')} {shlex.quote(REMOTE_HOME + '/.claude')}",
        timeout=120,
    )


def run_remote_pipeline(sandbox, args: argparse.Namespace) -> tuple[int, str]:
    from daytona.common.pty import PtySize

    remote_root = args.remote_repo_root.rstrip("/")
    remote_output_root = f"{remote_root}/outputs"
    local_repo_root = Path(args.local_repo_root).expanduser().resolve()
    people_rel = str(Path(args.local_people_file).expanduser().resolve().relative_to(local_repo_root))
    prompt_rel = str(Path(args.local_system_prompt).expanduser().resolve().relative_to(local_repo_root))
    sandbox.process.exec(
        f"mkdir -p {shlex.quote(remote_output_root + '/' + args.run_id)} && rm -rf {shlex.quote(remote_output_root + '/' + args.run_id)}/* && chown -R {shlex.quote(REMOTE_USER)}:{shlex.quote(REMOTE_USER)} {shlex.quote(remote_output_root)}",
        timeout=120,
    )
    runner_cmd = (
        "set -a && source .env.remote && source .minimax.remote && set +a && "
        "unset DAYTONA_API_KEY DAYTONA_API_URL DAYTONA_PYTHON_BIN DAYTONA_SANDBOX_ID DAYTONA_SANDBOX_NAME && "
        "export SHELL=/bin/bash && "
        "export CLAUDE_WAFER_BIN=$(command -v claude) && "
        "export CLAUDE_WAFER_LAUNCHER_KIND=minimax && "
        f"export MINIMAX_ENV_FILE={shlex.quote(remote_root + '/.minimax.remote')} && "
        f"export CV_RANK_EXA_CLI={shlex.quote(remote_root + '/scripts/exa')} && "
        f"export PYTHONPATH={shlex.quote(remote_root + '/src')} && "
        f"chmod +x scripts/exa scripts/exa_cli.py {shlex.quote(REMOTE_HOME + '/tools/orchestrator/claude_wafer_agent.py')} >/dev/null 2>&1 || true && "
        "python3 scripts/build_attendee_dossiers.py "
        f"--people-file {shlex.quote(people_rel)} "
        f"--limit {int(args.limit)} "
        f"--output-dir {shlex.quote(remote_output_root)} "
        f"--run-id {shlex.quote(args.run_id)} "
        f"--wafer-agent {shlex.quote(REMOTE_HOME + '/tools/orchestrator/claude_wafer_agent.py')} "
        f"--system-prompt-file {shlex.quote(prompt_rel)} "
        f"--wafer-web-mode {shlex.quote(args.web_mode)} "
        f"--wafer-timeout-seconds {int(args.timeout_seconds)} "
        f"--claude-model {shlex.quote(args.model)} "
        "--claude-output-format stream-json "
        f"{'--wafer-synthesis-only' if args.wafer_synthesis_only else '--no-wafer-synthesis-only'} "
        f"{'--wafer-expansion-pass' if args.wafer_expansion_pass else '--no-wafer-expansion-pass'} "
        f"{'--wafer-braindump-pass' if args.wafer_braindump_pass else '--no-wafer-braindump-pass'} "
        f"--effort {shlex.quote(args.effort)}"
    )
    if args.local_config:
        config_rel = str(Path(args.local_config).expanduser().resolve().relative_to(local_repo_root))
        runner_cmd = runner_cmd.replace(
            f"--wafer-web-mode {shlex.quote(args.web_mode)} ",
            f"--config {shlex.quote(config_rel)} --wafer-web-mode {shlex.quote(args.web_mode)} ",
        )

    session_id = f"cv-rank-minimax-{args.run_id}-{uuid.uuid4().hex[:8]}"
    pty_handle = sandbox.process.create_pty_session(
        id=session_id,
        cwd=remote_root,
        envs={"TERM": "xterm-256color"},
        pty_size=PtySize(cols=160, rows=40),
    )
    wrapped_cmd = f"su -s /bin/bash {shlex.quote(REMOTE_USER)} -c {shlex.quote(f'cd {shlex.quote(remote_root)} && {runner_cmd}')}"
    pty_handle.send_input(f"bash -lc {shlex.quote(wrapped_cmd)}; exit $?\n")
    chunks: list[str] = []
    for data in pty_handle:
        text = data.decode("utf-8", errors="replace")
        chunks.append(text)
        sys.stdout.write(text)
        sys.stdout.flush()
    exit_code = getattr(pty_handle, "exit_code", None)
    if exit_code is None:
        exit_code = 0
    return exit_code, "".join(chunks)


def download_outputs(sandbox, args: argparse.Namespace) -> None:
    remote_root = args.remote_repo_root.rstrip("/")
    remote_output_root = f"{remote_root}/outputs"
    remote_archive = f"/tmp/cv-rank-output-{args.run_id}.tar.gz"
    local_output_root = Path(args.local_output_root).expanduser().resolve()
    local_output_root.mkdir(parents=True, exist_ok=True)
    local_run_dir = local_output_root / args.run_id
    if local_run_dir.exists():
        shutil.rmtree(local_run_dir)
    result = sandbox.process.exec(
        f"cd {shlex.quote(remote_output_root)} && tar -czf {shlex.quote(remote_archive)} {shlex.quote(args.run_id)}",
        timeout=600,
    )
    if result.exit_code != 0:
        raise SystemExit(f"Failed to archive remote output:\n{result.result}")
    archive = sandbox.fs.download_file(remote_archive)
    with tempfile.NamedTemporaryFile(suffix=".tar.gz") as tmp:
        tmp.write(archive)
        tmp.flush()
        with tarfile.open(tmp.name, "r:gz") as tar:
            tar.extractall(local_output_root)


def main() -> int:
    args = parse_args()
    sandbox = ensure_sandbox(args)
    ensure_remote_runtime(sandbox, args)
    exit_code, stdout_text = run_remote_pipeline(sandbox, args)
    local_run_dir = Path(args.local_output_root).expanduser().resolve() / args.run_id
    local_run_dir.mkdir(parents=True, exist_ok=True)
    (local_run_dir / "daytona-runner.stdout.txt").write_text(stdout_text, encoding="utf-8")
    try:
        download_outputs(sandbox, args)
    except Exception:
        raise
    sys.stdout.write(stdout_text)
    if stdout_text and not stdout_text.endswith("\n"):
        sys.stdout.write("\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
