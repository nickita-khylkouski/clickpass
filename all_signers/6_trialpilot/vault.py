"""Local JSON vault for secrets used by TrialPilot.

This vault is intentionally lightweight:
- file-per-secret JSON storage
- local filesystem permission hardening
- no encryption or third-party crypto dependencies
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import stat
from typing import Any, Mapping


_SAFE_KEY_CHARS = re.compile(r"[^A-Za-z0-9_.-]+")


def mask_secret(value: str | None, *, keep_tail: int = 4, mask_char: str = "*") -> str:
    """Return a masked value that keeps only the tail characters visible."""
    if value is None:
        return ""
    raw = str(value)
    if not raw:
        return ""
    keep = max(0, int(keep_tail))
    if keep == 0:
        return mask_char * len(raw)
    if len(raw) <= keep:
        return mask_char * len(raw)
    return (mask_char * (len(raw) - keep)) + raw[-keep:]


@dataclass(frozen=True)
class SecretMetadata:
    key: str
    path: str
    updated_at: str
    masked: str

    def to_dict(self) -> dict[str, str]:
        return {
            "key": self.key,
            "path": self.path,
            "updated_at": self.updated_at,
            "masked": self.masked,
        }


class LocalVault:
    """Simple local vault with hardened permissions and JSON storage."""

    def __init__(self, root_dir: str | Path = "~/.trialpilot/vault") -> None:
        self.root_dir = Path(root_dir).expanduser()
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._harden_dir(self.root_dir)

    def set_secret(self, key: str, value: str, *, metadata: Mapping[str, Any] | None = None) -> SecretMetadata:
        safe_key = self._normalize_key(key)
        payload = {
            "key": safe_key,
            "value": str(value),
            "metadata": dict(metadata or {}),
            "updated_at": _utc_now_iso(),
        }
        secret_path = self._secret_path(safe_key)
        self._write_json_atomic(secret_path, payload)
        return SecretMetadata(
            key=safe_key,
            path=str(secret_path),
            updated_at=payload["updated_at"],
            masked=mask_secret(payload["value"]),
        )

    def get_secret(self, key: str, default: str | None = None) -> str | None:
        record = self.get_record(key)
        if record is None:
            return default
        value = record.get("value")
        return str(value) if value is not None else default

    def get_record(self, key: str) -> dict[str, Any] | None:
        safe_key = self._normalize_key(key)
        path = self._secret_path(safe_key)
        if not path.exists():
            return None
        self._harden_file(path)
        raw = path.read_text(encoding="utf-8")
        data = json.loads(raw)
        if not isinstance(data, dict):
            return None
        return data

    def delete_secret(self, key: str) -> bool:
        safe_key = self._normalize_key(key)
        path = self._secret_path(safe_key)
        if not path.exists():
            return False
        path.unlink()
        return True

    def has_secret(self, key: str) -> bool:
        safe_key = self._normalize_key(key)
        return self._secret_path(safe_key).exists()

    def list_keys(self) -> list[str]:
        self._harden_dir(self.root_dir)
        keys: list[str] = []
        for path in sorted(self.root_dir.glob("*.json")):
            if path.name.startswith("."):
                continue
            keys.append(path.stem)
            self._harden_file(path)
        return keys

    def get_secret_metadata(self, key: str) -> SecretMetadata | None:
        record = self.get_record(key)
        if record is None:
            return None
        value = str(record.get("value", ""))
        updated_at = str(record.get("updated_at", ""))
        safe_key = self._normalize_key(key)
        return SecretMetadata(
            key=safe_key,
            path=str(self._secret_path(safe_key)),
            updated_at=updated_at,
            masked=mask_secret(value),
        )

    def _secret_path(self, safe_key: str) -> Path:
        return self.root_dir / f"{safe_key}.json"

    def _normalize_key(self, key: str) -> str:
        value = str(key).strip()
        if not value:
            raise ValueError("Secret key must not be empty")
        normalized = _SAFE_KEY_CHARS.sub("_", value)
        if normalized in {".", ".."}:
            raise ValueError("Secret key is invalid after normalization")
        return normalized

    def _write_json_atomic(self, path: Path, payload: Mapping[str, Any]) -> None:
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._harden_dir(self.root_dir)
        tmp_path = path.with_suffix(path.suffix + ".tmp")
        tmp_path.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        self._harden_file(tmp_path)
        os.replace(tmp_path, path)
        self._harden_file(path)

    def _harden_dir(self, path: Path) -> None:
        if os.name != "posix":
            return
        current_mode = stat.S_IMODE(path.stat().st_mode)
        # Owner-only rwx for vault directory.
        if current_mode != 0o700:
            path.chmod(0o700)

    def _harden_file(self, path: Path) -> None:
        if os.name != "posix" or not path.exists():
            return
        current_mode = stat.S_IMODE(path.stat().st_mode)
        # Owner-only read/write for secrets.
        if current_mode != 0o600:
            path.chmod(0o600)


def _utc_now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


__all__ = ["LocalVault", "SecretMetadata", "mask_secret"]
