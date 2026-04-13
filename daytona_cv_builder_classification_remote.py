#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import sys
import tarfile
import tempfile
import time
import uuid
from pathlib import Path

from daytona_codex_remote import ensure_remote_codex, ensure_sandbox, load_local_exa_key
from minimax_key_selection import (
    build_minimax_runtime_values,
    cooldown_matcher,
    mark_key_cooldown,
)

REMOTE_USER = "daytona"
REMOTE_HOME = f"/home/{REMOTE_USER}"
PROGRESS_LINE_RE = re.compile(r"^\[\d+/\d+\] (completed|failed) ", flags=re.MULTILINE)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run CV builder classification inside a Daytona sandbox.")
    parser.add_argument("--api-key", default=os.environ.get("DAYTONA_API_KEY"))
    parser.add_argument("--api-url", default=os.environ.get("DAYTONA_API_URL", "https://app.daytona.io/api"))
    parser.add_argument("--sandbox-id", default=os.environ.get("DAYTONA_SANDBOX_ID"))
    parser.add_argument(
        "--sandbox-name",
        default=os.environ.get("DAYTONA_SANDBOX_NAME", "cv-rank-classifier-{run_id}"),
    )
    parser.add_argument("--cpu", type=int, default=int(os.environ.get("DAYTONA_CPU", "4")))
    parser.add_argument("--memory", type=int, default=int(os.environ.get("DAYTONA_MEMORY", "8")))
    parser.add_argument("--disk", type=int, default=int(os.environ.get("DAYTONA_DISK", "10")))
    parser.add_argument("--auto-stop-interval", type=int, default=int(os.environ.get("DAYTONA_AUTO_STOP_INTERVAL", "5")))
    parser.add_argument("--auto-archive-interval", type=int, default=int(os.environ.get("DAYTONA_AUTO_ARCHIVE_INTERVAL", "5")))
    parser.add_argument("--auto-delete-interval", type=int, default=int(os.environ.get("DAYTONA_AUTO_DELETE_INTERVAL", "0")))
    parser.add_argument(
        "--remote-codex-home",
        default=os.environ.get("DAYTONA_REMOTE_CODEX_HOME", f"{REMOTE_HOME}/.codex"),
    )
    parser.add_argument("--local-codex-home", default=os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"))

    parser.add_argument("--local-repo-root", required=True)
    parser.add_argument("--remote-repo-root", default=os.environ.get("DAYTONA_REMOTE_CV_RANK_ROOT", f"{REMOTE_HOME}/tools/cv-rank"))
    parser.add_argument("--remote-wafer-root", default=os.environ.get("DAYTONA_REMOTE_WAFER_ROOT", f"{REMOTE_HOME}/tools/claude-wafer-share"))

    parser.add_argument("--local-dossier-manifest", required=True, help="JSON list of local dossier JSON file paths to upload for this shard.")
    parser.add_argument("--local-system-prompt", required=True)
    parser.add_argument("--local-question-pack", required=True)
    parser.add_argument("--local-output-root", required=True)
    parser.add_argument("--run-id", required=True)

    parser.add_argument("--lane", choices=("codex", "claude", "minimax", "wafer"), required=True)
    parser.add_argument("--model", required=True)
    parser.add_argument("--tools", default="default")
    parser.add_argument("--effort", default="low")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--max-attempts", type=int, default=4)
    parser.add_argument("--retry-backoff-seconds", type=float, default=3.0)
    parser.add_argument("--partnership-target", default="XYZ")
    parser.add_argument("--hiring-role", default="XYZ role")
    parser.add_argument("--append-index", action="store_true")
    parser.add_argument("--skip-summary", action="store_true")

    parser.add_argument("--local-minimax-env", default=os.environ.get("MINIMAX_ENV_FILE") or str(Path.home() / ".claude-wafer" / "minimax.env"))
    parser.add_argument("--local-wafer-share-root", default=str(Path(__file__).resolve().parent / "dist" / "claude-wafer-share"))
    parser.add_argument("--local-wafer-env", default=str(Path.home() / ".claude-wafer" / "wafer.env"))
    parser.add_argument("--local-claude-credentials", default=str(Path.home() / ".claude" / ".credentials.json"))
    parser.add_argument("--local-claude-oauth-token", default=str(Path.home() / ".claude" / "oauth_token"))
    parser.add_argument("--local-claude-code-oauth-token", default=os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""))
    parser.add_argument(
        "--local-claude-code-oauth-token-file",
        default=str(Path.home() / ".claude" / "claude_code_oauth_token"),
    )
    return parser.parse_args()


