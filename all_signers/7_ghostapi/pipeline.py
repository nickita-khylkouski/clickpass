from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from .analysis import analyze_har, analyze_har_file, load_har
from .capture import capture_har_with_playwright
from .compare import compare_analyses
from .har import parse_har_endpoints
from .mcp_codegen import generate_mcp_server
from .merge import merge_endpoints
from .openapi import build_openapi
from .openapi_codegen import generate_openapi
from .passive import run_passive_recon
from .pentest import build_authorized_audit_prompt, create_audit_report, run_nuclei_scan
from .policy import enforce_authorized_target
from .recipe import (
    apply_replay_outcome,
    build_recipe_from_analysis,
    default_recipe_path,
    load_recipe,
    replay_recipe_fast_path,
    save_recipe,
)
from .recipe_store import RecipeStore
from .scanner import baseline_endpoint_checks, run_nuclei

PROFILES = {"inventory", "openapi", "mcp", "full"}
MODES = {"generic", "integration-bootstrap", "due-diligence"}


@dataclass
class MapResult:
    endpoints: list
    used_har: str | None
    passive_count: int
    har_count: int


@dataclass
class FastPathResult:
    attempted: bool
    used_fast_path: bool
    success: bool
    reason: str
    recipe_path: str | None = None
    result: dict[str, Any] | None = None


@dataclass
class CrackArtifacts:
    output_dir: str
    har_path: str
    analysis_json: str
    openapi_json: str
    mcp_server_py: str


def map_target(
    *,
    target_url: str,
    target_host: str,
    har_path: str | None,
    run_passive: bool,
    js_files: list[str] | None = None,
) -> MapResult:
    collected = []
    passive_count = 0
    har_count = 0

    if run_passive:
        passive = run_passive_recon(target_url=target_url, target_host=target_host, js_files=js_files or [])
        passive = _filter_endpoints_to_host(passive, target_host)
        collected.extend(passive)
        passive_count = len(passive)

    if har_path:
        har_eps = parse_har_endpoints(har_path, target_host=target_host)
        collected.extend(har_eps)
        har_count = len(har_eps)

    merged = merge_endpoints(collected)
    return MapResult(endpoints=merged, used_har=har_path, passive_count=passive_count, har_count=har_count)


def scan_target(target_url: str, endpoints: list, run_nuclei_checks: bool = False):
    findings = []
    findings.extend(baseline_endpoint_checks(endpoints))
    if run_nuclei_checks:
        findings.extend(run_nuclei(target_url))
    return findings


async def capture_then_map(
    *,
    target_url: str,
    out_har: str,
    target_host: str,
    capture_seconds: int,
    headed: bool,
    run_passive: bool,
    js_files: list[str] | None,
    email: str | None,
    password: str | None,
    phone: str | None,
    totp_secret: str | None,
) -> MapResult:
    har_path = await capture_har_with_playwright(
        target_url=target_url,
        out_har=out_har,
        capture_seconds=capture_seconds,
        headed=headed,
        email=email,
        password=password,
        phone=phone,
        totp_secret=totp_secret,
    )
    return map_target(
        target_url=target_url,
        target_host=target_host,
        har_path=har_path,
        run_passive=run_passive,
        js_files=js_files,
    )


def analyze_artifacts(
    *,
    target_url: str,
    har_path: str,
    server_name: str,
    include_hosts: set[str] | None = None,
) -> dict[str, Any]:
    har = load_har(har_path)
    analysis = analyze_har(har, include_hosts=include_hosts)
    openapi = build_openapi(analysis, title=f"{target_url} Captured API")
    mcp_code = generate_mcp_server(analysis, server_name=server_name)
    return {
        "analysis": analysis,
        "openapi": openapi,
        "mcp_server": mcp_code,
    }


def resolve_profile(mode: str, profile: str | None) -> str:
    if mode not in MODES:
        raise ValueError(f"Unknown mode: {mode}")
    if profile:
        if profile not in PROFILES:
            raise ValueError(f"Unknown profile: {profile}")
        return profile
    if mode == "due-diligence":
        return "inventory"
    return "full"


def write_analysis_artifacts(
    analysis: dict[str, Any],
    *,
    out_dir: str | Path,
    title: str,
    server_name: str,
    profile: str,
) -> None:
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    _write_json(out / "analysis.json", analysis)
    (out / "SUMMARY.md").write_text(summary_md(analysis), encoding="utf-8")

    if profile in {"openapi", "full"}:
        _write_json(out / "openapi.json", build_openapi(analysis, title=title))
    if profile in {"mcp", "full"}:
        (out / "mcp_server.py").write_text(generate_mcp_server(analysis, server_name=server_name), encoding="utf-8")


