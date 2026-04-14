#!/usr/bin/env python3
from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import time
from pathlib import Path
from typing import Mapping

DEFAULT_MINIMAX_KEY_COOLDOWN_SECONDS = 30


def read_simple_env_file(path: Path) -> dict[str, str]:
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


def split_key_blob(raw: str) -> list[str]:
    keys: list[str] = []
    for piece in re.split(r"[\n,]+", raw or ""):
        value = str(piece or "").strip().strip("'\"")
        if not value or value.startswith("#"):
            continue
        if re.match(r"^MINIMAX_API_KEY(?:S|_\d+)?=", value):
            value = value.split("=", 1)[1].strip().strip("'\"")
        if value and value not in keys:
            keys.append(value)
    return keys


def minimax_key_health_path(env_file: Path) -> Path:
    override = str(os.environ.get("MINIMAX_KEY_HEALTH_FILE", "")).strip()
    if override:
        return Path(override).expanduser().resolve()
    return env_file.expanduser().resolve().with_name("minimax_key_health.json")


def load_key_health(path: Path) -> dict[str, object]:
    if not path.exists():
        return {"keys": {}}
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return {"keys": {}}
    if not isinstance(payload, dict):
        return {"keys": {}}
    keys = payload.get("keys")
    if not isinstance(keys, dict):
        payload["keys"] = {}
    return payload


def save_key_health(path: Path, payload: dict[str, object]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=True, indent=2) + "\n", encoding="utf-8")


def cooldown_seconds() -> int:
    raw = str(os.environ.get("MINIMAX_KEY_COOLDOWN_SECONDS", "")).strip()
    if raw.isdigit():
        return max(1, int(raw))
    return DEFAULT_MINIMAX_KEY_COOLDOWN_SECONDS


def cooldown_matcher(text: str) -> bool:
    hay = str(text or "").lower()
    patterns = (
        "rate limit",
        "too many requests",
        "quota",
        "exhausted",
        "exceeded your current quota",
        "resource exhausted",
        "try again later",
        "overloaded_error",
        "traffic is currently high",
        "token plan is designed for individual",
        "429",
        "529",
    )
    return any(pattern in hay for pattern in patterns)


def active_cooldowns(env_file: Path, *, now: float | None = None) -> dict[int, dict[str, object]]:
    current_time = float(now or time.time())
    payload = load_key_health(minimax_key_health_path(env_file))
    raw_keys = payload.get("keys") if isinstance(payload.get("keys"), dict) else {}
    out: dict[int, dict[str, object]] = {}
    dirty = False
    for key_index, record in list(raw_keys.items()):
        if not isinstance(record, dict):
            dirty = True
            continue
        cooldown_until = float(record.get("cooldown_until_epoch") or 0)
        try:
            parsed_index = int(key_index)
        except Exception:
            dirty = True
            continue
        if cooldown_until > current_time:
            out[parsed_index] = dict(record)
        else:
            dirty = True
    if dirty:
        payload["keys"] = {str(idx): record for idx, record in out.items()}
        save_key_health(minimax_key_health_path(env_file), payload)
    return out


def mark_key_cooldown(
    *,
    env_file: Path,
    key_index: int,
    reason: str,
    observed_text: str = "",
    cooldown_for_seconds: int | None = None,
) -> None:
    window = int(cooldown_for_seconds or cooldown_seconds())
    now_epoch = time.time()
    payload = load_key_health(minimax_key_health_path(env_file))
    keys = payload.get("keys")
    if not isinstance(keys, dict):
        keys = {}
        payload["keys"] = keys
    keys[str(int(key_index))] = {
        "cooldown_until_epoch": now_epoch + window,
        "reason": str(reason),
        "updated_at_epoch": now_epoch,
        "observed_text": str(observed_text or "")[:2000],
    }
    save_key_health(minimax_key_health_path(env_file), payload)


