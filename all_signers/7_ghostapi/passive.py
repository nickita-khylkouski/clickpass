from __future__ import annotations

from pathlib import Path
from urllib.parse import urlparse

from ghostapi.models import Endpoint
from ghostapi.utils import extract_urls, has_binary, iter_jsonl_lines, run_cmd


def _endpoint_from_url(url: str, source: str, note: str | None = None) -> Endpoint:
    parsed = urlparse(url)
    path = parsed.path or "/"
    notes = [note] if note else []
    return Endpoint(method="GET", url=url, path=path, source=source, notes=notes)


def run_katana(target_url: str, timeout_seconds: int = 60) -> list[Endpoint]:
    if not has_binary("katana"):
        return []
    cmd = ["katana", "-u", target_url, "-silent", "-jsonl"]
    completed = run_cmd(cmd, timeout_seconds=timeout_seconds)
    endpoints: list[Endpoint] = []
    if completed.returncode != 0:
        return endpoints

    for payload in iter_jsonl_lines(completed.stdout):
        for key in ("request", "response", "url"):
            val = payload.get(key)
            if isinstance(val, str) and val.startswith("http"):
                endpoints.append(_endpoint_from_url(val, source="katana"))
            if isinstance(val, dict):
                nested_url = val.get("url")
                if isinstance(nested_url, str) and nested_url.startswith("http"):
                    endpoints.append(_endpoint_from_url(nested_url, source="katana"))

    if not endpoints:
        for url in extract_urls(completed.stdout):
            endpoints.append(_endpoint_from_url(url, source="katana"))
    return endpoints


def run_gau(target_host: str, timeout_seconds: int = 60) -> list[Endpoint]:
    if not has_binary("gau"):
        return []
    cmd = ["gau", target_host]
    completed = run_cmd(cmd, timeout_seconds=timeout_seconds)
    if completed.returncode != 0:
        return []
    return [_endpoint_from_url(url, source="gau", note="historical") for url in extract_urls(completed.stdout)]


def run_jsluice(js_files: list[str], timeout_seconds: int = 60) -> list[Endpoint]:
    if not has_binary("jsluice"):
        return []
    endpoints: list[Endpoint] = []
    for file_path in js_files:
        path = Path(file_path)
        if not path.exists():
            continue
        cmd = ["jsluice", "urls", str(path)]
        completed = run_cmd(cmd, timeout_seconds=timeout_seconds)
        if completed.returncode != 0:
            continue
        for url in extract_urls(completed.stdout):
            endpoints.append(_endpoint_from_url(url, source="jsluice", note=f"from {path.name}"))
    return endpoints


def run_passive_recon(
    target_url: str,
    target_host: str,
    js_files: list[str] | None = None,
    timeout_seconds: int = 60,
) -> list[Endpoint]:
    endpoints: list[Endpoint] = []
    endpoints.extend(run_katana(target_url, timeout_seconds=timeout_seconds))
    endpoints.extend(run_gau(target_host, timeout_seconds=timeout_seconds))
    if js_files:
        endpoints.extend(run_jsluice(js_files, timeout_seconds=timeout_seconds))
    return endpoints

