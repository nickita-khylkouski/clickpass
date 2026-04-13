#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import subprocess
import sys
import tempfile
import tarfile
import time
from pathlib import Path


ROOT = Path(__file__).resolve().parent


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Run Codex remotely inside a Daytona sandbox.")
    parser.add_argument("--api-key", default=os.environ.get("DAYTONA_API_KEY"))
    parser.add_argument("--api-url", default=os.environ.get("DAYTONA_API_URL", "https://app.daytona.io/api"))
    parser.add_argument("--sandbox-id", default=os.environ.get("DAYTONA_SANDBOX_ID"))
    parser.add_argument("--sandbox-name", default=os.environ.get("DAYTONA_SANDBOX_NAME", "codex-scraper"))
    parser.add_argument("--cpu", type=int, default=int(os.environ.get("DAYTONA_CPU", "4")))
    parser.add_argument("--memory", type=int, default=int(os.environ.get("DAYTONA_MEMORY", "8")))
    parser.add_argument("--disk", type=int, default=int(os.environ.get("DAYTONA_DISK", "10")))
    parser.add_argument("--auto-stop-interval", type=int, default=int(os.environ.get("DAYTONA_AUTO_STOP_INTERVAL", "5")))
    parser.add_argument("--auto-archive-interval", type=int, default=int(os.environ.get("DAYTONA_AUTO_ARCHIVE_INTERVAL", "5")))
    parser.add_argument("--auto-delete-interval", type=int, default=int(os.environ.get("DAYTONA_AUTO_DELETE_INTERVAL", "0")))
    parser.add_argument("--remote-codex-home", default=os.environ.get("DAYTONA_REMOTE_CODEX_HOME", "/root/.codex"))
    parser.add_argument("--local-codex-home", default=os.environ.get("CODEX_HOME") or str(Path.home() / ".codex"))
    parser.add_argument("--local-exa-root", default=os.environ.get("CV_RANK_ROOT", str(Path.home() / "cv-rank")))
    parser.add_argument("--remote-exa-root", default=os.environ.get("DAYTONA_REMOTE_EXA_ROOT", "/root/tools/cv-rank"))
    parser.add_argument("--output-file", required=True)
    parser.add_argument("--model")
    parser.add_argument("--workdir", default=os.environ.get("DAYTONA_REMOTE_WORKDIR", "/root"))
    parser.add_argument("prompt")
    return parser.parse_args()


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


def load_daytona_env_file() -> dict[str, str]:
    merged: dict[str, str] = {}
    candidates = [
        Path.cwd() / ".env.daytona",
        ROOT / ".env.daytona",
    ]
    seen: set[Path] = set()
    for path in candidates:
        resolved = path.resolve()
        if resolved in seen:
            continue
        seen.add(resolved)
        merged.update(_read_simple_env_file(path))
    return merged


def resolve_daytona_settings(
    api_key: str | None,
    api_url: str | None,
) -> tuple[str, str]:
    env_file = load_daytona_env_file()
    resolved_api_key = str(
        api_key
        or os.environ.get("DAYTONA_API_KEY")
        or env_file.get("DAYTONA_API_KEY")
        or ""
    ).strip().strip("'").strip('"')
    resolved_api_url = str(
        api_url
        or os.environ.get("DAYTONA_API_URL")
        or env_file.get("DAYTONA_API_URL")
        or "https://app.daytona.io/api"
    ).strip().strip("'").strip('"')
    return resolved_api_key, resolved_api_url


def load_daytona():
    try:
        from daytona import Daytona, DaytonaConfig, CreateSandboxFromImageParams, Image, Resources
    except Exception as exc:
        helper_python = ROOT / ".venv-daytona" / "bin" / "python"
        helper_prefix = helper_python.parent.parent
        if (
            os.environ.get("_DAYTONA_REMOTE_REEXEC") != "1"
            and helper_python.exists()
            and (
                Path(sys.executable) != helper_python
                or Path(sys.prefix).resolve() != helper_prefix.resolve()
            )
        ):
            env = os.environ.copy()
            env["_DAYTONA_REMOTE_REEXEC"] = "1"
            os.execve(str(helper_python), [str(helper_python), *sys.argv], env)
        raise SystemExit(
            "daytona package is not installed in the active Python. "
            "Set DAYTONA_PYTHON_BIN to a Python that has `daytona` installed."
        ) from exc
    return Daytona, DaytonaConfig, CreateSandboxFromImageParams, Image, Resources


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


def load_local_exa_keys() -> list[str]:
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


