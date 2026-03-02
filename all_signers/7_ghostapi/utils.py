from __future__ import annotations

import json
import re
import shutil
import subprocess
from pathlib import Path
from typing import Iterable, Optional


URL_RE = re.compile(r"https?://[^\s\"'<>]+")


def has_binary(name: str) -> bool:
    return shutil.which(name) is not None


def run_cmd(
    cmd: list[str],
    timeout_seconds: int = 60,
    cwd: Optional[Path] = None,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        cmd,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        timeout=timeout_seconds,
        cwd=str(cwd) if cwd else None,
        check=False,
    )


def extract_urls(text: str) -> list[str]:
    return URL_RE.findall(text)


def iter_jsonl_lines(text: str) -> Iterable[dict]:
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            yield payload

