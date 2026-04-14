#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import shlex
import sys
import tarfile
import tempfile
import time
from pathlib import Path
from typing import Any

from daytona_claude_mini_remote import (
    REMOTE_HOME,
    REMOTE_USER,
    collect_minimax_env,
    ensure_remote_runtime,
    ensure_sandbox,
    exec_with_retry,
    normalize_model,
    resolve_tool_filters,
)


ROOT = Path(__file__).resolve().parent
DEFAULT_OUTPUT_ROOT = ROOT / "data" / "web_audit_runs"
DEFAULT_PHASE1_FILE = ROOT / "prompts" / "minimax_web_audit_phase1_placeholder.md"
DEFAULT_GUIDE_FILE = ROOT / "prompts" / "minimax_web_audit_guide_placeholder.md"
DEFAULT_REMOTE_ROOT = f"{REMOTE_HOME}/tools/web-audit"
RUNTIME_READY_MARKER = ".web-audit-runtime-ready-v4"
SYSTEM_PATH_EXPORT = "export PATH=/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin:$PATH; "
AUDIT_WORDLIST_PATH = f"{DEFAULT_REMOTE_ROOT}/wordlists/common.txt"
REMOTE_NODE_BIN = f"{DEFAULT_REMOTE_ROOT}/node-runtime/bin/node"
PHASE2_TEXT = (
    "Go deeper. Find more things. This is not serious enough for reporting yet. "
    "Follow the guide, explore everything you reasonably can, and cite every meaningful claim with evidence."
)
PHASE3_TEXT = (
    "Go deeper. Find more things. This is not serious enough for reporting yet. "
    "Follow the guide, explore everything you reasonably can, and cite every meaningful claim with evidence. "
    "Then report your top 3 most critical vulnerabilities you found."
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Run a 3-step MiniMax web audit inside a Daytona sandbox with reverse proxy and remote-debugging Chrome."
    )
    parser.add_argument("target_url", help="Target website URL to audit.")
    parser.add_argument("--api-key", default=os.environ.get("DAYTONA_API_KEY"))
    parser.add_argument("--api-url", default=os.environ.get("DAYTONA_API_URL", "https://app.daytona.io/api"))
    parser.add_argument("--sandbox-id", default=os.environ.get("DAYTONA_SANDBOX_ID"))
    parser.add_argument("--sandbox-name", default=os.environ.get("DAYTONA_SANDBOX_NAME"))
    parser.add_argument("--sandbox-prefix", default=os.environ.get("DAYTONA_SANDBOX_PREFIX", "web-audit"))
    parser.add_argument("--sandbox-policy", choices=("fresh", "reuse", "pool"), default="fresh")
    parser.add_argument("--cpu", type=int, default=int(os.environ.get("DAYTONA_CPU", "4")))
    parser.add_argument("--memory", type=int, default=int(os.environ.get("DAYTONA_MEMORY", "8")))
    parser.add_argument("--disk", type=int, default=int(os.environ.get("DAYTONA_DISK", "10")))
    parser.add_argument("--auto-stop-interval", type=int, default=int(os.environ.get("DAYTONA_AUTO_STOP_INTERVAL", "5")))
    parser.add_argument("--auto-archive-interval", type=int, default=int(os.environ.get("DAYTONA_AUTO_ARCHIVE_INTERVAL", "5")))
    parser.add_argument("--auto-delete-interval", type=int, default=int(os.environ.get("DAYTONA_AUTO_DELETE_INTERVAL", "0")))
    parser.add_argument("--remote-root", default=os.environ.get("DAYTONA_REMOTE_WEB_AUDIT_ROOT", DEFAULT_REMOTE_ROOT))
    parser.add_argument("--local-minimax-env", default=os.environ.get("MINIMAX_ENV_FILE") or str(Path.home() / ".claude-wafer" / "minimax.env"))
    parser.add_argument("--model", default=os.environ.get("MINIMAX_MODEL", "MiniMax-M2.7"))
    parser.add_argument("--tools", default="default")
    parser.add_argument("--web-mode", choices=("minimax", "exa", "mixed", "none"), default="minimax")
    parser.add_argument("--allowed-tools", default="Bash,Read,Write,Edit")
    parser.add_argument("--disallowed-tools", default="")
    parser.add_argument("--effort", choices=("low", "medium", "high", "max"), default="low")
    parser.add_argument("--timeout-seconds", type=int, default=1800)
    parser.add_argument("--local-phase1-file", default=str(DEFAULT_PHASE1_FILE))
    parser.add_argument("--local-guide-file", default=str(DEFAULT_GUIDE_FILE))
    parser.add_argument("--local-output-root", default=str(DEFAULT_OUTPUT_ROOT))
    parser.add_argument("--alias", default="")
    parser.add_argument("--session-id", default="")
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--chrome-debug-port", type=int, default=0)
    parser.add_argument("--proxy-port", type=int, default=0)
    parser.add_argument("--dry-run", action="store_true")
    return parser.parse_args()


def emit_status(message: str) -> None:
    print(f"[web-audit] {message}", flush=True)


def emit_stream(label: str, text: str) -> None:
    if not text:
        return
    sys.stdout.write(f"\n[web-audit:{label}]\n{text}")
    if not text.endswith("\n"):
        sys.stdout.write("\n")
    sys.stdout.flush()


def read_text_or_placeholder(path_str: str, *, fallback_path: Path) -> tuple[str, str]:
    path = Path(path_str).expanduser().resolve() if str(path_str).strip() else fallback_path.resolve()
    if not path.exists():
        path = fallback_path.resolve()
    return path.read_text(encoding="utf-8"), str(path)


def first_existing_path(candidates: list[Path]) -> Path | None:
    for path in candidates:
        expanded = path.expanduser().resolve()
        if expanded.exists():
            return expanded
    return None


def derive_run_port(base: int, span: int, run_id: str) -> int:
    digest = hashlib.sha1(run_id.encode("utf-8")).hexdigest()
    return int(base) + (int(digest[:8], 16) % int(span))


