#!/usr/bin/env python3

from __future__ import annotations

import argparse
import os
import shlex
import sys
import uuid
from pathlib import Path

from daytona_codex_remote import ensure_sandbox, load_local_exa_key
from daytona_cv_builder_classification_remote import (
    REMOTE_HOME,
    REMOTE_USER,
    build_wafer_bundle,
    ensure_base_runtime,
    install_remote_claude_auth,
    load_wafer_api_key,
    render_minimax_env,
)
from minimax_key_selection import (
    build_minimax_runtime_values,
    cooldown_matcher,
    mark_key_cooldown,
)


ROOT = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Claude-family prompt batches inside a Daytona sandbox.")
    parser.add_argument("--api-key", default=os.environ.get("DAYTONA_API_KEY"))
    parser.add_argument("--api-url", default=os.environ.get("DAYTONA_API_URL", "https://app.daytona.io/api"))
    parser.add_argument("--sandbox-id", default=os.environ.get("DAYTONA_SANDBOX_ID"))
    parser.add_argument("--sandbox-name", default=os.environ.get("DAYTONA_SANDBOX_NAME", "claude-prompt-batch"))
    parser.add_argument("--runtime", choices=("wafer", "claude", "minimax"), required=True)
    parser.add_argument("--cpu", type=int, default=int(os.environ.get("DAYTONA_CPU", "2")))
    parser.add_argument("--memory", type=int, default=int(os.environ.get("DAYTONA_MEMORY", "4")))
    parser.add_argument("--disk", type=int, default=int(os.environ.get("DAYTONA_DISK", "10")))
    parser.add_argument("--auto-stop-interval", type=int, default=int(os.environ.get("DAYTONA_AUTO_STOP_INTERVAL", "5")))
    parser.add_argument("--auto-archive-interval", type=int, default=int(os.environ.get("DAYTONA_AUTO_ARCHIVE_INTERVAL", "5")))
    parser.add_argument("--auto-delete-interval", type=int, default=int(os.environ.get("DAYTONA_AUTO_DELETE_INTERVAL", "15")))
    parser.add_argument("--local-wafer-share-root", default=str(ROOT / "dist" / "claude-wafer-share"))
    parser.add_argument("--local-wafer-env", default=str(Path.home() / ".claude-wafer" / "wafer.env"))
    parser.add_argument("--local-minimax-env", default=str(Path.home() / ".claude-wafer" / "minimax.env"))
    parser.add_argument("--local-claude-credentials", default=str(Path.home() / ".claude" / ".credentials.json"))
    parser.add_argument("--local-claude-oauth-token", default=str(Path.home() / ".claude" / "oauth_token"))
    parser.add_argument("--local-claude-code-oauth-token", default=os.environ.get("CLAUDE_CODE_OAUTH_TOKEN", ""))
    parser.add_argument("--local-claude-code-oauth-token-file", default=str(Path.home() / ".claude" / "claude_code_oauth_token"))
    parser.add_argument("--remote-wafer-root", default=f"{REMOTE_HOME}/tools/claude-wafer-share")
    parser.add_argument("--remote-work-root", default=f"{REMOTE_HOME}/tools/daytona-prompt-batch")
    parser.add_argument("--output-file", required=True)
    parser.add_argument("--append-output", action="store_true")
    parser.add_argument("--model", default="claude-sonnet-4-5")
    parser.add_argument("--tools", default="default")
    parser.add_argument("--effort", default="low")
    parser.add_argument("--web-mode", "--wafer-web-mode", dest="web_mode", choices=("exa", "mixed", "claude"), default="exa")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--workdir", default="")
    parser.add_argument("--run-id", default="")
    parser.add_argument("--agent-mode", choices=("ask", "start", "send"), default="ask")
    parser.add_argument("--session-alias", default="")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--create-session", action="store_true")
    parser.add_argument("--allowed-tools", default="")
    parser.add_argument("--disallowed-tools", default="")
    parser.add_argument("prompt")
    return parser.parse_args()