def collect_candidate_keys(
    *,
    values: Mapping[str, str],
    environ: Mapping[str, str] | None = None,
) -> list[str]:
    env = dict(environ or os.environ)
    keys: list[str] = []

    def add(value: str) -> None:
        for key in split_key_blob(value):
            if key not in keys:
                keys.append(key)

    add(str(values.get("MINIMAX_API_KEYS", "")))
    indexed_value_names = sorted(name for name in values if re.fullmatch(r"MINIMAX_API_KEY_\d+", name))
    for name in indexed_value_names:
        add(str(values.get(name, "")))
    add(str(values.get("MINIMAX_API_KEY", "")))

    add(str(env.get("MINIMAX_API_KEYS", "")))
    indexed_env_names = sorted(name for name in env if re.fullmatch(r"MINIMAX_API_KEY_\d+", name))
    for name in indexed_env_names:
        add(str(env.get(name, "")))
    add(str(env.get("MINIMAX_API_KEY", "")))
    return keys


def choose_key(keys: list[str], *, seed: str | None = None) -> tuple[str, int]:
    if not keys:
        raise ValueError("No MiniMax API keys available.")
    if len(keys) == 1:
        return keys[0], 0
    if seed:
        digest = hashlib.sha256(seed.encode("utf-8")).digest()
        index = int.from_bytes(digest[:8], "big") % len(keys)
        return keys[index], index
    index = secrets.randbelow(len(keys))
    return keys[index], index


def build_minimax_runtime_values(
    *,
    model: str,
    env_file: Path,
    environ: Mapping[str, str] | None = None,
    seed: str | None = None,
) -> dict[str, str]:
    env = dict(environ or os.environ)
    values = read_simple_env_file(env_file)
    for key in (
        "MINIMAX_API_KEYS",
        "MINIMAX_API_KEY",
        "MINIMAX_API_HOST",
        "API_TIMEOUT_MS",
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC",
    ):
        override = str(env.get(key, "")).strip()
        if override:
            values[key] = override
    for name, value in env.items():
        if re.fullmatch(r"MINIMAX_API_KEY_\d+", name) and str(value).strip():
            values[name] = str(value).strip()

    keys = collect_candidate_keys(values=values, environ=env)
    if not keys:
        raise SystemExit(f"Missing MINIMAX_API_KEY or MINIMAX_API_KEYS in {env_file} or environment.")
    blocked = active_cooldowns(env_file)
    available_pairs = [(idx, key) for idx, key in enumerate(keys) if idx not in blocked]
    if not available_pairs:
        soonest_index, soonest_record = min(
            blocked.items(),
            key=lambda item: float(item[1].get("cooldown_until_epoch") or 0),
        )
        retry_in = max(0, int(float(soonest_record.get("cooldown_until_epoch") or 0) - time.time()))
        raise SystemExit(
            f"All MiniMax API keys are on cooldown; next key index {soonest_index} becomes eligible in about {retry_in}s."
        )
    available_keys = [key for _, key in available_pairs]
    selected_key, available_index = choose_key(
        available_keys,
        seed=seed or env.get("MINIMAX_KEY_SEED", "").strip() or None,
    )
    selected_index = int(available_pairs[available_index][0])

    api_host = str(values.get("MINIMAX_API_HOST", "")).strip() or "https://api.minimax.io"
    timeout_ms = str(values.get("API_TIMEOUT_MS", "")).strip() or "3000000"
    disable_traffic = str(values.get("CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC", "")).strip() or "1"
    return {
        "MINIMAX_API_KEY": selected_key,
        "MINIMAX_API_HOST": api_host,
        "MINIMAX_MODEL": model,
        "API_TIMEOUT_MS": timeout_ms,
        "CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC": disable_traffic,
        "MINIMAX_KEY_INDEX": str(selected_index),
        "MINIMAX_KEY_COUNT": str(len(keys)),
        "MINIMAX_KEY_AVAILABLE_COUNT": str(len(available_pairs)),
    }