def load_local_exa_key(seed: str | None = None) -> str | None:
    keys = load_local_exa_keys()
    if not keys:
        return None
    if len(keys) == 1:
        return keys[0]
    if seed:
        digest = hashlib.sha256(seed.encode("utf-8")).digest()
        index = int.from_bytes(digest[:8], "big") % len(keys)
        return keys[index]
    return keys[0]


def ensure_sandbox(args: argparse.Namespace):
    Daytona, DaytonaConfig, CreateSandboxFromImageParams, Image, Resources = load_daytona()
    args.api_key, args.api_url = resolve_daytona_settings(args.api_key, args.api_url)
    if not args.api_key:
        raise SystemExit("Missing Daytona API key. Set DAYTONA_API_KEY.")
    client = Daytona(
        DaytonaConfig(
            api_key=args.api_key,
            api_url=args.api_url,
        )
    )
    sandbox = None
    if args.sandbox_id:
        try:
            sandbox = client.get(args.sandbox_id)
        except Exception:
            sandbox = None
    if sandbox is None and args.sandbox_name:
        try:
            sandboxes = client.list()
            for item in getattr(sandboxes, "items", []) or []:
                if getattr(item, "name", None) == args.sandbox_name:
                    sandbox = client.get(getattr(item, "id"))
                    break
        except Exception:
            sandbox = None
    if sandbox is None:
        resources = Resources(cpu=args.cpu, memory=args.memory, disk=args.disk)
        params = CreateSandboxFromImageParams(
            name=args.sandbox_name,
            image=Image.debian_slim("3.13"),
            resources=resources,
            auto_stop_interval=args.auto_stop_interval,
            auto_archive_interval=args.auto_archive_interval,
            auto_delete_interval=args.auto_delete_interval,
        )
        try:
            sandbox = client.create(params, timeout=0)
        except Exception as exc:
            # Two workers targeting the same sandbox name can race here.
            # If creation lost the race, the sandbox should become visible shortly.
            if "already exists" not in str(exc):
                raise
            sandbox = None
            for _ in range(10):
                time.sleep(1)
                try:
                    sandboxes = client.list()
                    items = getattr(sandboxes, "items", []) or []
                    for item in items:
                        if getattr(item, "name", None) == args.sandbox_name:
                            sandbox = client.get(getattr(item, "id"))
                            break
                    if sandbox is not None:
                        break
                except Exception:
                    continue
            if sandbox is None:
                raise
    return sandbox


def minimal_remote_config(model: str | None) -> bytes:
    chosen_model = model or "gpt-5.4-mini"
    return (
        f'model = "{chosen_model}"\n'
        'model_reasoning_effort = "medium"\n'
        'personality = "pragmatic"\n'
        'approval_policy = "never"\n'
        'sandbox_mode = "danger-full-access"\n'
        '\n'
        '[features]\n'
        'unified_exec = true\n'
        'experimental_use_rmcp_client = true\n'
        '\n'
        '[mcp_servers.exa]\n'
        'url = "https://mcp.exa.ai/mcp?tools=web_search_advanced_exa"\n'
        'bearer_token_env_var = "EXA_API_KEY"\n'
    ).encode("utf-8")


def build_exa_bundle(local_root: Path) -> bytes | None:
    if not local_root.exists():
        return None
    candidate_paths = [
        local_root / "scripts" / "exa",
        local_root / "scripts" / "exa_cli.py",
        local_root / "src" / "cv_rank" / "__init__.py",
        local_root / "src" / "cv_rank" / "research" / "__init__.py",
        local_root / "src" / "cv_rank" / "research" / "exa_person_research.py",
    ]
    existing = [p for p in candidate_paths if p.exists()]
    if len(existing) < 3:
        return None
    with tempfile.NamedTemporaryFile(suffix=".tar.gz") as tmp:
        with tarfile.open(tmp.name, "w:gz") as tar:
            for path in existing:
                tar.add(path, arcname=str(path.relative_to(local_root)))
        return Path(tmp.name).read_bytes()


