from __future__ import annotations

import json
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import urlparse


@dataclass
class RecipeEndpoint:
    method: str
    path: str
    auth_required: bool
    observed_calls: int


@dataclass
class SiteRecipe:
    version: str
    target_host: str
    base_url: str
    generated_at: float
    confidence: float
    success_count: int = 0
    failure_count: int = 0
    endpoints: list[RecipeEndpoint] = field(default_factory=list)
    recommended_method: str = "GET"
    recommended_path: str = "/"
    notes: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["endpoints"] = [asdict(ep) for ep in self.endpoints]
        return payload

    @classmethod
    def from_dict(cls, payload: dict[str, Any]) -> "SiteRecipe":
        endpoints = [
            RecipeEndpoint(
                method=str(item.get("method", "GET")).upper(),
                path=str(item.get("path", "/")),
                auth_required=bool(item.get("auth_required", False)),
                observed_calls=int(item.get("observed_calls", 0)),
            )
            for item in payload.get("endpoints", [])
            if isinstance(item, dict)
        ]
        return cls(
            version=str(payload.get("version", "1")),
            target_host=str(payload.get("target_host", "")),
            base_url=str(payload.get("base_url", "")),
            generated_at=float(payload.get("generated_at", time.time())),
            confidence=float(payload.get("confidence", 0.0)),
            success_count=int(payload.get("success_count", 0)),
            failure_count=int(payload.get("failure_count", 0)),
            endpoints=endpoints,
            recommended_method=str(payload.get("recommended_method", "GET")).upper(),
            recommended_path=str(payload.get("recommended_path", "/")),
            notes=[str(x) for x in payload.get("notes", [])],
        )


def default_recipe_path(store_dir: str, target_url: str) -> Path:
    host = (urlparse(target_url).hostname or "unknown").lower()
    p = Path(store_dir)
    p.mkdir(parents=True, exist_ok=True)
    return p / f"{host}.json"


def load_recipe(path: str | Path) -> SiteRecipe | None:
    p = Path(path)
    if not p.exists():
        return None
    return SiteRecipe.from_dict(json.loads(p.read_text(encoding="utf-8")))


def save_recipe(recipe: SiteRecipe, path: str | Path) -> None:
    p = Path(path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(recipe.to_dict(), indent=2), encoding="utf-8")


def build_recipe_from_analysis(target_url: str, analysis: dict[str, Any]) -> SiteRecipe:
    parsed = urlparse(target_url)
    host = (parsed.hostname or "").lower()
    summary = analysis.get("summary", {}) if isinstance(analysis, dict) else {}
    server_url = str(summary.get("server_url", "")).strip() or f"{parsed.scheme}://{host}"
    raw_eps = analysis.get("endpoints", []) if isinstance(analysis, dict) else []

    endpoints = [
        RecipeEndpoint(
            method=str(item.get("method", "GET")).upper(),
            path=str(item.get("path_template", "/")),
            auth_required=bool(item.get("auth_required", False)),
            observed_calls=int(item.get("calls", 0)),
        )
        for item in raw_eps
        if isinstance(item, dict)
    ]
    confidence = estimate_confidence(analysis)
    recommended_method, recommended_path = choose_recommended_fast_path(endpoints)
    notes = [
        "Auto-generated from authorized traffic capture.",
        "Fast path uses replay against recommended endpoint and falls back to fresh capture when needed.",
    ]
    return SiteRecipe(
        version="1",
        target_host=host,
        base_url=server_url,
        generated_at=time.time(),
        confidence=confidence,
        endpoints=endpoints,
        recommended_method=recommended_method,
        recommended_path=recommended_path,
        notes=notes,
    )


def estimate_confidence(analysis: dict[str, Any]) -> float:
    summary = analysis.get("summary", {}) if isinstance(analysis, dict) else {}
    endpoints = analysis.get("endpoints", []) if isinstance(analysis, dict) else []
    endpoint_count = int(summary.get("endpoints_found", 0) or len(endpoints))
    server_url = str(summary.get("server_url", "")).strip()
    auth_count = sum(1 for ep in endpoints if isinstance(ep, dict) and ep.get("auth_required"))

    score = 0.15
    score += min(endpoint_count / 20.0, 0.55)
    if server_url:
        score += 0.15
    if auth_count > 0:
        score += 0.1
    return max(0.0, min(0.98, round(score, 3)))


def choose_recommended_fast_path(endpoints: list[RecipeEndpoint]) -> tuple[str, str]:
    get_candidates = [ep for ep in endpoints if ep.method == "GET" and "{" not in ep.path]
    if get_candidates:
        best = sorted(get_candidates, key=lambda ep: ep.observed_calls, reverse=True)[0]
        return best.method, best.path
    if endpoints:
        best = sorted(endpoints, key=lambda ep: ep.observed_calls, reverse=True)[0]
        return best.method, best.path
    return "GET", "/"


def replay_recipe_fast_path(
    recipe: SiteRecipe,
    *,
    method: str | None = None,
    path: str | None = None,
    auth_header: str | None = None,
    allow_stateful: bool = False,
    timeout_seconds: int = 15,
) -> dict[str, Any]:
    req_method = (method or recipe.recommended_method).upper()
    req_path = path or recipe.recommended_path
    if req_method != "GET" and not allow_stateful:
        return {
            "ok": False,
            "error": "stateful_method_blocked",
            "message": "Only GET replay is allowed unless --allow-stateful is set.",
            "method": req_method,
            "path": req_path,
        }

    url = recipe.base_url.rstrip("/") + req_path
    headers = {"Accept": "application/json"}
    if auth_header:
        headers["Authorization"] = auth_header

    request = urllib.request.Request(url=url, headers=headers, method=req_method)
    try:
        with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
            body = response.read().decode("utf-8", errors="replace")
            return {
                "ok": 200 <= response.status < 300,
                "status": response.status,
                "method": req_method,
                "path": req_path,
                "url": url,
                "body": body,
                "confidence": recipe.confidence,
            }
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        return {
            "ok": False,
            "status": exc.code,
            "method": req_method,
            "path": req_path,
            "url": url,
            "body": body,
            "confidence": recipe.confidence,
            "error": "http_error",
        }
    except Exception as exc:  # pragma: no cover - network dependent
        return {
            "ok": False,
            "method": req_method,
            "path": req_path,
            "url": url,
            "confidence": recipe.confidence,
            "error": str(exc),
        }


def apply_replay_outcome(recipe: SiteRecipe, ok: bool) -> SiteRecipe:
    if ok:
        recipe.success_count += 1
        recipe.confidence = min(0.99, round(recipe.confidence + 0.03, 3))
    else:
        recipe.failure_count += 1
        recipe.confidence = max(0.05, round(recipe.confidence - 0.12, 3))
    return recipe