def ensure_extra_runtime(sandbox, *, remote_root: str) -> None:
    remote_scripts = f"{remote_root}/scripts"
    remote_gmail_tool = f"{remote_root}/tools/gmail-agent-tool"
    remote_workspace_mcp = f"{REMOTE_HOME}/.config/workspace-mcp"
    runtime_marker = f"{remote_root}/{RUNTIME_READY_MARKER}"
    runtime_lock = f"{remote_root}/.runtime-install.lock"
    exec_with_retry(
        sandbox,
        (
            f"mkdir -p {shlex.quote(remote_scripts)} {shlex.quote(remote_gmail_tool)} {shlex.quote(remote_workspace_mcp)} && "
            f"chown -R {shlex.quote(REMOTE_USER)}:{shlex.quote(REMOTE_USER)} "
            f"{shlex.quote(remote_scripts)} {shlex.quote(remote_gmail_tool)} {shlex.quote(remote_workspace_mcp)}"
        ),
        timeout=60,
    )
    uploads = {
        ROOT / "scripts" / "reverse_proxy.py": f"{remote_scripts}/reverse_proxy.py",
        ROOT / "scripts" / "install_web_audit_tools.py": f"{remote_scripts}/install_web_audit_tools.py",
        ROOT / "scripts" / "chrome_devtools_mcp_launch.sh": f"{remote_scripts}/chrome_devtools_mcp_launch.sh",
        ROOT / "tools" / "gmail-agent-tool" / "gmail_agent_tool.py": f"{remote_gmail_tool}/gmail_agent_tool.py",
        ROOT / "tools" / "gmail-agent-tool" / "gmail-agent": f"{remote_gmail_tool}/gmail-agent",
        ROOT / "gmail": f"{remote_root}/gmail",
    }
    for local_path, remote_path in uploads.items():
        sandbox.fs.upload_file(local_path.read_bytes(), remote_path)
    gmail_credentials = first_existing_path(
        [
            Path.home() / ".config/workspace-mcp/google-oauth.env",
            Path.home() / "Library/Application Support/gogcli/credentials.json",
            ROOT / "secure" / "google-oauth.env",
            ROOT / "secure" / "gogcli-credentials.json",
            ROOT / ".research" / "events-google-export" / "auth" / "credentials.json",
            ROOT / ".research" / "events-google-export" / "auth" / "client_secret.json",
        ]
    )
    gmail_token = first_existing_path(
        [
            Path.home() / ".config/workspace-mcp/gmail-agent-token.json",
            Path.home() / ".config/workspace-mcp/token-cv.json",
            Path.home() / ".config/workspace-mcp/token.json",
            ROOT / ".research" / "events-google-export" / "auth" / "gmail-token.json",
            ROOT / ".research" / "events-google-export" / "auth" / "token-cv.json",
            ROOT / ".research" / "events-google-export" / "auth" / "token.json",
        ]
    )
    if gmail_credentials is not None:
        remote_credentials_name = "google-oauth.env" if gmail_credentials.suffix.lower() == ".env" else "credentials.json"
        sandbox.fs.upload_file(gmail_credentials.read_bytes(), f"{remote_workspace_mcp}/{remote_credentials_name}")
    if gmail_token is not None:
        sandbox.fs.upload_file(gmail_token.read_bytes(), f"{remote_workspace_mcp}/gmail-agent-token.json")
    setup_cmd = (
        "set -e; "
        f"if [ -f {shlex.quote(runtime_marker)} ] "
        "&& test -x /usr/bin/curl "
        "&& test -x /usr/bin/rg "
        "&& (test -x /usr/bin/chromium || test -x /usr/bin/chromium-browser) "
        "&& test -x /usr/bin/ffuf "
        "&& test -x /usr/bin/unzip "
        "&& test -x /home/daytona/.local/bin/subfinder "
        "&& test -x /home/daytona/.local/bin/httpx "
        "&& test -x /home/daytona/.local/bin/nuclei "
        f"&& test -x {shlex.quote(f'{remote_root}/node-runtime/bin/node')} "
        f"&& test -x {shlex.quote(f'{remote_root}/chrome-devtools-mcp/node_modules/.bin/chrome-devtools-mcp')} "
        f"&& test -f {shlex.quote(AUDIT_WORDLIST_PATH)}; then "
        "exit 0; "
        "fi; "
        f"while ! mkdir {shlex.quote(runtime_lock)} >/dev/null 2>&1; do "
        f"if [ -f {shlex.quote(runtime_marker)} ] "
        "&& test -x /usr/bin/curl "
        "&& test -x /usr/bin/rg "
        "&& (test -x /usr/bin/chromium || test -x /usr/bin/chromium-browser) "
        "&& test -x /usr/bin/ffuf "
        "&& test -x /usr/bin/unzip "
        "&& test -x /home/daytona/.local/bin/subfinder "
        "&& test -x /home/daytona/.local/bin/httpx "
        "&& test -x /home/daytona/.local/bin/nuclei "
        f"&& test -x {shlex.quote(f'{remote_root}/node-runtime/bin/node')} "
        f"&& test -x {shlex.quote(f'{remote_root}/chrome-devtools-mcp/node_modules/.bin/chrome-devtools-mcp')} "
        f"&& test -f {shlex.quote(AUDIT_WORDLIST_PATH)}; then "
        "exit 0; "
        "fi; "
        "sleep 2; "
        "done; "
        f"trap 'rmdir {shlex.quote(runtime_lock)} >/dev/null 2>&1 || true' EXIT; "
        f"if [ -f {shlex.quote(runtime_marker)} ] "
        "&& test -x /usr/bin/curl "
        "&& test -x /usr/bin/rg "
        "&& (test -x /usr/bin/chromium || test -x /usr/bin/chromium-browser) "
        "&& test -x /usr/bin/ffuf "
        "&& test -x /usr/bin/unzip "
        "&& test -x /home/daytona/.local/bin/subfinder "
        "&& test -x /home/daytona/.local/bin/httpx "
        "&& test -x /home/daytona/.local/bin/nuclei "
        f"&& test -x {shlex.quote(f'{remote_root}/node-runtime/bin/node')} "
        f"&& test -x {shlex.quote(f'{remote_root}/chrome-devtools-mcp/node_modules/.bin/chrome-devtools-mcp')} "
        f"&& test -f {shlex.quote(AUDIT_WORDLIST_PATH)}; then "
        "exit 0; "
        "fi; "
        "dpkg --configure -a >/dev/null 2>&1 || true; "
        "DEBIAN_FRONTEND=noninteractive apt-get install -f -y >/dev/null 2>&1 || true; "
        "if ! command -v curl >/dev/null 2>&1; then "
        "apt-get update >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends curl >/dev/null; "
        "fi; "
        "if ! command -v rg >/dev/null 2>&1; then "
        "apt-get update >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ripgrep >/dev/null; "
        "fi; "
        "if ! command -v chromium >/dev/null 2>&1 && ! command -v chromium-browser >/dev/null 2>&1 "
        "&& [ ! -x /usr/bin/chromium ] && [ ! -x /usr/bin/chromium-browser ]; then "
        "apt-get update >/dev/null && "
        "(DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends chromium >/dev/null "
        "|| DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends chromium-browser >/dev/null); "
        "fi; "
        "(command -v chromium >/dev/null 2>&1 || command -v chromium-browser >/dev/null 2>&1 || test -x /usr/bin/chromium || test -x /usr/bin/chromium-browser); "
        "if ! command -v ffuf >/dev/null 2>&1 || ! command -v unzip >/dev/null 2>&1; then "
        "apt-get update >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends ffuf unzip >/dev/null; "
        "fi; "
        "if ! command -v xz >/dev/null 2>&1; then "
        "apt-get update >/dev/null && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends xz-utils >/dev/null; "
        "fi; "
        "if ! command -v amass >/dev/null 2>&1; then "
        "apt-get update >/dev/null && (DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends amass >/dev/null || true); "
        "fi; "
        f"mkdir -p {shlex.quote(f'{remote_root}/node-runtime')} && "
        f"if ! {shlex.quote(f'{remote_root}/node-runtime/bin/node')} --version >/dev/null 2>&1; then "
        "NODE_TARBALL=$(curl -fsSL https://nodejs.org/dist/latest-v22.x/SHASUMS256.txt | awk '/linux-x64\\.tar\\.xz$/ {print $2; exit}'); "
        "test -n \"$NODE_TARBALL\"; "
        f"rm -rf {shlex.quote(f'{remote_root}/node-runtime')}/*; "
        f"curl -fsSL \"https://nodejs.org/dist/latest-v22.x/$NODE_TARBALL\" | tar -xJf - -C {shlex.quote(f'{remote_root}/node-runtime')} --strip-components=1; "
        "fi; "
        f"chown -R {shlex.quote(REMOTE_USER)}:{shlex.quote(REMOTE_USER)} {shlex.quote(f'{remote_root}/node-runtime')} && "
        "su -s /bin/bash "
        + shlex.quote(REMOTE_USER)
        + " -c "
        + shlex.quote(
            "cd \"$HOME\"; "
            "export PATH="
            + shlex.quote(f"{remote_root}/node-runtime/bin")
            + ":\"$HOME/.local/bin:$PATH\"; "
            "python3 "
            + shlex.quote(f"{remote_scripts}/install_web_audit_tools.py")
            + " >/dev/null; "
            + "mkdir -p "
            + shlex.quote(f"{remote_root}/chrome-devtools-mcp")
            + "; "
            + "npm install --silent --no-package-lock --prefix "
            + shlex.quote(f"{remote_root}/chrome-devtools-mcp")
            + " chrome-devtools-mcp@latest >/dev/null; "
            + "mkdir -p "
            + shlex.quote(f"{remote_root}/wordlists")
            + "; "
            + "cat > "
            + shlex.quote(AUDIT_WORDLIST_PATH)
            + " <<'EOF'\n"
            + "admin\napi\napp\nauth\nlogin\nlogout\nsignup\nregister\nsignin\nuser\nusers\naccount\naccounts\nprofile\nsettings\nconfig\ndashboard\ninternal\nprivate\nswagger\nopenapi\ndocs\napi-docs\nv1\nv2\ngraphql\nhealth\nstatus\nmetrics\nbilling\ncheckout\npayments\nsubscription\nsubscriptions\nstripe\nwebhook\nwebhooks\nuploads\nfiles\nassets\nstatic\njs\nbundle\nchunks\nrobots.txt\nsecurity.txt\n.env\n.git/config\nEOF"
        )
        + " && touch "
        + shlex.quote(runtime_marker)
    )
    result = exec_with_retry(sandbox, setup_cmd, timeout=1800)
    if result.exit_code != 0:
        raise SystemExit(f"Failed to install browser audit runtime:\n{result.result}")
    exec_with_retry(
        sandbox,
        (
            f"chmod +x {shlex.quote(remote_scripts + '/reverse_proxy.py')} "
            f"{shlex.quote(remote_scripts + '/install_web_audit_tools.py')} "
            f"{shlex.quote(remote_scripts + '/chrome_devtools_mcp_launch.sh')} "
            f"{shlex.quote(remote_gmail_tool + '/gmail-agent')} "
            f"{shlex.quote(remote_root + '/gmail')} && "
            f"mkdir -p {shlex.quote(REMOTE_HOME + '/.config')} {shlex.quote(REMOTE_HOME + '/.cache')} {shlex.quote(REMOTE_HOME + '/.npm')} && "
            f"chown -R {shlex.quote(REMOTE_USER)}:{shlex.quote(REMOTE_USER)} "
            f"{shlex.quote(remote_scripts)} {shlex.quote(remote_gmail_tool)} {shlex.quote(remote_workspace_mcp)} {shlex.quote(remote_root + '/gmail')} "
            f"{shlex.quote(REMOTE_HOME + '/.config')} {shlex.quote(REMOTE_HOME + '/.cache')} {shlex.quote(REMOTE_HOME + '/.npm')}"
        ),
        timeout=60,
        attempts=1,
    )