def install_remote_agent(sandbox, *, run_id: str) -> None:
    agent_path = ROOT / "claude_wafer_agent.py"
    minimax_helper_path = ROOT / "minimax_key_selection.py"
    remote_agent = f"{REMOTE_HOME}/tools/orchestrator/claude_wafer_agent.py"
    remote_minimax_helper = f"{REMOTE_HOME}/tools/orchestrator/minimax_key_selection.py"
    remote_agent_tmp = f"/tmp/claude_wafer_agent-{run_id}-{uuid.uuid4().hex[:8]}.py"
    remote_helper_tmp = f"/tmp/minimax_key_selection-{run_id}-{uuid.uuid4().hex[:8]}.py"
    sandbox.fs.upload_file(agent_path.read_bytes(), remote_agent_tmp)
    sandbox.fs.upload_file(minimax_helper_path.read_bytes(), remote_helper_tmp)
    lock_dir = "/tmp/daytona-claude-agent-install.lock"
    result = sandbox.process.exec(
        (
            f"lock={shlex.quote(lock_dir)}; "
            "while ! mkdir \"$lock\" 2>/dev/null; do sleep 1; done; "
            "trap 'rmdir \"$lock\"' EXIT; "
            f"mkdir -p {shlex.quote(REMOTE_HOME + '/tools/orchestrator')} && "
            f"install -m 0755 {shlex.quote(remote_agent_tmp)} {shlex.quote(remote_agent + '.new')} && "
            f"install -m 0644 {shlex.quote(remote_helper_tmp)} {shlex.quote(remote_minimax_helper + '.new')} && "
            f"mv {shlex.quote(remote_minimax_helper + '.new')} {shlex.quote(remote_minimax_helper)} && "
            f"mv {shlex.quote(remote_agent + '.new')} {shlex.quote(remote_agent)}"
        ),
        timeout=120,
    )
    if result.exit_code != 0:
        raise SystemExit(f"Failed to install remote claude_wafer_agent.py:\n{result.result}")


def ensure_remote_wafer_runtime(sandbox, args: argparse.Namespace, *, run_id: str) -> None:
    wafer_share_root = Path(args.local_wafer_share_root).expanduser().resolve()
    remote_wafer_root = args.remote_wafer_root.rstrip("/")
    remote_wafer_bundle = f"/tmp/claude-wafer-share-{run_id}.tar.gz"
    wafer_bundle = build_wafer_bundle(wafer_share_root)
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
    sandbox.fs.upload_file(wafer_env_text.encode("utf-8"), f"{args.remote_repo_root}/.wafer.remote")
    sandbox.process.exec(
        f"mkdir -p {shlex.quote(remote_wafer_root)}/runtime/claude-wafer/state",
        timeout=120,
    )
    sandbox.fs.upload_file(
        wafer_env_text.encode("utf-8"),
        f"{remote_wafer_root}/runtime/claude-wafer/wafer.env",
    )

    exa_key = load_local_exa_key(run_id)
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


def ensure_remote_claude_config(sandbox, args: argparse.Namespace, *, run_id: str) -> str:
    remote_root = args.remote_repo_root.rstrip("/")
    config_dir = f"{remote_root}/.claude-config"
    setup = sandbox.process.exec(
        (
            f"mkdir -p {shlex.quote(config_dir)} && "
            f"chown -R {shlex.quote(REMOTE_USER)}:{shlex.quote(REMOTE_USER)} {shlex.quote(config_dir)}"
        ),
        timeout=120,
    )
    if setup.exit_code != 0:
        raise SystemExit(f"Failed to prepare Claude config dir:\n{setup.result}")
    if args.web_mode in {"exa", "mixed"}:
        exa_key = load_local_exa_key(run_id)
        if exa_key:
            exa_url = (
                "https://mcp.exa.ai/mcp"
                f"?exaApiKey={exa_key}"
                "&tools=web_search_advanced_exa,web_search_exa,web_fetch_exa"
            )
            mcp_cmd = (
                f"CLAUDE_CONFIG_DIR={shlex.quote(config_dir)} claude mcp remove exa >/dev/null 2>&1 || true; "
                f"CLAUDE_CONFIG_DIR={shlex.quote(config_dir)} claude mcp add -s user --transport http exa {shlex.quote(exa_url)}"
            )
            add_exa = sandbox.process.exec(
                f"su -s /bin/bash {shlex.quote(REMOTE_USER)} -c {shlex.quote(mcp_cmd)}",
                timeout=300,
            )
            if add_exa.exit_code != 0:
                raise SystemExit(
                    "Failed to register Exa MCP for Claude-family runtime.\n"
                    f"exit_code={add_exa.exit_code}\n"
                    f"output:\n{add_exa.result}"
                )
    if args.runtime == "minimax" and args.web_mode in {"claude", "mixed"}:
        minimax_mcp_cmd = (
            f"export PATH=\"$HOME/.local/bin:$PATH\"; "
            "if ! command -v uvx >/dev/null 2>&1; then "
            "python3 -m pip install --user --quiet uv; "
            "fi; "
            f"cd {shlex.quote(remote_root)} && "
            "set -a && . .minimax.remote && set +a && "
            f"CLAUDE_CONFIG_DIR={shlex.quote(config_dir)} claude mcp remove MiniMax >/dev/null 2>&1 || true; "
            f"CLAUDE_CONFIG_DIR={shlex.quote(config_dir)} claude mcp add -s user MiniMax "
            "--env MINIMAX_API_KEY=$MINIMAX_API_KEY "
            "--env MINIMAX_API_HOST=$MINIMAX_API_HOST "
            "-- uvx minimax-coding-plan-mcp -y"
        )
        add_minimax_mcp = sandbox.process.exec(
            f"su -s /bin/bash {shlex.quote(REMOTE_USER)} -c {shlex.quote(minimax_mcp_cmd)}",
            timeout=600,
        )
        if add_minimax_mcp.exit_code != 0:
            raise SystemExit(
                "Failed to register MiniMax MCP for MiniMax runtime.\n"
                f"exit_code={add_minimax_mcp.exit_code}\n"
                f"output:\n{add_minimax_mcp.result}"
            )
    verify = sandbox.process.exec(
        (
            f"su -s /bin/bash {shlex.quote(REMOTE_USER)} -c "
            f"{shlex.quote(f'CLAUDE_CONFIG_DIR={shlex.quote(config_dir)} claude mcp list')}"
        ),
        timeout=120,
    )
    verify_output = str(verify.result or "")
    if verify.exit_code != 0:
        raise SystemExit(
            "Failed to verify Claude MCP configuration.\n"
            f"exit_code={verify.exit_code}\n"
            f"output:\n{verify_output}"
        )
    if args.web_mode in {"exa", "mixed"} and "exa" not in verify_output.lower():
        raise SystemExit(f"Exa MCP missing after registration.\n{verify_output}")
    if args.runtime == "minimax" and args.web_mode in {"claude", "mixed"} and "minimax" not in verify_output.lower():
        raise SystemExit(f"MiniMax MCP missing after registration.\n{verify_output}")
    return config_dir