def load_manifest(path: Path) -> list[Path]:
    raw = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(raw, list):
        raise SystemExit(f"Manifest must be a JSON list: {path}")
    out: list[Path] = []
    for entry in raw:
        p = Path(str(entry)).expanduser().resolve()
        if p.exists() and p.suffix.lower() == ".json":
            out.append(p)
    if not out:
        raise SystemExit(f"No valid dossier JSON files from manifest: {path}")
    return out


def build_runtime_bundle(
    *,
    local_repo_root: Path,
    dossier_paths: list[Path],
    system_prompt_path: Path,
    question_pack_path: Path,
) -> bytes:
    wanted = [
        local_repo_root / "scripts" / "run_cv_builder_classification.py",
        local_repo_root / "src" / "cv_rank",
    ]
    with tempfile.NamedTemporaryFile(suffix=".tar.gz") as tmp:
        with tarfile.open(tmp.name, "w:gz") as tar:
            for path in wanted:
                if not path.exists():
                    raise SystemExit(f"Missing required runtime path: {path}")
                if path.is_dir():
                    tar.add(path, arcname=str(path.relative_to(local_repo_root)))
                else:
                    tar.add(path, arcname=str(path.relative_to(local_repo_root)))
            tar.add(system_prompt_path, arcname="prompts/cv_builder_classification_system_prompt.txt")
            tar.add(question_pack_path, arcname="prompts/cv_builder_classification_question_pack_v1.json")
            for idx, dpath in enumerate(dossier_paths):
                arc = f"input_dossiers/{idx:04d}-{dpath.name}"
                tar.add(dpath, arcname=arc)
        return Path(tmp.name).read_bytes()


def build_wafer_bundle(wafer_share_root: Path) -> bytes:
    if not wafer_share_root.exists():
        raise SystemExit(f"Missing Wafer share bundle: {wafer_share_root}")
    with tempfile.NamedTemporaryFile(suffix=".tar.gz") as tmp:
        with tarfile.open(tmp.name, "w:gz") as tar:
            tar.add(wafer_share_root, arcname="claude-wafer-share")
        return Path(tmp.name).read_bytes()


def load_wafer_api_key(wafer_env_path: Path) -> str:
    direct = os.environ.get("WAFER_API_KEY", "").strip()
    if direct:
        return direct
    if wafer_env_path.exists():
        for raw in wafer_env_path.read_text(encoding="utf-8").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            if key.strip() == "WAFER_API_KEY":
                v = value.strip().strip('"').strip("'")
                if v:
                    return v
    raise SystemExit("Missing WAFER_API_KEY for Daytona Wafer run.")


def render_minimax_env(values: dict[str, str]) -> str:
    return (
        f"MINIMAX_API_KEY={shlex.quote(values['MINIMAX_API_KEY'])}\n"
        f"MINIMAX_API_HOST={shlex.quote(values['MINIMAX_API_HOST'])}\n"
        f"MINIMAX_MODEL={shlex.quote(values['MINIMAX_MODEL'])}\n"
        f"API_TIMEOUT_MS={shlex.quote(values['API_TIMEOUT_MS'])}\n"
        f"CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC={shlex.quote(values['CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC'])}\n"
        f"MINIMAX_KEY_INDEX={shlex.quote(values['MINIMAX_KEY_INDEX'])}\n"
        f"MINIMAX_KEY_COUNT={shlex.quote(values['MINIMAX_KEY_COUNT'])}\n"
    )


def collect_minimax_env(model: str, env_file: Path, *, seed: str | None = None) -> str:
    values = build_minimax_runtime_values(model=model, env_file=env_file, seed=seed)
    return render_minimax_env(values)