def ensure_chrome_devtools_mcp(
    sandbox,
    *,
    remote_root: str,
    config_dir: str,
    chrome_debug_url: str,
) -> None:
    lock_dir = "/tmp/claude-mini-mcp-config.lock"
    launcher_path = f"{remote_root}/scripts/chrome_devtools_mcp_launch.sh"
    add_cmd = (
        f"export HOME={shlex.quote(REMOTE_HOME)}; "
        "export SHELL=/bin/bash; "
        "export PATH=\"$HOME/.local/bin:/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin:$PATH\"; "
        "export XDG_CONFIG_HOME=\"$HOME/.config\"; "
        "export XDG_CACHE_HOME=\"$HOME/.cache\"; "
        "export npm_config_cache=\"$HOME/.npm\"; "
        "mkdir -p \"$XDG_CONFIG_HOME\" \"$XDG_CACHE_HOME\" \"$npm_config_cache\"; "
        f"cd {shlex.quote(remote_root)} && "
        "set -a && . .minimax.remote && set +a && "
        f"lock={shlex.quote(lock_dir)}; "
        "while ! mkdir \"$lock\" 2>/dev/null; do sleep 0.25; done; "
        "trap 'rmdir \"$lock\"' EXIT; "
        f"CLAUDE_CONFIG_DIR={shlex.quote(config_dir)} claude mcp remove -s user chrome-devtools >/dev/null 2>&1 || true; "
        f"CLAUDE_CONFIG_DIR={shlex.quote(config_dir)} claude mcp add -s user chrome-devtools -- "
        f"{shlex.quote(launcher_path)} --browserUrl={shlex.quote(chrome_debug_url)} >/dev/null 2>&1"
    )
    result = exec_with_retry(
        sandbox,
        f"su -s /bin/bash {shlex.quote(REMOTE_USER)} -c {shlex.quote(add_cmd)}",
        timeout=600,
    )
    if result.exit_code != 0:
        raise SystemExit(
            "Failed to register chrome-devtools MCP.\n"
            f"exit_code={result.exit_code}\n"
            f"output:\n{result.result}"
        )
    verify = exec_with_retry(
        sandbox,
        (
            f"su -s /bin/bash {shlex.quote(REMOTE_USER)} -c "
            f"{shlex.quote(f'CLAUDE_CONFIG_DIR={shlex.quote(config_dir)} claude mcp list')}"
        ),
        timeout=120,
    )
    verify_output = str(verify.result or "")
    verify_lower = verify_output.lower()
    chrome_lines = [line.strip().lower() for line in verify_output.splitlines() if "chrome-devtools" in line.lower()]
    chrome_ok = any("connected" in line and "failed" not in line for line in chrome_lines)
    if (
        verify.exit_code != 0
        or "chrome-devtools" not in verify_lower
        or not chrome_ok
    ):
        raise SystemExit(f"chrome-devtools MCP missing after registration.\n{verify_output}")