def ensure_remote_runtime(sandbox, args: argparse.Namespace) -> str:
    run_id = str(args.run_id or args.sandbox_name or uuid.uuid4().hex)
    remote_work_root = args.remote_work_root.rstrip("/")
    args.remote_repo_root = f"{remote_work_root}/{run_id}"
    ensure_base_runtime(sandbox, args)
    install_remote_agent(sandbox, run_id=run_id)
    if args.runtime == "wafer":
        ensure_remote_wafer_runtime(sandbox, args, run_id=run_id)
    elif args.runtime == "claude":
        install_remote_claude_auth(sandbox, args)
        args.remote_claude_config_dir = ensure_remote_claude_config(sandbox, args, run_id=run_id)
    elif args.runtime == "minimax":
        minimax_env_path = Path(args.local_minimax_env).expanduser().resolve()
        minimax_values = build_minimax_runtime_values(model=args.model, env_file=minimax_env_path, seed=run_id)
        args.minimax_key_index = int(minimax_values["MINIMAX_KEY_INDEX"])
        minimax_env_text = render_minimax_env(minimax_values)
        sandbox.fs.upload_file(minimax_env_text.encode("utf-8"), f"{args.remote_repo_root}/.minimax.remote")
        args.remote_claude_config_dir = ensure_remote_claude_config(sandbox, args, run_id=run_id)
    return run_id


def normalize_agent_output(text: str) -> str:
    if not text.strip():
        return text
    if "\n\n" in text:
        header, body = text.split("\n\n", 1)
        header_lines = [line.strip() for line in header.splitlines() if line.strip()]
        if header_lines and all(
            line.startswith("mode:") or line.startswith("alias:") or line.startswith("session_id:")
            for line in header_lines
        ):
            return body
    return text


