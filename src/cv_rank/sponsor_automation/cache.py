"""Deterministic filesystem cache for sponsor automation OpenAI outputs."""

from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
from pathlib import Path
from typing import Any

DEFAULT_CACHE_DIR = Path("/Users/nickita/cv-rank/.cache/sponsor_automation")


def _canonical_json(value: Any) -> str:
    """Serialize *value* deterministically for stable hashing."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=True)


def prompt_hash(prompt: str) -> str:
    if not isinstance(prompt, str):
        raise TypeError("prompt must be a string")
    data = prompt.encode("utf-8")
    return hashlib.sha256(data).hexdigest()


def claims_hash(claims: Any) -> str:
    canonical = _canonical_json(claims)
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def prompt_claims_cache_key(prompt: str, claims: Any) -> str:
    """Hash of prompt + claims hash. Used as cache file key."""
    p_hash = prompt_hash(prompt)
    c_hash = claims_hash(claims)
    material = f"{p_hash}:{c_hash}".encode("ascii")
    return hashlib.sha256(material).hexdigest()


def cache_file_path(cache_key: str, cache_dir: Path = DEFAULT_CACHE_DIR) -> Path:
    if len(cache_key) != 64 or any(ch not in "0123456789abcdef" for ch in cache_key):
        raise ValueError("cache_key must be a 64-char lowercase hex sha256 digest")
    return cache_dir / f"{cache_key}.json"


def ensure_cache_dir(cache_dir: Path = DEFAULT_CACHE_DIR) -> Path:
    cache_dir.mkdir(parents=True, exist_ok=True)
    return cache_dir


def read_cache_by_key(cache_key: str, cache_dir: Path = DEFAULT_CACHE_DIR) -> dict[str, Any] | None:
    path = cache_file_path(cache_key, cache_dir=cache_dir)
    if not path.exists():
        return None

    try:
        raw = path.read_text(encoding="utf-8")
        payload = json.loads(raw)
    except (OSError, json.JSONDecodeError):
        return None

    if not isinstance(payload, dict):
        return None
    return payload


def write_cache_by_key(cache_key: str, payload: dict[str, Any], cache_dir: Path = DEFAULT_CACHE_DIR) -> Path:
    if not isinstance(payload, dict):
        raise TypeError("payload must be a dict")

    directory = ensure_cache_dir(cache_dir=cache_dir)
    path = cache_file_path(cache_key, cache_dir=directory)

    body = json.dumps(payload, sort_keys=True, ensure_ascii=True, indent=2)
    tmp_fd, tmp_name = tempfile.mkstemp(prefix=f".{cache_key}.", suffix=".tmp", dir=str(directory))
    try:
        with os.fdopen(tmp_fd, "w", encoding="utf-8") as tmp_file:
            tmp_file.write(body)
            tmp_file.flush()
            os.fsync(tmp_file.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)

    return path


def read_cache(prompt: str, claims: Any, cache_dir: Path = DEFAULT_CACHE_DIR) -> tuple[str, dict[str, Any] | None]:
    key = prompt_claims_cache_key(prompt, claims)
    return key, read_cache_by_key(key, cache_dir=cache_dir)


def write_cache(
    prompt: str,
    claims: Any,
    payload: dict[str, Any],
    cache_dir: Path = DEFAULT_CACHE_DIR,
) -> tuple[str, Path]:
    key = prompt_claims_cache_key(prompt, claims)
    enriched_payload = dict(payload)
    enriched_payload.setdefault("cache_key", key)
    enriched_payload.setdefault("prompt_hash", prompt_hash(prompt))
    enriched_payload.setdefault("claims_hash", claims_hash(claims))
    enriched_payload.setdefault("cached_at_unix", int(time.time()))
    path = write_cache_by_key(key, enriched_payload, cache_dir=cache_dir)
    return key, path