def start_reverse_proxy(sandbox, *, remote_root: str, run_dir: str, target_url: str, port: int) -> str:
    proxy_port = int(port) if int(port) > 0 else 18080
    proxy_url = f"http://127.0.0.1:{proxy_port}"
    target_match = exec_with_retry(
        sandbox,
        f"curl -sS -o /dev/null {shlex.quote(proxy_url)}",
        timeout=15,
        attempts=1,
    )
    if target_match.exit_code == 0:
        sandbox.fs.upload_file(b"reused existing reverse proxy\n", f"{run_dir}/proxy.stdout.txt")
        sandbox.fs.upload_file(b"", f"{run_dir}/proxy.stderr.txt")
        return proxy_url
    exec_with_retry(
        sandbox,
        f"pkill -f {shlex.quote(f'reverse_proxy.py {target_url} --host 127.0.0.1 --port {proxy_port}')} >/dev/null 2>&1 || true",
        timeout=30,
        attempts=1,
    )
    cmd = (
        f"cd {shlex.quote(remote_root)} && "
        f"nohup python3 scripts/reverse_proxy.py {shlex.quote(target_url)} --host 127.0.0.1 --port {proxy_port} "
        f">{shlex.quote(run_dir + '/proxy.stdout.txt')} 2>{shlex.quote(run_dir + '/proxy.stderr.txt')} < /dev/null & "
        f"echo $! > {shlex.quote(run_dir + '/proxy.pid')}"
    )
    result = exec_with_retry(
        sandbox,
        f"su -s /bin/bash {shlex.quote(REMOTE_USER)} -c {shlex.quote(cmd)}",
        timeout=60,
    )
    if result.exit_code != 0:
        raise SystemExit(f"Failed to start reverse proxy:\n{result.result}")
    for _ in range(60):
        probe = exec_with_retry(
            sandbox,
            SYSTEM_PATH_EXPORT + f"curl -sS -o /dev/null {shlex.quote(proxy_url)}",
            timeout=15,
            attempts=1,
        )
        if probe.exit_code == 0:
            return proxy_url
        time.sleep(1)
    stdout_probe = exec_with_retry(
        sandbox,
        f"cat {shlex.quote(run_dir + '/proxy.stdout.txt')} 2>/dev/null || true",
        timeout=30,
        attempts=1,
    )
    stderr_probe = exec_with_retry(
        sandbox,
        f"cat {shlex.quote(run_dir + '/proxy.stderr.txt')} 2>/dev/null || true",
        timeout=30,
        attempts=1,
    )
    raise SystemExit(
        "Reverse proxy did not come up in time.\n"
        f"stdout:\n{stdout_probe.result or ''}\n"
        f"stderr:\n{stderr_probe.result or ''}"
    )


