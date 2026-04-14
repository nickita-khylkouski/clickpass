#!/usr/bin/env python3

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path


ROOT = Path(__file__).resolve().parent


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
        ROOT.parent.parent / ".env.daytona",
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
        or env_file.get("DAYTONA_API_KEY")
        or os.environ.get("DAYTONA_API_KEY")
        or ""
    ).strip().strip("'").strip('"')
    resolved_api_url = str(
        api_url
        or env_file.get("DAYTONA_API_URL")
        or os.environ.get("DAYTONA_API_URL")
        or "https://app.daytona.io/api"
    ).strip().strip("'").strip('"')
    return resolved_api_key, resolved_api_url


def _resolve_daytona_python_bin() -> Path | None:
    env_file = load_daytona_env_file()
    candidates = [
        str(os.environ.get("DAYTONA_PYTHON_BIN") or "").strip(),
        str(env_file.get("DAYTONA_PYTHON_BIN") or "").strip(),
        str(ROOT.parent.parent / ".venv-daytona" / "bin" / "python"),
    ]
    for raw in candidates:
        value = str(raw or "").strip().strip("'").strip('"')
        if not value:
            continue
        path = Path(value).expanduser()
        if not path.is_absolute():
            path = (Path.cwd() / path).resolve()
        if path.exists():
            return path
    return None


def load_daytona():
    try:
        from daytona import Daytona, DaytonaConfig, CreateSandboxFromImageParams, Image, Resources
    except Exception as exc:
        helper_python = _resolve_daytona_python_bin()
        helper_prefix = helper_python.parent.parent if helper_python else None
        if (
            helper_python
            and os.environ.get("_DAYTONA_REMOTE_REEXEC") != "1"
            and helper_prefix
        ):
            env = os.environ.copy()
            current_pythonpath = env.get("PYTHONPATH", "")
            extra_paths = [
                str(helper_prefix / "lib" / "python3.13" / "site-packages"),
                str(helper_prefix / "lib" / "python3.12" / "site-packages"),
                str(helper_prefix / "lib" / "python3.11" / "site-packages"),
            ]
            additions = [path for path in extra_paths if Path(path).exists()]
            if additions:
                env["PYTHONPATH"] = os.pathsep.join(additions + ([current_pythonpath] if current_pythonpath else []))
            env_file = load_daytona_env_file()
            if env_file.get("DAYTONA_PYTHON_BIN") and not env.get("DAYTONA_PYTHON_BIN"):
                env["DAYTONA_PYTHON_BIN"] = str(env_file["DAYTONA_PYTHON_BIN"])
            env["_DAYTONA_REMOTE_REEXEC"] = "1"
            os.execve(str(helper_python), [str(helper_python), *sys.argv], env)
        chosen = str(helper_python) if helper_python else "<unset>"
        raise SystemExit(
            "daytona package is not installed in the active Python. "
            f"Configured Daytona Python: {chosen}. "
            "Install `daytona` into that interpreter or set DAYTONA_PYTHON_BIN to a Python that has it."
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


def _exa_health_path() -> Path:
    return Path.home() / ".claude-wafer" / "exa_key_health.json"


def _read_exa_health() -> dict[str, dict[str, str]]:
    path = _exa_health_path()
    if not path.exists():
        return {}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {}
    return payload if isinstance(payload, dict) else {}


def _write_exa_health(payload: dict[str, dict[str, str]]) -> None:
    path = _exa_health_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _exa_key_id(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def _healthy_exa_keys(keys: list[str]) -> list[str]:
    health = _read_exa_health()
    now_ts = datetime.now(tz=UTC).timestamp()
    healthy: list[str] = []
    for key in keys:
        record = health.get(_exa_key_id(key)) or {}
        cooldown_until = str(record.get("cooldown_until") or "").strip()
        if cooldown_until:
            try:
                if datetime.fromisoformat(cooldown_until).timestamp() > now_ts:
                    continue
            except Exception:
                pass
        healthy.append(key)
    return healthy


def mark_local_exa_key_cooldown(
    key: str,
    *,
    reason: str,
    cooldown_seconds: int = 30 * 24 * 60 * 60,
) -> None:
    value = str(key or "").strip()
    if not value:
        return
    health = _read_exa_health()
    health[_exa_key_id(value)] = {
        "reason": reason,
        "cooldown_until": (datetime.now(tz=UTC) + timedelta(seconds=max(60, int(cooldown_seconds)))).isoformat(),
        "updated_at": datetime.now(tz=UTC).isoformat(),
    }
    _write_exa_health(health)


def load_local_exa_key(seed: str | None = None) -> str | None:
    keys = load_local_exa_keys()
    if not keys:
        return None
    preferred = _healthy_exa_keys(keys)
    if preferred:
        keys = preferred
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
    if getattr(args, "sandbox_id", None):
        try:
            sandbox = client.get(args.sandbox_id)
        except Exception:
            sandbox = None

    def find_named_sandbox() -> object | None:
        if not getattr(args, "sandbox_name", None):
            return None
        try:
            sandboxes = client.list()
            for item in getattr(sandboxes, "items", []) or []:
                if getattr(item, "name", None) == args.sandbox_name:
                    return client.get(getattr(item, "id"))
        except Exception:
            return None
        return None

    if sandbox is None and getattr(args, "sandbox_name", None):
        sandbox = find_named_sandbox()
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
            detail = str(exc).lower()
            lost_race = (
                "already exists" in detail
                or "total cpu limit exceeded" in detail
                or "resource exhausted" in detail
            )
            if not lost_race:
                raise
            sandbox = None
            for _ in range(30):
                time.sleep(1)
                sandbox = find_named_sandbox()
                if sandbox is not None:
                    break
            if sandbox is None:
                raise
    return sandbox