def run_learning_pipeline(
    *,
    har_path: str | Path,
    include_hosts: set[str] | None,
    out_dir: str | Path,
    title: str,
    server_name: str,
    mode: str = "generic",
    profile: str | None = None,
    existing_analysis: dict[str, Any] | None = None,
) -> dict[str, Any]:
    final_profile = resolve_profile(mode, profile)
    analysis = analyze_har(load_har(har_path), include_hosts=include_hosts)
    if existing_analysis is not None:
        analysis = merge_analyses(existing_analysis, analysis)
    write_analysis_artifacts(
        analysis,
        out_dir=out_dir,
        title=title,
        server_name=server_name,
        profile=final_profile,
    )
    return analysis


def merge_analyses(old: dict[str, Any], new: dict[str, Any]) -> dict[str, Any]:
    merged: dict[tuple[str, str, str], dict[str, Any]] = {}
    for endpoint_list in (old.get("endpoints") or [], new.get("endpoints") or []):
        for source in endpoint_list:
            key = (source["method"], source["host"], source["path_template"])
            if key not in merged:
                merged[key] = dict(source)
                continue
            current = merged[key]
            current["calls"] = int(current.get("calls", 0)) + int(source.get("calls", 0))
            current["auth_required"] = bool(current.get("auth_required")) or bool(source.get("auth_required"))
            current["statuses"] = sorted(set(current.get("statuses", [])) | set(source.get("statuses", [])))
            current["query_params"] = sorted(
                set(current.get("query_params", [])) | set(source.get("query_params", []))
            )
            if source.get("request_schema") and not current.get("request_schema"):
                current["request_schema"] = source["request_schema"]
            if source.get("response_schema") and not current.get("response_schema"):
                current["response_schema"] = source["response_schema"]

    endpoints = sorted(merged.values(), key=lambda e: (e["method"], e["path_template"]))
    old_summary = old.get("summary") or {}
    new_summary = new.get("summary") or {}
    server_url = new_summary.get("server_url") or old_summary.get("server_url") or ""
    entries_seen = int(old_summary.get("entries_seen", 0)) + int(new_summary.get("entries_seen", 0))
    return {
        "summary": {
            "entries_seen": entries_seen,
            "endpoints_found": len(endpoints),
            "server_url": server_url,
        },
        "endpoints": endpoints,
    }


def summary_md(analysis: dict[str, Any]) -> str:
    summary = analysis.get("summary") or {}
    endpoints = analysis.get("endpoints") or []
    auth_count = sum(1 for ep in endpoints if ep.get("auth_required"))
    non_2xx = sum(1 for ep in endpoints if any(s < 200 or s >= 300 for s in ep.get("statuses") or []))
    no_schema = sum(1 for ep in endpoints if not ep.get("response_schema"))
    lines = [
        "# GhostAPI Summary",
        "",
        "Only use this output for authorized targets.",
        "",
        f"- Entries seen: `{summary.get('entries_seen', 0)}`",
        f"- Endpoints found: `{summary.get('endpoints_found', 0)}`",
        f"- Primary server: `{summary.get('server_url', '')}`",
        "",
        "## Risk Buckets",
        f"- Auth-required endpoints: `{auth_count}`",
        f"- Endpoints with non-2xx statuses: `{non_2xx}`",
        f"- Endpoints without inferred response schema: `{no_schema}`",
        "",
        "## Endpoints",
    ]
    for ep in endpoints:
        auth = "auth" if ep.get("auth_required") else "no-auth"
        lines.append(f"- `{ep['method']} {ep['path_template']}` ({auth}, calls={ep['calls']})")
    return "\n".join(lines).rstrip() + "\n"


def try_fast_path_from_recipe_store(
    *,
    target_url: str,
    recipe_store_dir: str,
    min_confidence: float,
    auth_header: str | None = None,
    method: str | None = None,
    path: str | None = None,
    allow_stateful: bool = False,
) -> FastPathResult:
    recipe_path = default_recipe_path(recipe_store_dir, target_url)
    recipe = load_recipe(recipe_path)
    if not recipe:
        return FastPathResult(
            attempted=False,
            used_fast_path=False,
            success=False,
            reason="no_recipe",
            recipe_path=str(recipe_path),
        )
    if recipe.confidence < min_confidence:
        return FastPathResult(
            attempted=True,
            used_fast_path=False,
            success=False,
            reason=f"low_confidence:{recipe.confidence}",
            recipe_path=str(recipe_path),
        )

    replay = replay_recipe_fast_path(
        recipe,
        method=method,
        path=path,
        auth_header=auth_header,
        allow_stateful=allow_stateful,
    )
    apply_replay_outcome(recipe, ok=bool(replay.get("ok")))
    save_recipe(recipe, recipe_path)
    ok = bool(replay.get("ok"))
    return FastPathResult(
        attempted=True,
        used_fast_path=ok,
        success=ok,
        reason="fast_path_success" if ok else "fast_path_failed",
        recipe_path=str(recipe_path),
        result=replay,
    )