def start_chromium(sandbox, *, run_dir: str, port: int, startup_url: str) -> str:
    debug_url = f"http://127.0.0.1:{int(port)}"
    exec_with_retry(
        sandbox,
        SYSTEM_PATH_EXPORT + f"pkill -f {shlex.quote(f'remote-debugging-port={int(port)}')} >/dev/null 2>&1 || true",
        timeout=30,
        attempts=1,
    )
    chrome_cmd = (
        "python3 - <<'PY'\n"
        "import os\n"
        "import subprocess\n"
        "from pathlib import Path\n"
        f"home = Path({REMOTE_HOME!r})\n"
        f"run_dir = Path({run_dir!r})\n"
        f"port = {int(port)}\n"
        f"startup_url = {startup_url!r}\n"
        "candidates = [\n"
        "    '/usr/bin/chromium',\n"
        "    '/usr/bin/chromium-browser',\n"
        "    '/usr/bin/google-chrome',\n"
        "    '/bin/chromium',\n"
        "    '/bin/chromium-browser',\n"
        "]\n"
        "path_env = '/usr/local/bin:/usr/local/sbin:/usr/bin:/usr/sbin:/bin:/sbin:' + os.environ.get('PATH', '')\n"
        "for token in path_env.split(':'):\n"
        "    token = token.strip()\n"
        "    if not token:\n"
        "        continue\n"
        "    for name in ('chromium', 'chromium-browser', 'google-chrome'):\n"
        "        candidates.append(str(Path(token) / name))\n"
        "chrome_bin = next((candidate for candidate in candidates if Path(candidate).exists() and os.access(candidate, os.X_OK)), '')\n"
        "if not chrome_bin:\n"
        "    raise SystemExit('missing chromium; checked: ' + ', '.join(dict.fromkeys(candidates)))\n"
        "profile_dir = run_dir / 'chrome-profile'\n"
        "artifacts_dir = run_dir / 'artifacts'\n"
        "profile_dir.mkdir(parents=True, exist_ok=True)\n"
        "artifacts_dir.mkdir(parents=True, exist_ok=True)\n"
        "env = os.environ.copy()\n"
        f"env['HOME'] = {REMOTE_HOME!r}\n"
        "env['PATH'] = path_env\n"
        "env['XDG_CONFIG_HOME'] = str(home / '.config')\n"
        "env['XDG_CACHE_HOME'] = str(home / '.cache')\n"
        "Path(env['XDG_CONFIG_HOME']).mkdir(parents=True, exist_ok=True)\n"
        "Path(env['XDG_CACHE_HOME']).mkdir(parents=True, exist_ok=True)\n"
        "stdout_path = run_dir / 'chrome.stdout.txt'\n"
        "stderr_path = run_dir / 'chrome.stderr.txt'\n"
        "with stdout_path.open('ab', buffering=0) as stdout_f, stderr_path.open('ab', buffering=0) as stderr_f:\n"
        "    proc = subprocess.Popen(\n"
        "        [\n"
        "            chrome_bin,\n"
        "            '--headless=new',\n"
        "            '--disable-gpu',\n"
        "            '--no-sandbox',\n"
        "            '--disable-dev-shm-usage',\n"
        "            '--ignore-certificate-errors',\n"
        "            '--enable-features=NetworkService,NetworkServiceInProcess',\n"
        "            '--no-first-run',\n"
        "            '--no-default-browser-check',\n"
        "            '--remote-debugging-address=127.0.0.1',\n"
        "            f'--remote-debugging-port={port}',\n"
        "            f'--user-data-dir={profile_dir}',\n"
        "            'about:blank',\n"
        "        ],\n"
        "        cwd=str(home),\n"
        "        env=env,\n"
        "        stdin=subprocess.DEVNULL,\n"
        "        stdout=stdout_f,\n"
        "        stderr=stderr_f,\n"
        "        start_new_session=True,\n"
        "        close_fds=True,\n"
        "    )\n"
        "    (run_dir / 'chrome.pid').write_text(str(proc.pid), encoding='utf-8')\n"
        "PY"
    )
    result = exec_with_retry(
        sandbox,
        f"su -s /bin/bash {shlex.quote(REMOTE_USER)} -c {shlex.quote(chrome_cmd)}",
        timeout=60,
    )
    if result.exit_code != 0:
        raise SystemExit(f"Failed to launch Chromium:\n{result.result}")
    for _ in range(60):
        probe = exec_with_retry(
            sandbox,
            SYSTEM_PATH_EXPORT + f"curl -fsS {shlex.quote(debug_url + '/json/version')}",
            timeout=15,
            attempts=1,
        )
        if probe.exit_code == 0 and "Browser" in str(probe.result or ""):
            return debug_url
        time.sleep(1)
    raise SystemExit("Chromium remote debugging endpoint did not come up in time.")


def build_phase_prompt(
    *,
    phase_label: str,
    phase_text: str,
    guide_text: str,
    target_url: str,
    proxy_url: str,
    chrome_debug_url: str,
    remote_root: str,
    run_dir: str,
) -> str:
    return f"""You are performing a serious web security audit inside a Daytona sandbox.

Primary target origin URL:
{target_url}

Reverse-proxied localhost mirror inside this sandbox:
{proxy_url}

Remote-debugging Chrome endpoint:
{chrome_debug_url}

Environment and evidence rules:
- Prefer the real target origin URL for browsing, auth flow testing, API testing, subdomain discovery, and reporting.
- Use the proxied localhost URL only when you specifically need the local mirror, when the origin route is difficult to reproduce directly, or when the guide tells you to compare behavior.
- The `nickita@cerebralvalley.ai` email is only a tester inbox. Ignore `cerebralvalley.ai` as a scan target unless the provided target itself is actually under that scope.
- The audit scope is only the provided target origin and subdomains that belong to that same target domain. Do not pivot to another company, another domain, or the tester email domain unless the provided target is explicitly under that scope.
- Never infer a new target from the tester email, local file paths, placeholder text, or incidental mentions in the prompt. If the target looks misconfigured, report that misconfiguration instead of inventing a new audit target.
- You have Bash, a reverse proxy, Chromium with remote debugging enabled, and Chrome DevTools MCP.
- Use Chrome DevTools MCP as your primary browser control path. Do not rely on Playwright or any side browser helper in this audit flow.
- Start every phase in Chrome DevTools first: open the real target URL, take a snapshot, inspect the network requests, and use that browser evidence to drive the rest of the phase.
- Use Bash recon only to support or validate browser-led hypotheses. Do not spend the whole phase dumping shell enumeration before touching Chrome.
- Keep the conversation compact. Do not paste or read giant raw HTML, giant JS bundles, giant `list_network_requests` dumps, or entire persisted tool-result files into context.
- Never `Read` a whole large `.claude-config/.../tool-results/*.json` file just because it exists. Use targeted inspection only: `Read` with small limits, `rg`, `grep`, `jq`, `sed -n '1,120p'`, `head -c 4000`, or targeted request fetches.
- For Chrome network work, start with a small `pageSize` and then inspect only specific interesting `reqid`s with `get_network_request`.
- For HTML inspection, prefer DevTools snapshots over raw `curl` dumps. If you need `curl`, use headers or the first few KB only.
- For JS bundles, fetch one candidate bundle at a time and search targeted terms like `api`, `/api/`, `graphql`, `auth`, `token`, `admin`, `swagger`, `openapi`, `stripe`, `subscription`, `customer`, `user`, `org`, and `workspace` rather than loading the whole file into context.
- If a command or MCP call returns persisted output, summarize it from a bounded preview. Do not recursively read the full saved artifact unless it is small and directly relevant.
- Keep each individual tool result small enough to stay well under the context window whenever possible.
- For OTP retrieval, prefer the preloaded Gmail token flow: use `tools/gmail-agent-tool/gmail-agent latest`, `read`, or `clean-raw` first. Do not run `gmail auth` unless Gmail explicitly reports missing or expired auth.
- Keep shell outputs bounded. If a command is noisy, save the full output to an artifact file and inspect a filtered preview instead of flooding the context window.
- If `subfinder`, `amass`, `ffuf`, or similar tools produce huge output, summarize the high-signal subset and continue the audit instead of stalling on raw recon volume.
- Validate findings before you claim them.
- Save useful artifacts under {run_dir}/artifacts when it helps your investigation.
- Cite evidence with concrete URLs, response details, file paths, screenshots, network traces, or command output.
- Do not report speculative findings.
- Keep going until the result is serious enough for reporting.
- If headless browser auth is blocked by captcha, device checks, or manual interaction, stop and write exact operator instructions for a visible-browser handoff instead of pretending the flow succeeded.

Useful commands in this sandbox:
- `subfinder -d $(python3 - <<'PY'
from urllib.parse import urlparse
print(urlparse({target_url!r}).netloc)
PY
)`
- `echo {target_url} | httpx -silent -status-code -title`
- `nuclei -u {target_url} -severity critical,high,medium`
- `ffuf -u {target_url.rstrip('/')}/FUZZ -w {AUDIT_WORDLIST_PATH} -mc all`
- `amass enum -passive -d $(python3 - <<'PY'
from urllib.parse import urlparse
print(urlparse({target_url!r}).netloc)
PY
)`
- Chrome DevTools MCP should be used for page navigation, network inspection, JavaScript inspection, and API-route validation.
- Prefer MCP screenshots, network inspection, console inspection, request replay, and DOM snapshots over ad hoc browser wrappers.
- `curl -i {target_url}`
- `curl -fsS {chrome_debug_url}/json/version`
- `curl -fsS {chrome_debug_url}/json/list`

Guide file contents:
{guide_text}

{phase_label} instructions:
{phase_text}
"""