def run_remote_prompt(sandbox, args: argparse.Namespace) -> tuple[int, str]:
    remote_root = args.remote_repo_root.rstrip("/")
    remote_prompt = f"{remote_root}/prompt.txt"
    sandbox.fs.upload_file((args.prompt + "\n").encode("utf-8"), remote_prompt)
    workdir = str(args.workdir or "").strip() or remote_root
    runtime_exports = []
    runtime_args = []
    agent_args = []
    if args.runtime == "wafer":
        runtime_exports.extend(
            [
                "export CLAUDE_WAFER_LAUNCHER_KIND=wafer",
                f"export CLAUDE_WAFER_BIN={shlex.quote(args.remote_wafer_root.rstrip('/') + '/bin/claude-wafer')}",
                f"export CLAUDE_WAFER_ROOT={shlex.quote(args.remote_wafer_root.rstrip('/') + '/runtime/claude-wafer')}",
            ]
        )
        runtime_args.extend(
            [
                f"--wafer-model {shlex.quote(args.model)}",
                f"--wafer-web-mode {shlex.quote(args.web_mode)}",
            ]
        )
    elif args.runtime == "claude":
        runtime_exports.extend(
            [
                "export CLAUDE_WAFER_LAUNCHER_KIND=plain",
                "export CLAUDE_WAFER_BIN=$(command -v claude)",
                f"export CLAUDE_CONFIG_DIR={shlex.quote(str(getattr(args, 'remote_claude_config_dir', remote_root + '/.claude-config')))}",
            ]
        )
    elif args.runtime == "minimax":
        runtime_exports.extend(
            [
                "export CLAUDE_WAFER_LAUNCHER_KIND=minimax",
                "export CLAUDE_WAFER_BIN=$(command -v claude)",
                f"export MINIMAX_ENV_FILE={shlex.quote(remote_root + '/.minimax.remote')}",
                f"export MINIMAX_KEY_SEED={shlex.quote(str(args.run_id or args.sandbox_name))}",
                f"export CLAUDE_CONFIG_DIR={shlex.quote(str(getattr(args, 'remote_claude_config_dir', remote_root + '/.claude-config')))}",
            ]
        )
    if args.runtime in {"claude", "minimax"}:
        exa_allowed = "mcp__exa__web_search_exa,mcp__exa__web_fetch_exa,mcp__exa__web_search_advanced_exa"
        minimax_allowed = "mcp__MiniMax__web_search,mcp__MiniMax__understand_image"
        allowed_tools = str(args.allowed_tools or "").strip()
        disallowed_tools = str(args.disallowed_tools or "").strip()
        if args.runtime == "minimax" and args.web_mode == "claude":
            allowed_tools = ",".join(filter(None, [allowed_tools, minimax_allowed]))
            disallowed_tools = ",".join(filter(None, [disallowed_tools, "WebSearch,WebFetch"]))
        elif args.runtime == "minimax" and args.web_mode == "mixed":
            allowed_tools = ",".join(filter(None, [allowed_tools, minimax_allowed, exa_allowed]))
            disallowed_tools = ",".join(filter(None, [disallowed_tools, "WebSearch,WebFetch"]))
        elif args.web_mode == "exa":
            allowed_tools = ",".join(filter(None, [allowed_tools, exa_allowed]))
            disallowed_tools = ",".join(filter(None, [disallowed_tools, "WebSearch,WebFetch"]))
        elif args.web_mode == "mixed":
            allowed_tools = ",".join(filter(None, [allowed_tools, exa_allowed]))
        if allowed_tools:
            agent_args.extend(["--allowed-tools", allowed_tools])
        if disallowed_tools:
            agent_args.extend(["--disallowed-tools", disallowed_tools])
    if str(args.session_alias or "").strip():
        agent_args.extend(["--alias", str(args.session_alias).strip()])
    if str(args.session_id or "").strip():
        agent_args.extend(["--session-id", str(args.session_id).strip()])
    if bool(args.create_session):
        agent_args.append("--create-session")
    cmd = (
        "unset DAYTONA_API_KEY DAYTONA_API_URL DAYTONA_PYTHON_BIN DAYTONA_SANDBOX_ID DAYTONA_SANDBOX_NAME && "
        "export SHELL=/bin/bash && "
        + " && ".join(runtime_exports)
        + " && "
        + f"cd {shlex.quote(workdir)} && "
        + f"python3 /home/daytona/tools/orchestrator/claude_wafer_agent.py {shlex.quote(str(args.agent_mode or 'ask'))} "
        + f"--prompt-file {shlex.quote(remote_prompt)} "
        + f"--tools {shlex.quote(args.tools)} "
        + "--output-format text "
        + f"--effort {shlex.quote(args.effort)} "
        + f"--model {shlex.quote(args.model)} "
        + (" ".join(shlex.quote(part) for part in agent_args) + " " if agent_args else "")
        + (" ".join(runtime_args) + " " if runtime_args else "")
    )
    result = sandbox.process.exec(
        f"su -s /bin/bash {shlex.quote(REMOTE_USER)} -c {shlex.quote(f'bash -lc {shlex.quote(cmd)}')}",
        timeout=int(args.timeout_seconds),
    )
    return int(result.exit_code or 0), str(result.result or "")


def main() -> int:
    args = parse_args()
    sandbox = ensure_sandbox(args)
    run_id = ensure_remote_runtime(sandbox, args)
    args.run_id = run_id
    exit_code, output_text = run_remote_prompt(sandbox, args)

    output_path = Path(args.output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    normalized = normalize_agent_output(output_text)
    if (
        args.runtime == "minimax"
        and getattr(args, "minimax_key_index", None) is not None
        and cooldown_matcher(output_text)
    ):
        mark_key_cooldown(
            env_file=Path(args.local_minimax_env).expanduser().resolve(),
            key_index=int(args.minimax_key_index),
            reason="runtime_quota_or_rate_limit",
            observed_text=output_text,
        )
    if args.append_output and output_path.exists() and output_path.stat().st_size > 0:
        with output_path.open("a", encoding="utf-8") as fh:
            if not normalized.startswith("\n"):
                fh.write("\n\n")
            fh.write(normalized)
    else:
        output_path.write_text(normalized, encoding="utf-8")
    sys.stdout.write(output_text)
    if output_text and not output_text.endswith("\n"):
        sys.stdout.write("\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