def refresh_recipe_from_analysis(
    *,
    target_url: str,
    analysis: dict[str, Any],
    recipe_store_dir: str,
) -> str:
    recipe_path = default_recipe_path(recipe_store_dir, target_url)
    previous = load_recipe(recipe_path)
    fresh = build_recipe_from_analysis(target_url=target_url, analysis=analysis)
    if previous:
        fresh.success_count = previous.success_count
        fresh.failure_count = previous.failure_count
        fresh.confidence = round((fresh.confidence * 0.65) + (previous.confidence * 0.35), 3)
    save_recipe(fresh, recipe_path)
    return str(recipe_path)


def compare_with_recipe(
    *,
    host: str,
    har_path: str,
    recipe_db: str,
    include_hosts: set[str] | None,
) -> dict[str, Any]:
    store = RecipeStore(recipe_db)
    item = store.get(host)
    if item is None:
        raise ValueError(f"Recipe not found for host: {host}")
    current = analyze_har(load_har(har_path), include_hosts=include_hosts)
    return compare_analyses(item["analysis"], current)


def enforce_scope(target_url: str, confirm_authorized: bool, policy_file: str | None) -> str:
    return enforce_authorized_target(target_url, confirm_authorized=confirm_authorized, policy_file=policy_file)


def run_capture_map_sync(**kwargs):
    return asyncio.run(capture_then_map(**kwargs))


# Backward-compatible wrappers from prior pipeline API.
def _default_name_from_url(url: str) -> str:
    host = urlparse(url).netloc or "site"
    return host.replace(":", "_").replace(".", "_")


def crack_site(
    *,
    url: str,
    out_dir: str,
    name: str | None,
    authorized: bool,
    scope_file: str | None = None,
    har_path: str | None = None,
    task: str | None = None,
    email: str | None = None,
    password: str | None = None,
    headless: bool = True,
) -> CrackArtifacts:
    _ = task
    from .scope import assert_authorized_target

    assert_authorized_target(url=url, authorized=authorized, scope_file=scope_file)
    run_name = name or _default_name_from_url(url)
    target_dir = Path(out_dir) / run_name
    target_dir.mkdir(parents=True, exist_ok=True)

    resolved_har = str(target_dir / "capture.har") if not har_path else har_path
    if not har_path:
        asyncio.run(
            capture_har_with_playwright(
                target_url=url,
                out_har=resolved_har,
                email=email,
                password=password,
                headed=not headless,
            )
        )

    analysis = analyze_har_file(har_path=resolved_har, site=url)
    analysis_json_path = target_dir / "analysis.json"
    analysis_json_path.write_text(json.dumps(analysis.to_dict(), indent=2), encoding="utf-8")

    openapi_path = target_dir / "openapi.json"
    openapi_path.write_text(generate_openapi(analysis), encoding="utf-8")

    mcp_path = target_dir / "generated_mcp_server.py"
    generate_mcp_server(analysis, str(mcp_path))

    return CrackArtifacts(
        output_dir=str(target_dir),
        har_path=resolved_har,
        analysis_json=str(analysis_json_path),
        openapi_json=str(openapi_path),
        mcp_server_py=str(mcp_path),
    )


def audit_analysis(
    *,
    analysis_json_path: str,
    out_dir: str,
    target_url: str,
    authorized: bool,
    run_nuclei: bool,
) -> dict:
    from .models import SiteAnalysis
    from .scope import assert_authorized_target

    assert_authorized_target(url=target_url, authorized=authorized, scope_file=None)
    data = json.loads(Path(analysis_json_path).read_text(encoding="utf-8"))
    analysis = SiteAnalysis.from_dict(data)
    Path(out_dir).mkdir(parents=True, exist_ok=True)

    prompt_path = Path(out_dir) / "authorized_audit_prompt.md"
    prompt_path.write_text(build_authorized_audit_prompt(analysis), encoding="utf-8")

    nuclei_summary = {"ran": False, "reason": "disabled"}
    if run_nuclei:
        nuclei_summary = run_nuclei_scan(
            target=target_url,
            output_json=str(Path(out_dir) / "nuclei.jsonl"),
        )

    report_path = Path(out_dir) / "audit_report.json"
    create_audit_report(analysis, nuclei_summary=nuclei_summary, output_path=str(report_path))
    return {
        "report": str(report_path),
        "prompt": str(prompt_path),
        "nuclei": nuclei_summary,
    }


def _filter_endpoints_to_host(endpoints: list, target_host: str) -> list:
    normalized = target_host.lower()
    filtered = []
    for ep in endpoints:
        host = (urlparse(ep.url).hostname or "").lower()
        if host == normalized or host.endswith(f".{normalized}"):
            filtered.append(ep)
    return filtered


def _write_json(path: Path, payload: dict[str, Any]) -> None:
    path.write_text(json.dumps(payload, indent=2, sort_keys=True), encoding="utf-8")