def install_remote_claude_auth(sandbox, args: argparse.Namespace) -> None:
    cred_path = Path(args.local_claude_credentials).expanduser().resolve()
    oauth_path = Path(args.local_claude_oauth_token).expanduser().resolve()
    oauth_code_token_file = Path(args.local_claude_code_oauth_token_file).expanduser().resolve()
    oauth_code_token = str(args.local_claude_code_oauth_token or "").strip()
    if not oauth_code_token and oauth_code_token_file.exists():
        oauth_code_token = oauth_code_token_file.read_text(encoding="utf-8").strip()
    if not cred_path.exists() and not oauth_path.exists():
        raise SystemExit(
            "Missing Claude auth material for claude lane. "
            f"Looked for credentials file at {cred_path} and oauth token at {oauth_path}. "
            "Run local `claude auth login` first, then relaunch."
        )

    remote_cred_tmp = ""
    if cred_path.exists():
        remote_cred_tmp = f"/tmp/claude-credentials-{args.run_id}-{uuid.uuid4().hex[:8]}.json"
        sandbox.fs.upload_file(cred_path.read_bytes(), remote_cred_tmp)

    remote_oauth_tmp = ""
    if oauth_path.exists():
        remote_oauth_tmp = f"/tmp/claude-oauth-{args.run_id}-{uuid.uuid4().hex[:8]}.txt"
        sandbox.fs.upload_file(oauth_path.read_bytes(), remote_oauth_tmp)

    remote_code_oauth_tmp = ""
    if oauth_code_token:
        remote_code_oauth_tmp = f"/tmp/claude-code-oauth-{args.run_id}-{uuid.uuid4().hex[:8]}.txt"
        sandbox.fs.upload_file((oauth_code_token + "\n").encode("utf-8"), remote_code_oauth_tmp)

    install_cmd = f"mkdir -p {shlex.quote(REMOTE_HOME + '/.claude')}"
    if remote_cred_tmp:
        install_cmd += (
            " && "
            f"install -m 0600 {shlex.quote(remote_cred_tmp)} {shlex.quote(REMOTE_HOME + '/.claude/.credentials.json')}"
        )
    if remote_oauth_tmp:
        install_cmd += (
            " && "
            f"install -m 0600 {shlex.quote(remote_oauth_tmp)} {shlex.quote(REMOTE_HOME + '/.claude/oauth_token')} && "
            f"chown {shlex.quote(REMOTE_USER)}:{shlex.quote(REMOTE_USER)} {shlex.quote(REMOTE_HOME + '/.claude/oauth_token')}"
        )
    if remote_code_oauth_tmp:
        install_cmd += (
            " && "
            f"install -m 0600 {shlex.quote(remote_code_oauth_tmp)} {shlex.quote(REMOTE_HOME + '/.claude/claude_code_oauth_token')} && "
            f"chown {shlex.quote(REMOTE_USER)}:{shlex.quote(REMOTE_USER)} {shlex.quote(REMOTE_HOME + '/.claude/claude_code_oauth_token')}"
        )
    install_cmd += (
        " && "
        f"chown -R {shlex.quote(REMOTE_USER)}:{shlex.quote(REMOTE_USER)} {shlex.quote(REMOTE_HOME + '/.claude')}"
    )
    result = sandbox.process.exec(install_cmd, timeout=120)
    if result.exit_code != 0:
        raise SystemExit(f"Failed to install Claude auth in Daytona sandbox:\n{result.result}")


def ensure_base_runtime(sandbox, args: argparse.Namespace) -> None:
    remote_root = args.remote_repo_root.rstrip("/")
    remote_wafer_root = args.remote_wafer_root.rstrip("/")
    lock_dir = "/tmp/cv-rank-classifier-install.lock"
    marker = f"{remote_root}/.cv_rank_classifier_ready"
    setup_cmd = (
        f"id -u {shlex.quote(REMOTE_USER)} >/dev/null 2>&1 || useradd -m -s /bin/bash {shlex.quote(REMOTE_USER)}; "
        f"mkdir -p {shlex.quote(remote_root)} {shlex.quote(REMOTE_HOME + '/tools/orchestrator')} {shlex.quote(REMOTE_HOME + '/.claude')} {shlex.quote(remote_wafer_root)}; "
        f"chown -R {shlex.quote(REMOTE_USER)}:{shlex.quote(REMOTE_USER)} {shlex.quote(REMOTE_HOME)}; "
        f"lock={shlex.quote(lock_dir)}; "
        "while ! mkdir \"$lock\" 2>/dev/null; do sleep 1; done; "
        "trap 'rmdir \"$lock\"' EXIT; "
        f"if [ ! -f {shlex.quote(marker)} ]; then "
        "python3 -m ensurepip --upgrade >/dev/null 2>&1 || true; "
        "python3 -m pip install --quiet pyyaml python-dotenv >/dev/null 2>&1 || true; "
        "apt-get update >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends nodejs npm >/dev/null; "
        "if ! command -v claude >/dev/null 2>&1; then npm install -g @anthropic-ai/claude-code >/dev/null; fi && "
        f"touch {shlex.quote(marker)}; "
        "fi"
    )
    result = sandbox.process.exec(setup_cmd, timeout=1800)
    if result.exit_code != 0:
        raise SystemExit(f"Failed to prepare Daytona classification runtime:\n{result.result}")