def resolve_audit_tool_filters(args: argparse.Namespace) -> tuple[str, str]:
    allowed_raw = str(getattr(args, "allowed_tools", "") or "").strip()
    if allowed_raw:
        return resolve_tool_filters(args)
    disallowed = str(getattr(args, "disallowed_tools", "") or "").strip()
    parts = [item.strip() for item in disallowed.split(",") if item.strip()]
    for token in ("WebSearch", "WebFetch"):
        if token not in parts:
            parts.append(token)
    return "", ",".join(parts)


def read_remote_text(
    sandbox,
    path: str,
    *,
    timeout: int = 20,
    attempts: int = 3,
) -> str:
    try:
        result = exec_with_retry(
            sandbox,
            f"cat {shlex.quote(path)} 2>/dev/null || true",
            timeout=timeout,
            attempts=attempts,
        )
    except Exception:
        return ""
    return str(result.result or "")


def remote_file_exists(
    sandbox,
    path: str,
    *,
    timeout: int = 15,
    attempts: int = 3,
) -> bool:
    try:
        result = exec_with_retry(
            sandbox,
            f"test -f {shlex.quote(path)} && echo yes || true",
            timeout=timeout,
            attempts=attempts,
        )
    except Exception:
        return False
    return str(result.result or "").strip() == "yes"


def extract_terminal_result_rc(stream_text: str) -> int | None:
    for line in reversed(stream_text.splitlines()):
        raw = line.strip()
        if not raw:
            continue
        try:
            payload = json.loads(raw)
        except json.JSONDecodeError:
            continue
        if payload.get("type") != "result":
            continue
        return 1 if payload.get("is_error") else 0
    return None


def phase_output_indicates_overload(*texts: str) -> bool:
    haystack = "\n".join(texts).lower()
    non_retryable_markers = (
        "prompt is too long",
        "context window exceeds limit",
        "invalid_request_error",
        "\"terminal_reason\":\"blocking_limit\"",
    )
    if any(marker in haystack for marker in non_retryable_markers):
        return False
    markers = (
        "api error: 529",
        "\"overloaded_error\"",
        "server cluster is currently under high load",
        "\"error_status\":529",
        "\"error_status\": 529",
    )
    return any(marker in haystack for marker in markers)


