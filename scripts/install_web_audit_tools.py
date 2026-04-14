#!/usr/bin/env python3
from __future__ import annotations

import io
import json
import os
import stat
import tarfile
import urllib.request
import zipfile
from pathlib import Path


TOOLS = (
    {
        "name": "subfinder",
        "repo": "projectdiscovery/subfinder",
        "binary": "subfinder",
    },
    {
        "name": "httpx",
        "repo": "projectdiscovery/httpx",
        "binary": "httpx",
    },
    {
        "name": "nuclei",
        "repo": "projectdiscovery/nuclei",
        "binary": "nuclei",
    },
)


def fetch_bytes(url: str) -> bytes:
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "User-Agent": "web-audit-tool-installer",
        },
    )
    with urllib.request.urlopen(req, timeout=120) as response:
        return response.read()


def fetch_json(url: str) -> dict:
    return json.loads(fetch_bytes(url).decode("utf-8"))


def choose_asset(release: dict, tool_name: str) -> dict:
    candidates = []
    for asset in release.get("assets", []) or []:
        name = str(asset.get("name") or "").lower()
        if tool_name not in name:
            continue
        if "linux" not in name or "amd64" not in name:
            continue
        if not (name.endswith(".zip") or name.endswith(".tar.gz") or name.endswith(".tgz")):
            continue
        candidates.append(asset)
    if not candidates:
        raise RuntimeError(f"No linux amd64 asset found for {tool_name}")
    candidates.sort(key=lambda item: str(item.get("name") or ""))
    return candidates[0]


def extract_binary(archive_name: str, payload: bytes, binary_name: str) -> bytes:
    lowered = archive_name.lower()
    if lowered.endswith(".zip"):
        with zipfile.ZipFile(io.BytesIO(payload)) as zf:
            for info in zf.infolist():
                if info.is_dir():
                    continue
                if Path(info.filename).name == binary_name:
                    return zf.read(info)
        raise RuntimeError(f"Binary {binary_name} not found in {archive_name}")
    if lowered.endswith(".tar.gz") or lowered.endswith(".tgz"):
        with tarfile.open(fileobj=io.BytesIO(payload), mode="r:gz") as tf:
            for member in tf.getmembers():
                if not member.isfile():
                    continue
                if Path(member.name).name == binary_name:
                    extracted = tf.extractfile(member)
                    if extracted is None:
                        break
                    return extracted.read()
        raise RuntimeError(f"Binary {binary_name} not found in {archive_name}")
    raise RuntimeError(f"Unsupported archive type: {archive_name}")


def install_tool(dest_dir: Path, *, repo: str, tool_name: str, binary_name: str) -> Path:
    dest_dir.mkdir(parents=True, exist_ok=True)
    output_path = dest_dir / binary_name
    if output_path.exists():
        return output_path
    release = fetch_json(f"https://api.github.com/repos/{repo}/releases/latest")
    asset = choose_asset(release, tool_name)
    archive_name = str(asset["name"])
    download_url = str(asset["browser_download_url"])
    binary_bytes = extract_binary(archive_name, fetch_bytes(download_url), binary_name)
    output_path.write_bytes(binary_bytes)
    output_path.chmod(output_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    return output_path


def main() -> int:
    dest_dir = Path.home() / ".local" / "bin"
    installed: list[str] = []
    for tool in TOOLS:
        path = install_tool(
            dest_dir,
            repo=str(tool["repo"]),
            tool_name=str(tool["name"]),
            binary_name=str(tool["binary"]),
        )
        installed.append(str(path))
    print("\n".join(installed))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