def ensure_remote_runtime(sandbox, args: argparse.Namespace) -> None:
    local_repo_root = Path(args.local_repo_root).expanduser().resolve()
    manifest_path = Path(args.local_dossier_manifest).expanduser().resolve()
    system_prompt_path = Path(args.local_system_prompt).expanduser().resolve()
    question_pack_path = Path(args.local_question_pack).expanduser().resolve()
    dossier_paths = load_manifest(manifest_path)

    # Prepare base runtime and directories before uploading/extracting bundles.
    ensure_base_runtime(sandbox, args)

    bundle = build_runtime_bundle(
        local_repo_root=local_repo_root,
        dossier_paths=dossier_paths,
        system_prompt_path=system_prompt_path,
        question_pack_path=question_pack_path,
    )
    remote_root = args.remote_repo_root.rstrip("/")
    remote_bundle = f"/tmp/cv-rank-classifier-{args.run_id}.tar.gz"
    sandbox.fs.upload_file(bundle, remote_bundle)
    bundle_extract_lock = "/tmp/cv-rank-classifier-bundle.lock"
    extract_result = sandbox.process.exec(
        (
            f"lock={shlex.quote(bundle_extract_lock)}; "
            "while ! mkdir \"$lock\" 2>/dev/null; do sleep 1; done; "
            "trap 'rmdir \"$lock\"' EXIT; "
            f"rm -rf {shlex.quote(remote_root + '/input_dossiers')} && "
            f"cd {shlex.quote(remote_root)} && tar --overwrite -xzf {shlex.quote(remote_bundle)}"
        ),
        timeout=600,
    )
    if extract_result.exit_code != 0:
        raise SystemExit(f"Failed to extract classifier runtime bundle:\n{extract_result.result}")

    agent_path = Path(__file__).resolve().parent / "claude_wafer_agent.py"
    minimax_helper_path = Path(__file__).resolve().parent / "minimax_key_selection.py"
    remote_agent = f"{REMOTE_HOME}/tools/orchestrator/claude_wafer_agent.py"
    remote_minimax_helper = f"{REMOTE_HOME}/tools/orchestrator/minimax_key_selection.py"
    remote_agent_tmp = f"/tmp/claude_wafer_agent-{args.run_id}-{uuid.uuid4().hex[:8]}.py"
    remote_minimax_helper_tmp = f"/tmp/minimax_key_selection-{args.run_id}-{uuid.uuid4().hex[:8]}.py"
    sandbox.fs.upload_file(agent_path.read_bytes(), remote_agent_tmp)
    sandbox.fs.upload_file(minimax_helper_path.read_bytes(), remote_minimax_helper_tmp)
    agent_install_lock = "/tmp/cv-builder-agent-install.lock"
    install_agent_result = sandbox.process.exec(
        (
            f"lock={shlex.quote(agent_install_lock)}; "
            "while ! mkdir \"$lock\" 2>/dev/null; do sleep 1; done; "
            "trap 'rmdir \"$lock\"' EXIT; "
            f"mkdir -p {shlex.quote(REMOTE_HOME + '/tools/orchestrator')} && "
            f"install -m 0755 {shlex.quote(remote_agent_tmp)} {shlex.quote(remote_agent + '.new')} && "
            f"install -m 0644 {shlex.quote(remote_minimax_helper_tmp)} {shlex.quote(remote_minimax_helper + '.new')} && "
            f"mv {shlex.quote(remote_minimax_helper + '.new')} {shlex.quote(remote_minimax_helper)} && "
            f"mv {shlex.quote(remote_agent + '.new')} {shlex.quote(remote_agent)}"
        ),
        timeout=120,
    )
    if install_agent_result.exit_code != 0:
        raise SystemExit(f"Failed to install remote claude_wafer_agent.py:\n{install_agent_result.result}")

    if args.lane == "codex":
        ensure_remote_codex(sandbox, args)
        sandbox.process.exec(
            f"mkdir -p {shlex.quote(args.remote_codex_home)} && "
            f"chown -R {shlex.quote(REMOTE_USER)}:{shlex.quote(REMOTE_USER)} {shlex.quote(args.remote_codex_home)}",
            timeout=120,
        )
    elif args.lane == "claude":
        install_remote_claude_auth(sandbox, args)
    elif args.lane == "minimax":
        minimax_env_path = Path(args.local_minimax_env).expanduser().resolve()
        minimax_values = build_minimax_runtime_values(model=args.model, env_file=minimax_env_path, seed=args.run_id)
        args.minimax_key_index = int(minimax_values["MINIMAX_KEY_INDEX"])
        minimax_env_text = render_minimax_env(minimax_values)
        sandbox.fs.upload_file(minimax_env_text.encode("utf-8"), f"{remote_root}/.minimax.remote")
    elif args.lane == "wafer":
        wafer_share_root = Path(args.local_wafer_share_root).expanduser().resolve()
        wafer_bundle = build_wafer_bundle(wafer_share_root)
        remote_wafer_root = args.remote_wafer_root.rstrip("/")
        remote_wafer_bundle = f"/tmp/claude-wafer-share-{args.run_id}.tar.gz"
        sandbox.fs.upload_file(wafer_bundle, remote_wafer_bundle)
        install_wafer = (
            f"mkdir -p {shlex.quote(remote_wafer_root)} && "
            f"cd {shlex.quote(remote_wafer_root)} && tar -xzf {shlex.quote(remote_wafer_bundle)} --strip-components=1 && "
            f"chown -R {shlex.quote(REMOTE_USER)}:{shlex.quote(REMOTE_USER)} {shlex.quote(remote_wafer_root)}"
        )
        result = sandbox.process.exec(install_wafer, timeout=900)
        if result.exit_code != 0:
            raise SystemExit(f"Failed to install wafer runtime in Daytona:\n{result.result}")
        wafer_env_path = Path(args.local_wafer_env).expanduser().resolve()
        wafer_key = load_wafer_api_key(wafer_env_path)
        wafer_env_text = (
            f"WAFER_API_KEY={shlex.quote(wafer_key)}\n"
            "WAFER_BASE_URL='https://pass.wafer.ai/v1'\n"
            f"WAFER_MODEL={shlex.quote(args.model)}\n"
        )
        sandbox.fs.upload_file(wafer_env_text.encode("utf-8"), f"{remote_root}/.wafer.remote")
        sandbox.fs.upload_file(
            wafer_env_text.encode("utf-8"),
            f"{remote_wafer_root}/runtime/claude-wafer/wafer.env",
        )

        exa_key = load_local_exa_key(args.run_id or args.sandbox_name)
        if exa_key:
            config_dir = f"{remote_wafer_root}/runtime/claude-wafer/state/config"
            exa_url = (
                "https://mcp.exa.ai/mcp"
                f"?exaApiKey={exa_key}"
                "&tools=web_search_advanced_exa,web_search_exa,web_fetch_exa"
            )
            mcp_cmd = (
                f"CLAUDE_CONFIG_DIR={shlex.quote(config_dir)} claude mcp remove exa >/dev/null 2>&1 || true; "
                f"CLAUDE_CONFIG_DIR={shlex.quote(config_dir)} claude mcp add --transport http exa {shlex.quote(exa_url)} >/dev/null 2>&1 || true"
            )
            sandbox.process.exec(
                (
                    f"mkdir -p {shlex.quote(config_dir)} && "
                    f"chown -R {shlex.quote(REMOTE_USER)}:{shlex.quote(REMOTE_USER)} {shlex.quote(config_dir)} && "
                    f"su -s /bin/bash {shlex.quote(REMOTE_USER)} -c {shlex.quote(mcp_cmd)}"
                ),
                timeout=300,
            )
            verify_mcp = sandbox.process.exec(
                (
                    f"su -s /bin/bash {shlex.quote(REMOTE_USER)} -c "
                    f"{shlex.quote(f'CLAUDE_CONFIG_DIR={shlex.quote(config_dir)} claude mcp list')}"
                ),
                timeout=120,
            )
            verify_output = (verify_mcp.result or "").strip()
            if verify_mcp.exit_code != 0 or "exa" not in verify_output.lower():
                raise SystemExit(
                    "Failed to verify Exa MCP registration for Wafer lane.\n"
                    f"exit_code={verify_mcp.exit_code}\n"
                    f"output:\n{verify_output}"
                )