def ensure_remote_exa_tooling(sandbox, args: argparse.Namespace) -> None:
    local_exa_root = getattr(args, "local_exa_root", None) or getattr(args, "local_repo_root", None)
    if not local_exa_root:
        return
    bundle = build_exa_bundle(Path(local_exa_root).expanduser())
    if not bundle:
        return
    remote_root = getattr(args, "remote_exa_root", None) or getattr(args, "remote_repo_root", None) or "/root/tools/cv-rank"
    remote_root = remote_root.rstrip("/")
    remote_archive = f"/tmp/cv-rank-exa-bundle-{os.getpid()}.tar.gz"
    marker = f"{remote_root}/.cv_rank_exa_ready"
    lock_dir = f"{remote_root}/.cv_rank_exa_install.lock"
    sandbox.process.exec(
        f"mkdir -p {shlex.quote(remote_root)} && mkdir -p {shlex.quote(remote_root + '/scripts')} && mkdir -p {shlex.quote(remote_root + '/src')}",
        timeout=60,
    )
    sandbox.fs.upload_file(bundle, remote_archive)
    install = sandbox.process.exec(
        (
            f"lock={shlex.quote(lock_dir)}; "
            "while ! mkdir \"$lock\" 2>/dev/null; do sleep 1; done; "
            "trap 'rmdir \"$lock\"' EXIT; "
            f"cd {shlex.quote(remote_root)} && "
            f"tar -xzf {shlex.quote(remote_archive)} --strip-components=0 && "
            f"chmod +x scripts/exa scripts/exa_cli.py && "
            f"if [ ! -f {shlex.quote(marker)} ]; then "
            "if python3 -c \"import importlib.util,sys; sys.exit(0 if importlib.util.find_spec('exa_py') else 1)\"; "
            "then true; "
            "else python3 -m ensurepip --upgrade >/dev/null 2>&1 || true; python3 -m pip install --quiet exa-py; "
            "fi && "
            f"touch {shlex.quote(marker)}; "
            "fi"
        ),
        timeout=600,
    )
    if install.exit_code != 0:
        raise SystemExit(f"Failed to prepare Exa tooling in Daytona sandbox:\n{install.result}")


def ensure_remote_codex(sandbox, args: argparse.Namespace) -> None:
    sandbox.process.exec("mkdir -p /root/.codex", timeout=60)
    auth_path = Path(args.local_codex_home) / "auth.json"
    if not auth_path.exists():
        raise SystemExit(f"Missing local Codex auth file: {auth_path}")
    sandbox.fs.upload_file(auth_path.read_bytes(), f"{args.remote_codex_home}/auth.json")
    sandbox.fs.upload_file(minimal_remote_config(args.model), f"{args.remote_codex_home}/config.toml")

    marker = "/root/.codex/.cv_rank_codex_ready"
    lock_dir = "/tmp/cv-rank-codex-bootstrap.lock"
    version_check = sandbox.process.exec(f"test -f {shlex.quote(marker)} && node --version && npm --version && codex --version", timeout=60)
    if version_check.exit_code == 0:
        ensure_remote_exa_tooling(sandbox, args)
        return

    install_cmd = (
        f"lock={shlex.quote(lock_dir)}; "
        "while ! mkdir \"$lock\" 2>/dev/null; do sleep 1; done; "
        "trap 'rmdir \"$lock\"' EXIT; "
        f"if [ ! -f {shlex.quote(marker)} ]; then "
        "apt-get update && "
        "DEBIAN_FRONTEND=noninteractive apt-get install -y nodejs npm && "
        "npm install -g @openai/codex && "
        f"touch {shlex.quote(marker)}; "
        "fi"
    )
    result = sandbox.process.exec(install_cmd, timeout=1800)
    if result.exit_code != 0:
        raise SystemExit(f"Failed to install Codex in Daytona sandbox:\n{result.result}")
    ensure_remote_exa_tooling(sandbox, args)


def run_remote_codex(sandbox, args: argparse.Namespace) -> tuple[int, str, str]:
    remote_output = "/tmp/codex-last-message.txt"
    model_arg = f"--model {shlex.quote(args.model)} " if args.model else ""
    exa_key = load_local_exa_key(getattr(args, "run_id", None) or getattr(args, "sandbox_name", None))
    exa_prefix = f"export EXA_API_KEY={shlex.quote(exa_key)} && " if exa_key else ""
    cmd = (
        f"{exa_prefix}"
        f"export CODEX_HOME={shlex.quote(args.remote_codex_home)} && "
        f"cd {shlex.quote(args.workdir)} && "
        f"codex exec --skip-git-repo-check --dangerously-bypass-approvals-and-sandbox "
        f"{model_arg}-o {shlex.quote(remote_output)} {shlex.quote(args.prompt)}"
    )
    result = sandbox.process.exec(cmd, timeout=1800)
    output_bytes = sandbox.fs.download_file(remote_output)
    output_text = output_bytes.decode("utf-8", errors="replace") if output_bytes else ""
    return result.exit_code or 0, result.result or "", output_text


def main() -> int:
    args = parse_args()
    sandbox = ensure_sandbox(args)
    ensure_remote_codex(sandbox, args)
    exit_code, stdout_text, final_output = run_remote_codex(sandbox, args)

    output_path = Path(args.output_file)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(final_output, encoding="utf-8")
    sys.stdout.write(stdout_text)
    if stdout_text and not stdout_text.endswith("\n"):
        sys.stdout.write("\n")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
