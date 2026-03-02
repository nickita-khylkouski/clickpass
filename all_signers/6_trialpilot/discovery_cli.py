"""Startup discovery CLI powered by Dedalus MCP server chaining.

This module is focused on:
1) Discovering recent AI/startup signals from X + web search MCP servers
2) Verifying free trial/free tier/API-key availability claims
3) Producing ranked, actionable output (JSON + CSV)
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import html
import hashlib
import inspect
import json
import os
import re
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from time import perf_counter
from typing import Any, Sequence
from urllib.error import HTTPError, URLError
from urllib.parse import urljoin, urlparse
from urllib.request import Request, urlopen

from dotenv import load_dotenv

try:
    from dedalus_labs import AsyncDedalus, DedalusRunner
except ImportError:  # pragma: no cover - runtime dependency guard
    AsyncDedalus = None
    DedalusRunner = None


DEFAULT_MODEL = "openai/gpt-5-mini"
DEFAULT_LOOKBACK_DAYS = 7
DEFAULT_MAX_CANDIDATES = 25
DEFAULT_MAX_STEPS = 14
DEFAULT_OUT_DIR = "trialpilot_out/discovery"
DEFAULT_NICHE_OUT_DIR = "trialpilot_out/niche_tools"
DEFAULT_NICHE_EXA_SERVER = "tsion/exa"
DEFAULT_NICHE_LIMIT = 10
DISALLOWED_TOOL_DIRECTORY_HOSTS = {
    "developertoolkit.ai",
    "www.developertoolkit.ai",
    "myaiexp.com",
    "www.myaiexp.com",
    "vibecoding.app",
    "www.vibecoding.app",
    "everydev.ai",
    "www.everydev.ai",
    "ai-claude.net",
    "www.ai-claude.net",
}

DEFAULT_FIRECRAWL_QUERIES = [
    "site:ycombinator.com/launches AI API agents",
    "site:ycombinator.com/companies/ AI developer tools",
    "site:news.ycombinator.com Show HN AI API",
    "site:x.com AI API launch",
    "new YC startup API key signup docs",
]

FIRECRAWL_IGNORED_SOURCE_HOSTS = {
    "bookface-images.s3.amazonaws.com",
    "youtube.com",
    "www.youtube.com",
    "docs.ycombinator.com",
}

FIRECRAWL_IGNORED_NAME_TOKENS = (
    "startups funded by y combinator",
    "requests for startups",
    "how to start a",
)

SIGNUP_PAGE_TOKENS = (
    "sign up",
    "signup",
    "create account",
    "register",
    "log in",
    "login",
    "continue with email",
)
API_PAGE_TOKENS = (
    "api key",
    "api keys",
    "api",
    "documentation",
    "docs",
    "developer",
    "developers",
    "reference",
    "sdk",
    "quickstart",
    "authentication",
)

BLOG_LIKE_PATH_TOKENS = (
    "/blog",
    "/news",
    "/press",
    "/changelog",
    "/careers",
    "/about",
    "/company",
    "/pricing",
    "/customer",
    "/customers",
)

DEFAULT_DISCOVERY_MCP_SERVERS = [
    "windsor/x-api-mcp",
    "tsion/exa",
    "windsor/brave-search-mcp",
    "issac/fetch-mcp",
    "tsion/context7",
]
DEFAULT_X_MCP_SERVER = "windsor/x-api-mcp"

DEFAULT_QUERIES = [
    "new AI startup API launch",
    "free tier API for developers",
    "YC AI startup API credits",
    "agent infrastructure startup launch",
]

BURST_QUERY_TEMPLATES = [
    "new agent startup API launch",
    "AI devtool free credits API key",
    "YC startup API free tier",
    "MCP startup launch credits",
    "inference API startup free trial",
    "voice AI API free credits",
    "video AI API free tier",
    "browser automation API startup free trial",
    "email agent API startup launch",
    "open-source AI startup hosted API free plan",
    "RAG API startup free credits",
    "multimodal API startup launch",
    "LLM eval startup API free trial",
    "coding agent startup API credits",
    "vector database startup free tier API",
    "search API startup free credits",
    "document OCR startup API free trial",
    "workflow automation startup API free plan",
]

SERVER_PACKS: list[dict[str, str]] = [
    {
        "slug": "windsor/x-api-mcp",
        "role": "Real-time startup signal",
        "why": "Earliest launch chatter and momentum on X.",
    },
    {
        "slug": "tsion/exa",
        "role": "High-quality web discovery",
        "why": "Finds launch pages, docs, and pricing quickly.",
    },
    {
        "slug": "windsor/brave-search-mcp",
        "role": "Independent search cross-check",
        "why": "Reduces single-source bias from one index.",
    },
    {
        "slug": "issac/fetch-mcp",
        "role": "Page extraction",
        "why": "Pulls target pages for free-tier/API verification.",
    },
    {
        "slug": "tsion/context7",
        "role": "Up-to-date docs lookup",
        "why": "Confirms API setup and current docs details.",
    },
]

HARDCODED_NICHE_TOOLS: list[dict[str, str]] = [
    {
        "name": "AgentMail",
        "what": "API inbox/email infrastructure for AI agents.",
        "why_cool": "Useful for OTP, notifications, and agent-native message flows.",
        "official_url": "https://agentmail.to",
        "signup_url": "https://app.agentmail.to",
        "api_key_url": "https://docs.agentmail.to",
        "discussion_url": "https://news.ycombinator.com/item?id=46812608",
        "freshness_note": "Launch HN Jan 29, 2026 (YC S25).",
    },
    {
        "name": "Parse",
        "what": "Turn websites into usable APIs for automation/scraping workloads.",
        "why_cool": "Good for fast data extraction pipelines without bespoke parsers.",
        "official_url": "https://parse.bot",
        "signup_url": "https://parse.bot",
        "api_key_url": "https://docs.parse.bot",
        "discussion_url": "https://news.ycombinator.com/item?id=44833655",
        "freshness_note": "YC Launch page active in 2025-2026.",
    },
    {
        "name": "Hyperspell",
        "what": "Context and memory layer for agent workflows.",
        "why_cool": "Helps agents keep durable context across tools and sessions.",
        "official_url": "https://hyperspell.com",
        "signup_url": "https://hyperspell.com",
        "api_key_url": "https://docs.hyperspell.com",
        "discussion_url": "https://www.ycombinator.com/launches/Ocs-hyperspell-build-ai-agents-with-memory",
        "freshness_note": "YC F25 launch.",
    },
    {
        "name": "Unsiloed AI",
        "what": "Document ingestion/parsing tuned for LLM and RAG systems.",
        "why_cool": "Useful when raw PDFs/docs need clean, structured extraction.",
        "official_url": "https://www.unsiloed.ai",
        "signup_url": "https://app.unsiloed.ai",
        "api_key_url": "https://docs.unsiloed.ai",
        "discussion_url": "https://news.ycombinator.com/item?id=44272502",
        "freshness_note": "Show HN in 2025 and active platform.",
    },
    {
        "name": "Compyle",
        "what": "AI software engineering platform for building and iterating faster.",
        "why_cool": "Startup-scale alternative for agent-assisted code workflows.",
        "official_url": "https://compyle.ai",
        "signup_url": "https://app.compyle.ai",
        "api_key_url": "https://docs.compyle.ai",
        "discussion_url": "https://news.ycombinator.com/item?id=45541752",
        "freshness_note": "Show HN in late 2025.",
    },
    {
        "name": "Tessl",
        "what": "Spec-centric AI software development platform.",
        "why_cool": "Focused on structured, maintainable AI-assisted dev lifecycles.",
        "official_url": "https://tessl.io",
        "signup_url": "https://app.tessl.io",
        "api_key_url": "https://docs.tessl.io",
        "discussion_url": "https://news.ycombinator.com/item?id=42137464",
        "freshness_note": "Major product momentum in 2025.",
    },
    {
        "name": "Firecrawl",
        "what": "Web crawl/extract/search APIs returning LLM-ready data.",
        "why_cool": "Fast path to high-quality web context in agent pipelines.",
        "official_url": "https://www.firecrawl.dev",
        "signup_url": "https://www.firecrawl.dev/app",
        "api_key_url": "https://docs.firecrawl.dev",
        "discussion_url": "https://news.ycombinator.com/item?id=40404615",
        "freshness_note": "Strong OSS and developer traction.",
    },
]


@dataclass(slots=True)
class StartupCandidate:
    name: str
    website: str
    category: str
    free_offer: str
    api_docs_url: str
    signup_url: str
    api_key_url: str
    signal_summary: str
    evidence_urls: list[str]
    free_value_score: float
    api_readiness_score: float
    momentum_score: float
    overall_score: float
    notes: str


NicheTool = dict[str, Any]


def _utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_local_env(path: str = ".env") -> None:
    load_dotenv(path, override=False)


def _extract_json_payload(text: str) -> dict[str, Any]:
    raw = (text or "").strip()
    if not raw:
        raise ValueError("Model returned an empty response.")

    try:
        parsed = json.loads(raw)
        if isinstance(parsed, dict):
            return parsed
    except json.JSONDecodeError:
        pass

    match = re.search(r"\{[\s\S]*\}", raw)
    if not match:
        raise ValueError("Could not find JSON object in model response.")
    candidate = match.group(0)
    parsed = json.loads(candidate)
    if not isinstance(parsed, dict):
        raise ValueError("Parsed JSON is not an object.")
    return parsed


def _extract_connect_url(error_text: str) -> str:
    match = re.search(r"https://as\.dedaluslabs\.ai/oauth/connect\?session=[A-Za-z0-9\-_]+", error_text)
    return match.group(0) if match else ""


def _is_oauth_required_error(exc: Exception) -> bool:
    return "oauth_required" in f"{type(exc).__name__}: {exc}".lower()


def _is_retryable_error(exc: Exception) -> bool:
    detail = f"{type(exc).__name__}: {exc}".lower()
    retry_tokens = (
        "timeout",
        "timed out",
        "rate limit",
        "429",
        "500",
        "502",
        "503",
        "504",
        "temporarily unavailable",
        "connection reset",
    )
    return any(token in detail for token in retry_tokens)


def _render_runtime_error(exc: Exception, *, mode: str) -> dict[str, Any]:
    detail = f"{type(exc).__name__}: {exc}"
    connect_url = _extract_connect_url(detail)
    code = "runtime_error"
    if "oauth_required" in detail.lower():
        code = "oauth_required"
    elif "timeout" in detail.lower():
        code = "timeout"
    return {
        "ok": False,
        "mode": mode,
        "error_code": code,
        "detail": detail,
        "connect_url": connect_url,
        "hint": (
            (
                "Open connect_url to authorize the MCP server, then rerun."
                if connect_url
                else (
                    "Increase --max-runtime-seconds/--timeout-seconds for live runs."
                    if mode == "niche-tools"
                    else "Increase --timeout-seconds or reduce --queries-per-run."
                )
                if code == "timeout"
                else "Check MCP server auth/credentials and rerun."
            )
        ),
    }


def _clip_score(value: Any, fallback: float) -> float:
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return fallback
    return max(0.0, min(10.0, numeric))


def _has_free_signal(text: str) -> bool:
    low = text.lower()
    tokens = ("free", "trial", "credits", "no cost", "sandbox")
    return any(token in low for token in tokens)


def _score_candidate(raw: dict[str, Any]) -> tuple[float, float, float, float]:
    free_offer = str(raw.get("free_offer", "") or "")
    api_docs_url = str(raw.get("api_docs_url", "") or "")
    signup_url = str(raw.get("signup_url", "") or "")
    api_key_url = str(raw.get("api_key_url", "") or "")
    signal_summary = str(raw.get("signal_summary", "") or "")
    notes = str(raw.get("notes", "") or "")
    website = str(raw.get("website", "") or "")
    evidence_urls = raw.get("evidence_urls")

    low_offer = free_offer.lower()
    free_value = 2.5
    if "free forever" in low_offer:
        free_value += 4.0
    elif "free tier" in low_offer or "free plan" in low_offer:
        free_value += 3.5
    elif "free trial" in low_offer or "trial" in low_offer:
        free_value += 2.0
    if "credit" in low_offer:
        free_value += 1.0
    if "no credit card" in low_offer:
        free_value += 1.0
    if not _has_free_signal(free_offer):
        free_value = max(1.5, free_value - 1.0)
    free_value = _clip_score(free_value, fallback=4.0)

    api_readiness = 1.5
    if website:
        api_readiness += 1.0
    if api_docs_url:
        api_readiness += 4.0
    if signup_url:
        api_readiness += 2.0
    if api_key_url:
        api_readiness += 2.0
    low_notes = notes.lower()
    if "not found" in low_notes and "api" in low_notes:
        api_readiness -= 3.0
    if "no clear public api" in low_notes:
        api_readiness -= 2.0
    api_readiness = _clip_score(api_readiness, fallback=4.0)

    momentum = 2.5
    low_summary = signal_summary.lower()
    for token in ("launch", "launched", "announced", "new", "this week", "today", "feb"):
        if token in low_summary:
            momentum += 0.8
    if isinstance(evidence_urls, list):
        momentum += min(3.0, 0.6 * len([x for x in evidence_urls if str(x).strip()]))
    momentum = _clip_score(momentum, fallback=5.0)

    weighted = round((0.4 * free_value) + (0.35 * api_readiness) + (0.25 * momentum), 2)
    overall = weighted
    return free_value, api_readiness, momentum, overall


def _normalize_candidate(raw: dict[str, Any]) -> StartupCandidate:
    if not isinstance(raw, dict):
        raise ValueError("Candidate item must be an object.")

    evidence_urls: list[str] = []
    raw_evidence = raw.get("evidence_urls")
    if isinstance(raw_evidence, list):
        for item in raw_evidence:
            value = str(item).strip()
            if value:
                evidence_urls.append(value)

    free_value, api_readiness, momentum, overall = _score_candidate(raw)
    name = str(raw.get("name", "")).strip() or "unknown-startup"
    category = str(raw.get("category", "")).strip() or "ai-tooling"

    return StartupCandidate(
        name=name,
        website=str(raw.get("website", "")).strip(),
        category=category,
        free_offer=str(raw.get("free_offer", "")).strip(),
        api_docs_url=str(raw.get("api_docs_url", "")).strip(),
        signup_url=str(raw.get("signup_url", "")).strip(),
        api_key_url=str(raw.get("api_key_url", "")).strip(),
        signal_summary=str(raw.get("signal_summary", "")).strip(),
        evidence_urls=evidence_urls,
        free_value_score=free_value,
        api_readiness_score=api_readiness,
        momentum_score=momentum,
        overall_score=overall,
        notes=str(raw.get("notes", "")).strip(),
    )


def _normalize_credentials(raw: str | None) -> list[dict[str, Any]] | None:
    if not raw:
        return None
    value = raw.strip()
    if not value:
        return None

    path = Path(value).expanduser().resolve()
    blob = path.read_text(encoding="utf-8") if path.exists() else value
    parsed = json.loads(blob)

    if isinstance(parsed, list):
        normalized = []
        for item in parsed:
            if not isinstance(item, dict):
                continue
            connection_name = str(item.get("connection_name", "")).strip()
            values = item.get("values", {})
            if connection_name and isinstance(values, dict):
                normalized.append({"connection_name": connection_name, "values": values})
        return normalized or None

    if isinstance(parsed, dict):
        normalized = []
        for connection_name, values in parsed.items():
            if isinstance(values, dict):
                normalized.append({"connection_name": str(connection_name), "values": values})
        return normalized or None

    raise ValueError("credentials must be a JSON list or object.")


def _build_discovery_prompt(queries: Sequence[str], lookback_days: int, max_candidates: int) -> str:
    bullets = "\n".join(f"- {q}" for q in queries)
    return (
        "You are an AI startup scout. Use available MCP tools aggressively.\n"
        "Priority: X signals first, then validate on official websites/docs pages.\n"
        "Speed preference: prioritize breadth over deep crawling.\n"
        "Avoid exhaustive page scraping; validate the most credible sources first.\n"
        f"Time window: last {lookback_days} days.\n"
        f"Return at most {max_candidates} startups.\n\n"
        "Search intents:\n"
        f"{bullets}\n\n"
        "For each startup, verify:\n"
        "1) API existence\n"
        "2) free tier/trial/credits\n"
        "3) signup and API key acquisition path\n\n"
        "Return STRICT JSON only with this shape:\n"
        "{\n"
        '  "generated_at_utc":"string",\n'
        '  "summary":"string",\n'
        '  "candidates":[\n'
        "    {\n"
        '      "name":"string",\n'
        '      "website":"string",\n'
        '      "category":"string",\n'
        '      "free_offer":"string",\n'
        '      "api_docs_url":"string",\n'
        '      "signup_url":"string",\n'
        '      "api_key_url":"string",\n'
        '      "signal_summary":"string",\n'
        '      "evidence_urls":["string"],\n'
        '      "free_value_score":0,\n'
        '      "api_readiness_score":0,\n'
        '      "momentum_score":0,\n'
        '      "overall_score":0,\n'
        '      "notes":"string"\n'
        "    }\n"
        "  ]\n"
        "}\n"
    )


def _build_x_smoke_prompt(query: str, lookback_days: int) -> str:
    return (
        "Use X MCP tools to fetch recent posts and return a compact JSON report.\n"
        f"Query intent: {query}\n"
        f"Time window: last {lookback_days} days.\n"
        "Return STRICT JSON only:\n"
        "{\n"
        '  "summary":"string",\n'
        '  "posts":[{"author":"string","text":"string","url":"string","created_at":"string"}],\n'
        '  "errors":["string"]\n'
        "}\n"
        "If X tools fail/auth fails, include explicit reason in errors.\n"
    )


def _build_niche_tools_prompt(limit: int, lookback_days: int) -> str:
    return (
        f"Today is {datetime.now(timezone.utc).date().isoformat()}.\n"
        "You are scouting niche/cool startup-ish AI developer tools.\n"
        f"Time window preference: active in the last {lookback_days} days, but include strong recent projects in 2025-2026.\n"
        "Avoid giant incumbents unless clearly niche in this context.\n"
        f"Return at most {limit} items.\n"
        "Each item must include these fields: name, what, why_cool, official_url, signup_url, api_key_url, discussion_url, freshness_note.\n"
        "Use official product/company/GitHub pages for official_url/signup_url/api_key_url, not roundup/directory pages.\n"
        "discussion_url can be from HN, Reddit, GitHub discussions/issues, Product Hunt, YC Launches, or X.\n"
        "Return STRICT JSON object:\n"
        '{ "items": [ { "name":"", "what":"", "why_cool":"", "official_url":"", "signup_url":"", "api_key_url":"", "discussion_url":"", "freshness_note":"" } ] }\n'
    )


def _chunked(items: Sequence[str], chunk_size: int) -> list[list[str]]:
    if chunk_size <= 0:
        return [list(items)]
    out: list[list[str]] = []
    for idx in range(0, len(items), chunk_size):
        out.append(list(items[idx : idx + chunk_size]))
    return out or [[]]


def _normalize_queries(args: argparse.Namespace) -> list[str]:
    ordered: list[str] = []
    seen: set[str] = set()

    def _add(candidate: str) -> None:
        value = str(candidate).strip()
        if not value:
            return
        key = value.lower()
        if key in seen:
            return
        seen.add(key)
        ordered.append(value)

    for query in list(args.query or []):
        _add(query)

    query_file = str(getattr(args, "query_file", "") or "").strip()
    if query_file:
        for line in Path(query_file).expanduser().resolve().read_text(encoding="utf-8").splitlines():
            _add(line)

    if bool(getattr(args, "burst_queries", False)):
        for query in BURST_QUERY_TEMPLATES:
            _add(query)

    if not ordered:
        for query in DEFAULT_QUERIES:
            _add(query)
    return ordered


def _candidate_to_row(item: StartupCandidate) -> dict[str, Any]:
    return {
        "name": item.name,
        "website": item.website,
        "category": item.category,
        "free_offer": item.free_offer,
        "api_docs_url": item.api_docs_url,
        "signup_url": item.signup_url,
        "api_key_url": item.api_key_url,
        "signal_summary": item.signal_summary,
        "free_value_score": item.free_value_score,
        "api_readiness_score": item.api_readiness_score,
        "momentum_score": item.momentum_score,
        "overall_score": item.overall_score,
        "notes": item.notes,
        "evidence_urls": "; ".join(item.evidence_urls),
    }


def _write_outputs(out_dir: Path, payload: dict[str, Any], candidates: Sequence[StartupCandidate]) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "startup_discovery.json"
    csv_path = out_dir / "startup_discovery.csv"

    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    headers = [
        "name",
        "website",
        "category",
        "free_offer",
        "api_docs_url",
        "signup_url",
        "api_key_url",
        "signal_summary",
        "free_value_score",
        "api_readiness_score",
        "momentum_score",
        "overall_score",
        "notes",
        "evidence_urls",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        for item in candidates:
            writer.writerow(_candidate_to_row(item))

    return {"json": str(json_path), "csv": str(csv_path)}


def _is_http_url(value: str) -> bool:
    low = str(value or "").strip().lower()
    if not (low.startswith("http://") or low.startswith("https://")):
        return False
    return not any(ch.isspace() for ch in low)


def _url_status(url: str, timeout_seconds: float) -> dict[str, Any]:
    normalized = str(url or "").strip()
    if not _is_http_url(normalized):
        return {"url": normalized, "ok": False, "status": None, "error": "invalid_url"}

    req = Request(
        normalized,
        headers={"User-Agent": "trialpilot-discovery/1.0"},
        method="GET",
    )
    try:
        with urlopen(req, timeout=max(2.0, timeout_seconds)) as resp:  # noqa: S310
            code = int(getattr(resp, "status", 0) or resp.getcode() or 0)
        ok = (200 <= code < 400) or code in {401, 403}
        return {"url": normalized, "ok": ok, "status": code, "error": ""}
    except HTTPError as exc:
        code = int(getattr(exc, "code", 0) or 0)
        return {"url": normalized, "ok": False, "status": code, "error": str(exc)}
    except URLError as exc:
        return {"url": normalized, "ok": False, "status": None, "error": str(exc.reason)}
    except ValueError as exc:
        return {"url": normalized, "ok": False, "status": None, "error": str(exc)}


def _url_probe(url: str, timeout_seconds: float, *, max_bytes: int = 140_000) -> dict[str, Any]:
    normalized = str(url or "").strip()
    if not _is_http_url(normalized):
        return {"url": normalized, "ok": False, "status": None, "final_url": normalized, "text": "", "error": "invalid_url"}

    req = Request(
        normalized,
        headers={"User-Agent": "trialpilot-discovery/1.0"},
        method="GET",
    )
    try:
        with urlopen(req, timeout=max(1.5, timeout_seconds)) as resp:  # noqa: S310
            code = int(getattr(resp, "status", 0) or resp.getcode() or 0)
            final_url = str(resp.geturl() or normalized)
            blob = resp.read(max(4_096, int(max_bytes)))
        ok = 200 <= code < 400
        return {
            "url": normalized,
            "ok": ok,
            "status": code,
            "final_url": final_url,
            "text": blob.decode("utf-8", errors="ignore") if ok else "",
            "error": "" if ok else f"http_status_{code}",
        }
    except HTTPError as exc:
        return {
            "url": normalized,
            "ok": False,
            "status": int(getattr(exc, "code", 0) or 0),
            "final_url": normalized,
            "text": "",
            "error": str(exc),
        }
    except URLError as exc:
        return {"url": normalized, "ok": False, "status": None, "final_url": normalized, "text": "", "error": str(exc.reason)}
    except (OSError, TimeoutError) as exc:
        return {"url": normalized, "ok": False, "status": None, "final_url": normalized, "text": "", "error": str(exc)}
    except ValueError as exc:
        return {"url": normalized, "ok": False, "status": None, "final_url": normalized, "text": "", "error": str(exc)}


def _extract_anchor_candidates(page_url: str, html_text: str) -> list[tuple[str, str]]:
    if not html_text:
        return []
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for match in re.finditer(
        r"<a\b[^>]*href=['\"]([^'\"]+)['\"][^>]*>(.*?)</a>",
        html_text,
        flags=re.IGNORECASE | re.DOTALL,
    ):
        href = html.unescape(str(match.group(1) or "").strip())
        raw_text = re.sub(r"<[^>]+>", " ", str(match.group(2) or ""), flags=re.IGNORECASE)
        text = re.sub(r"\s+", " ", html.unescape(raw_text)).strip().lower()
        if not href or href.startswith(("javascript:", "mailto:", "tel:", "#")):
            continue
        url = urljoin(page_url, href)
        if not _is_http_url(url):
            continue
        if url in seen:
            continue
        seen.add(url)
        out.append((text, url))
    return out


def _contains_any_token(text: str, tokens: Sequence[str]) -> bool:
    low = str(text or "").lower()
    return any(token in low for token in tokens)


def _is_blog_like_path(path: str) -> bool:
    low = str(path or "").lower()
    return any(token in low for token in BLOG_LIKE_PATH_TOKENS)


def _verified_signup_url(
    official_url: str,
    *,
    timeout_seconds: float,
    preferred_url: str = "",
) -> str:
    root = _root_from_url(official_url)
    if not root:
        return ""

    probe_cache: dict[str, dict[str, Any]] = {}

    def _probe(url: str) -> dict[str, Any]:
        key = str(url or "").strip()
        if key not in probe_cache:
            probe_cache[key] = _url_probe(key, timeout_seconds=timeout_seconds)
        return probe_cache[key]

    candidates: list[str] = []
    if _is_http_url(preferred_url) and _same_domain_tail(root, preferred_url):
        candidates.append(preferred_url)

    home_probe = _probe(root)
    if bool(home_probe.get("ok")):
        final_home = str(home_probe.get("final_url", root) or root)
        for text, href in _extract_anchor_candidates(final_home, str(home_probe.get("text", "") or "")):
            if not _same_domain_tail(root, href):
                continue
            if _contains_any_token(text, SIGNUP_PAGE_TOKENS) or _contains_any_token(href, ("signup", "sign-up", "register", "login", "start", "trial", "join")):
                candidates.append(href)

    for value in [
        f"{root}/signup",
        f"{root}/sign-up",
        f"{root}/register",
        f"{root}/join",
        f"{root}/login",
        f"{root}/sign-in",
        f"{root}/auth/signup",
        _build_subdomain_url(root, "app"),
        _build_subdomain_url(root, "dashboard"),
    ]:
        if _is_http_url(value):
            candidates.append(value)

    seen: set[str] = set()
    for candidate in candidates[:20]:
        url = str(candidate or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        if not _same_domain_tail(root, url):
            continue
        probe = _probe(url)
        if not bool(probe.get("ok")):
            continue
        final_url = str(probe.get("final_url", url) or url)
        if not _same_domain_tail(root, final_url):
            continue
        path = (urlparse(final_url).path or "").lower()
        body = str(probe.get("text", "") or "").lower()
        if _is_blog_like_path(path):
            continue
        if _contains_any_token(path, ("signup", "sign-up", "register", "login", "sign-in", "auth", "join")):
            return final_url
        if _contains_any_token(body, SIGNUP_PAGE_TOKENS):
            return final_url
    return ""


def _verified_api_docs_url(
    official_url: str,
    *,
    timeout_seconds: float,
    preferred_url: str = "",
) -> str:
    root = _root_from_url(official_url)
    if not root:
        return ""

    probe_cache: dict[str, dict[str, Any]] = {}

    def _probe(url: str) -> dict[str, Any]:
        key = str(url or "").strip()
        if key not in probe_cache:
            probe_cache[key] = _url_probe(key, timeout_seconds=timeout_seconds)
        return probe_cache[key]

    candidates: list[str] = []
    if _is_http_url(preferred_url) and _same_domain_tail(root, preferred_url):
        candidates.append(preferred_url)

    home_probe = _probe(root)
    if bool(home_probe.get("ok")):
        final_home = str(home_probe.get("final_url", root) or root)
        for text, href in _extract_anchor_candidates(final_home, str(home_probe.get("text", "") or "")):
            if not _same_domain_tail(root, href):
                continue
            if _contains_any_token(href, ("docs", "developer", "developers", "reference", "api", "sdk", "openapi")):
                candidates.append(href)
                continue
            # Some docs pages are linked with short labels like "API".
            if _contains_any_token(text, ("api", "docs", "developer", "reference", "sdk")):
                candidates.append(href)

    for value in [
        _build_subdomain_url(root, "docs"),
        _build_subdomain_url(root, "developers"),
        f"{root}/docs",
        f"{root}/docs/api",
        f"{root}/api",
        f"{root}/reference",
        f"{root}/developers",
        f"{root}/developer",
        f"{root}/documentation",
    ]:
        if _is_http_url(value):
            candidates.append(value)

    seen: set[str] = set()
    for candidate in candidates[:20]:
        url = str(candidate or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        if not _same_domain_tail(root, url):
            continue
        probe = _probe(url)
        if not bool(probe.get("ok")):
            continue
        final_url = str(probe.get("final_url", url) or url)
        if not _same_domain_tail(root, final_url):
            continue
        path = (urlparse(final_url).path or "").lower()
        body = str(probe.get("text", "") or "").lower()
        if _is_blog_like_path(path):
            continue
        if _contains_any_token(path, ("docs", "developer", "developers", "reference", "api", "openapi", "sdk")):
            return final_url
        if _host_from_url(final_url).startswith("docs."):
            return final_url
        if _contains_any_token(body, API_PAGE_TOKENS):
            return final_url
    return ""


def _json_post(url: str, payload: dict[str, Any], *, headers: dict[str, str], timeout_seconds: float) -> dict[str, Any]:
    req = Request(
        url,
        data=json.dumps(payload).encode("utf-8"),
        headers=headers,
        method="POST",
    )
    with urlopen(req, timeout=max(2.0, timeout_seconds)) as resp:  # noqa: S310
        body = resp.read().decode("utf-8", errors="replace")
    parsed = json.loads(body)
    if not isinstance(parsed, dict):
        raise ValueError("Unexpected JSON response shape.")
    return parsed


def _resolve_firecrawl_api_key(args: argparse.Namespace) -> str:
    inline = str(getattr(args, "firecrawl_api_key", "") or "").strip()
    if inline:
        return inline
    env_name = str(getattr(args, "firecrawl_api_key_env", "FIRECRAWL_API_KEY") or "FIRECRAWL_API_KEY")
    env = os.getenv(env_name, "").strip()
    if env:
        return env
    raise ValueError(f"Missing Firecrawl API key. Set {env_name} or pass --firecrawl-api-key.")


def _firecrawl_search(api_key: str, query: str, *, limit: int, timeout_seconds: float) -> list[dict[str, Any]]:
    payload = {"query": query, "limit": max(1, int(limit))}
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    parsed = _json_post(
        "https://api.firecrawl.dev/v1/search",
        payload,
        headers=headers,
        timeout_seconds=timeout_seconds,
    )
    if not bool(parsed.get("success")):
        return []
    data = parsed.get("data")
    if not isinstance(data, list):
        return []
    out: list[dict[str, Any]] = []
    for item in data:
        if isinstance(item, dict):
            out.append(item)
    return out


def _firecrawl_scrape_markdown(api_key: str, url: str, *, timeout_seconds: float) -> str:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Content-Type": "application/json",
    }
    try:
        parsed = _json_post(
            "https://api.firecrawl.dev/v1/scrape",
            {"url": url, "formats": ["markdown"]},
            headers=headers,
            timeout_seconds=timeout_seconds,
        )
    except Exception:
        return ""
    if not bool(parsed.get("success")):
        return ""
    data = parsed.get("data")
    if not isinstance(data, dict):
        return ""
    return str(data.get("markdown", "") or "")


def _extract_http_urls(text: str) -> list[str]:
    if not text:
        return []
    # Keep URL extraction simple/fast; trim common markdown punctuation wrappers.
    raw_urls = re.findall(r"https?://[^\s)>\"]+", text)
    cleaned: list[str] = []
    seen: set[str] = set()
    for raw in raw_urls:
        url = raw.strip().rstrip(".,;:!?)]}")
        if not _is_http_url(url) or url in seen:
            continue
        seen.add(url)
        cleaned.append(url)
    return cleaned


def _yc_title_to_name(title: str) -> str:
    raw = str(title or "").strip()
    if not raw:
        return ""
    for sep in (" | ", " - ", " – "):
        if sep in raw:
            return raw.split(sep, 1)[0].strip()
    return raw


def _name_from_official_url(url: str) -> str:
    host = _host_from_url(url)
    if not host:
        return ""
    if host.startswith("www."):
        host = host[4:]
    first = host.split(".", 1)[0]
    first = first.replace("-", " ").replace("_", " ").strip()
    return " ".join(part.capitalize() for part in first.split()) if first else ""


def _normalize_firecrawl_result_url(url: str) -> str:
    raw = str(url or "").strip()
    if not _is_http_url(raw):
        return ""
    parsed = urlparse(raw)
    host = (parsed.netloc or "").lower()
    if host.startswith("docs."):
        return f"{parsed.scheme}://{host[5:]}"
    return _root_from_url(raw)


def _firecrawl_company_website_from_yc_page(api_key: str, yc_url: str, *, timeout_seconds: float) -> str:
    return _firecrawl_external_product_url_from_page(api_key, yc_url, timeout_seconds=timeout_seconds)


def _firecrawl_external_product_url_from_page(api_key: str, page_url: str, *, timeout_seconds: float) -> str:
    markdown = _firecrawl_scrape_markdown(api_key, page_url, timeout_seconds=timeout_seconds)
    if not markdown:
        return ""
    page_host = _host_from_url(page_url)
    for url in _extract_http_urls(markdown):
        host = _host_from_url(url)
        if not host:
            continue
        if not _is_probably_product_host(host):
            continue
        if page_host and host == page_host:
            continue
        if "ycombinator.com" in host or "twitter.com" in host or "x.com" in host:
            continue
        if _is_disallowed_directory_url(url):
            continue
        return _root_from_url(url)
    return ""


def _yc_slug_hint_from_url(url: str) -> str:
    parsed = urlparse(str(url or "").strip())
    segments = [part for part in (parsed.path or "").split("/") if part]
    if not segments:
        return ""
    leaf = segments[-1].strip().lower()
    if not leaf:
        return ""
    if segments[0] == "launches" and "-" in leaf:
        # launch IDs look like "NvQ-agentmail-the-api-first-..."; keep the product token.
        tail = leaf.split("-", 1)[1]
        return tail.split("-", 1)[0]
    return leaf.split("-", 1)[0]


def _firecrawl_fallback_official_from_slug(api_key: str, source_url: str, *, timeout_seconds: float) -> str:
    hint = _yc_slug_hint_from_url(source_url)
    if not hint:
        return ""
    hint_token = re.sub(r"[^a-z0-9]", "", hint.lower())
    hint_prefix = hint_token[:4]
    query = f"{hint} AI startup official site API"
    try:
        rows = _firecrawl_search(api_key, query, limit=5, timeout_seconds=timeout_seconds)
    except Exception:
        return ""
    for row in rows:
        candidate_url = _normalize_firecrawl_result_url(str(row.get("url", "")))
        if not _is_http_url(candidate_url):
            continue
        host = _host_from_url(candidate_url)
        if not _is_probably_product_host(host):
            continue
        if "ycombinator.com" in host or _is_disallowed_directory_url(candidate_url):
            continue
        if hint_prefix:
            host_norm = re.sub(r"[^a-z0-9]", "", host)
            title_norm = re.sub(r"[^a-z0-9]", "", str(row.get("title", "")).lower())
            if hint_prefix not in host_norm and hint_prefix not in title_norm:
                continue
        return candidate_url
    return ""


def _is_probably_product_host(host: str) -> bool:
    value = str(host or "").strip().lower()
    if not value:
        return False
    blocked_suffixes = (
        "amazonaws.com",
        "cloudfront.net",
        "googleusercontent.com",
    )
    blocked_exact = {
        "linkedin.com",
        "www.linkedin.com",
        "youtube.com",
        "www.youtube.com",
        "youtu.be",
        "medium.com",
        "www.medium.com",
        "reddit.com",
        "www.reddit.com",
        "old.reddit.com",
        "x.com",
        "www.x.com",
        "twitter.com",
        "www.twitter.com",
        "github.com",
        "www.github.com",
    }
    if value in blocked_exact:
        return False
    if value.startswith("hn."):
        return False
    if any(value.endswith(suffix) for suffix in blocked_suffixes):
        return False
    return "." in value


def _is_yc_company_profile_url(url: str) -> bool:
    parsed = urlparse(str(url or "").strip())
    host = (parsed.netloc or "").lower()
    if host not in {"ycombinator.com", "www.ycombinator.com"}:
        return False
    segments = [part for part in (parsed.path or "").split("/") if part]
    if len(segments) != 2:
        return False
    return segments[0] == "companies"


def _is_yc_launch_url(url: str) -> bool:
    parsed = urlparse(str(url or "").strip())
    host = (parsed.netloc or "").lower()
    if host not in {"ycombinator.com", "www.ycombinator.com"}:
        return False
    segments = [part for part in (parsed.path or "").split("/") if part]
    return len(segments) >= 2 and segments[0] == "launches"


def _is_hn_item_url(url: str) -> bool:
    parsed = urlparse(str(url or "").strip())
    host = (parsed.netloc or "").lower()
    if host not in {"news.ycombinator.com"}:
        return False
    path = parsed.path or ""
    return path == "/item"


def _is_x_status_url(url: str) -> bool:
    parsed = urlparse(str(url or "").strip())
    host = (parsed.netloc or "").lower()
    if host not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        return False
    path = parsed.path or ""
    return "/status/" in path


def _x_mirror_urls(url: str) -> list[str]:
    parsed = urlparse(str(url or "").strip())
    host = (parsed.netloc or "").lower()
    if host not in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        return []
    path = parsed.path or ""
    query = f"?{parsed.query}" if parsed.query else ""
    return [f"https://fxtwitter.com{path}{query}", f"https://vxtwitter.com{path}{query}"]


def _name_from_yc_title(title: str, fallback_url: str) -> str:
    raw = str(title or "").strip()
    if raw.lower().startswith("launch yc:"):
        raw = raw.split(":", 1)[1].strip()
    for sep in (" | ", " - ", " – ", " — "):
        if sep in raw:
            raw = raw.split(sep, 1)[0].strip()
    if ":" in raw and len(raw.split(":", 1)[0]) <= 48:
        raw = raw.split(":", 1)[0].strip()
    return raw or _name_from_official_url(fallback_url)


def _firecrawl_row_rank(row: dict[str, Any]) -> int:
    url = str(row.get("url", "")).strip()
    title = str(row.get("title", "")).lower()
    description = str(row.get("description", "")).lower()
    score = 0
    if _is_yc_launch_url(url):
        score += 40
    elif _is_yc_company_profile_url(url):
        score += 35
    elif _is_hn_item_url(url):
        score += 20
    elif _is_x_status_url(url):
        score += 15
    if "launch yc" in title:
        score += 8
    keywords = ("api", "agent", "developer", "sdk", "docs", "platform", "tool")
    score += sum(1 for token in keywords if token in title or token in description)
    return score


def _discover_niche_tools_firecrawl(
    args: argparse.Namespace,
    *,
    limit: int,
) -> tuple[list[NicheTool], str, dict[str, Any]]:
    started_at = perf_counter()
    api_key = _resolve_firecrawl_api_key(args)
    timeout_seconds = max(1.5, float(getattr(args, "firecrawl_timeout_seconds", 6.0)))
    per_query = max(1, int(getattr(args, "firecrawl_per_query", 6)))
    parallel_queries = max(1, int(getattr(args, "firecrawl_parallel_queries", 4)))
    max_scrapes = max(1, int(getattr(args, "firecrawl_max_scrapes", 12)))
    max_fallback_searches = max(0, int(getattr(args, "firecrawl_max_fallback_searches", 6)))
    scan_cap = max(limit * 4, int(getattr(args, "firecrawl_scan_cap", 50)))
    queries = list(getattr(args, "firecrawl_query", []) or [])
    if not queries:
        queries = list(DEFAULT_FIRECRAWL_QUERIES)

    warning = ""
    search_calls = 0
    scrape_calls = 0
    fallback_searches = 0
    raw_results: list[dict[str, Any]] = []
    workers = min(parallel_queries, max(1, len(queries)))
    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = {
            pool.submit(
                _firecrawl_search,
                api_key,
                str(query),
                limit=per_query,
                timeout_seconds=timeout_seconds,
            ): str(query)
            for query in queries
        }
        for future in as_completed(futures):
            query = futures[future]
            search_calls += 1
            try:
                batch = future.result()
                raw_results.extend(batch)
            except Exception as exc:
                warning = f"{warning + ' | ' if warning else ''}firecrawl query failed ({query}): {type(exc).__name__}: {exc}"

    raw_results.sort(key=_firecrawl_row_rank, reverse=True)

    seen_urls: set[str] = set()
    seen_domains: set[str] = set()
    candidates: list[NicheTool] = []

    def _warn(message: str) -> None:
        nonlocal warning
        warning = f"{warning + ' | ' if warning else ''}{message}"

    def _maybe_fallback(source_url: str) -> str:
        nonlocal fallback_searches
        if fallback_searches >= max_fallback_searches:
            return ""
        fallback_searches += 1
        return _firecrawl_fallback_official_from_slug(
            api_key,
            source_url,
            timeout_seconds=timeout_seconds,
        )

    def _scrape_once(page_url: str) -> str:
        nonlocal scrape_calls
        if scrape_calls >= max_scrapes:
            return ""
        scrape_calls += 1
        return _firecrawl_external_product_url_from_page(
            api_key,
            page_url,
            timeout_seconds=timeout_seconds,
        )

    for row in raw_results:
        if len(candidates) >= limit or len(seen_urls) >= scan_cap:
            break
        url = str(row.get("url", "")).strip()
        if not _is_http_url(url):
            continue
        if url in seen_urls:
            continue
        seen_urls.add(url)

        parsed = urlparse(url)
        host = (parsed.netloc or "").lower()
        official_url = ""
        name = _name_from_yc_title(str(row.get("title", "")), url)
        from_hn = False
        from_x = False

        if _is_yc_company_profile_url(url):
            try:
                official_url = _scrape_once(url)
            except Exception as exc:
                official_url = _maybe_fallback(url)
                if not official_url:
                    _warn(f"firecrawl scrape failed ({url}): {type(exc).__name__}: {exc}")
                    continue
        elif _is_yc_launch_url(url):
            try:
                official_url = _scrape_once(url)
            except Exception as exc:
                official_url = _maybe_fallback(url)
                if not official_url:
                    _warn(f"firecrawl scrape failed ({url}): {type(exc).__name__}: {exc}")
                    continue
        elif _is_hn_item_url(url):
            from_hn = True
            try:
                official_url = _scrape_once(url)
            except Exception as exc:
                _warn(f"firecrawl scrape failed ({url}): {type(exc).__name__}: {exc}")
                continue
        elif _is_x_status_url(url):
            from_x = True
            scrape_error = ""
            for candidate_page in [url, *_x_mirror_urls(url)]:
                try:
                    official_url = _scrape_once(candidate_page)
                    if official_url:
                        break
                except Exception as exc:
                    scrape_error = f"{type(exc).__name__}: {exc}"
            if not official_url:
                if scrape_error:
                    _warn(f"firecrawl scrape failed ({url}): {scrape_error}")
                continue
        elif host in {"www.ycombinator.com", "ycombinator.com"}:
            # Skip YC index/tag pages; keep only concrete launches and company profile pages.
            continue
        else:
            if host in FIRECRAWL_IGNORED_SOURCE_HOSTS:
                continue
            if not _is_allowed_discussion_url(url):
                # Keep the discovery startup-focused by preferring explicit launch/discussion sources.
                continue
            official_url = _normalize_firecrawl_result_url(url)

        if not _is_http_url(official_url) and (_is_yc_company_profile_url(url) or _is_yc_launch_url(url)):
            official_url = _maybe_fallback(url)
        if not _is_http_url(official_url):
            continue
        official_host = _host_from_url(official_url)
        if not _is_probably_product_host(official_host):
            continue
        if _is_disallowed_directory_url(official_url):
            continue
        if official_host in FIRECRAWL_IGNORED_SOURCE_HOSTS:
            continue

        if from_hn or from_x:
            name = _name_from_official_url(official_url)
        if not name:
            name = _name_from_official_url(official_url)
        if not name:
            continue
        lowered_name = name.strip().lower()
        if any(token in lowered_name for token in FIRECRAWL_IGNORED_NAME_TOKENS):
            continue

        root = _root_from_url(official_url)
        if not root:
            continue
        domain_key = _domain_tail(official_host)
        if not domain_key:
            continue
        signup_url = _verified_signup_url(
            root,
            timeout_seconds=min(6.0, timeout_seconds),
        )
        if not _is_http_url(signup_url):
            continue
        api_key_url = _verified_api_docs_url(
            root,
            timeout_seconds=min(6.0, timeout_seconds),
        )
        if not _is_http_url(api_key_url):
            continue
        if not _same_domain_tail(root, signup_url):
            continue
        if not _same_domain_tail(root, api_key_url):
            continue
        if domain_key in seen_domains:
            continue
        seen_domains.add(domain_key)
        discussion_url = url if _is_allowed_discussion_url(url) else ""

        snippet = str(row.get("description", "")).strip()
        item: NicheTool = {
            "name": name,
            "what": snippet or "AI developer tool with programmatic API.",
            "why_cool": "Has quick signup path and API docs entrypoint for dev workflows.",
            "official_url": root,
            "signup_url": signup_url,
            "api_key_url": api_key_url,
            "discussion_url": discussion_url,
            "freshness_note": "Live Firecrawl discovery run.",
        }
        candidates.append(item)

    stats = {
        "query_count": len(queries),
        "search_calls": search_calls,
        "scrape_calls": scrape_calls,
        "fallback_searches": fallback_searches,
        "raw_results": len(raw_results),
        "scan_cap": scan_cap,
        "max_scrapes": max_scrapes,
        "parallel_queries": workers,
        "elapsed_seconds": round(perf_counter() - started_at, 2),
    }
    return _dedupe_niche_items(candidates), warning, stats


def _normalize_niche_item(raw: dict[str, Any]) -> NicheTool:
    return {
        "name": str(raw.get("name", "")).strip(),
        "what": str(raw.get("what", "")).strip(),
        "why_cool": str(raw.get("why_cool", "")).strip(),
        "official_url": str(raw.get("official_url", "")).strip(),
        "signup_url": str(raw.get("signup_url", "")).strip(),
        "api_key_url": str(raw.get("api_key_url", "")).strip(),
        "discussion_url": str(raw.get("discussion_url", "")).strip(),
        "freshness_note": str(raw.get("freshness_note", "")).strip(),
    }


def _root_from_url(url: str) -> str:
    raw = str(url or "").strip()
    if not _is_http_url(raw):
        return ""
    parsed = urlparse(raw)
    if not parsed.scheme or not parsed.netloc:
        return ""
    return f"{parsed.scheme}://{parsed.netloc}"


def _is_allowed_discussion_url(url: str) -> bool:
    raw = str(url or "").strip()
    if not _is_http_url(raw):
        return False
    parsed = urlparse(raw)
    host = (parsed.netloc or "").lower()
    path = parsed.path or ""
    if host in {"news.ycombinator.com", "www.reddit.com", "reddit.com", "old.reddit.com"}:
        return True
    if host in {"ycombinator.com", "www.ycombinator.com"} and path.startswith("/launches/"):
        return True
    if host in {"github.com", "www.github.com"} and ("/discussions/" in path or "/issues/" in path):
        return True
    if host in {"producthunt.com", "www.producthunt.com"}:
        return True
    if host in {"x.com", "www.x.com", "twitter.com", "www.twitter.com"}:
        return True
    return False


def _domain_tail(host: str) -> str:
    value = str(host or "").strip().lower()
    if value.startswith("www."):
        value = value[4:]
    parts = [part for part in value.split(".") if part]
    if len(parts) >= 2:
        return ".".join(parts[-2:])
    return value


def _host_from_url(url: str) -> str:
    raw = str(url or "").strip()
    if not _is_http_url(raw):
        return ""
    return (urlparse(raw).netloc or "").lower()


def _same_domain_tail(url_a: str, url_b: str) -> bool:
    host_a = _host_from_url(url_a)
    host_b = _host_from_url(url_b)
    if not host_a or not host_b:
        return False
    return _domain_tail(host_a) == _domain_tail(host_b)


def _is_disallowed_directory_url(url: str) -> bool:
    host = _host_from_url(url)
    return host in DISALLOWED_TOOL_DIRECTORY_HOSTS


def _build_subdomain_url(root_url: str, subdomain: str) -> str:
    root = _root_from_url(root_url)
    if not root:
        return ""
    parsed = urlparse(root)
    host = parsed.netloc
    if host.startswith("www."):
        host = host[4:]
    if not host:
        return ""
    return f"{parsed.scheme}://{subdomain}.{host}"


def _first_live_url(candidates: Sequence[str], timeout_seconds: float) -> str:
    seen: set[str] = set()
    for candidate in candidates:
        url = str(candidate or "").strip()
        if not url or url in seen:
            continue
        seen.add(url)
        if not _is_http_url(url):
            continue
        status = _url_status(url, timeout_seconds=timeout_seconds)
        if bool(status.get("ok")):
            return url
    return ""


def _candidate_signup_urls(official_url: str) -> list[str]:
    root = _root_from_url(official_url)
    if not root:
        return []
    return [
        _build_subdomain_url(root, "app"),
        _build_subdomain_url(root, "dashboard"),
        f"{root}/signup",
        f"{root}/sign-up",
        f"{root}/register",
        f"{root}/get-started",
        f"{root}/start",
        f"{root}/app",
        f"{root}/login",
        official_url,
        root,
    ]


def _candidate_api_urls(official_url: str) -> list[str]:
    root = _root_from_url(official_url)
    if not root:
        return []
    return [
        _build_subdomain_url(root, "docs"),
        _build_subdomain_url(root, "developers"),
        f"{root}/docs",
        f"{root}/docs/api",
        f"{root}/api",
        f"{root}/api/docs",
        f"{root}/reference",
        f"{root}/developer",
        f"{root}/developers",
        f"{root}/documentation",
        official_url,
    ]


def _enrich_niche_item_urls(item: NicheTool, timeout_seconds: float) -> NicheTool:
    enriched = dict(item)
    official_url = str(enriched.get("official_url", "")).strip()
    if not _is_http_url(official_url):
        return enriched

    signup = str(enriched.get("signup_url", "")).strip()
    signup_ok = False
    if _is_http_url(signup):
        signup_ok = bool(_url_status(signup, timeout_seconds=timeout_seconds).get("ok"))
    if not signup_ok:
        found_signup = _first_live_url(_candidate_signup_urls(official_url), timeout_seconds=timeout_seconds)
        if found_signup:
            enriched["signup_url"] = found_signup

    api_key = str(enriched.get("api_key_url", "")).strip()
    api_ok = False
    if _is_http_url(api_key):
        api_ok = bool(_url_status(api_key, timeout_seconds=timeout_seconds).get("ok"))
    if not api_ok:
        found_api = _first_live_url(_candidate_api_urls(official_url), timeout_seconds=timeout_seconds)
        if found_api:
            enriched["api_key_url"] = found_api

    return enriched


def _dedupe_niche_items(items: Sequence[NicheTool]) -> list[NicheTool]:
    out: list[NicheTool] = []
    seen: set[str] = set()
    for item in items:
        name = str(item.get("name", "")).strip().lower()
        official = str(item.get("official_url", "")).strip().lower()
        key = f"{name}|{official}"
        if not name or key in seen:
            continue
        seen.add(key)
        out.append(item)
    return out


def _is_niche_item_complete(item: NicheTool) -> bool:
    name = str(item.get("name", "")).strip()
    if not name:
        return False
    required_urls = ("official_url", "signup_url", "api_key_url")
    if not all(_is_http_url(str(item.get(field, "")).strip()) for field in required_urls):
        return False
    official_url = str(item.get("official_url", "")).strip()
    signup_url = str(item.get("signup_url", "")).strip()
    api_key_url = str(item.get("api_key_url", "")).strip()
    if _is_disallowed_directory_url(official_url):
        return False
    if _is_disallowed_directory_url(signup_url):
        return False
    if _is_disallowed_directory_url(api_key_url):
        return False
    if not _same_domain_tail(official_url, signup_url):
        return False
    if not _same_domain_tail(official_url, api_key_url):
        return False

    discussion_url = str(item.get("discussion_url", "")).strip()
    if discussion_url and not _is_allowed_discussion_url(discussion_url):
        # Discussion link is useful context but optional; do not reject if missing/unknown.
        item["discussion_url"] = ""
    return True


def _render_niche_links(items: Sequence[NicheTool]) -> list[dict[str, str]]:
    return [
        {
            "name": str(item.get("name", "")),
            "official_url": str(item.get("official_url", "")),
            "signup_url": str(item.get("signup_url", "")),
            "api_key_url": str(item.get("api_key_url", "")),
            "discussion_url": str(item.get("discussion_url", "")),
        }
        for item in items
    ]


def _write_niche_outputs(out_dir: Path, payload: dict[str, Any], items: Sequence[NicheTool]) -> dict[str, str]:
    out_dir.mkdir(parents=True, exist_ok=True)
    json_path = out_dir / "niche_tools.json"
    csv_path = out_dir / "niche_tools.csv"
    json_path.write_text(json.dumps(payload, indent=2), encoding="utf-8")

    headers = [
        "name",
        "what",
        "why_cool",
        "official_url",
        "signup_url",
        "api_key_url",
        "discussion_url",
        "freshness_note",
    ]
    with csv_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=headers)
        writer.writeheader()
        for item in items:
            writer.writerow({key: item.get(key, "") for key in headers})

    return {"json": str(json_path), "csv": str(csv_path)}


def _niche_live_cache_path(out_dir: str) -> Path:
    return Path(out_dir).expanduser().resolve() / "latest_live_cache.json"


def _read_niche_live_cache(out_dir: str) -> list[NicheTool]:
    path = _niche_live_cache_path(out_dir)
    if not path.exists():
        return []
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return []
    items = payload.get("items")
    if not isinstance(items, list):
        return []
    out: list[NicheTool] = []
    for item in items:
        if isinstance(item, dict):
            out.append(_normalize_niche_item(item))
    return out


def _write_niche_live_cache(out_dir: str, items: Sequence[NicheTool]) -> None:
    path = _niche_live_cache_path(out_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "cached_at_utc": _utc_now(),
        "count": len(items),
        "items": list(items),
    }
    path.write_text(json.dumps(payload, indent=2), encoding="utf-8")


def _resolve_api_key(args: argparse.Namespace) -> str:
    inline = str(args.dedalus_api_key or "").strip()
    if inline:
        return inline
    env = os.getenv(args.api_key_env, "").strip()
    if env:
        return env
    raise ValueError(f"Missing Dedalus API key. Set {args.api_key_env} or pass --dedalus-api-key.")


def _resolve_mcp_servers(args: argparse.Namespace) -> list[str]:
    if args.mcp_server:
        return list(args.mcp_server)
    return list(DEFAULT_DISCOVERY_MCP_SERVERS)


def _emit(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, indent=2, sort_keys=True))


def _batch_id(
    *,
    queries: Sequence[str],
    model: str,
    lookback_days: int,
    max_steps: int,
    servers: Sequence[str],
) -> str:
    payload = json.dumps(
        {
            "queries": list(queries),
            "model": model,
            "lookback_days": lookback_days,
            "max_steps": max_steps,
            "servers": list(servers),
        },
        sort_keys=True,
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()[:16]


async def _run_discovery_batch(
    *,
    batch_index: int,
    batch: Sequence[str],
    client: Any,
    model: str,
    lookback_days: int,
    max_steps: int,
    mcp_servers: Sequence[str],
    x_server: str,
    x_fallback: bool,
    credentials: list[dict[str, Any]] | None,
    batch_retries: int,
    retry_backoff: float,
    batch_max_candidates: int,
    verbose: bool,
) -> dict[str, Any]:
    last_exc: Exception | None = None
    for attempt in range(1, batch_retries + 1):
        batch_servers = list(mcp_servers)
        runner = DedalusRunner(client, verbose=verbose)
        try:
            response = await runner.run(
                input=_build_discovery_prompt(
                    queries=batch,
                    lookback_days=lookback_days,
                    max_candidates=batch_max_candidates,
                ),
                model=model,
                max_steps=max_steps,
                mcp_servers=batch_servers,
                credentials=credentials,
                response_format={"type": "json_object"},
            )
            return {
                "ok": True,
                "batch_index": batch_index,
                "batch_id": _batch_id(
                    queries=batch,
                    model=model,
                    lookback_days=lookback_days,
                    max_steps=max_steps,
                    servers=batch_servers,
                ),
                "queries": list(batch),
                "response": response,
                "servers_used": batch_servers,
                "attempts_used": attempt,
            }
        except Exception as exc:
            last_exc = exc
            if x_fallback and _is_oauth_required_error(exc) and x_server in batch_servers:
                fallback_servers = [item for item in batch_servers if item != x_server]
                if fallback_servers:
                    runner = DedalusRunner(client, verbose=verbose)
                    try:
                        response = await runner.run(
                            input=_build_discovery_prompt(
                                queries=batch,
                                lookback_days=lookback_days,
                                max_candidates=batch_max_candidates,
                            ),
                            model=model,
                            max_steps=max_steps,
                            mcp_servers=fallback_servers,
                            credentials=credentials,
                            response_format={"type": "json_object"},
                        )
                        return {
                            "ok": True,
                            "batch_index": batch_index,
                            "batch_id": _batch_id(
                                queries=batch,
                                model=model,
                                lookback_days=lookback_days,
                                max_steps=max_steps,
                                servers=fallback_servers,
                            ),
                            "queries": list(batch),
                            "response": response,
                            "servers_used": fallback_servers,
                            "attempts_used": attempt,
                            "x_fallback_applied": True,
                        }
                    except Exception as fallback_exc:
                        last_exc = fallback_exc

            should_retry = attempt < batch_retries and _is_retryable_error(last_exc)
            if should_retry and retry_backoff > 0:
                await asyncio.sleep(retry_backoff * attempt)

    return {
        "ok": False,
        "batch_index": batch_index,
        "queries": list(batch),
        "error": f"{type(last_exc).__name__}: {last_exc}" if last_exc else "unknown error",
    }


async def _run_discover(args: argparse.Namespace) -> int:
    if AsyncDedalus is None or DedalusRunner is None:
        raise RuntimeError("Missing dedalus_labs package. Install with: pip install dedalus_labs")

    api_key = _resolve_api_key(args)
    x_api_key = str(args.dedalus_x_api_key or "").strip() or None
    creds = _normalize_credentials(args.credentials_json)
    mcp_servers = _resolve_mcp_servers(args)
    x_server = str(args.x_mcp_server).strip()
    queries = _normalize_queries(args)

    queries_per_run = max(1, int(args.queries_per_run))
    max_steps = max(2, int(args.max_steps))
    batch_retries = max(1, int(args.batch_retries))
    retry_backoff = max(0.0, float(args.retry_backoff_seconds))
    parallel_batches = max(1, int(args.parallel_batches))
    batch_max_candidates = max(int(args.max_candidates), 12)

    if bool(args.fast_mode):
        queries_per_run = max(queries_per_run, 10)
        max_steps = min(max_steps, 6)
        batch_retries = min(batch_retries, 1)
        parallel_batches = max(parallel_batches, 3)
        batch_max_candidates = min(batch_max_candidates, 10)

    query_batches = _chunked(queries, queries_per_run)

    client = AsyncDedalus(api_key=api_key, x_api_key=x_api_key, timeout=float(args.timeout_seconds))
    batch_summaries: list[str] = []
    raw_candidate_dicts: list[dict[str, Any]] = []
    mcp_trace: list[dict[str, Any]] = []
    failed_batches: list[dict[str, Any]] = []
    successful_batches = 0
    semaphore = asyncio.Semaphore(parallel_batches)

    async def _worker(batch_index: int, batch: list[str]) -> dict[str, Any]:
        async with semaphore:
            return await _run_discovery_batch(
                batch_index=batch_index,
                batch=batch,
                client=client,
                model=args.model,
                lookback_days=int(args.lookback_days),
                max_steps=max_steps,
                mcp_servers=mcp_servers,
                x_server=x_server,
                x_fallback=bool(args.x_fallback),
                credentials=creds,
                batch_retries=batch_retries,
                retry_backoff=retry_backoff,
                batch_max_candidates=batch_max_candidates,
                verbose=bool(args.verbose),
            )

    tasks = [
        asyncio.create_task(_worker(batch_index, batch))
        for batch_index, batch in enumerate(query_batches, start=1)
    ]
    batch_results = await asyncio.gather(*tasks)
    batch_results.sort(key=lambda item: int(item.get("batch_index", 0)))

    for result in batch_results:
        if not bool(result.get("ok")):
            failed_batches.append(
                {
                    "batch_index": int(result.get("batch_index", 0)),
                    "queries": list(result.get("queries", [])),
                    "error": str(result.get("error", "unknown error")),
                }
            )
            continue

        successful_batches += 1
        response = result.get("response")
        if response is None:
            failed_batches.append(
                {
                    "batch_index": int(result.get("batch_index", 0)),
                    "queries": list(result.get("queries", [])),
                    "error": "missing response object",
                }
            )
            continue

        try:
            parsed = _extract_json_payload(str(response.final_output))
        except Exception as exc:
            failed_batches.append(
                {
                    "batch_index": int(result.get("batch_index", 0)),
                    "queries": list(result.get("queries", [])),
                    "error": f"{type(exc).__name__}: {exc}",
                }
            )
            continue

        batch_summaries.append(str(parsed.get("summary", "")).strip())
        raw_candidates = parsed.get("candidates")
        if isinstance(raw_candidates, list):
            for item in raw_candidates:
                if isinstance(item, dict):
                    raw_candidate_dicts.append(item)

        mcp_trace.extend(
            {
                "server_name": item.server_name,
                "tool_name": item.tool_name,
                "is_error": item.is_error,
                "duration_ms": item.duration_ms,
            }
            for item in response.mcp_results
        )

    if failed_batches and not bool(args.continue_on_batch_error):
        first = failed_batches[0]
        _emit(_render_runtime_error(RuntimeError(str(first.get("error", "batch failed"))), mode="discover"))
        return 1

    deduped_raw: list[dict[str, Any]] = []
    seen_keys: set[str] = set()
    for item in raw_candidate_dicts:
        key = f"{str(item.get('name', '')).strip().lower()}|{str(item.get('website', '')).strip().lower()}"
        if key in seen_keys:
            continue
        seen_keys.add(key)
        deduped_raw.append(item)

    candidates: list[StartupCandidate] = []
    for item in deduped_raw:
        candidates.append(_normalize_candidate(item))
    candidates.sort(key=lambda c: c.overall_score, reverse=True)
    candidates = candidates[: max(1, int(args.max_candidates))]

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out_dir).expanduser().resolve() / timestamp
    output_payload = {
        "generated_at_utc": _utc_now(),
        "mode": "discover",
        "summary": "\n".join(part for part in batch_summaries if part),
        "query_count": len(queries),
        "queries": queries,
        "query_batches": query_batches,
        "batch_count": len(query_batches),
        "parallel_batches": parallel_batches,
        "fast_mode": bool(args.fast_mode),
        "queries_per_run": queries_per_run,
        "effective_max_steps": max_steps,
        "successful_batches": successful_batches,
        "failed_batches": failed_batches,
        "mcp_servers": mcp_servers,
        "mcp_trace": mcp_trace,
        "candidate_count": len(candidates),
        "candidates": [_candidate_to_row(item) for item in candidates],
    }
    files = _write_outputs(out_dir, output_payload, candidates)

    ok = successful_batches > 0
    _emit(
        {
            "ok": ok,
            "mode": "discover",
            "candidate_count": len(candidates),
            "top_candidates": [item.name for item in candidates[:10]],
            "mcp_calls": len(mcp_trace),
            "query_count": len(queries),
            "batch_count": len(query_batches),
            "parallel_batches": parallel_batches,
            "fast_mode": bool(args.fast_mode),
            "successful_batches": successful_batches,
            "failed_batches": len(failed_batches),
            "partial_success": bool(failed_batches) and successful_batches > 0,
            "output_dir": str(out_dir),
            "files": files,
        }
    )
    return 0 if ok else 1


async def _run_x_smoke(args: argparse.Namespace) -> int:
    if AsyncDedalus is None or DedalusRunner is None:
        raise RuntimeError("Missing dedalus_labs package. Install with: pip install dedalus_labs")

    api_key = _resolve_api_key(args)
    x_api_key = str(args.dedalus_x_api_key or "").strip() or None
    creds = _normalize_credentials(args.credentials_json)
    query = str(args.smoke_query or "AI startup launch API free trial")

    client = AsyncDedalus(api_key=api_key, x_api_key=x_api_key, timeout=float(args.timeout_seconds))
    runner = DedalusRunner(client, verbose=bool(args.verbose))
    try:
        response = await runner.run(
            input=_build_x_smoke_prompt(query=query, lookback_days=args.lookback_days),
            model=args.model,
            max_steps=max(4, min(args.max_steps, 10)),
            mcp_servers=[args.x_mcp_server],
            credentials=creds,
            response_format={"type": "json_object"},
        )
    except Exception as exc:
        _emit(_render_runtime_error(exc, mode="x-smoke"))
        return 1
    parsed = _extract_json_payload(str(response.final_output))
    posts = parsed.get("posts")
    if not isinstance(posts, list):
        posts = []
    errors = parsed.get("errors")
    if not isinstance(errors, list):
        errors = []

    mcp_trace = [
        {
            "server_name": item.server_name,
            "tool_name": item.tool_name,
            "is_error": item.is_error,
            "duration_ms": item.duration_ms,
        }
        for item in response.mcp_results
    ]
    _emit(
        {
            "ok": True,
            "mode": "x-smoke",
            "x_mcp_server": args.x_mcp_server,
            "summary": str(parsed.get("summary", "")),
            "post_count": len(posts),
            "posts": posts[:10],
            "errors": [str(item) for item in errors],
            "mcp_calls": len(mcp_trace),
            "mcp_trace": mcp_trace,
        }
    )
    return 0


async def _run_niche_tools(args: argparse.Namespace) -> int:
    run_started_at = perf_counter()
    limit = max(1, int(args.limit))
    items: list[NicheTool] = []
    source = "hardcoded"
    warning = ""
    provider_stats: dict[str, Any] = {}
    mcp_trace: list[dict[str, Any]] = []
    provider = str(getattr(args, "provider", "dedalus_exa") or "dedalus_exa").strip().lower()

    live_mode = bool(args.live) and not bool(args.hardcoded_only)

    if live_mode:
        if provider == "firecrawl":
            try:
                items, warning, provider_stats = await asyncio.wait_for(
                    asyncio.to_thread(_discover_niche_tools_firecrawl, args, limit=limit),
                    timeout=max(5.0, float(args.max_runtime_seconds)),
                )
                source = "live_firecrawl"
            except Exception as exc:
                warning = f"live discovery failed: {type(exc).__name__}: {exc}"
                if not bool(args.hardcoded_fallback) and not bool(args.use_live_cache):
                    _emit(_render_runtime_error(exc, mode="niche-tools"))
                    return 1
        elif AsyncDedalus is None or DedalusRunner is None:
            warning = "dedalus_labs not installed; using hardcoded catalog."
        else:
            try:
                api_key = _resolve_api_key(args)
                x_api_key = str(args.dedalus_x_api_key or "").strip() or None
                client = AsyncDedalus(api_key=api_key, x_api_key=x_api_key, timeout=float(args.timeout_seconds))
                runner = DedalusRunner(client, verbose=bool(args.verbose))
                response = await asyncio.wait_for(
                    runner.run(
                        input=_build_niche_tools_prompt(limit=limit, lookback_days=args.lookback_days),
                        model=args.model,
                        max_steps=max(4, min(args.max_steps, 10)),
                        mcp_servers=[str(args.exa_mcp_server).strip()],
                        response_format={"type": "json_object"},
                    ),
                    timeout=max(5.0, float(args.max_runtime_seconds)),
                )
                parsed = _extract_json_payload(str(response.final_output))
                raw_items = parsed.get("items")
                if isinstance(raw_items, list):
                    for raw in raw_items:
                        if isinstance(raw, dict):
                            items.append(_normalize_niche_item(raw))
                mcp_trace = [
                    {
                        "server_name": item.server_name,
                        "tool_name": item.tool_name,
                        "is_error": item.is_error,
                        "duration_ms": item.duration_ms,
                    }
                    for item in response.mcp_results
                ]
                source = "live_exa"
            except Exception as exc:
                warning = f"live discovery failed: {type(exc).__name__}: {exc}"
                if not bool(args.hardcoded_fallback) and not bool(args.use_live_cache):
                    _emit(_render_runtime_error(exc, mode="niche-tools"))
                    return 1

    items = _dedupe_niche_items(items)
    if live_mode and bool(args.enrich_live_urls) and source == "live_exa":
        items = [
            _enrich_niche_item_urls(item, timeout_seconds=max(2.0, float(args.link_timeout_seconds)))
            for item in items
        ]
    complete_items = [item for item in items if _is_niche_item_complete(item)]
    if items and not complete_items:
        warning = (
            f"{warning + ' | ' if warning else ''}"
            "live discovery returned no complete entries with signup/api_key/discussion links."
        )
    items = complete_items

    if bool(args.hardcoded_only):
        source = "hardcoded"
        items = [_normalize_niche_item(item) for item in HARDCODED_NICHE_TOOLS]
    elif not items and bool(args.hardcoded_fallback):
        source = "hardcoded"
        items = [_normalize_niche_item(item) for item in HARDCODED_NICHE_TOOLS]
    elif not items and live_mode and bool(args.use_live_cache):
        cached_items = _read_niche_live_cache(str(args.out_dir))
        cached_items = [item for item in cached_items if _is_niche_item_complete(item)]
        if cached_items:
            source = "live_cache"
            items = cached_items
            warning = f"{warning + ' | ' if warning else ''}used latest live cache."
    elif not items:
        _emit(
            {
                "ok": False,
                "mode": "niche-tools",
                "source": source if source.startswith("live_") else "live",
                "warning": warning or "live discovery produced no complete entries.",
                "hint": "Rerun with --provider firecrawl --max-runtime-seconds 60 or use --hardcoded-only.",
                "count": 0,
                "items": [],
            }
        )
        return 1
    items = _dedupe_niche_items(items)[:limit]

    if bool(args.check_links):
        for item in items:
            item["link_checks"] = {
                "official_url": _url_status(str(item.get("official_url", "")), timeout_seconds=float(args.timeout_seconds)),
                "signup_url": _url_status(str(item.get("signup_url", "")), timeout_seconds=float(args.timeout_seconds)),
                "api_key_url": _url_status(str(item.get("api_key_url", "")), timeout_seconds=float(args.timeout_seconds)),
                "discussion_url": _url_status(str(item.get("discussion_url", "")), timeout_seconds=float(args.timeout_seconds)),
            }

    timestamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.out_dir).expanduser().resolve() / timestamp
    payload = {
        "generated_at_utc": _utc_now(),
        "mode": "niche-tools",
        "source": source,
        "provider": provider,
        "runtime_seconds": round(perf_counter() - run_started_at, 2),
        "provider_stats": provider_stats,
        "warning": warning,
        "count": len(items),
        "items": items,
        "mcp_trace": mcp_trace,
    }
    files = _write_niche_outputs(out_dir, payload, items)
    if source in {"live_exa", "live_firecrawl"} and items:
        _write_niche_live_cache(str(args.out_dir), items)

    response_items: Any = items
    if str(args.output_format) == "links":
        response_items = _render_niche_links(items)

    _emit(
        {
            "ok": True,
            "mode": "niche-tools",
            "source": source,
            "provider": provider,
            "runtime_seconds": round(perf_counter() - run_started_at, 2),
            "provider_stats": provider_stats,
            "warning": warning,
            "count": len(items),
            "top_names": [str(item.get("name", "")) for item in items[:10]],
            "output_dir": str(out_dir),
            "files": files,
            "output_format": str(args.output_format),
            "items": response_items,
        }
    )
    return 0


def _run_servers(_: argparse.Namespace) -> int:
    _emit(
        {
            "ok": True,
            "mode": "servers",
            "recommended_server_pack": SERVER_PACKS,
            "default_discovery_mcp_servers": DEFAULT_DISCOVERY_MCP_SERVERS,
            "x_fallback_default": True,
            "continue_on_batch_error_default": True,
            "notes": "Use x + exa + brave + fetch + context7 together for discovery and verification.",
        }
    )
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="trialpilot-discovery",
        description="Discover AI startups with free-trial/API offers via Dedalus MCP servers.",
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    common_parent = argparse.ArgumentParser(add_help=False)
    common_parent.add_argument("--model", default=DEFAULT_MODEL)
    common_parent.add_argument("--api-key-env", default="DEDALUS_API_KEY")
    common_parent.add_argument("--dedalus-api-key", default="")
    common_parent.add_argument("--dedalus-x-api-key", default="")
    common_parent.add_argument("--credentials-json", default="")
    common_parent.add_argument("--lookback-days", type=int, default=DEFAULT_LOOKBACK_DAYS)
    common_parent.add_argument("--max-steps", type=int, default=DEFAULT_MAX_STEPS)
    common_parent.add_argument("--timeout-seconds", type=float, default=120.0)
    common_parent.add_argument("--verbose", action="store_true")

    servers = subparsers.add_parser("servers", help="List recommended MCP server pack.")
    servers.set_defaults(func=_run_servers)

    smoke = subparsers.add_parser("x-smoke", parents=[common_parent], help="Smoke test X MCP integration.")
    smoke.add_argument("--x-mcp-server", default=DEFAULT_X_MCP_SERVER)
    smoke.add_argument("--smoke-query", default="AI startup API launch")
    smoke.set_defaults(func=_run_x_smoke)

    discover = subparsers.add_parser("discover", parents=[common_parent], help="Run full startup discovery pipeline.")
    discover.add_argument("--query", action="append", default=[])
    discover.add_argument("--query-file", default="")
    discover.add_argument("--burst-queries", action="store_true")
    discover.add_argument("--queries-per-run", type=int, default=8)
    discover.add_argument("--parallel-batches", type=int, default=1)
    discover.add_argument("--fast-mode", action="store_true")
    discover.add_argument("--max-candidates", type=int, default=DEFAULT_MAX_CANDIDATES)
    discover.add_argument("--mcp-server", action="append", default=[])
    discover.add_argument("--x-mcp-server", default=DEFAULT_X_MCP_SERVER)
    discover.add_argument("--no-x-fallback", dest="x_fallback", action="store_false")
    discover.add_argument("--batch-retries", type=int, default=2)
    discover.add_argument("--retry-backoff-seconds", type=float, default=1.5)
    discover.add_argument("--stop-on-batch-error", dest="continue_on_batch_error", action="store_false")
    discover.set_defaults(x_fallback=True)
    discover.set_defaults(continue_on_batch_error=True)
    discover.add_argument("--out-dir", default=DEFAULT_OUT_DIR)
    discover.set_defaults(func=_run_discover)

    niche = subparsers.add_parser(
        "niche-tools",
        parents=[common_parent],
        help="Discover niche startup-ish AI dev tools with signup/API-key links.",
    )
    niche.add_argument("--limit", type=int, default=DEFAULT_NICHE_LIMIT)
    niche.add_argument(
        "--provider",
        choices=("firecrawl", "dedalus_exa"),
        default="firecrawl",
        help="Live discovery provider. firecrawl is faster/direct for web discovery.",
    )
    niche.add_argument("--exa-mcp-server", default=DEFAULT_NICHE_EXA_SERVER)
    niche.add_argument("--firecrawl-api-key", default="")
    niche.add_argument("--firecrawl-api-key-env", default="FIRECRAWL_API_KEY")
    niche.add_argument("--firecrawl-query", action="append", default=[])
    niche.add_argument("--firecrawl-per-query", type=int, default=6)
    niche.add_argument("--firecrawl-timeout-seconds", type=float, default=6.0)
    niche.add_argument("--firecrawl-parallel-queries", type=int, default=4)
    niche.add_argument("--firecrawl-max-scrapes", type=int, default=12)
    niche.add_argument("--firecrawl-max-fallback-searches", type=int, default=6)
    niche.add_argument("--firecrawl-scan-cap", type=int, default=50)
    niche.add_argument(
        "--live",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Use live discovery (enabled by default).",
    )
    niche.add_argument("--hardcoded-only", action="store_true")
    niche.add_argument("--hardcoded-fallback", dest="hardcoded_fallback", action="store_true")
    niche.add_argument("--no-hardcoded-fallback", dest="hardcoded_fallback", action="store_false")
    niche.set_defaults(hardcoded_fallback=False)
    niche.add_argument("--check-links", action="store_true")
    niche.add_argument(
        "--enrich-live-urls",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Attempt to fill missing signup/docs URLs from official domains.",
    )
    niche.add_argument("--link-timeout-seconds", type=float, default=3.0, help="Timeout for per-link enrichment checks.")
    niche.add_argument(
        "--max-runtime-seconds",
        type=float,
        default=45.0,
        help="Hard cap for live discovery runtime before fallback.",
    )
    niche.add_argument("--out-dir", default=DEFAULT_NICHE_OUT_DIR)
    niche.add_argument(
        "--use-live-cache",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="If live times out, return the latest successful live result.",
    )
    niche.add_argument(
        "--output-format",
        choices=("full", "links"),
        default="full",
        help="full = detailed records, links = name + onboarding links only.",
    )
    niche.set_defaults(model="openai/gpt-4.1")
    niche.set_defaults(max_steps=4)
    niche.set_defaults(timeout_seconds=45.0)
    niche.set_defaults(func=_run_niche_tools)

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    _load_local_env()
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    func = getattr(args, "func", None)
    if func is None:
        parser.error("No command provided.")
        return 2

    if inspect.iscoroutinefunction(func):
        return asyncio.run(func(args))
    return int(func(args))


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