def run_remote_classification(sandbox, args: argparse.Namespace) -> tuple[int, str]:
    from daytona.common.pty import PtySize

    remote_root = args.remote_repo_root.rstrip("/")
    remote_output_root = f"{remote_root}/outputs/cv_builder_classification_daytona"
    lane_bin_export = ""
    lane_extra_env = ""
    if args.lane == "wafer":
        lane_bin_export = f"export CLAUDE_WAFER_BIN={shlex.quote(args.remote_wafer_root.rstrip('/') + '/bin/claude-wafer')} && "
        lane_extra_env = "set -a && source .wafer.remote && set +a && "
    elif args.lane in ("minimax", "claude"):
        lane_bin_export = "export CLAUDE_WAFER_BIN=$(command -v claude) && "
        if args.lane == "minimax":
            lane_extra_env = "set -a && source .minimax.remote && set +a && "
        else:
            lane_extra_env = (
                "if [ -f ~/.claude/claude_code_oauth_token ]; then "
                "export CLAUDE_CODE_OAUTH_TOKEN=\"$(cat ~/.claude/claude_code_oauth_token)\"; "
                "fi && "
            )
    elif args.lane == "codex":
        lane_bin_export = (
            "export CLAUDE_WAFER_BIN=$(command -v codex) && "
            f"export CODEX_HOME={shlex.quote(args.remote_codex_home)} && "
        )

    cmd = (
        "unset DAYTONA_API_KEY DAYTONA_API_URL DAYTONA_PYTHON_BIN DAYTONA_SANDBOX_ID DAYTONA_SANDBOX_NAME && "
        f"{lane_extra_env}"
        "export SHELL=/bin/bash && "
        f"{lane_bin_export}"
        f"export PYTHONPATH={shlex.quote(remote_root + '/src')} && "
        f"[ -d {shlex.quote(remote_root + '/input_dossiers')} ] || (echo 'Missing input_dossiers at {remote_root}/input_dossiers' && exit 2) && "
        f"ls -1 {shlex.quote(remote_root + '/input_dossiers')} | head -n 5 && "
        f"chmod +x {shlex.quote(remote_root + '/scripts/run_cv_builder_classification.py')} {shlex.quote(REMOTE_HOME + '/tools/orchestrator/claude_wafer_agent.py')} >/dev/null 2>&1 || true && "
        "python3 scripts/run_cv_builder_classification.py "
        f"--dossier-dir {shlex.quote(remote_root + '/input_dossiers')} "
        "--dossier-glob '*.json' "
        f"--out-root {shlex.quote(remote_output_root)} "
        f"--run-id {shlex.quote(args.run_id)} "
        f"--runner {shlex.quote(REMOTE_HOME + '/tools/orchestrator/claude_wafer_agent.py')} "
        "--system-prompt-file prompts/cv_builder_classification_system_prompt.txt "
        "--question-pack-file prompts/cv_builder_classification_question_pack_v1.json "
        f"--lane {shlex.quote(args.lane)} "
        f"--model {shlex.quote(args.model)} "
        f"--tools {shlex.quote(args.tools)} "
        f"--effort {shlex.quote(args.effort)} "
        f"--wafer-timeout-seconds {int(args.timeout_seconds)} "
        f"--max-attempts {int(args.max_attempts)} "
        f"--retry-backoff-seconds {float(args.retry_backoff_seconds)} "
        f"--partnership-target {shlex.quote(args.partnership_target)} "
        f"--hiring-role {shlex.quote(args.hiring_role)}"
    )
    if args.append_index:
        cmd += " --append-index"
    if args.skip_summary:
        cmd += " --skip-summary"

    session_id = f"cv-builder-classify-{args.run_id}-{uuid.uuid4().hex[:8]}"
    pty = sandbox.process.create_pty_session(
        id=session_id,
        cwd=remote_root,
        envs={"TERM": "xterm-256color"},
        pty_size=PtySize(cols=160, rows=40),
    )
    wrapped_cmd = f"su -s /bin/bash {shlex.quote(REMOTE_USER)} -c {shlex.quote(f'cd {shlex.quote(remote_root)} && bash -lc {shlex.quote(cmd)}')}"
    pty.send_input(f"bash -lc {shlex.quote(wrapped_cmd)}; exit $?\n")
    chunks: list[str] = []
    line_buffer = ""
    last_sync_at = 0.0

    def maybe_sync_partial(force: bool = False) -> None:
        nonlocal last_sync_at
        now = time.monotonic()
        if not force and now - last_sync_at < 5.0:
            return
        try:
            download_outputs(sandbox, args, partial=True)
            last_sync_at = now
        except Exception:
            pass

    for data in pty:
        text = data.decode("utf-8", errors="replace")
        chunks.append(text)
        sys.stdout.write(text)
        sys.stdout.flush()
        merged = line_buffer + text
        if merged.endswith("\n"):
            complete_text = merged
            line_buffer = ""
        else:
            complete_text, _, line_buffer = merged.rpartition("\n")
        if complete_text and PROGRESS_LINE_RE.search(complete_text):
            maybe_sync_partial()
    exit_code = getattr(pty, "exit_code", None)
    if exit_code is None:
        exit_code = 0
    maybe_sync_partial(force=True)
    return exit_code, "".join(chunks)