def run_phase(
    sandbox,
    *,
    remote_root: str,
    run_dir: str,
    config_dir: str,
    alias: str,
    phase_name: str,
    prompt_text: str,
    model: str,
    effort: str,
    tools: str,
    allowed_tools: str,
    disallowed_tools: str,
    timeout_seconds: int,
    start: bool,
) -> int:
    prompt_path = f"{run_dir}/{phase_name}.prompt.txt"
    stdout_path = f"{run_dir}/{phase_name}.stdout.txt"
    stderr_path = f"{run_dir}/{phase_name}.stderr.txt"
    combined_path = f"{run_dir}/{phase_name}.combined.txt"
    pid_path = f"{run_dir}/{phase_name}.pid"
    exit_path = f"{run_dir}/{phase_name}.exitcode"
    launch_lock_dir = f"{run_dir}/{phase_name}.launching"
    sandbox.fs.upload_file(prompt_text.encode("utf-8"), prompt_path)
    agent_timeout = int(timeout_seconds or 0)
    if agent_timeout > 0:
        timeout_prefix = f"timeout {agent_timeout} "
        exec_timeout = max(60, agent_timeout + 300)
    else:
        timeout_prefix = ""
        exec_timeout = 604800
    retry_delays = [20, 60, 180]

    for attempt_idx in range(len(retry_delays) + 1):
        cmd_parts = [
            "python3",
            "claude_wafer_agent.py",
            "start" if start else "send",
            "--alias",
            alias,
            "--tools",
            tools,
            "--output-format",
            "stream-json",
            "--model",
            normalize_model(model),
            "--effort",
            effort,
        ]
        if allowed_tools:
            cmd_parts.extend(["--allowed-tools", allowed_tools])
        if disallowed_tools:
            cmd_parts.extend(["--disallowed-tools", disallowed_tools])
        if start:
            cmd_parts.append("--force-new")
        cmd_parts.extend(
            [
                "--stdout-file",
                stdout_path,
                "--stderr-file",
                stderr_path,
                "--combined-file",
                combined_path,
                "--prompt-file",
                prompt_path,
            ]
        )
        agent_cmd = (
            f"{timeout_prefix}{shlex.join(cmd_parts)} "
            f"> {shlex.quote(run_dir + '/' + phase_name + '.driver.stdout.txt')} "
            f"2> {shlex.quote(run_dir + '/' + phase_name + '.driver.stderr.txt')}"
        )
        inner_cmd = (
            f"cd {shlex.quote(remote_root)} && "
            "set -a && . .minimax.remote && set +a && "
            "export SHELL=/bin/bash && "
            "export HOME=/home/daytona && "
            f"export XDG_CONFIG_HOME={shlex.quote(run_dir + '/.config')} && "
            f"export XDG_CACHE_HOME={shlex.quote(run_dir + '/.cache')} && "
            "mkdir -p \"$XDG_CONFIG_HOME\" \"$XDG_CACHE_HOME\" && "
            f"export PATH={shlex.quote(remote_root)}:\"$HOME/go/bin:$HOME/.local/bin:$PATH\" && "
            "export CLAUDE_WAFER_BIN=$(command -v claude) && "
            "export CLAUDE_WAFER_LAUNCHER_KIND=minimax && "
            f"export CLAUDE_CONFIG_DIR={shlex.quote(config_dir)} && "
            f"export MINIMAX_ENV_FILE={shlex.quote(remote_root + '/.minimax.remote')} && "
            f"if [ -f {shlex.quote(pid_path)} ] && kill -0 $(cat {shlex.quote(pid_path)}) >/dev/null 2>&1; then exit 0; fi; "
            f"if ! mkdir {shlex.quote(launch_lock_dir)} >/dev/null 2>&1; then exit 0; fi; "
            f"rm -f {shlex.quote(exit_path)} {shlex.quote(pid_path)} {shlex.quote(stdout_path)} {shlex.quote(stderr_path)}; "
            f"( {agent_cmd}; printf '%s' $? > {shlex.quote(exit_path)}; rm -rf {shlex.quote(launch_lock_dir)} ) < /dev/null & "
            f"echo $! > {shlex.quote(pid_path)}; "
            f"rmdir {shlex.quote(launch_lock_dir)} >/dev/null 2>&1 || true"
        )
        launch_error: Exception | None = None
        result = None
        try:
            result = exec_with_retry(
                sandbox,
                f"su -s /bin/bash {shlex.quote(REMOTE_USER)} -c {shlex.quote(inner_cmd)}",
                timeout=20,
                attempts=3,
                sleep_seconds=2.0,
            )
        except Exception as exc:  # noqa: BLE001
            launch_error = exc
        if result is not None and result.exit_code != 0:
            raise SystemExit(f"Failed to start {phase_name}:\n{result.result}")
        if launch_error is not None and not remote_file_exists(sandbox, pid_path):
            raise SystemExit(f"Failed to start {phase_name}: {launch_error}")

        stdout_seen = ""
        stderr_seen = ""
        stdout_full = ""
        stderr_full = ""
        phase_rc: int | None = None
        deadline = time.time() + exec_timeout
        while True:
            stdout_full = read_remote_text(sandbox, stdout_path, timeout=20, attempts=3)
            stderr_full = read_remote_text(sandbox, stderr_path, timeout=20, attempts=3)
            if len(stdout_full) > len(stdout_seen):
                emit_stream(f"{phase_name}:stdout", stdout_full[len(stdout_seen) :])
                stdout_seen = stdout_full
            if len(stderr_full) > len(stderr_seen):
                emit_stream(f"{phase_name}:stderr", stderr_full[len(stderr_seen) :])
                stderr_seen = stderr_full

            exit_text = read_remote_text(sandbox, exit_path, timeout=15, attempts=3).strip()
            if exit_text:
                try:
                    phase_rc = int(exit_text)
                except ValueError:
                    phase_rc = 1
                break
            terminal_rc = extract_terminal_result_rc(stdout_full)
            if terminal_rc is not None:
                phase_rc = int(terminal_rc)
                try:
                    exec_with_retry(
                        sandbox,
                        (
                            f"(test -f {shlex.quote(pid_path)} && "
                            f"pkill -TERM -P $(cat {shlex.quote(pid_path)}) >/dev/null 2>&1) || true; "
                            f"(test -f {shlex.quote(pid_path)} && "
                            f"kill $(cat {shlex.quote(pid_path)}) >/dev/null 2>&1) || true; "
                            f"printf '%s' {phase_rc} > {shlex.quote(exit_path)}; "
                            f"rm -f {shlex.quote(pid_path)}; "
                            f"rm -rf {shlex.quote(launch_lock_dir)}"
                        ),
                        timeout=20,
                        attempts=3,
                    )
                except Exception:
                    pass
                break
            if time.time() >= deadline:
                try:
                    exec_with_retry(
                        sandbox,
                        f"(test -f {shlex.quote(pid_path)} && kill $(cat {shlex.quote(pid_path)}) >/dev/null 2>&1) || true",
                        timeout=15,
                        attempts=3,
                    )
                except Exception:
                    pass
                phase_rc = 124
                break
            time.sleep(2)

        phase_rc = int(phase_rc or 0)
        overload = phase_rc != 0 and phase_output_indicates_overload(stdout_full, stderr_full)
        if overload and attempt_idx < len(retry_delays):
            delay = retry_delays[attempt_idx]
            emit_status(f"{phase_name} overloaded retry={attempt_idx + 1} sleep={delay}s")
            time.sleep(delay)
            continue
        return phase_rc

    return 1


def download_run_dir(sandbox, *, remote_root: str, run_id: str, local_output_root: Path) -> Path:
    remote_archive = f"/tmp/web-audit-{run_id}.tar.gz"
    result = exec_with_retry(
        sandbox,
        (
            f"cd {shlex.quote(remote_root + '/runs')} && "
            f"tar --exclude={shlex.quote(run_id + '/chrome-profile')} "
            f"--exclude={shlex.quote(run_id + '/chrome-profile/*')} "
            f"--exclude={shlex.quote(run_id + '/.cache')} "
            f"--exclude={shlex.quote(run_id + '/.cache/*')} "
            f"--exclude={shlex.quote(run_id + '/.config')} "
            f"--exclude={shlex.quote(run_id + '/.config/*')} "
            f"-czf {shlex.quote(remote_archive)} {shlex.quote(run_id)}"
        ),
        timeout=600,
    )
    if result.exit_code != 0:
        raise SystemExit(f"Failed to archive remote audit run:\n{result.result}")
    archive_bytes = sandbox.fs.download_file(remote_archive)
    local_output_root.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(suffix=".tar.gz") as tmp:
        tmp.write(archive_bytes)
        tmp.flush()
        with tarfile.open(tmp.name, "r:gz") as tar:
            safe_members = [member for member in tar.getmembers() if not (member.issym() or member.islnk())]
            tar.extractall(local_output_root, members=safe_members, filter="data")
    return local_output_root / run_id


def run_remote(args: argparse.Namespace) -> int:
    phase1_text, phase1_source = read_text_or_placeholder(args.local_phase1_file, fallback_path=DEFAULT_PHASE1_FILE)
    guide_text, guide_source = read_text_or_placeholder(args.local_guide_file, fallback_path=DEFAULT_GUIDE_FILE)
    if args.dry_run:
        print(json.dumps(
            {
                "target_url": args.target_url,
                "run_id": args.run_id,
                "sandbox_policy": args.sandbox_policy,
                "sandbox_prefix": args.sandbox_prefix,
                "phase1_source": phase1_source,
                "guide_source": guide_source,
                "local_output_root": str(Path(args.local_output_root).expanduser().resolve()),
            },
            indent=2,
        ))
        return 0

    emit_status("ensuring sandbox")
    sandbox, sandbox_name = ensure_sandbox(args)
    emit_status(f"sandbox ready name={sandbox_name}")
    minimax_env_text = collect_minimax_env(args)
    remote_root, _, config_dir = ensure_remote_runtime(sandbox, args, minimax_env_text)
    emit_status(f"runtime ready remote_root={remote_root}")
    ensure_extra_runtime(sandbox, remote_root=remote_root)
    emit_status("extra runtime ready")

    run_dir = f"{remote_root}/runs/{args.run_id}"
    run_config_dir = f"{run_dir}/.claude-config"
    proxy_port = int(args.proxy_port) if int(args.proxy_port) > 0 else derive_run_port(18080, 20000, args.run_id)
    chrome_port = int(args.chrome_debug_port) if int(args.chrome_debug_port) > 0 else derive_run_port(9222, 20000, args.run_id)
    exec_with_retry(
        sandbox,
        (
            f"mkdir -p {shlex.quote(run_dir)} {shlex.quote(run_dir + '/artifacts')} {shlex.quote(run_config_dir)} && "
            f"cp -R {shlex.quote(config_dir)}/. {shlex.quote(run_config_dir)}/ >/dev/null 2>&1 || true && "
            f"chown -R {shlex.quote(REMOTE_USER)}:{shlex.quote(REMOTE_USER)} {shlex.quote(run_dir)}"
        ),
        timeout=60,
    )
    meta = {
        "run_id": args.run_id,
        "target_url": args.target_url,
        "phase1_source": phase1_source,
        "guide_source": guide_source,
        "model": normalize_model(args.model),
        "web_mode": args.web_mode,
        "sandbox_name": sandbox_name,
        "proxy_port": proxy_port,
        "chrome_debug_port": chrome_port,
    }
    sandbox.fs.upload_file((json.dumps(meta, ensure_ascii=False, indent=2) + "\n").encode("utf-8"), f"{run_dir}/meta.json")

    emit_status("starting reverse proxy")
    proxy_url = start_reverse_proxy(
        sandbox,
        remote_root=remote_root,
        run_dir=run_dir,
        target_url=args.target_url,
        port=proxy_port,
    )
    emit_status(f"proxy ready url={proxy_url}")

    emit_status("starting chromium")
    chrome_debug_url = start_chromium(sandbox, run_dir=run_dir, port=chrome_port, startup_url=args.target_url)
    emit_status(f"chrome ready debug_url={chrome_debug_url}")
    emit_status("registering chrome-devtools mcp")
    ensure_chrome_devtools_mcp(
        sandbox,
        remote_root=remote_root,
        config_dir=run_config_dir,
        chrome_debug_url=chrome_debug_url,
    )
    emit_status("chrome-devtools mcp ready")

    allowed_tools, disallowed_tools = resolve_audit_tool_filters(args)
    alias = f"web-audit-{args.run_id}"

    phase1_prompt = build_phase_prompt(
        phase_label="Phase 1",
        phase_text=phase1_text,
        guide_text=guide_text,
        target_url=args.target_url,
        proxy_url=proxy_url,
        chrome_debug_url=chrome_debug_url,
        remote_root=remote_root,
        run_dir=run_dir,
    )
    phase2_prompt = build_phase_prompt(
        phase_label="Phase 2",
        phase_text=PHASE2_TEXT,
        guide_text=guide_text,
        target_url=args.target_url,
        proxy_url=proxy_url,
        chrome_debug_url=chrome_debug_url,
        remote_root=remote_root,
        run_dir=run_dir,
    )
    phase3_prompt = build_phase_prompt(
        phase_label="Phase 3",
        phase_text=PHASE3_TEXT,
        guide_text=guide_text,
        target_url=args.target_url,
        proxy_url=proxy_url,
        chrome_debug_url=chrome_debug_url,
        remote_root=remote_root,
        run_dir=run_dir,
    )

    emit_status("running phase1")
    rc1 = run_phase(
        sandbox,
        remote_root=remote_root,
        run_dir=run_dir,
        config_dir=run_config_dir,
        alias=alias,
        phase_name="phase1",
        prompt_text=phase1_prompt,
        model=args.model,
        effort=args.effort,
        tools=args.tools,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        timeout_seconds=args.timeout_seconds,
        start=True,
    )
    if rc1 != 0:
        emit_status(f"phase1 failed rc={rc1}")
        local_run_dir = download_run_dir(sandbox, remote_root=remote_root, run_id=args.run_id, local_output_root=Path(args.local_output_root).expanduser().resolve())
        print(str(local_run_dir))
        return rc1

    emit_status("running phase2")
    rc2 = run_phase(
        sandbox,
        remote_root=remote_root,
        run_dir=run_dir,
        config_dir=run_config_dir,
        alias=alias,
        phase_name="phase2",
        prompt_text=phase2_prompt,
        model=args.model,
        effort=args.effort,
        tools=args.tools,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        timeout_seconds=args.timeout_seconds,
        start=False,
    )
    if rc2 != 0:
        emit_status(f"phase2 failed rc={rc2}")
        local_run_dir = download_run_dir(sandbox, remote_root=remote_root, run_id=args.run_id, local_output_root=Path(args.local_output_root).expanduser().resolve())
        print(str(local_run_dir))
        return rc2

    emit_status("running phase3")
    rc3 = run_phase(
        sandbox,
        remote_root=remote_root,
        run_dir=run_dir,
        config_dir=run_config_dir,
        alias=alias,
        phase_name="phase3",
        prompt_text=phase3_prompt,
        model=args.model,
        effort=args.effort,
        tools=args.tools,
        allowed_tools=allowed_tools,
        disallowed_tools=disallowed_tools,
        timeout_seconds=args.timeout_seconds,
        start=False,
    )
    local_run_dir = download_run_dir(
        sandbox,
        remote_root=remote_root,
        run_id=args.run_id,
        local_output_root=Path(args.local_output_root).expanduser().resolve(),
    )
    emit_status(f"downloaded run dir={local_run_dir}")
    print(str(local_run_dir))
    return rc3


def main() -> int:
    args = parse_args()
    return int(run_remote(args))


if __name__ == "__main__":
    raise SystemExit(main())