def download_outputs(sandbox, args: argparse.Namespace, *, partial: bool = False) -> None:
    remote_root = args.remote_repo_root.rstrip("/")
    remote_base = f"{remote_root}/outputs/cv_builder_classification_daytona"
    archive_suffix = "partial" if partial else "full"
    remote_archive = f"/tmp/cv-builder-classify-{args.run_id}-{archive_suffix}.tar.gz"
    local_output_root = Path(args.local_output_root).expanduser().resolve()
    local_output_root.mkdir(parents=True, exist_ok=True)
    if partial:
        result = sandbox.process.exec(
            (
                f"cd {shlex.quote(remote_base)} && "
                f"run={shlex.quote(args.run_id)} && "
                "tmp=$(mktemp) && "
                "for path in "
                "\"$run/index.json\" "
                "\"$run/summary.json\" "
                "\"$run/question_pack.resolved.json\" "
                "\"$run/run_config.json\"; "
                "do [ -f \"$path\" ] && printf '%s\\n' \"$path\" >> \"$tmp\"; done && "
                "find \"$run/classifications\" -type f -name '*.classification.json' -print >> \"$tmp\" 2>/dev/null || true && "
                "find \"$run/raw\" -type f \\( -name '*.raw.txt' -o -name '*.raw.failed.txt' \\) -print >> \"$tmp\" 2>/dev/null || true && "
                "if [ ! -s \"$tmp\" ]; then rm -f \"$tmp\"; exit 0; fi && "
                f"tar -czf {shlex.quote(remote_archive)} -T \"$tmp\" && "
                "rm -f \"$tmp\""
            ),
            timeout=600,
        )
    else:
        result = sandbox.process.exec(
            f"cd {shlex.quote(remote_base)} && tar -czf {shlex.quote(remote_archive)} {shlex.quote(args.run_id)}",
            timeout=600,
        )
    if result.exit_code != 0:
        raise SystemExit(f"Failed to archive remote classifier output:\n{result.result}")
    if partial and "No such file" in str(result.result or ""):
        return
    archive = sandbox.fs.download_file(remote_archive)
    with tempfile.NamedTemporaryFile(suffix=".tar.gz") as tmp:
        tmp.write(archive)
        tmp.flush()
        with tarfile.open(tmp.name, "r:gz") as tar:
            tar.extractall(local_output_root)


def main() -> int:
    args = parse_args()
    if args.sandbox_name and "{run_id}" in str(args.sandbox_name):
        args.sandbox_name = str(args.sandbox_name).format(run_id=args.run_id)
    sandbox = ensure_sandbox(args)
    ensure_remote_runtime(sandbox, args)
    exit_code, stdout_text = run_remote_classification(sandbox, args)

    local_out = Path(args.local_output_root).expanduser().resolve() / args.run_id
    local_out.mkdir(parents=True, exist_ok=True)
    (local_out / "daytona-runner.stdout.txt").write_text(stdout_text, encoding="utf-8")

    try:
        download_outputs(sandbox, args)
    except Exception:
        pass

    if (
        args.lane == "minimax"
        and getattr(args, "minimax_key_index", None) is not None
        and cooldown_matcher(stdout_text)
    ):
        mark_key_cooldown(
            env_file=Path(args.local_minimax_env).expanduser().resolve(),
            key_index=int(args.minimax_key_index),
            reason="runtime_quota_or_rate_limit",
            observed_text=stdout_text,
        )

    if stdout_text and not stdout_text.endswith("\n"):
        sys.stdout.write("\n")
    return int(exit_code or 0)


if __name__ == "__main__":
    raise SystemExit(main())
