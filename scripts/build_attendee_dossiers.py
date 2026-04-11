#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from typing import Any

import psycopg2
import psycopg2.extras
from dotenv import load_dotenv

try:
    from exa_py import Exa
    EXA_AVAILABLE = True
except ImportError:
    EXA_AVAILABLE = False

REPO_ROOT = Path(__file__).resolve().parent.parent
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

from cv_rank.checkpoint import create_run_dir, save_meta
from cv_rank.config import load_config
from cv_rank.enrichment.github_api import enrich_from_github_api
from cv_rank.enrichment.platform_db import enrich_from_platform_db
from cv_rank.enrichment.supabase import enrich_from_supabase
from cv_rank.profile import format_profile
from cv_rank.research.exa_person_research import collect_person_evidence, get_exa_api_key as shared_get_exa_api_key

DEFAULT_WAFER_AGENT = Path(
    os.environ.get(
        "CLAUDE_WAFER_AGENT",
        "/Users/nickita/.superset/worktrees/start/beaded-mind/claude_wafer_agent.py",
    )
)
DEFAULT_SYSTEM_PROMPT = REPO_ROOT / "prompts" / "wafer_attendee_dossier_system_prompt.txt"
DEFAULT_OUTPUT_ROOT = REPO_ROOT / "outputs" / "attendee_dossiers"
DEFAULT_LOCAL_EXA_CLI = REPO_ROOT / "scripts" / "exa"
DEFAULT_REMOTE_EXA_CLI = Path(os.environ.get("DAYTONA_REMOTE_EXA_CLI", "/root/tools/cv-rank/scripts/exa"))
FREE_EMAIL_DOMAINS = {
    "gmail.com",
    "googlemail.com",
    "outlook.com",
    "hotmail.com",
    "yahoo.com",
    "icloud.com",
    "me.com",
    "proton.me",
    "protonmail.com",
}


def resolve_exa_cli_path(*, codex_mode: bool = False) -> str:
    override = os.environ.get("CV_RANK_EXA_CLI", "").strip()
    if override:
        return override
    if codex_mode and os.environ.get("DAYTONA_API_KEY"):
        return str(DEFAULT_REMOTE_EXA_CLI)
    return str(DEFAULT_LOCAL_EXA_CLI)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Build per-attendee dossiers by combining CV internal data with Wafer web "
            "research that must confirm identity before attributing updates."
        )
    )
    parser.add_argument("--event", help="Event title/slug/id filter for checked-in attendees.")
    parser.add_argument("--email", help="Single attendee email to process.")
    parser.add_argument(
        "--people-file",
        help="JSON file with explicit people records to process instead of querying attendees.",
    )
    parser.add_argument("--limit", type=int, default=5, help="Max attendees to process.")
    parser.add_argument(
        "--config",
        default=str(REPO_ROOT / "config.yaml"),
        help="cv-rank config path.",
    )
    parser.add_argument(
        "--output-dir",
        default=str(DEFAULT_OUTPUT_ROOT),
        help="Root directory for dossier runs.",
    )
    parser.add_argument(
        "--run-id",
        help="Optional explicit run id.",
    )
    parser.add_argument(
        "--wafer-agent",
        default=str(DEFAULT_WAFER_AGENT),
        help="Path to claude_wafer_agent.py.",
    )
    parser.add_argument(
        "--wafer-api-key-file",
        help="Optional Wafer API key file override for this run.",
    )
    parser.add_argument(
        "--exa-api-key-file",
        help="Optional Exa API key file override for deterministic Exa presearch.",
    )
    parser.add_argument(
        "--wafer-web-mode",
        default="mixed",
        choices=("exa", "mixed", "claude"),
        help="Web mode to use for Wafer research.",
    )
    parser.add_argument(
        "--allowed-tools",
        default="",
        help="Allowed tool names for Wafer. Defaults are chosen from the selected web mode.",
    )
    parser.add_argument(
        "--disallowed-tools",
        default="",
        help="Disallowed tool names for Wafer. Defaults are chosen from the selected web mode.",
    )
    parser.add_argument(
        "--system-prompt-file",
        default=str(DEFAULT_SYSTEM_PROMPT),
        help="Appended system prompt file for Wafer dossier research.",
    )
    parser.add_argument(
        "--wafer-timeout-seconds",
        type=int,
        default=180,
        help="Hard timeout for each Wafer dossier request.",
    )
    parser.add_argument(
        "--fallback-model",
        help="Claude CLI fallback model to use when the primary model is overloaded.",
    )
    parser.add_argument(
        "--claude-model",
        help="Claude CLI model override (for example: haiku or claude-haiku-4-5-20251001).",
    )
    parser.add_argument(
        "--effort",
        choices=("low", "medium", "high", "max"),
        default="low",
        help="Claude CLI effort level for Wafer/ZAI runs.",
    )
    parser.add_argument(
        "--claude-output-format",
        choices=("text", "json", "stream-json"),
        default="text",
        help="Claude CLI output format used by the dossier runner.",
    )
    parser.add_argument(
        "--wafer-synthesis-only",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="After deterministic Exa presearch, disable Wafer web tools and use Wafer only to synthesize from provided evidence.",
    )
    parser.add_argument(
        "--wafer-expansion-pass",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Run a second Wafer pass that expands search and strengthens evidence/quotes.",
    )
    parser.add_argument(
        "--wafer-braindump-pass",
        action=argparse.BooleanOptionalAction,
        default=False,
        help="Run a final Wafer pass that produces a quote-heavy evidence braindump in markdown.",
    )
    parser.add_argument(
        "--skip-wafer",
        action="store_true",
        help="Only build internal context files; skip Wafer research calls.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Do not call Wafer; print selected attendees and write seed artifacts only.",
    )
    return parser.parse_args()


def get_exa_api_key() -> str:
    return os.environ.get("EXA_API_KEY", "").strip() or discover_exa_api_key()


def load_env() -> None:
    load_dotenv(REPO_ROOT / ".env")


def connect_platform_db() -> psycopg2.extensions.connection:
    dsn = os.environ.get("PLATFORM_DATABASE_URL", "").strip()
    if not dsn:
        raise SystemExit("Missing PLATFORM_DATABASE_URL in env.")
    conn = psycopg2.connect(dsn)
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout TO 0")
    return conn


def fetch_attendees(
    conn: psycopg2.extensions.connection,
    *,
    event_filter: str | None,
    email: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    where_parts = [
        'ea."checkedIn" = TRUE',
        "up.email IS NOT NULL",
        "up.email <> ''",
    ]
    params: list[Any] = []

    if event_filter:
        where_parts.append(
            '(pe.title ILIKE %s OR pe.slug = %s OR pe.id::text = %s)'
        )
        params.extend([f"%{event_filter}%", event_filter, event_filter])

    if email:
        where_parts.append("LOWER(up.email) = %s")
        params.append(email.lower().strip())

    query = f"""
        WITH ranked AS (
            SELECT
                up."userId"::text AS user_id,
                LOWER(up.email) AS email,
                up."firstName" AS first_name,
                up."lastName" AS last_name,
                up."linkedinUsername" AS linkedin_username,
                up."githubUsername" AS github_username,
                up."xHandle" AS x_handle,
                up."siteUrl" AS site_url,
                up.description,
                up.details,
                up.location,
                ea.status AS applicant_status,
                ea."createdAt" AS applicant_created_at,
                pe.id::text AS event_id,
                pe.title AS latest_checked_in_event,
                pe.slug AS latest_checked_in_event_slug,
                pe."startDateTime" AS latest_checked_in_event_start,
                ROW_NUMBER() OVER (
                    PARTITION BY LOWER(up.email)
                    ORDER BY ea."createdAt" DESC
                ) AS rn
            FROM "UserProfile" up
            JOIN "EventApplicant" ea ON ea."userId" = up."userId"
            JOIN "PlatformEvent" pe ON pe.id = ea."eventId"
            WHERE {" AND ".join(where_parts)}
        )
        SELECT *
        FROM ranked
        WHERE rn = 1
        ORDER BY applicant_created_at DESC
        LIMIT %s
    """
    params.append(limit)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(query, params)
        rows = cur.fetchall()
    return [dict(row) for row in rows]


def load_people_file(path: str | Path) -> list[dict[str, Any]]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, list):
        raise SystemExit("--people-file must contain a JSON array of person objects")
    rows: list[dict[str, Any]] = []
    for idx, item in enumerate(payload):
        if not isinstance(item, dict):
            raise SystemExit(f"--people-file entry {idx} is not an object")
        rows.append(dict(item))
    return rows


def build_seed_people(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    people: list[dict[str, Any]] = []
    for row in rows:
        first = (row.get("first_name") or row.get("firstName") or "").strip()
        last = (row.get("last_name") or row.get("lastName") or "").strip()
        full_name = (
            row.get("name")
            or " ".join(part for part in (first, last) if part).strip()
            or row.get("email")
            or f"person-{len(people)+1}"
        )
        linkedin_username = (row.get("linkedin_username") or row.get("linkedinUsername") or "").strip()
        github_username = (row.get("github_username") or row.get("githubUsername") or "").strip()
        x_handle = (row.get("x_handle") or row.get("xHandle") or "").strip()
        linkedin_url = (row.get("linkedin_url") or row.get("linkedinUrl") or "").strip()
        github_url = (row.get("github_url") or row.get("githubUrl") or "").strip()

        person = {
            "name": full_name,
            "email": row.get("email") or "",
            "linkedin_url": (
                linkedin_url
                or (
                    f"https://www.linkedin.com/in/{linkedin_username}"
                    if linkedin_username
                    else ""
                )
            ),
            "github_url": (
                github_url or (f"https://github.com/{github_username}" if github_username else "")
            ),
            "x_handle": x_handle,
            "personal_website": row.get("site_url") or row.get("siteUrl") or row.get("personal_website") or "",
            "self_description": row.get("description") or row.get("self_description") or "",
            "location": row.get("location") or "",
            "platform_user_id": row.get("user_id") or row.get("userId") or "",
            "latest_checked_in_event": row.get("latest_checked_in_event") or "",
            "latest_checked_in_event_slug": row.get("latest_checked_in_event_slug") or "",
            "latest_checked_in_event_start": row.get("latest_checked_in_event_start"),
            "company": row.get("company") or "",
            "title": row.get("title") or "",
            "platform_details": row.get("details"),
            "_seed_row": row,
        }
        # Preserve any prefetched enrichment fields already attached to the row.
        for key, value in row.items():
            if key not in person:
                person[key] = value
        for key in (
            "company",
            "title",
            "role",
            "location",
            "self_description",
            "platform_user_id",
            "total_cv_events",
            "event_history",
            "hackathon_submissions",
            "judging_scores_received",
            "positions_with_companies",
            "linkedin_headline",
            "linkedin_bio",
            "linkedin_skills",
            "linkedin_projects",
            "publications",
            "certifications",
            "_prefetched_enrichment",
        ):
            if row.get(key) and not person.get(key):
                person[key] = row.get(key)
        people.append(person)
    return people


def enrich_people(people: list[dict[str, Any]], config: dict) -> list[dict[str, Any]]:
    prefetched = [p.get("_prefetched_enrichment") or {} for p in people if isinstance(p, dict)]
    # Presence of a provider key means queue prefetch already made an explicit
    # decision for that source, including intentionally disabling it to avoid
    # remote DB/API calls. Do not re-enable the provider remotely just because
    # the prefetched value is false.
    supabase_prefetched = bool(prefetched) and all("supabase" in meta for meta in prefetched)
    github_prefetched = bool(prefetched) and all("github" in meta for meta in prefetched)
    platform_prefetched = bool(prefetched) and all("platform_db" in meta for meta in prefetched)

    if config["enrichment"]["supabase"]["enabled"] and not supabase_prefetched:
        people = enrich_from_supabase(people, config)
    if config["enrichment"].get("github", {}).get("enabled") and not github_prefetched:
        people = enrich_from_github_api(people, config)
    if config["enrichment"].get("platform_db", {}).get("enabled") and not platform_prefetched:
        people = enrich_from_platform_db(people, config)
    return people


def hydrate_enrichment_config(config: dict[str, Any]) -> dict[str, Any]:
    supabase_cfg = config["enrichment"]["supabase"]
    if not supabase_cfg.get("url"):
        supabase_cfg["url"] = os.environ.get("SUPABASE_URL", "")
    if not supabase_cfg.get("key"):
        supabase_cfg["key"] = os.environ.get("SUPABASE_KEY", "")

    github_cfg = config["enrichment"]["github"]
    if not github_cfg.get("token"):
        github_cfg["token"] = os.environ.get("GITHUB_TOKEN", "")

    platform_cfg = config["enrichment"]["platform_db"]
    if not platform_cfg.get("dsn"):
        platform_cfg["dsn"] = os.environ.get("PLATFORM_DATABASE_URL", "")

    return config


def slugify(value: str) -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9]+", "-", value.strip().lower()).strip("-")
    return cleaned or "person"


def json_dump(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str) + "\n", encoding="utf-8")


def build_internal_context(person: dict[str, Any]) -> str:
    email = str(person.get("email") or "").strip().lower()
    email_local_part = email.split("@", 1)[0] if "@" in email else ""
    email_domain = email.split("@", 1)[1] if "@" in email else ""
    anchors = {
        "name": person.get("name"),
        "email": email,
        "email_local_part": email_local_part,
        "email_domain": email_domain,
        "linkedin_url": person.get("linkedin_url"),
        "github_url": person.get("github_url"),
        "x_handle": person.get("x_handle"),
        "personal_website": person.get("personal_website"),
        "company": person.get("company"),
        "title": person.get("title") or person.get("current_job"),
        "location": person.get("location"),
        "latest_checked_in_event": person.get("latest_checked_in_event"),
        "total_cv_events": person.get("total_cv_events"),
        "checkin_count": person.get("checkin_count"),
    }
    compact = {
        key: value
        for key, value in anchors.items()
        if value not in ("", None, [], {})
    }
    sections = [
        "INTERNAL IDENTITY ANCHORS",
        json.dumps(compact, indent=2, default=str),
        "",
        "INTERNAL PROFILE",
        format_profile(person),
    ]
    if person.get("platform_details"):
        sections.extend(["", "RAW PLATFORM DETAILS", json.dumps(person["platform_details"], indent=2, default=str)])
    return "\n".join(sections).strip()


def build_query_seeds(person: dict[str, Any]) -> list[str]:
    seeds: list[str] = []
    name = str(person.get("name") or "").strip()
    company = str(person.get("company") or "").strip()
    email = str(person.get("email") or "").strip().lower()
    email_local_part = email.split("@", 1)[0] if "@" in email else ""
    email_domain = email.split("@", 1)[1] if "@" in email else ""
    linkedin_url = str(person.get("linkedin_url") or "").strip()
    github_url = str(person.get("github_url") or "").strip()
    github_username = github_url.rstrip("/").rsplit("/", 1)[-1] if github_url else ""
    linkedin_username = linkedin_url.rstrip("/").rsplit("/", 1)[-1] if linkedin_url else ""
    event_name = str(person.get("latest_checked_in_event") or "").strip()
    aliases = company_aliases(company, email_domain)

    if linkedin_url:
        seeds.append(linkedin_url)
    if github_url:
        seeds.append(github_url)
    if linkedin_username:
        seeds.append(f"\"{linkedin_username}\"")
    if github_username:
        seeds.append(f"\"{github_username}\"")
    if name:
        seeds.append(f"\"{name}\"")
    if name and company:
        seeds.append(f"\"{name}\" \"{company}\"")
    if name and email_domain and email_domain not in FREE_EMAIL_DOMAINS:
        seeds.append(f"\"{name}\" site:{email_domain}")
    if email_local_part and email_domain and email_domain not in FREE_EMAIL_DOMAINS:
        seeds.append(f"\"{email_local_part}\" site:{email_domain}")
    if name and company:
        seeds.append(f"\"{name}\" \"{company}\" LinkedIn")
        seeds.append(f"\"{name}\" \"{company}\" team")
        seeds.append(f"\"{name}\" \"{company}\" about")
        seeds.append(f"\"{name}\" \"{company}\" post")
    if name and github_username:
        seeds.append(f"\"{name}\" \"{github_username}\"")
        seeds.append(f"\"{name}\" site:github.com")
    if name and linkedin_username:
        seeds.append(f"\"{name}\" \"{linkedin_username}\"")
        seeds.append(f"\"{name}\" site:linkedin.com")
    if name:
        seeds.append(f"\"{name}\" site:medium.com")
        seeds.append(f"\"{name}\" site:devpost.com")
        seeds.append(f"\"{name}\" site:substack.com")
        seeds.append(f"\"{name}\" site:huggingface.co")
    if name and event_name:
        seeds.append(f"\"{name}\" \"{event_name}\"")
        seeds.append(f"\"{name}\" \"{event_name}\" project")
        seeds.append(f"\"{name}\" \"{event_name}\" hackathon")
    for alias in aliases:
        seeds.append(f"\"{name}\" \"{alias}\"")
        seeds.append(f"\"{name}\" \"{alias}\" LinkedIn")
        if email_domain:
            seeds.append(f"\"{name}\" site:{email_domain} \"{alias}\"")
        if alias.lower().replace(" ", "") == "aivalley":
            seeds.append(f"\"{name}\" site:aivalley.ai")
    deduped: list[str] = []
    seen: set[str] = set()
    for seed in seeds:
        if seed and seed not in seen:
            seen.add(seed)
            deduped.append(seed)
    return deduped[:16]


def build_exact_exa_queries(person: dict[str, Any]) -> list[str]:
    queries: list[str] = []
    name = str(person.get("name") or "").strip()
    email = str(person.get("email") or "").strip().lower()
    email_local_part = email.split("@", 1)[0] if "@" in email else ""
    email_domain = email.split("@", 1)[1] if "@" in email else ""
    company = str(person.get("company") or "").strip()
    event_name = str(person.get("latest_checked_in_event") or "").strip()
    linkedin_url = str(person.get("linkedin_url") or "").strip()
    github_url = str(person.get("github_url") or "").strip()

    if linkedin_url:
        queries.append(linkedin_url)
    if github_url:
        queries.append(github_url)
    if name and company:
        queries.append(f"\"{name}\" \"{company}\"")
    if name and email_domain and email_domain not in FREE_EMAIL_DOMAINS:
        queries.append(f"\"{name}\" site:{email_domain}")
    if email_local_part and email_domain and email_domain not in FREE_EMAIL_DOMAINS:
        queries.append(f"\"{email_local_part}\" site:{email_domain}")
    if name and event_name:
        queries.append(f"\"{name}\" \"{event_name}\"")
    if name:
        queries.append(f"\"{name}\" site:github.com")
        queries.append(f"\"{name}\" site:linkedin.com")

    deduped: list[str] = []
    seen: set[str] = set()
    for query in queries:
        if query and query not in seen:
            seen.add(query)
            deduped.append(query)
    return deduped[:8]


def company_aliases(company: str, email_domain: str) -> list[str]:
    aliases: list[str] = []
    normalized_company = company.strip().lower()
    normalized_domain = email_domain.strip().lower()
    if "cerebral valley" in normalized_company or normalized_domain == "cerebralvalley.ai":
        aliases.extend(["Cerebral Valley", "CV", "AI Valley"])
    return aliases


def uses_plain_claude_launcher() -> bool:
    launcher = os.environ.get("CLAUDE_WAFER_BIN", "").strip()
    return bool(launcher) and Path(launcher).name.lower() == "claude"


def uses_codex_launcher() -> bool:
    explicit = os.environ.get("CLAUDE_WAFER_LAUNCHER_KIND", "").strip().lower()
    if explicit == "codex":
        return True
    launcher = os.environ.get("CLAUDE_WAFER_BIN", "").strip()
    return bool(launcher) and Path(launcher).name.lower() == "codex"


def uses_minimax_launcher() -> bool:
    explicit = os.environ.get("CLAUDE_WAFER_LAUNCHER_KIND", "").strip().lower()
    if explicit == "minimax":
        return True
    launcher = os.environ.get("CLAUDE_WAFER_BIN", "").strip().lower()
    return "minimax" in launcher


def build_direct_url_hypotheses(person: dict[str, Any]) -> list[str]:
    urls: list[str] = []
    email = str(person.get("email") or "").strip().lower()
    email_local_part = email.split("@", 1)[0] if "@" in email else ""
    company = str(person.get("company") or "").strip()
    email_domain = email.split("@", 1)[1] if "@" in email else ""
    github_url = str(person.get("github_url") or "").strip()
    x_handle = str(person.get("x_handle") or "").strip().lstrip("@")
    linkedin_url = str(person.get("linkedin_url") or "").strip()
    aliases = company_aliases(company, email_domain)

    for url in (linkedin_url, github_url, str(person.get("personal_website") or "").strip()):
        if url:
            urls.append(url)

    if "Cerebral Valley" in aliases:
        if email_local_part:
            urls.append(f"https://cerebralvalley.ai/u/{email_local_part}")
        if x_handle:
            urls.append(f"https://cerebralvalley.ai/u/{x_handle}")
        if github_url:
            github_user = github_url.rstrip("/").rsplit("/", 1)[-1]
            if github_user:
                urls.append(f"https://cerebralvalley.ai/u/{github_user}")
        urls.extend(
            [
                "https://www.aivalley.io/about",
                "https://www.linkedin.com/company/aivalleyio/",
            ]
        )

    deduped: list[str] = []
    seen: set[str] = set()
    for url in urls:
        if url and url not in seen:
            seen.add(url)
            deduped.append(url)
    return deduped[:8]


def has_sparse_public_anchors(person: dict[str, Any]) -> bool:
    rich_anchor_count = sum(
        1
        for key in ("linkedin_url", "github_url", "x_handle", "personal_website")
        if person.get(key)
    )
    return rich_anchor_count == 0


def resolve_tool_policy(args: argparse.Namespace) -> tuple[str, str, str]:
    if args.wafer_synthesis_only:
        return "claude", "", "mcp__exa__web_search_exa,mcp__exa__web_fetch_exa,WebSearch,WebFetch"

    web_mode = args.wafer_web_mode
    plain_claude = uses_plain_claude_launcher()
    minimax_mode = uses_minimax_launcher()
    exa_available = bool(os.environ.get("EXA_API_KEY", "").strip() or discover_exa_api_key())
    if not exa_available:
        if web_mode in {"exa", "mixed"} and (plain_claude or minimax_mode):
            web_mode = "claude"
        elif web_mode == "exa":
            web_mode = "mixed"

    allowed = args.allowed_tools.strip()
    disallowed = args.disallowed_tools.strip()
    if not allowed and not disallowed:
        if plain_claude and web_mode == "exa":
            allowed = "mcp__exa__web_search_advanced_exa"
            disallowed = "WebSearch,WebFetch"
        elif plain_claude and web_mode == "mixed":
            allowed = "mcp__exa__web_search_advanced_exa,WebSearch"
            disallowed = ""
        elif web_mode == "exa":
            allowed = "mcp__exa__web_search_exa,mcp__exa__web_fetch_exa"
            disallowed = "WebSearch,WebFetch"
        elif web_mode == "mixed":
            allowed = "mcp__exa__web_search_exa,mcp__exa__web_fetch_exa,WebSearch,WebFetch"
            disallowed = ""
        else:
            allowed = ""
            disallowed = ""
    return web_mode, allowed, disallowed


def discover_exa_api_key() -> str:
    return shared_get_exa_api_key()


def build_research_prompt(
    person: dict[str, Any],
    context: str,
    *,
    synthesis_only: bool,
    claude_model: str | None = None,
    web_mode: str = "exa",
) -> str:
    name = person.get("name") or person.get("email") or "Unknown attendee"
    zai_mode = (
        "claude-zai" in os.environ.get("CLAUDE_WAFER_BIN", "")
        or bool(os.environ.get("ZAI_MODEL_OVERRIDE", "").strip())
    )
    plain_claude_mode = uses_plain_claude_launcher() and not zai_mode
    minimax_mode = uses_minimax_launcher()
    codex_mode = uses_codex_launcher() and not zai_mode
    haiku_mode = "haiku" in (claude_model or "").strip().lower()
    exa_cli = resolve_exa_cli_path(codex_mode=codex_mode)
    exact_exa_queries = build_exact_exa_queries(person)
    native_web_only = web_mode == "claude"
    plain_claude_exa_only = plain_claude_mode and web_mode == "exa"
    plain_claude_mixed = plain_claude_mode and web_mode == "mixed"
    tool_instruction = (
        "Do not do additional web search. Deterministic Exa search/fetch has already been run for you. "
        "Use the supplied seed evidence, fetched pages, and internal context to synthesize the dossier."
        if synthesis_only
        else (
            "Native-web workflow: use the model's built-in web search and direct page reads as the only research path in this run. "
            "Do not depend on Exa. Start with exact identity-anchored searches, open the strongest first-party/profile pages, and keep the run moving even if external helpers are absent."
            if native_web_only
            else (
            "Web research workflow for Codex: start with built-in web_search and direct first-party/profile pages. "
            "Treat native Codex web tools as the primary lane. Use the local Exa CLI only as optional corroboration when it is available and useful. "
            "Do not block the run on Exa availability. "
            "When using the local Exa CLI, run a real search command immediately; do not call --help, --version, or other probe commands first."
            if codex_mode
            else (
            "Web research workflow for MiniMax: prefer the model's native web-search capability and direct page reads. "
            "Treat Exa as optional corroboration only. If native web search produces good direct pages, continue and write the dossier without waiting on Exa."
            if minimax_mode
            else (
            "Web research workflow: first call mcp__exa__web_search_advanced_exa with a small exact query and low result count. "
            "If you need full-page text and no Exa fetch MCP tool is present, use Bash with the local Exa helper scripts."
            if plain_claude_exa_only
            else (
                "Web research workflow: start with mcp__exa__web_search_advanced_exa using a small exact query and a low result count. "
                "If Exa is thin or misses a likely direct page, you may then use WebSearch for one or two exact follow-ups. "
                "Do not use WebFetch in this run; if you need page text, use Bash with the local Exa CLI contents command instead."
                if plain_claude_mixed
                else "Web research workflow: first call mcp__exa__web_search_exa, then call "
                "mcp__exa__web_fetch_exa on the strongest candidates. Do not say Exa is unavailable "
                "unless an actual tool call fails."
            )
            )
            )
            )
        )
    )
    query_seeds = build_query_seeds(person)
    sparse = has_sparse_public_anchors(person)
    aliases = company_aliases(str(person.get("company") or ""), str(person.get("email") or "").split("@", 1)[1] if "@" in str(person.get("email") or "") else "")
    direct_urls = build_direct_url_hypotheses(person)
    timebox = (
        "Use the supplied seed evidence first. "
        + (
            "Do not perform additional web search."
            if synthesis_only
            else (
                "Make at most 2 additional search/fetch attempts. If those do not produce attributable public evidence, return insufficient rather than exploring indefinitely."
                if zai_mode
                else "Make at most 3 additional search/fetch attempts. If those do not produce attributable public evidence, return insufficient rather than exploring indefinitely."
            )
        )
        if sparse
        else "Use the supplied seed evidence first. "
        + (
            "Do not perform additional web search."
            if synthesis_only
            else (
                "Make at most 1 additional search/fetch attempt before returning the best evidence-backed result."
                if zai_mode
                else "Make at most 2 additional search/fetch attempts before returning the best evidence-backed result."
            )
        )
    )
    query_seed_block = "\n".join(f"- {seed}" for seed in query_seeds)
    alias_block = "\n".join(f"- {alias}" for alias in aliases) if aliases else "- None"
    direct_url_block = "\n".join(f"- {url}" for url in direct_urls) if direct_urls else "- None"
    presearch = person.get("_seed_evidence") or {}
    overview_queries = presearch.get("overview_queries") or []
    overview_hits = presearch.get("overview_hits") or []
    search_queries = presearch.get("queries") or []
    search_hits = presearch.get("hits") or []
    fetched = presearch.get("fetched") or []
    overview_query_limit = 2 if zai_mode else 4
    overview_hit_limit = 2 if zai_mode else 3
    search_query_limit = 4 if zai_mode else 6
    search_hit_limit = 3 if zai_mode else 4
    fetched_limit = 3 if zai_mode else 4
    overview_hit_clip = 180 if zai_mode else 260
    search_hit_clip = 140 if zai_mode else 180
    fetched_clip = 180 if zai_mode else 260
    extra_search_plan_line = (
        "\n6. Because this run is using GLM-5 via Claude Code, keep tool use tight: prefer one exact-profile search, fetch the strongest direct pages, and then answer instead of looping."
        if zai_mode and not synthesis_only
        else ""
    )
    if haiku_mode and not synthesis_only:
        extra_search_plan_line += (
            "\n7. Because this run is using Claude Haiku, keep the search disciplined and Exa-first: only use mcp__exa__web_search_advanced_exa for web research, keep each query exact, inspect only the strongest 1-3 direct pages, and answer from those instead of broad exploration."
        )
    if native_web_only and not synthesis_only:
        extra_search_plan_line += (
            "\n7. This is a native-web run: use only built-in web search and direct page reads. Do not stall on Exa or mention Exa as a blocker."
        )
    if codex_mode and not synthesis_only:
        extra_search_plan_line += (
            "\n8. Because this run is using Codex, keep it tool-heavy and evidence-led: start with built-in web_search, direct profile pages, and direct org pages. Use the local Exa CLI only as an optional second lane for corroboration. Do not stall the run waiting on Exa."
        )
    if minimax_mode and not synthesis_only:
        extra_search_plan_line += (
            "\n9. Because this run is using MiniMax, prefer the model's native web-search tool and direct pages first. Exa is optional. If native search is giving usable first-party/profile pages, continue and write the dossier."
            "\n10. In MiniMax runs, do not stop at a sparse dossier after one blocked page. Push through to gather multiple concrete artifacts, profile snippets, repo pages, team pages, author pages, or event pages before concluding the footprint is thin."
        )
    exa_fallback_line = (
        f"\n- If Exa MCP tools are not present or are flaky in this runtime, use Bash with the local Exa CLI instead:"
        f"\n  {exa_cli} search --num-results 5 'EXACT QUERY'"
        f"\n  {exa_cli} contents 'https://example.com/page'"
        f"\n  {exa_cli} answer 'QUESTION ABOUT THE PERSON'"
        "\n- Prefer the local Exa CLI over generic curl scraping."
        "\n- Use `search --preset people` only for true person/profile discovery, especially LinkedIn-style lookups."
        "\n- For university, company, project, blog, or event corroboration, use plain `search` with no preset."
        "\n- Do not call the CLI with --help, --version, or any dry-run flag."
    )
    plain_claude_exa_rule = (
        "- This run is native-web-only. Use built-in web search and direct page reads as the only research path.\n"
        "- Start with exact identity anchors and prefer first-party/profile pages, org pages, GitHub repos, author bios, and public event/community pages.\n"
        "- Do not mention missing Exa access as a blocker; finish from native tools and the supplied context.\n"
        "- Keep browsing narrow and evidence-backed; do not wander once you have enough to write the dossier."
        if native_web_only
        else (
        "- If mcp__exa__web_search_advanced_exa is available, use it as the only web-research path. Do not use Bash, WebSearch, WebFetch, or generic browsing for research in this run.\n"
        "- If Exa MCP fails in this plain-Claude run, stop and return insufficient instead of switching to broader tools."
        if plain_claude_exa_only and not codex_mode
        else (
        "- In this Codex run, use native built-in web_search as the primary lane before finalizing.\n"
        "- Start with exact identity anchors, direct profile pages, and direct org pages, then keep exploring until the dossier is strong and evidence-backed.\n"
        "- Keep each query exact and identity-anchored.\n"
        "- Use the local Exa CLI only as optional corroboration in Codex runs.\n"
        f"- Use Bash commands like `{exa_cli} search --num-results 5 'EXACT QUERY'` and `{exa_cli} contents 'https://example.com/page'`.\n"
        f"- Use `{exa_cli} search --preset people ...` only for actual person/profile discovery. Do not use the `people` preset for generic domains like university, company, event, or project pages.\n"
        "- Never block the run on Exa; if Exa is thin, unavailable, or flaky, keep going with native web_search and direct pages.\n"
        "- Never spend a turn on --help, --version, or probing.\n"
        "- Keep browsing narrow and evidence-backed; do not wander once you have enough to write the dossier."
        if codex_mode
            else (
            "- In this MiniMax run, prefer native web-search and direct page reads.\n"
            "- Use Exa only as optional corroboration when it is clearly available and useful.\n"
            "- Never block the run on Exa.\n"
            "- Do at least 3 exact identity-anchored search or direct-page steps before concluding that external signal is thin, unless the first two already give strong direct evidence.\n"
            "- If you cannot verify a present-day role or company, keep harvesting attributable artifacts and move those into public_outputs and evidence_clips rather than returning a nearly empty dossier.\n"
            "- When role/company is uncertain but the person is real, produce a dense dossier with evidence-backed possible_updates, public_outputs, and evidence_clips instead of stopping at 'unverified'.\n"
            "- Keep browsing narrow and evidence-backed; do not wander once you have enough to write the dossier."
            if minimax_mode
            else (
            "- Start with Exa for exact identity search.\n"
            "- In this mixed run, you may use WebSearch only after Exa, only for exact identity follow-ups, and only when Exa is thin or ambiguous.\n"
            f"- If you need the text of a direct page, use `{exa_cli} contents 'https://example.com/page'` rather than WebFetch.\n"
            "- Keep Exa searches narrow: exact name plus one anchor, and usually `--num-results 3` or `5`.\n"
            "- Do not do broad open-ended browsing."
            if plain_claude_mixed
            else ""
            )
            )
        )
        )
    )
    exact_exa_query_block = "\n".join(f"- {query}" for query in exact_exa_queries) if exact_exa_queries else "- None"

    overview_query_block = "\n".join(f"- {query}" for query in overview_queries[:overview_query_limit]) if overview_queries else "- None"
    overview_hit_block = format_seed_item_lines(
        overview_hits,
        item_limit=overview_hit_limit,
        text_limit=overview_hit_clip,
        text_keys=("snippet", "summary"),
    )
    search_query_block = "\n".join(f"- {query}" for query in search_queries[:search_query_limit]) if search_queries else "- None"
    search_hit_block = format_seed_item_lines(
        search_hits,
        item_limit=search_hit_limit,
        text_limit=search_hit_clip,
        text_keys=("snippet", "summary"),
    )
    fetched_block = format_seed_item_lines(
        fetched,
        item_limit=fetched_limit,
        text_limit=fetched_clip,
        title_keys=("url", "title"),
        url_keys=("url",),
        text_keys=("excerpt_long", "excerpt", "summary"),
    )
    return f"""Build a professional update dossier for this Cerebral Valley attendee.

You must use the internal context below as the source of truth for identity anchors before attributing any web findings to this person.

Goal:
- verify identity first
- build the richest evidence-backed professional dossier you can
- separate confirmed facts from possible leads

Output:
- Return exactly one top-level JSON object and nothing else.
- Do not include prose before the opening `{{` or after the closing `}}`.
- The saved dossier file will be whatever JSON you return, so make that object the real dossier.
- Use this schema as the target shape:
  - `schema_version`
  - `subject`
  - `current_snapshot`
  - `professional_history`
  - `confirmed_updates`
  - `possible_updates`
  - `company_updates`
  - `public_outputs`
  - `evidence_clips`
  - `research_gaps`
- Field names should match that schema when possible.
- If a field is unknown, use `null`, `[]`, or `{{}}` instead of prose outside the JSON.
- Keep the shape stable:
  - `subject` must be an object, never a bare string
  - `current_snapshot` should be an object when possible, not only a paragraph string
  - list fields should stay lists, even when you only have 1 item
- Prefer explicit structured fields over burying facts in summary text:
  - `subject.name`
  - `subject.email`
  - `subject.identity_status`
  - `current_snapshot.current_role`
  - `current_snapshot.current_company`
  - `current_snapshot.location`
  - `current_snapshot.summary`
- For `public_outputs`, prefer objects with:
  - `title`
  - `type`
  - `url`
  - `date`
  - `why_relevant`
- For `evidence_clips`, prefer objects with:
  - `source`
  - `url`
  - `excerpt`
  - `why_it_matters`
- If you create or reference a local dossier file, still return the actual full JSON dossier object in the final answer. Never return only a note saying the file was written elsewhere.
- Make the current snapshot explicit when possible: if evidence supports a present-day role/company/location, spell them out as `current_role`, `current_company`, and `location` instead of burying them in narrative text.
- If present-day role/company cannot be externally verified, do not give up early. Fill the dossier with other attributable evidence: repositories, project pages, talks, hackathon pages, package pages, author bios, profiles, and concrete evidence clips.
- A thin public footprint does not justify a thin dossier. If you only have partial identity confirmation, make `possible_updates`, `public_outputs`, and `evidence_clips` denser rather than returning almost nothing.

Rules:
- do not guess
- {tool_instruction}
- If only Bash/Edit/Read are available and no mcp__exa__* tools appear, use the local Exa CLI via Bash instead of inventing missing MCP tools.{exa_fallback_line}
- {plain_claude_exa_rule}
- In Codex/CLI runs, do not spend turns grepping the local repo for the attendee name. The internal context, seed evidence, and fetched pages already contain the relevant local material.
- use the internal context as identity truth
- if identity is ambiguous, keep items in possible_updates
- prefer official/company/personal/research profile sources over mirrors
- only include evidence-backed statements
- every confirmed claim needs URL-backed evidence with short source-faithful excerpts
- prefer fuller outputs over overly compressed ones; when a source is important, give enough factual detail that an operator can understand the work without reopening the link
- for public_outputs and other confirmed artifacts, make `why_relevant` a compact but substantial 2-3 sentence factual summary of what the artifact is, what it does, and why it matters
- do not emit placeholder values like `Untitled`, `Unknown`, `N/A`, `TBD`, `link unavailable`, or empty-shell objects when the source can support something more concrete
- when you have a direct page but the page title is weak, derive a specific factual title from the page content instead of using a placeholder
- prioritize attributable outputs over generic mentions: repos, hackathon pages, demos, project writeups, posts, talks, author pages, package docs, team pages, paper/profile pages
- when the subject appears to be a founder/CEO/engineer/designer/etc. at a specific company right now, promote that into the current snapshot directly instead of leaving it only in `professional_history`
- confirmed_updates must be genuinely new external deltas, not restatements of baseline internal facts
- do not infer a role/company from a generic stealth page, logo asset, or weak LinkedIn snippet
- do not include process narration, completion notes, or meta commentary inside dossier fields
- do not include markdown fences
- do not include commentary like "here is the JSON" or "done"
- if you detect a different person with a similar name, keep that collision out of professional_history, public_outputs, and evidence_clips; mention it only in research_gaps if relevant
- aggressively harvest attributable artifacts: repo READMEs, project pages, demos, author bios, team pages, hackathon pages, speaker pages, package pages, portfolio pages
{timebox}

Search plan:
1. Start with the strongest direct anchors first: exact LinkedIn/GitHub/personal-site URLs and exact name.
2. Then search exact name plus employer/org/domain or event/community anchor from the internal context.
3. Prefer direct profile/org pages and already-fetched seed pages before broad exploration.
4. {"Do not perform additional web search beyond the supplied seed evidence." if synthesis_only else "Make only a small number of additional search/fetch attempts beyond the supplied seed evidence."}
5. If you still cannot verify identity, explain which exact search paths were attempted and return insufficient rather than exploring indefinitely.
{extra_search_plan_line}

Exact Exa queries to prefer first:
{exact_exa_query_block}

Suggested search seeds:
{query_seed_block}

Likely organization aliases:
{alias_block}

Direct URL hypotheses to fetch before giving up:
{direct_url_block}

General Exa overview queries already run for you:
{overview_query_block}

General Exa overview hits already found:
{overview_hit_block}

Deterministic Exa presearch queries already run for you:
{search_query_block}

Promising Exa search hits already found:
{search_hit_block}

Direct pages already fetched for you with longer excerpts:
{fetched_block}

ATTENDEE:
{name}

INTERNAL CONTEXT:
{context}
"""


def build_expansion_prompt(
    person: dict[str, Any],
    context: str,
    first_pass_dossier: dict[str, Any],
    *,
    synthesis_only: bool,
    claude_model: str | None = None,
    web_mode: str = "exa",
) -> str:
    name = person.get("name") or person.get("email") or "Unknown attendee"
    plain_claude_mode = uses_plain_claude_launcher()
    minimax_mode = uses_minimax_launcher()
    codex_mode = uses_codex_launcher()
    haiku_mode = "haiku" in (claude_model or "").strip().lower()
    exa_cli = resolve_exa_cli_path(codex_mode=codex_mode)
    native_web_only = web_mode == "claude"
    plain_claude_exa_only = plain_claude_mode and web_mode == "exa"
    plain_claude_mixed = plain_claude_mode and web_mode == "mixed"
    exact_exa_queries = build_exact_exa_queries(person)
    presearch = person.get("_seed_evidence") or {}
    overview_hits = presearch.get("overview_hits") or []
    fetched = presearch.get("fetched") or []
    overview_hit_block = format_seed_item_lines(
        overview_hits,
        item_limit=6,
        text_limit=260,
        text_keys=("snippet", "summary"),
    )
    fetched_block = format_seed_item_lines(
        fetched,
        item_limit=8,
        text_limit=400,
        title_keys=("url", "title"),
        url_keys=("url",),
        text_keys=("excerpt_long", "excerpt", "summary"),
    )
    plain_claude_haiku_rule = (
        "- In this native-web pass 2 run, improve the same dossier using only built-in web search and direct page reads.\n"
        "- Do not mention missing Exa access as a blocker; keep the run moving with native tools.\n"
        "- Make at most 2 additional exact identity-anchored searches before returning the stronger dossier."
        if native_web_only and not synthesis_only
        else (
        "- In this plain-Claude Haiku run, if Exa MCP is available, keep all web research inside Exa. If Exa fails, return the best evidence-backed dossier from seed evidence rather than switching tools.\n"
        "- In pass 2, make at most 2 additional Exa searches, each using an exact identity query from the list below."
        if plain_claude_exa_only and haiku_mode and not synthesis_only
        else (
        "- In this Codex run, pass 2 must improve the existing dossier instead of replacing it.\n"
            "- Use both corroboration lanes in pass 2: built-in web_search follow-ups and an Exa-backed path for the same identity.\n"
            f"- Prefer the local Exa CLI in pass 2, e.g. `{exa_cli} search --num-results 5 'EXACT QUERY'` and `{exa_cli} contents 'https://example.com/page'`.\n"
            f"- Use `{exa_cli} search --preset people ...` only when the target is a person/profile search. For university, company, event, project, or blog corroboration, use plain `{exa_cli} search`.\n"
            "- Only use Exa MCP directly if it is clearly available and behaving.\n"
            "- Never spend a turn on --help, --version, or probing.\n"
            "- Keep improving the same dossier until you have a strong evidence-backed result; merge the strongest new evidence into the existing dossier."
            if codex_mode and not synthesis_only
            else (
            "- In this plain-Claude Haiku mixed run, start with Exa and use WebSearch only for at most 1-2 exact identity follow-ups when Exa is thin.\n"
            "- In pass 2, make at most 2 additional searches total, anchored to the exact identity queries below."
            if plain_claude_mixed and haiku_mode and not synthesis_only
            else ""
            )
        )
        )
    )
    if minimax_mode and not synthesis_only:
        plain_claude_haiku_rule += (
            "\n- In this MiniMax pass 2 run, do not accept a sparse pass 1 dossier if additional exact search or direct-page reads could still improve it."
            "\n- Convert weak generic summaries into concrete evidence rows."
            "\n- If role/company remain uncertain, strengthen `possible_updates`, `public_outputs`, and `evidence_clips` with attributable artifacts and quotes."
        )
    exact_exa_query_block = "\n".join(f"- {query}" for query in exact_exa_queries[:6]) if exact_exa_queries else "- None"
    return f"""Expand and improve this professional update dossier for the same attendee.

This is pass 2. Pass 1 already produced a draft dossier. Your job now is to update and extend that same dossier, not replace it with a separate one.
1. {"Use the supplied Exa evidence and fetched pages to improve the dossier. Do not perform additional web search." if synthesis_only else "Expand the search further using Exa and any permitted web tools."}
2. Treat the pass 1 dossier as the base document. Preserve strong existing facts unless you find evidence that they should be downgraded or removed.
3. Append new evidence, stronger quotes, new artifacts, and better wording onto the existing dossier sections.
4. Stress-test weak claims and downgrade or remove them only when the evidence is genuinely weak.
5. Prefer first-party sources, org pages, profile pages, GitHub repos, author bios, and public event/community pages.
6. Be selective. Only keep the highest-signal updates and evidence.
7. {"This is a native-web pass 2 run: use only built-in web search and direct page reads, keep the searches exact, and do not mention Exa as a blocker." if native_web_only and not synthesis_only else ("Keep this Exa-first: use mcp__exa__web_search_advanced_exa for small exact searches only. Do not use Bash, WebSearch, or generic browsing if Exa MCP is available." if plain_claude_exa_only and not synthesis_only else ("Keep this Exa-first but mixed-capable: start with mcp__exa__web_search_advanced_exa, then use WebSearch only for exact follow-ups when needed. Do not use WebFetch; use the local Exa CLI contents command for direct page text." if plain_claude_mixed and not synthesis_only else "Keep the search tight and evidence-led."))}
8. {"Because this run is using Claude Haiku, prefer a few exact searches and strong direct pages over breadth. Do not chase broad media/news pages unless they clearly mention the exact subject and anchors." if haiku_mode and not synthesis_only else "Do not burn search budget on broad name-only exploration."}

Critical rules:
- Use the same identity standards as pass 1.
- Do not weaken identity quality just to add more findings.
- Remove or downgrade any claim that is only weakly supported.
- In Codex/CLI runs, do not spend turns grepping the local repo for the attendee name or seed strings. Treat the provided context and pass 1 dossier as the local source of truth.
- If a claim relies only on self-reported GitHub bio text, personal-site resume bullets, or Devpost self-description, prefer possible_updates unless a second source supports it.
- Do not use another person's profile, coworker page, or same-surname page as evidence for this subject.
- Prefer more evidence rows with short direct quotes in the excerpt field.
- {"Do not perform additional web search; work from the supplied seed evidence and fetched pages." if synthesis_only else "Search beyond the initial hits if the dossier still looks sparse."}
- Return exactly one revised top-level JSON dossier object and nothing else.
- Do not include prose before the opening `{{` or after the closing `}}`.
- Keep the same schema from pass 1: `schema_version`, `subject`, `current_snapshot`, `professional_history`, `confirmed_updates`, `possible_updates`, `company_updates`, `public_outputs`, `evidence_clips`, `research_gaps`.
- If you write a local file during pass 2, do not respond with only a completion note or file path. Return the full JSON dossier object itself in the final answer.
- Do not start over from scratch. Keep the useful pass 1 material and extend it.
- Keep the object shape stable:
  - `subject` stays an object, never a string
  - `current_snapshot` should stay an object when possible
  - `public_outputs` and `evidence_clips` stay as lists of objects
- Upgrade vague pass 1 fields instead of flattening them. If pass 1 had a useful role/company/location buried in prose, surface it into explicit fields rather than replacing the structure with a summary string.
- Keep the final dossier high-signal but not artificially tiny:
  - at most 8 confirmed_updates
  - at most 8 possible_updates
  - at most 15 public_outputs
  - at most 15 evidence_clips
  - prioritize evidence density and attributable artifact coverage over brevity
- Do not repeat low-signal company background unless it directly matters for the subject.
- Do not include process narration, completion notes, or meta commentary in any field.
- Do not include markdown fences.
- If you identify a likely different person with a similar name, exclude that source from public_outputs and evidence_clips.
- {plain_claude_haiku_rule}

Exact Exa queries to prefer first:
{exact_exa_query_block}

What to improve specifically:
- Increase evidence density.
- Add missing direct quotes from the strongest sources.
- Find incremental public outputs, repos, packages, demos, talks, hackathon pages, awards, or role/company changes that are actually attributable.
- Challenge any overconfident claim in the draft.
- Make the current snapshot more explicit. If the evidence supports a current role/company/location, write them directly into `current_snapshot` using fields like `current_role`, `current_company`, and `location` instead of leaving only a generic summary.
- Prefer specific role wording over vague labels like "builder" or "author" when the evidence supports something stronger.
- Replace placeholders with specifics wherever the evidence supports them. In particular, do not leave `Untitled` public outputs or empty `why_relevant` summaries if the underlying source page is readable.
- If pass 1 is thin, do not merely restate that it is thin. Use pass 2 to squeeze more attributable signal from direct profile pages, repo pages, author pages, org/team pages, and event/project pages.

ATTENDEE:
{name}

INTERNAL CONTEXT:
{context}

PASS 1 DRAFT DOSSIER:
{json.dumps(first_pass_dossier, indent=2, default=str)}

GENERAL OVERVIEW HITS ALREADY FOUND:
{overview_hit_block}

DIRECT PAGES ALREADY FETCHED:
{fetched_block}
"""


def build_braindump_prompt(
    person: dict[str, Any],
    context: str,
    final_dossier: dict[str, Any],
    seed_evidence: dict[str, Any] | None = None,
) -> str:
    name = person.get("name") or person.get("email") or "Unknown attendee"
    seed_evidence = seed_evidence or {}
    return f"""Build a research braindump for this attendee.

This is not the neat dossier. This is the evidence pack.

Goals:
1. Dump as much useful professional information as possible.
2. Include many short direct quotes from sources.
3. Prefer copied article/profile/repo text over your own summary whenever possible.
4. Make it easy for a human operator to skim and see what is real.

Rules:
- Output markdown only.
- No JSON.
- Do not call tools, do not search the web, and do not emit tool-call markup.
- Work only from the internal context, the final dossier, and the seed evidence below.
- No intro fluff like "let me" or "based on my research".
- Start immediately with `# {name}`.
- Prefer source-heavy bullets and quoted excerpts.
- If a claim is uncertain or self-reported, label it clearly.
- Include URLs inline.
- Include a section for:
  - Identity anchors
  - Current snapshot
  - Strongest evidence quotes
  - Repos / projects / public outputs
  - Career / company / school signals
  - Ambiguities and what is still unverified
- Keep the best direct quotes even if they are repetitive; this pass is for data acquisition, not elegance.

ATTENDEE:
{name}

INTERNAL CONTEXT:
{context}

FINAL DOSSIER SO FAR:
{json.dumps(final_dossier, indent=2, default=str)}

SEED EVIDENCE:
{json.dumps(seed_evidence, indent=2, default=str)}
"""


def collect_seed_evidence(person: dict[str, Any]) -> dict[str, Any]:
    return collect_person_evidence(person)


def extract_referenced_json_path(text: str, fallback_text: str | None = None) -> Path | None:
    sources = [text]
    if fallback_text:
        sources.append(fallback_text)

    cwd_roots: list[Path] = []
    seen_roots: set[str] = set()
    for source in sources:
        for match in re.finditer(r'"cwd":"([^"]+)"', source):
            root = Path(match.group(1))
            root_str = str(root)
            if root_str not in seen_roots:
                cwd_roots.append(root)
                seen_roots.add(root_str)
    current_root = Path.cwd()
    if str(current_root) not in seen_roots:
        cwd_roots.append(current_root)

    absolute_patterns = [
        r"`(/[^`\n]+\.json)`",
        r'"file_path":"(/[^"\n]+\.json)"',
        r"(/[^ \n]+\.json)",
    ]
    relative_patterns = [
        r"`([A-Za-z0-9._/-]+\.json)`",
        r'"file_path":"([A-Za-z0-9._/-]+\.json)"',
        r"\b([A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*\.json)\b",
    ]

    for source in sources:
        for pattern in absolute_patterns:
            for match in re.finditer(pattern, source):
                candidate = Path(match.group(1))
                if candidate.exists() and candidate.is_file():
                    return candidate
        for pattern in relative_patterns:
            for match in re.finditer(pattern, source):
                raw_candidate = match.group(1)
                if raw_candidate.startswith("/"):
                    continue
                for root in cwd_roots:
                    candidate = (root / raw_candidate).resolve()
                    if candidate.exists() and candidate.is_file():
                        return candidate
    return None


def iter_json_object_candidates(text: str) -> list[str]:
    candidates: list[str] = []
    start_positions = [idx for idx, ch in enumerate(text) if ch == "{"][:200]
    for start in start_positions:
        depth = 0
        in_string = False
        escape = False
        for idx in range(start, len(text)):
            ch = text[idx]
            if in_string:
                if escape:
                    escape = False
                elif ch == "\\":
                    escape = True
                elif ch == '"':
                    in_string = False
                continue
            if ch == '"':
                in_string = True
                continue
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    candidates.append(text[start : idx + 1])
                    break
    candidates.sort(key=len, reverse=True)
    return candidates


def looks_like_dossier_payload(payload: dict[str, Any]) -> bool:
    dossier_like_keys = {
        "subject",
        "current_snapshot",
        "professional_history",
        "confirmed_updates",
        "possible_updates",
        "company_updates",
        "public_outputs",
        "evidence_clips",
        "research_gaps",
        "identity_confidence",
        "identity_notes",
    }
    return any(key in payload for key in dossier_like_keys)


def recover_narrative_dossier(person: dict[str, Any], text: str) -> dict[str, Any]:
    name = str(person.get("name") or person.get("email") or "Unknown attendee").strip()
    email = str(person.get("email") or "").strip()
    lower = text.lower()

    identity_status = "verified" if "identity: confirmed" in lower or "identity confirmed" in lower else None
    identity_summary = clip_text(re.sub(r"\s+", " ", text).strip(), 1200)

    confirmed_updates: list[dict[str, Any]] = []
    numbered_pattern = re.compile(
        r"(?:^|\n)\s*(\d+)\.\s+\*\*(.+?)\*\*\s*[—-]\s*(.+?)(?=(?:\n\s*\d+\.\s+\*\*)|\Z)",
        re.DOTALL,
    )
    for match in numbered_pattern.finditer(text):
        title = re.sub(r"\s+", " ", match.group(2)).strip()
        body = re.sub(r"\s+", " ", match.group(3)).strip()
        if not title and not body:
            continue
        confirmed_updates.append(
            {
                "summary": title or clip_text(body, 180),
                "why_new": clip_text(body, 800),
                "evidence": [],
            }
        )

    if not confirmed_updates:
        bullet_pattern = re.compile(r"(?:^|\n)[-•]\s+(.+)")
        for match in bullet_pattern.finditer(text):
            bullet = re.sub(r"\s+", " ", match.group(1)).strip()
            if bullet:
                confirmed_updates.append({"summary": clip_text(bullet, 200), "evidence": []})
            if len(confirmed_updates) >= 8:
                break

    research_gaps = ["Recovered from narrative summary because the model did not return a clean JSON object."]

    referenced_json = extract_referenced_json_path(text)
    if referenced_json:
        research_gaps.append(f"Model referenced dossier file: {referenced_json}")

    return {
        "subject": {
            "name": name,
            "email": email,
            "identity_status": identity_status,
            "identity_summary": identity_summary,
            "anchors_used": [value for value in [email] if value],
        },
        "current_snapshot": {},
        "professional_history": [],
        "confirmed_updates": confirmed_updates[:8],
        "possible_updates": [],
        "company_updates": [],
        "public_outputs": [],
        "evidence_clips": [],
        "research_gaps": research_gaps,
    }


def extract_json_payload(text: str, person: dict[str, Any] | None = None, raw_text: str | None = None) -> dict[str, Any]:
    stripped = text.strip()
    if stripped:
        try:
            parsed = json.loads(stripped)
            if isinstance(parsed, dict):
                return parsed
        except json.JSONDecodeError:
            pass

    for candidate in iter_json_object_candidates(text):
        try:
            parsed = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(parsed, dict):
            return parsed

    referenced_json = extract_referenced_json_path(text, raw_text)
    if referenced_json:
        try:
            parsed = json.loads(referenced_json.read_text(encoding="utf-8"))
            if isinstance(parsed, dict):
                return parsed
        except Exception:
            pass

    if raw_text:
        for candidate in iter_json_object_candidates(raw_text):
            try:
                parsed = json.loads(candidate)
            except json.JSONDecodeError:
                continue
            if isinstance(parsed, dict) and looks_like_dossier_payload(parsed):
                return parsed

    if person is not None and stripped:
        return {
            "schema_version": "agent-dossier-v1",
            "parse_status": "non_json_agent_output",
            "subject": {
                "name": person.get("name"),
                "email": person.get("email"),
            },
            "agent_returned_text": stripped,
        }

    raise ValueError(f"No JSON object found in Wafer output.\nRAW OUTPUT:\n{text}")


def extract_claude_text_output(text: str, output_format: str) -> str:
    if output_format != "stream-json":
        return text

    assistant_chunks: list[str] = []
    result_candidates: list[str] = []
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if payload.get("type") == "assistant":
            message = payload.get("message") or {}
            for item in message.get("content") or []:
                if item.get("type") == "text" and item.get("text"):
                    assistant_chunks.append(str(item["text"]))
        if payload.get("type") == "item.completed":
            item = payload.get("item") or {}
            if item.get("type") == "agent_message" and item.get("text"):
                assistant_chunks.append(str(item["text"]))
        if payload.get("type") == "result" and payload.get("subtype") == "success":
            result_text = str(payload.get("result") or "").strip()
            if result_text:
                result_candidates.append(result_text)

    def score_candidate(candidate: str) -> tuple[int, int, int]:
        stripped = candidate.strip()
        if not stripped:
            return (0, 0, 0)
        if _looks_like_rate_limit_shell(stripped):
            return (0, 0, 0)
        try:
            parsed = json.loads(stripped)
        except json.JSONDecodeError:
            parsed = None
        if isinstance(parsed, dict):
            return (4, int(not _looks_like_process_narration(stripped)), len(stripped))
        if '"subject"' in stripped or '"confirmed_updates"' in stripped or '"public_outputs"' in stripped:
            return (3, int(not _looks_like_process_narration(stripped)), len(stripped))
        if stripped.startswith("{") and stripped.endswith("}"):
            return (2, int(not _looks_like_process_narration(stripped)), len(stripped))
        return (1, int(not _looks_like_process_narration(stripped)), len(stripped))

    text_candidates = list(result_candidates)
    for chunk in assistant_chunks:
        chunk = chunk.strip()
        if chunk:
            text_candidates.append(chunk)
    if text_candidates:
        return max(text_candidates, key=score_candidate)
    if assistant_chunks:
        return "\n".join(chunk for chunk in assistant_chunks if chunk.strip())
    return text


def _normalize_usage_payload(usage: dict[str, Any] | None) -> dict[str, Any]:
    usage = usage or {}
    server_tool_use = usage.get("server_tool_use") or {}
    cache_creation = usage.get("cache_creation") or {}
    return {
        "input_tokens": usage.get("input_tokens", 0) or 0,
        "output_tokens": usage.get("output_tokens", 0) or 0,
        "cached_input_tokens": usage.get("cached_input_tokens", 0) or 0,
        "cache_read_input_tokens": usage.get("cache_read_input_tokens", 0) or 0,
        "cache_creation_input_tokens": usage.get("cache_creation_input_tokens", 0) or 0,
        "ephemeral_1h_input_tokens": cache_creation.get("ephemeral_1h_input_tokens", 0) or 0,
        "ephemeral_5m_input_tokens": cache_creation.get("ephemeral_5m_input_tokens", 0) or 0,
        "web_search_requests": server_tool_use.get("web_search_requests", 0) or 0,
        "web_fetch_requests": server_tool_use.get("web_fetch_requests", 0) or 0,
        "service_tier": usage.get("service_tier"),
        "speed": usage.get("speed"),
    }


def _normalize_model_usage(model_usage: dict[str, Any] | None) -> dict[str, Any]:
    normalized: dict[str, Any] = {}
    for model_name, payload in (model_usage or {}).items():
        if not isinstance(payload, dict):
            continue
        normalized[str(model_name)] = {
            "input_tokens": payload.get("inputTokens", 0) or 0,
            "output_tokens": payload.get("outputTokens", 0) or 0,
            "cache_read_input_tokens": payload.get("cacheReadInputTokens", 0) or 0,
            "cache_creation_input_tokens": payload.get("cacheCreationInputTokens", 0) or 0,
            "web_search_requests": payload.get("webSearchRequests", 0) or 0,
            "cost_usd": payload.get("costUSD"),
            "context_window": payload.get("contextWindow"),
            "max_output_tokens": payload.get("maxOutputTokens"),
        }
    return normalized


def extract_usage_telemetry(
    raw_text: str,
    *,
    requested_model: str | None,
    phase: str,
    output_format: str,
) -> dict[str, Any]:
    last_usage: dict[str, Any] | None = None
    last_model_usage: dict[str, Any] | None = None
    actual_model: str | None = requested_model
    service_tier: str | None = None
    total_cost_usd: float | None = None
    session_id: str | None = None
    transcript_type = output_format

    for raw_line in raw_text.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            continue
        if not isinstance(payload, dict):
            continue

        if payload.get("type") == "thread.started":
            transcript_type = "codex-stream-json"
        elif payload.get("type") == "system":
            transcript_type = "claude-stream-json"

        if payload.get("session_id"):
            session_id = str(payload.get("session_id"))
        if payload.get("model"):
            actual_model = str(payload.get("model"))
        if payload.get("service_tier"):
            service_tier = str(payload.get("service_tier"))
        if payload.get("total_cost_usd") is not None:
            try:
                total_cost_usd = float(payload.get("total_cost_usd"))
            except Exception:
                pass
        if isinstance(payload.get("usage"), dict):
            last_usage = payload.get("usage")
            if payload["usage"].get("service_tier"):
                service_tier = str(payload["usage"].get("service_tier"))
        if isinstance(payload.get("modelUsage"), dict):
            last_model_usage = payload.get("modelUsage")

        message = payload.get("message") or {}
        if isinstance(message, dict):
            if message.get("model"):
                actual_model = str(message.get("model"))
            if isinstance(message.get("usage"), dict):
                last_usage = message.get("usage")
                if message["usage"].get("service_tier"):
                    service_tier = str(message["usage"].get("service_tier"))

    normalized_usage = _normalize_usage_payload(last_usage)
    normalized_model_usage = _normalize_model_usage(last_model_usage)
    if not actual_model and len(normalized_model_usage) == 1:
        actual_model = next(iter(normalized_model_usage))
    if not service_tier:
        service_tier = normalized_usage.get("service_tier")

    return {
        "phase": phase,
        "transcript_type": transcript_type,
        "requested_model": requested_model,
        "actual_model": actual_model,
        "session_id": session_id,
        "service_tier": service_tier,
        "total_cost_usd": total_cost_usd,
        "usage": normalized_usage,
        "model_usage": normalized_model_usage,
        "raw_transcript_bytes": len(raw_text.encode("utf-8", errors="ignore")),
    }


def aggregate_usage_telemetry(phases: dict[str, dict[str, Any]]) -> dict[str, Any]:
    aggregate = {
        "input_tokens": 0,
        "output_tokens": 0,
        "cached_input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "ephemeral_1h_input_tokens": 0,
        "ephemeral_5m_input_tokens": 0,
        "web_search_requests": 0,
        "web_fetch_requests": 0,
        "total_cost_usd": 0.0,
        "phases_counted": 0,
    }
    models: dict[str, dict[str, Any]] = {}
    for payload in phases.values():
        usage = payload.get("usage") or {}
        for key in (
            "input_tokens",
            "output_tokens",
            "cached_input_tokens",
            "cache_read_input_tokens",
            "cache_creation_input_tokens",
            "ephemeral_1h_input_tokens",
            "ephemeral_5m_input_tokens",
            "web_search_requests",
            "web_fetch_requests",
        ):
            aggregate[key] += int(usage.get(key) or 0)
        if payload.get("total_cost_usd") is not None:
            aggregate["total_cost_usd"] += float(payload["total_cost_usd"])
        aggregate["phases_counted"] += 1
        for model_name, model_usage in (payload.get("model_usage") or {}).items():
            bucket = models.setdefault(
                model_name,
                {
                    "input_tokens": 0,
                    "output_tokens": 0,
                    "cache_read_input_tokens": 0,
                    "cache_creation_input_tokens": 0,
                    "web_search_requests": 0,
                    "cost_usd": 0.0,
                },
            )
            for key in (
                "input_tokens",
                "output_tokens",
                "cache_read_input_tokens",
                "cache_creation_input_tokens",
                "web_search_requests",
            ):
                bucket[key] += int(model_usage.get(key) or 0)
            if model_usage.get("cost_usd") is not None:
                bucket["cost_usd"] += float(model_usage["cost_usd"])
    return {"usage": aggregate, "models": models}


def _looks_like_rate_limit_shell(text: str) -> bool:
    lowered = (text or "").strip().lower()
    if not lowered:
        return False
    needles = [
        "you've hit your limit",
        "you have hit your limit",
        '"type":"rate_limit_event"',
        '"error":"rate_limit"',
        '"ratelimittype":"five_hour"',
        '"subtype":"hook_started"',
    ]
    return any(needle in lowered for needle in needles)


def _has_meaningful_value(value: Any) -> bool:
    if value is None:
        return False
    if isinstance(value, str):
        normalized = value.strip()
        return bool(normalized) and normalized.lower() not in {"unknown", "none", "null", "n/a", "no summary", "untitled"}
    if isinstance(value, (list, dict)):
        return bool(value)
    return True


def _merge_scalar_dicts(base: dict[str, Any], delta: dict[str, Any]) -> dict[str, Any]:
    merged = dict(base)
    for key, value in delta.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_scalar_dicts(merged[key], value)
        elif _has_meaningful_value(value):
            merged[key] = value
        elif key not in merged:
            merged[key] = value
    return merged


def _item_merge_key(item: dict[str, Any]) -> str:
    for key in ("url", "title", "summary", "detail", "role", "quote", "excerpt", "organization", "company"):
        value = str(item.get(key) or "").strip().lower()
        if value:
            return f"{key}:{value}"
    return json.dumps(item, sort_keys=True, default=str)


def _merge_section_lists(base_items: Any, delta_items: Any) -> list[Any]:
    merged: list[Any] = []
    seen: dict[str, int] = {}
    for source in (base_items or [], delta_items or []):
        if not isinstance(source, list):
            continue
        for item in source:
            if isinstance(item, dict):
                key = _item_merge_key(item)
                if key in seen:
                    merged[seen[key]] = _merge_scalar_dicts(merged[seen[key]], item)
                else:
                    seen[key] = len(merged)
                    merged.append(dict(item))
            elif item not in merged:
                merged.append(item)
    return merged


def merge_dossiers(base: Any, delta: Any) -> Any:
    if isinstance(base, dict) and isinstance(delta, dict):
        merged = dict(base)
        for key, value in delta.items():
            if key in merged:
                merged[key] = merge_dossiers(merged[key], value)
            else:
                merged[key] = value
        return merged
    if isinstance(base, list) and isinstance(delta, list):
        return _merge_section_lists(base, delta)
    return delta if _has_meaningful_value(delta) else base


def _looks_like_process_narration(value: str) -> bool:
    normalized = value.strip().lower()
    return (
        normalized.startswith("let me ")
        or normalized.startswith("based on my ")
        or normalized.startswith("now i have enough evidence")
        or normalized.startswith("done.")
        or "has been written to `" in normalized
        or "the dossier above is complete" in normalized
        or "no further action needed" in normalized
        or "returned as a structured json object" in normalized
        or "key improvements in pass 2" in normalized
    )


def clip_text(value: str, limit: int) -> str:
    normalized = re.sub(r"\s+", " ", value or "").strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def format_seed_item_lines(
    items: Any,
    *,
    item_limit: int,
    text_limit: int,
    title_keys: tuple[str, ...] = ("title",),
    url_keys: tuple[str, ...] = ("url",),
    text_keys: tuple[str, ...] = ("snippet", "summary"),
) -> str:
    if not isinstance(items, list):
        return "- None"

    lines: list[str] = []
    for raw_item in items[:item_limit]:
        if isinstance(raw_item, dict):
            title = next((str(raw_item.get(key) or "").strip() for key in title_keys if raw_item.get(key)), "") or "Untitled"
            url = next((str(raw_item.get(key) or "").strip() for key in url_keys if raw_item.get(key)), "")
            text = next((str(raw_item.get(key) or "").strip() for key in text_keys if raw_item.get(key)), "")
            line = f"- {title}"
            if url:
                line += f" | {url}"
            if text:
                line += f" | {clip_text(text, text_limit)}"
            lines.append(line.strip())
        elif isinstance(raw_item, str) and raw_item.strip():
            lines.append(f"- {clip_text(raw_item.strip(), text_limit)}")
    return "\n".join(lines) if lines else "- None"


def looks_like_json_object_text(value: str) -> bool:
    stripped = (value or "").strip()
    return stripped.startswith("{") and stripped.endswith("}")


def normalize_dossier(person: dict[str, Any], dossier: dict[str, Any]) -> dict[str, Any]:
    cleaned = json.loads(json.dumps(dossier))

    raw_subject = cleaned.get("subject")
    subject = raw_subject if isinstance(raw_subject, dict) else {}
    if isinstance(raw_subject, str) and raw_subject.strip():
        subject["identity_summary"] = raw_subject.strip()
    raw_identity_summary = subject.get("identity_summary")
    if isinstance(raw_identity_summary, dict):
        summary_text = (
            raw_identity_summary.get("summary")
            or raw_identity_summary.get("status_note")
            or raw_identity_summary.get("status")
        )
        subject["identity_summary"] = str(summary_text or "").strip() or None
    elif isinstance(raw_identity_summary, list):
        pieces = [str(item).strip() for item in raw_identity_summary if str(item).strip()]
        subject["identity_summary"] = " | ".join(pieces) if pieces else None
    raw_identity_verification = subject.get("identity_verification")
    if isinstance(raw_identity_verification, dict):
        verification_status = str(raw_identity_verification.get("status") or "").strip()
        verification_summary = str(raw_identity_verification.get("summary") or "").strip()
        if verification_summary and not subject.get("identity_summary"):
            subject["identity_summary"] = verification_summary
        if verification_status and subject.get("identity_status") is None:
            lowered_status = verification_status.lower()
            if "verified" in lowered_status or "confirmed" in lowered_status:
                subject["identity_status"] = "confirmed"
            elif "likely" in lowered_status or "moderate" in lowered_status:
                subject["identity_status"] = "likely"
            elif "ambiguous" in lowered_status or "unresolved" in lowered_status:
                subject["identity_status"] = "ambiguous"
    if subject.get("identity_status") is None:
        if subject.get("identity_verified") is True:
            subject["identity_status"] = "confirmed"
        elif subject.get("identity_verified") is False:
            subject["identity_status"] = "ambiguous"
    if subject.get("identity_status") is None:
        confidence = str(cleaned.get("identity_confidence") or subject.get("identity_confidence") or "").strip().lower()
        if confidence in {"high", "strong", "confirmed", "verified"}:
            subject["identity_status"] = "confirmed"
        elif confidence in {"medium", "moderate", "likely"}:
            subject["identity_status"] = "likely"
        elif confidence in {"low", "weak", "ambiguous"}:
            subject["identity_status"] = "ambiguous"
    if not subject.get("identity_summary"):
        summary = subject.get("identity_verification") or subject.get("verification_method")
        if summary:
            subject["identity_summary"] = summary
    if _looks_like_process_narration(str(subject.get("identity_summary") or "")):
        replacement_summary = (
            cleaned.get("identity_notes")
            or subject.get("identity_notes")
            or subject.get("identity_verification")
            or subject.get("verification_method")
        )
        subject["identity_summary"] = str(replacement_summary or "").strip() or None
    if not subject.get("anchors_used"):
        anchors = []
        for key in ("email", "personal_site", "github_url", "linkedin_url"):
            value = subject.get(key)
            if value:
                anchors.append(value)
        if subject.get("github_handle"):
            anchors.append(subject["github_handle"])
        if subject.get("x_handle"):
            anchors.append(subject["x_handle"])
        if anchors:
            subject["anchors_used"] = anchors

    def coerce_dict_list(value: Any) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if isinstance(value, list):
            items.extend(item for item in value if isinstance(item, dict))
        elif isinstance(value, dict):
            for subkey, subvalue in value.items():
                if isinstance(subvalue, list):
                    for item in subvalue:
                        if isinstance(item, dict):
                            merged = dict(item)
                            merged.setdefault("type", str(subkey).replace("_", " "))
                            items.append(merged)
                elif isinstance(subvalue, dict):
                    merged = dict(subvalue)
                    merged.setdefault("type", str(subkey).replace("_", " "))
                    items.append(merged)
        return items

    def normalize_evidence_entries(item: dict[str, Any]) -> list[dict[str, Any]]:
        entries: list[dict[str, Any]] = []
        raw_evidence = item.get("evidence")
        if isinstance(raw_evidence, list):
            for raw in raw_evidence:
                if isinstance(raw, dict):
                    entries.append(dict(raw))
                elif isinstance(raw, str) and raw.strip():
                    entries.append({"url": raw.strip()})
        elif isinstance(raw_evidence, dict):
            entries.append(dict(raw_evidence))
        elif isinstance(raw_evidence, str) and raw_evidence.strip():
            entries.append({"url": raw_evidence.strip()})

        if not entries and item.get("evidence_url"):
            entries.append(
                {
                    "url": str(item.get("evidence_url") or "").strip(),
                    "excerpt": str(item.get("excerpt") or "").strip(),
                    "title": str(item.get("source") or item.get("source_type") or "").strip(),
                }
            )

        normalized_entries: list[dict[str, Any]] = []
        for entry in entries:
            normalized = dict(entry)
            if normalized.get("excerpt") is None and item.get("excerpt"):
                normalized["excerpt"] = item.get("excerpt")
            if not normalized.get("title") and item.get("source"):
                normalized["title"] = item.get("source")
            normalized_entries.append(normalized)
        return normalized_entries

    def normalize_section_items(raw_value: Any, *, fallback_summary_key: str | None = None) -> list[dict[str, Any]]:
        items = coerce_dict_list(raw_value)
        if isinstance(raw_value, list):
            for raw_item in raw_value:
                if isinstance(raw_item, str) and raw_item.strip():
                    items.append({"summary": raw_item.strip()})
        normalized_items: list[dict[str, Any]] = []
        for raw_item in items:
            item = dict(raw_item)
            if fallback_summary_key and item.get(fallback_summary_key) and not item.get("summary"):
                item["summary"] = item.get(fallback_summary_key)
            if item.get("fact") and not item.get("summary"):
                item["summary"] = item.get("fact")
            if item.get("company") and not item.get("organization"):
                item["organization"] = item.get("company")
            if item.get("title") and not item.get("role") and fallback_summary_key == "role":
                item["role"] = item.get("title")
            item["evidence"] = normalize_evidence_entries(item)
            normalized_items.append(item)
        return normalized_items

    snapshot = cleaned.get("current_snapshot")
    if not isinstance(snapshot, dict):
        snapshot = {}
    if snapshot.get("title") and not snapshot.get("current_role"):
        snapshot["current_role"] = snapshot.get("title")
    if snapshot.get("company") and not snapshot.get("current_company"):
        snapshot["current_company"] = snapshot.get("company")
    if snapshot.get("headline") and not snapshot.get("professional_focus"):
        snapshot["professional_focus"] = [snapshot.get("headline")]
    if not snapshot.get("current_role") or not snapshot.get("current_company"):
        history_items = coerce_dict_list(cleaned.get("professional_history") or [])
        currentish = []
        for item in history_items:
            status = str(item.get("status") or "").strip().lower()
            if status in {"current", "present", "active"}:
                currentish.append(item)
        if currentish:
            preferred = currentish[0]
            role = preferred.get("role") or preferred.get("title")
            org = preferred.get("organization") or preferred.get("company")
            if role and not snapshot.get("current_role"):
                snapshot["current_role"] = role
            if org and not snapshot.get("current_company"):
                snapshot["current_company"] = org
    cleaned["current_snapshot"] = snapshot

    outputs = normalize_section_items(cleaned.get("public_outputs") or [], fallback_summary_key="title")
    filtered_outputs: list[dict[str, Any]] = []
    for item in outputs:
        if item.get("name") and not item.get("title"):
            item["title"] = item["name"]
        if item.get("artifact_type") and not item.get("type"):
            item["type"] = item["artifact_type"]
        if item.get("description") and not item.get("summary"):
            item["summary"] = item["description"]
        joined = " ".join(
            str(item.get(key) or "") for key in ("title", "why_relevant", "url")
        ).lower()
        if "different person" in joined or "name collision" in joined:
            continue
        filtered_outputs.append(item)
    cleaned["public_outputs"] = filtered_outputs

    cleaned["professional_history"] = normalize_section_items(cleaned.get("professional_history") or [], fallback_summary_key="role")
    cleaned["confirmed_updates"] = normalize_section_items(cleaned.get("confirmed_updates") or [], fallback_summary_key="summary")
    cleaned["possible_updates"] = normalize_section_items(cleaned.get("possible_updates") or [], fallback_summary_key="summary")
    cleaned["company_updates"] = normalize_section_items(cleaned.get("company_updates") or [], fallback_summary_key="summary")

    raw_clips_value = cleaned.get("evidence_clips") or []
    raw_clips = coerce_dict_list(raw_clips_value)
    if isinstance(raw_clips_value, list):
        for raw_item in raw_clips_value:
            if isinstance(raw_item, str) and raw_item.strip():
                raw_clips.append({"quote": raw_item.strip(), "theme": "General"})
    clips: list[dict[str, Any]] = []
    for item in raw_clips:
        key_facts = item.get("key_facts")
        if isinstance(key_facts, list) and key_facts:
            for fact in key_facts:
                if isinstance(fact, str) and fact.strip():
                    clips.append(
                        {
                            "theme": item.get("source") or item.get("source_type") or "General",
                            "url": item.get("url") or item.get("source_url"),
                            "quote": fact.strip(),
                        }
                    )
        else:
            clips.append(item)
    deduped_clips = []
    seen_clip_keys: set[tuple[str, str]] = set()
    for item in clips:
        if item.get("excerpt") and not item.get("quote"):
            item["quote"] = item["excerpt"]
        if item.get("source_url") and not item.get("url"):
            item["url"] = item["source_url"]
        if item.get("source_type") and not item.get("theme"):
            item["theme"] = item["source_type"]
        quote = str(item.get("quote") or "").strip()
        url = str(item.get("url") or "").strip()
        if not quote or _looks_like_process_narration(quote):
            continue
        key = (quote, url)
        if key in seen_clip_keys:
            continue
        seen_clip_keys.add(key)
        deduped_clips.append(item)
    cleaned["evidence_clips"] = deduped_clips

    raw_gaps = cleaned.get("research_gaps") or []
    normalized_gaps: list[str] = []
    if isinstance(raw_gaps, list):
        for gap in raw_gaps:
            if isinstance(gap, str) and gap.strip():
                normalized_gaps.append(gap.strip())
            elif isinstance(gap, dict):
                headline = str(gap.get("gap") or gap.get("summary") or "").strip()
                reason = str(gap.get("reason") or "").strip()
                attempted = str(gap.get("search_attempted") or "").strip()
                pieces = [piece for piece in (headline, reason, attempted) if piece]
                if pieces:
                    normalized_gaps.append(" | ".join(pieces))
    cleaned["research_gaps"] = normalized_gaps

    if not subject.get("name") and person.get("name"):
        subject["name"] = person.get("name")
    if not subject.get("email") and person.get("email"):
        subject["email"] = person.get("email")
    cleaned["subject"] = subject

    return cleaned


def render_markdown_dossier(person: dict[str, Any], dossier: dict[str, Any]) -> str:
    subject = dossier.get("subject") or {}
    snapshot = dossier.get("current_snapshot") or {}
    lines = [
        f"# {person.get('name') or person.get('email')}",
        "",
        f"- Email: {person.get('email') or 'Unknown'}",
        f"- Identity status: {subject.get('identity_status') or 'unknown'}",
        f"- Identity summary: {subject.get('identity_summary') or 'No summary'}",
    ]

    anchors = subject.get("anchors_used") or []
    if anchors:
        lines.extend(["- Anchors used: " + "; ".join(str(anchor) for anchor in anchors)])

    lines.extend(
        [
            "",
            "## Current Snapshot",
            "",
            f"- Role: {snapshot.get('current_role') or 'Unknown'}",
            f"- Company: {snapshot.get('current_company') or 'Unknown'}",
            f"- Location: {snapshot.get('location') or 'Unknown'}",
        ]
    )

    focus = snapshot.get("professional_focus") or []
    if focus:
        lines.append(f"- Focus: {'; '.join(str(item) for item in focus)}")

    raw_history = dossier.get("professional_history") or []
    history: list[dict[str, Any]] = []
    if isinstance(raw_history, dict):
        for key in ("confirmed_roles", "roles", "history", "education"):
            bucket = raw_history.get(key) or []
            if isinstance(bucket, list):
                for item in bucket:
                    if isinstance(item, dict):
                        item = dict(item)
                        if key == "education" and not item.get("role"):
                            item["role"] = item.get("degree") or "Education"
                            item["organization"] = item.get("institution") or item.get("school_name") or "Unknown institution"
                            item["status"] = item.get("status") or "confirmed"
                        history.append(item)
    elif isinstance(raw_history, list):
        history = [item for item in raw_history if isinstance(item, dict)]
    lines.extend(["", "## Professional History", ""])
    if history:
        for item in history:
            role = item.get("role") or item.get("title") or "Unknown role"
            org = item.get("organization") or item.get("company") or item.get("institution") or item.get("school_name") or "Unknown org"
            period = item.get("period") or (
                " - ".join(
                    part for part in (
                        str(item.get("start_date") or item.get("from_date") or "").strip(),
                        str(item.get("end_date") or item.get("to_date") or "").strip() or "Present",
                    ) if part
                )
            ) or "Unknown period"
            status = item.get("status") or "unknown"
            lines.append(f"- {period} | {role} @ {org} [{status}]")
            for ev in (item.get("evidence") or [])[:2]:
                title = ev.get("title") or ev.get("url") or "evidence"
                url = ev.get("url") or ""
                excerpt = ev.get("excerpt") or ""
                piece = f"  evidence: {title}"
                if url:
                    piece += f" | {url}"
                if excerpt:
                    piece += f" | {excerpt}"
                lines.append(piece)
    else:
        lines.append("- None")

    for section_key, section_title in (
        ("confirmed_updates", "Confirmed Updates"),
        ("possible_updates", "Possible Updates"),
        ("company_updates", "Company Updates"),
        ("public_outputs", "Public Outputs"),
    ):
        raw_items = dossier.get(section_key) or []
        items: list[dict[str, Any]] = []
        if isinstance(raw_items, dict):
            for subkey, subvalue in raw_items.items():
                if isinstance(subvalue, list):
                    items.extend(item for item in subvalue if isinstance(item, dict))
                elif isinstance(subvalue, dict):
                    item = dict(subvalue)
                    item.setdefault("title", str(subkey).replace("_", " "))
                    items.append(item)
        elif isinstance(raw_items, list):
            items = [item for item in raw_items if isinstance(item, dict)]
        lines.extend(["", f"## {section_title}", ""])
        if not items:
            lines.append("- None")
            continue
        for item in items:
            summary = item.get("summary") or item.get("title") or "Untitled"
            lines.append(f"- {summary}")
            for extra_key in ("why_new", "why_uncertain", "date", "company", "type", "why_relevant"):
                extra_val = item.get(extra_key)
                if extra_val:
                    lines.append(f"  {extra_key}: {extra_val}")
            evidence = item.get("evidence") or []
            for ev in evidence[:3]:
                if isinstance(ev, dict):
                    title = ev.get("title") or ev.get("url") or "evidence"
                    url = ev.get("url") or ""
                    excerpt = ev.get("excerpt") or ""
                else:
                    title = str(ev).strip() or "evidence"
                    url = ""
                    excerpt = ""
                piece = f"  evidence: {title}"
                if url:
                    piece += f" | {url}"
                if excerpt:
                    piece += f" | {excerpt}"
                lines.append(piece)

    gaps = dossier.get("research_gaps") or []
    lines.extend(["", "## Research Gaps", ""])
    if gaps:
        lines.extend(f"- {gap}" for gap in gaps)
    else:
        lines.append("- None")

    clips = dossier.get("evidence_clips") or []
    lines.extend(["", "## Evidence Clips", ""])
    if clips:
        for item in clips[:12]:
            if isinstance(item, dict):
                theme = item.get("theme") or "General"
                quote = item.get("quote") or ""
                url = item.get("url") or ""
            else:
                theme = "General"
                quote = str(item).strip()
                url = ""
            line = f"- [{theme}] {quote}"
            if url:
                line += f" | {url}"
            lines.append(line)
    else:
        lines.append("- None")

    return "\n".join(lines) + "\n"


def render_research_braindump(
    person: dict[str, Any],
    dossier: dict[str, Any],
    context: str,
    seed_evidence: dict[str, Any] | None,
) -> str:
    raw_subject = dossier.get("subject")
    if isinstance(raw_subject, dict):
        subject = raw_subject
    elif isinstance(raw_subject, str) and raw_subject.strip():
        subject = {
            "identity_status": "unknown",
            "identity_summary": raw_subject.strip(),
        }
    else:
        subject = {}

    raw_snapshot = dossier.get("current_snapshot")
    if isinstance(raw_snapshot, dict):
        snapshot = raw_snapshot
    elif isinstance(raw_snapshot, str) and raw_snapshot.strip():
        snapshot = {"summary": raw_snapshot.strip()}
    else:
        snapshot = {}
    seed_evidence = seed_evidence or {}
    lines = [
        f"# {person.get('name') or person.get('email')}",
        "",
        "## Identity",
        "",
        f"- Email: {person.get('email') or 'Unknown'}",
        f"- Identity status: {subject.get('identity_status') or 'unknown'}",
        f"- Identity summary: {subject.get('identity_summary') or 'No summary'}",
    ]

    anchors = subject.get("anchors_used") or []
    if anchors:
        lines.extend(["", "## Identity Anchors", ""])
        lines.extend(f"- {anchor}" for anchor in anchors)

    lines.extend(
        [
            "",
            "## Current Snapshot",
            "",
            f"- Role: {snapshot.get('current_role') or 'Unknown'}",
            f"- Company: {snapshot.get('current_company') or 'Unknown'}",
            f"- Location: {snapshot.get('location') or 'Unknown'}",
        ]
    )
    if snapshot.get("summary"):
        lines.append(f"- Summary: {snapshot.get('summary')}")
    focus = snapshot.get("professional_focus") or []
    if focus:
        lines.append(f"- Focus: {'; '.join(str(item) for item in focus)}")

    def coerce_section_items(value: Any) -> list[dict[str, Any]]:
        items: list[dict[str, Any]] = []
        if isinstance(value, list):
            items.extend(item for item in value if isinstance(item, dict))
        elif isinstance(value, dict):
            for subkey, subvalue in value.items():
                if isinstance(subvalue, list):
                    for item in subvalue:
                        if isinstance(item, dict):
                            merged = dict(item)
                            merged.setdefault("type", str(subkey).replace("_", " "))
                            items.append(merged)
                elif isinstance(subvalue, dict):
                    merged = dict(subvalue)
                    merged.setdefault("type", str(subkey).replace("_", " "))
                    items.append(merged)
        return items

    def add_evidence_rows(items: Any, *, title: str, summary_key: str = "summary") -> None:
        normalized_items = coerce_section_items(items)
        lines.extend(["", f"## {title}", ""])
        if not normalized_items:
            lines.append("- None")
            return
        for item in normalized_items:
            header = item.get(summary_key) or item.get("title") or "Untitled"
            lines.append(f"- {header}")
            for key in ("category", "period", "organization", "role", "status", "why_new", "why_uncertain", "date", "company", "type", "why_relevant"):
                value = item.get(key)
                if value:
                    label = "summary" if key == "why_relevant" and title == "Public Outputs" else key
                    lines.append(f"  {label}: {value}")
            raw_evidence = item.get("evidence") or []
            if isinstance(raw_evidence, (dict, str)):
                evidence_items = [raw_evidence]
            elif isinstance(raw_evidence, list):
                evidence_items = raw_evidence
            else:
                evidence_items = []
            for ev in evidence_items:
                if isinstance(ev, dict):
                    title_val = ev.get("title") or ev.get("url") or "evidence"
                    url = ev.get("url") or ""
                    excerpt = ev.get("excerpt") or ""
                else:
                    title_val = str(ev).strip() or "evidence"
                    url = ""
                    excerpt = ""
                if url:
                    lines.append(f"  source: {title_val} | {url}")
                else:
                    lines.append(f"  source: {title_val}")
                if excerpt:
                    lines.append(f"  quote: {excerpt}")

    lines.extend([
        "",
        "## Analyst Summary",
        "",
        "This memo is intentionally dense. It preserves a broad, evidence-backed professional picture of the subject so an operator can assess them without reopening every source.",
    ])

    add_evidence_rows(dossier.get("professional_history") or [], title="Professional History", summary_key="role")
    add_evidence_rows(dossier.get("confirmed_updates") or [], title="Confirmed Updates")
    add_evidence_rows(dossier.get("possible_updates") or [], title="Possible Updates")
    add_evidence_rows(dossier.get("company_updates") or [], title="Company Updates")
    add_evidence_rows(dossier.get("public_outputs") or [], title="Public Outputs", summary_key="title")

    clips = dossier.get("evidence_clips") or []
    lines.extend(["", "## Evidence Clips", ""])
    if clips:
        for item in clips:
            if isinstance(item, dict):
                theme = item.get("theme") or "General"
                quote = item.get("quote") or ""
                url = item.get("url") or ""
            else:
                theme = "General"
                quote = str(item).strip()
                url = ""
            lines.append(f"- [{theme}] {quote}")
            if url:
                lines.append(f"  url: {url}")
    else:
        lines.append("- None")

    overview_queries = seed_evidence.get("overview_queries") or []
    lines.extend(["", "## Exa Overview Queries", ""])
    if overview_queries:
        lines.extend(f"- {query}" for query in overview_queries)
    else:
        lines.append("- None")

    overview_hits = seed_evidence.get("overview_hits") or []
    lines.extend(["", "## Exa Overview Hits", ""])
    if overview_hits:
        for item in overview_hits:
            if isinstance(item, dict):
                title_val = item.get("title") or "Untitled"
                url = item.get("url") or ""
                snippet = item.get("snippet") or item.get("summary") or ""
            else:
                title_val = "Untitled"
                url = ""
                snippet = str(item).strip()
            lines.append(f"- {title_val}")
            if url:
                lines.append(f"  url: {url}")
            if snippet:
                lines.append(f"  snippet: {snippet}")
    else:
        lines.append("- None")

    queries = seed_evidence.get("queries") or []
    lines.extend(["", "## Exa Query Seeds", ""])
    if queries:
        lines.extend(f"- {query}" for query in queries)
    else:
        lines.append("- None")

    hits = seed_evidence.get("hits") or []
    lines.extend(["", "## Exa Search Hits", ""])
    if hits:
        for item in hits:
            if isinstance(item, dict):
                title_val = item.get("title") or "Untitled"
                url = item.get("url") or ""
                snippet = item.get("snippet") or item.get("summary") or ""
            else:
                title_val = "Untitled"
                url = ""
                snippet = str(item).strip()
            lines.append(f"- {title_val}")
            if url:
                lines.append(f"  url: {url}")
            if snippet:
                lines.append(f"  snippet: {snippet}")
    else:
        lines.append("- None")

    fetched = seed_evidence.get("fetched") or []
    lines.extend(["", "## Exa Fetched Pages", ""])
    if fetched:
        for item in fetched:
            if isinstance(item, dict):
                url = item.get("url") or ""
                excerpt = item.get("excerpt_long") or item.get("excerpt") or ""
            else:
                url = ""
                excerpt = str(item).strip()
            lines.append(f"- {url or 'Untitled source'}")
            if excerpt:
                lines.append(f"  excerpt: {excerpt}")
    else:
        lines.append("- None")

    gaps = dossier.get("research_gaps") or []
    lines.extend(["", "## Research Gaps", ""])
    if gaps:
        lines.extend(f"- {gap}" for gap in gaps)
    else:
        lines.append("- None")

    lines.extend(["", "## Internal Context", "", context.strip()])
    return "\n".join(lines) + "\n"


def call_wafer_once(
    *,
    person: dict[str, Any],
    prompt_text: str,
    run_dir: Path,
    args: argparse.Namespace,
    phase: str,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    slug = slugify(person.get("email") or person.get("name") or "person")
    prompt_path = run_dir / "prompts" / f"{slug}.{phase}.txt"
    raw_dir = run_dir / "raw_wafer"
    stdout_capture_path = raw_dir / f"{slug}.{phase}.txt"
    last_message_path = raw_dir / f"{slug}.{phase}.combined.last-message.txt"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt_text, encoding="utf-8")
    wafer_agent_path = Path(args.wafer_agent).resolve()
    system_prompt_path = Path(args.system_prompt_file).resolve()

    web_mode, allowed_tools, disallowed_tools = resolve_tool_policy(args)
    cmd = [
        sys.executable,
        str(wafer_agent_path),
        "ask",
        "--prompt-file",
        str(prompt_path.resolve()),
        "--tools",
        "default",
        "--output-format",
        args.claude_output_format,
        "--wafer-web-mode",
        web_mode,
        "--append-system-prompt-file",
        str(system_prompt_path),
        "--stdout-file",
        str((raw_dir / f"{slug}.{phase}.txt").resolve()),
        "--stderr-file",
        str((raw_dir / f"{slug}.{phase}.stderr.txt").resolve()),
        "--combined-file",
        str((raw_dir / f"{slug}.{phase}.combined.txt").resolve()),
    ]
    if args.claude_model:
        cmd.extend(["--model", args.claude_model])
    if allowed_tools:
        cmd.extend(["--allowed-tools", allowed_tools])
    if disallowed_tools:
        cmd.extend(["--disallowed-tools", disallowed_tools])
    if args.fallback_model:
        cmd.extend(["--fallback-model", args.fallback_model])
    if args.effort:
        cmd.extend(["--effort", args.effort])
    if args.wafer_api_key_file:
        cmd.extend(["--wafer-api-key-file", args.wafer_api_key_file])

    env = os.environ.copy()
    if web_mode == "exa" and not env.get("EXA_API_KEY", "").strip():
        discovered_key = discover_exa_api_key()
        if discovered_key:
            env["EXA_API_KEY"] = discovered_key

    try:
        result = subprocess.run(
            cmd,
            cwd=str(wafer_agent_path.parent),
            text=True,
            capture_output=True,
            check=False,
            timeout=args.wafer_timeout_seconds,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Wafer dossier request timed out after {args.wafer_timeout_seconds}s."
        ) from exc
    raw_output = stdout_capture_path.read_text(encoding="utf-8") if stdout_capture_path.exists() else (result.stdout or "")
    extracted_output = extract_claude_text_output(raw_output, args.claude_output_format)
    if last_message_path.exists():
        last_message_text = last_message_path.read_text(encoding="utf-8").strip()
        if last_message_text and not _looks_like_rate_limit_shell(last_message_text):
            extracted_output = last_message_text
    telemetry = extract_usage_telemetry(
        raw_output,
        requested_model=args.claude_model,
        phase=phase,
        output_format=args.claude_output_format,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or extracted_output or raw_output or "Wafer dossier request failed.")
    return extract_json_payload(extracted_output, person, raw_output), raw_output, telemetry


def call_wafer_text_once(
    *,
    person: dict[str, Any],
    prompt_text: str,
    run_dir: Path,
    args: argparse.Namespace,
    phase: str,
) -> tuple[str, dict[str, Any]]:
    slug = slugify(person.get("email") or person.get("name") or "person")
    prompt_path = run_dir / "prompts" / f"{slug}.{phase}.txt"
    raw_dir = run_dir / "raw_wafer"
    stdout_capture_path = raw_dir / f"{slug}.{phase}.txt"
    last_message_path = raw_dir / f"{slug}.{phase}.combined.last-message.txt"
    prompt_path.parent.mkdir(parents=True, exist_ok=True)
    raw_dir.mkdir(parents=True, exist_ok=True)
    prompt_path.write_text(prompt_text, encoding="utf-8")
    wafer_agent_path = Path(args.wafer_agent).resolve()
    system_prompt_path = Path(args.system_prompt_file).resolve()

    web_mode, allowed_tools, disallowed_tools = resolve_tool_policy(args)
    if phase == "braindump":
        allowed_tools = ""
        braindump_blocklist = [
            "Bash",
            "WebSearch",
            "WebFetch",
            "mcp__exa__web_search_advanced_exa",
            "mcp__exa__web_search_exa",
            "mcp__exa__web_fetch_exa",
            "mcp__MiniMax__web_search",
        ]
        disallowed_tools = ",".join(braindump_blocklist)
    cmd = [
        sys.executable,
        str(wafer_agent_path),
        "ask",
        "--prompt-file",
        str(prompt_path.resolve()),
        "--tools",
        "default",
        "--output-format",
        args.claude_output_format,
        "--wafer-web-mode",
        web_mode,
        "--append-system-prompt-file",
        str(system_prompt_path),
        "--stdout-file",
        str((raw_dir / f"{slug}.{phase}.txt").resolve()),
        "--stderr-file",
        str((raw_dir / f"{slug}.{phase}.stderr.txt").resolve()),
        "--combined-file",
        str((raw_dir / f"{slug}.{phase}.combined.txt").resolve()),
    ]
    if args.claude_model:
        cmd.extend(["--model", args.claude_model])
    if allowed_tools:
        cmd.extend(["--allowed-tools", allowed_tools])
    if disallowed_tools:
        cmd.extend(["--disallowed-tools", disallowed_tools])
    if args.fallback_model:
        cmd.extend(["--fallback-model", args.fallback_model])
    if args.effort:
        cmd.extend(["--effort", args.effort])
    if args.wafer_api_key_file:
        cmd.extend(["--wafer-api-key-file", args.wafer_api_key_file])

    env = os.environ.copy()
    if web_mode == "exa" and not env.get("EXA_API_KEY", "").strip():
        discovered_key = discover_exa_api_key()
        if discovered_key:
            env["EXA_API_KEY"] = discovered_key

    try:
        result = subprocess.run(
            cmd,
            cwd=str(wafer_agent_path.parent),
            text=True,
            capture_output=True,
            check=False,
            timeout=args.wafer_timeout_seconds,
            env=env,
        )
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError(
            f"Wafer dossier request timed out after {args.wafer_timeout_seconds}s."
        ) from exc
    raw_output = stdout_capture_path.read_text(encoding="utf-8") if stdout_capture_path.exists() else (result.stdout or "")
    extracted_output = extract_claude_text_output(raw_output, args.claude_output_format)
    if last_message_path.exists():
        last_message_text = last_message_path.read_text(encoding="utf-8").strip()
        if last_message_text and not _looks_like_rate_limit_shell(last_message_text):
            extracted_output = last_message_text
    telemetry = extract_usage_telemetry(
        raw_output,
        requested_model=args.claude_model,
        phase=phase,
        output_format=args.claude_output_format,
    )
    if result.returncode != 0:
        raise RuntimeError(result.stderr or extracted_output or raw_output or "Wafer dossier request failed.")
    return extracted_output, telemetry


def run_wafer_research(
    *,
    person: dict[str, Any],
    context: str,
    run_dir: Path,
    args: argparse.Namespace,
) -> tuple[dict[str, Any], str, dict[str, Any]]:
    slug = slugify(person.get("email") or person.get("name") or "person")
    raw_dir = run_dir / "raw_wafer"
    raw_dir.mkdir(parents=True, exist_ok=True)
    resolved_web_mode = resolve_tool_policy(args)[0]

    prompt_text = build_research_prompt(
        person,
        context,
        synthesis_only=args.wafer_synthesis_only,
        claude_model=args.claude_model,
        web_mode=resolved_web_mode,
    )
    phase_usage: dict[str, dict[str, Any]] = {}

    first_dossier, first_raw, first_usage = call_wafer_once(
        person=person,
        prompt_text=prompt_text,
        run_dir=run_dir,
        args=args,
        phase="pass1",
    )
    phase_usage["pass1"] = first_usage
    (raw_dir / f"{slug}.pass1.txt").write_text(first_raw, encoding="utf-8")

    current_dossier = first_dossier
    current_raw = first_raw
    if not args.wafer_expansion_pass:
        (raw_dir / f"{slug}.txt").write_text(first_raw, encoding="utf-8")
    else:
        expansion_prompt = build_expansion_prompt(
            person,
            context,
            first_dossier,
            synthesis_only=args.wafer_synthesis_only,
            claude_model=args.claude_model,
            web_mode=resolved_web_mode,
        )
        try:
            second_dossier, second_raw, second_usage = call_wafer_once(
                person=person,
                prompt_text=expansion_prompt,
                run_dir=run_dir,
                args=args,
                phase="pass2",
            )
            phase_usage["pass2"] = second_usage
            (raw_dir / f"{slug}.pass2.txt").write_text(second_raw, encoding="utf-8")
            (raw_dir / f"{slug}.txt").write_text(second_raw, encoding="utf-8")
            current_dossier = merge_dossiers(first_dossier, second_dossier)
            current_raw = second_raw
        except Exception as exc:
            fallback_note = (
                first_raw
                + "\n\n=== PASS 2 FAILURE ===\n"
                + str(exc)
            )
            (raw_dir / f"{slug}.txt").write_text(fallback_note, encoding="utf-8")
            current_dossier = first_dossier
            current_raw = fallback_note

    if args.wafer_braindump_pass:
        try:
            braindump_prompt = build_braindump_prompt(
                person,
                context,
                current_dossier,
                person.get("_seed_evidence"),
            )
            braindump_text, braindump_usage = call_wafer_text_once(
                person=person,
                prompt_text=braindump_prompt,
                run_dir=run_dir,
                args=args,
                phase="braindump",
            )
            phase_usage["braindump"] = braindump_usage
            brain_dir = run_dir / "braindumps"
            brain_dir.mkdir(parents=True, exist_ok=True)
            (brain_dir / f"{slug}.md").write_text(braindump_text, encoding="utf-8")
            (raw_dir / f"{slug}.braindump.txt").write_text(braindump_text, encoding="utf-8")
        except Exception as exc:
            (raw_dir / f"{slug}.braindump.error.txt").write_text(str(exc), encoding="utf-8")

    return current_dossier, current_raw, phase_usage


def main() -> int:
    args = parse_args()
    load_env()
    if args.exa_api_key_file:
        raw_exa_keys = Path(args.exa_api_key_file).read_text(encoding="utf-8").strip()
        os.environ["EXA_API_KEYS"] = raw_exa_keys
        first_key = next((line.strip().strip("'\"") for line in re.split(r"[\n,]+", raw_exa_keys) if line.strip()), "")
        if first_key.startswith("EXA_API_KEY="):
            first_key = first_key.split("=", 1)[1].strip().strip("'\"")
        if first_key:
            os.environ["EXA_API_KEY"] = first_key
    output_root = Path(args.output_dir)
    run_dir = create_run_dir(output_root, args.run_id)
    save_meta(
        run_dir,
        event_filter=args.event,
        email_filter=args.email,
        limit=args.limit,
        wafer_web_mode=resolve_tool_policy(args)[0],
        skip_wafer=args.skip_wafer,
        dry_run=args.dry_run,
    )

    config = hydrate_enrichment_config(
        load_config(args.config if Path(args.config).exists() else None)
    )
    config["enrichment"]["platform_db"]["enabled"] = True

    if args.people_file:
        attendee_rows = load_people_file(args.people_file)
    else:
        conn = connect_platform_db()
        try:
            attendee_rows = fetch_attendees(
                conn,
                event_filter=args.event,
                email=args.email,
                limit=args.limit,
            )
        finally:
            conn.close()

    if not attendee_rows:
        raise SystemExit("No checked-in attendees found for the given filters.")

    json_dump(run_dir / "source_attendees.json", attendee_rows)
    people = build_seed_people(attendee_rows)
    people = enrich_people(people, config)
    json_dump(run_dir / "enriched_people.json", people)

    index_rows: list[dict[str, Any]] = []
    for person in people:
        slug = slugify(person.get("email") or person.get("name") or "person")
        person["_seed_evidence"] = collect_seed_evidence(person)
        (run_dir / "seed_evidence").mkdir(parents=True, exist_ok=True)
        json_dump(run_dir / "seed_evidence" / f"{slug}.json", person["_seed_evidence"])
        context = build_internal_context(person)
        (run_dir / "contexts").mkdir(parents=True, exist_ok=True)
        (run_dir / "contexts" / f"{slug}.md").write_text(context + "\n", encoding="utf-8")

        dossier_payload: dict[str, Any]
        raw_output = ""
        phase_usage: dict[str, dict[str, Any]] = {}
        if args.skip_wafer or args.dry_run:
            dossier_payload = {
                "subject": {
                    "name": person.get("name"),
                    "email": person.get("email"),
                    "identity_status": "not_run",
                    "identity_summary": "Wafer research skipped.",
                    "anchors_used": [],
                },
                "current_snapshot": {},
                "confirmed_updates": [],
                "possible_updates": [],
                "company_updates": [],
                "public_outputs": [],
                "research_gaps": ["Wafer research skipped."],
            }
        else:
            try:
                dossier_payload, raw_output, phase_usage = run_wafer_research(
                    person=person,
                    context=context,
                    run_dir=run_dir,
                    args=args,
                )
            except Exception as exc:
                raw_output = str(exc)
                dossier_payload = {
                    "subject": {
                        "name": person.get("name"),
                        "email": person.get("email"),
                        "identity_status": "error",
                        "identity_summary": f"Wafer research failed: {exc}",
                        "anchors_used": [],
                    },
                    "current_snapshot": {},
                    "confirmed_updates": [],
                    "possible_updates": [],
                    "company_updates": [],
                    "public_outputs": [],
                    "research_gaps": [f"Wafer research failed: {exc}"],
                }
            (run_dir / "raw_wafer").mkdir(parents=True, exist_ok=True)
            if not (run_dir / "raw_wafer" / f"{slug}.txt").exists():
                (run_dir / "raw_wafer" / f"{slug}.txt").write_text(raw_output, encoding="utf-8")

        dossier_dir = run_dir / "dossiers"
        dossier_dir.mkdir(parents=True, exist_ok=True)
        agent_output_text = extract_claude_text_output(raw_output, args.claude_output_format).strip() if raw_output else ""
        if agent_output_text:
            (dossier_dir / f"{slug}.agent.txt").write_text(agent_output_text + "\n", encoding="utf-8")
        json_dump(dossier_dir / f"{slug}.json", dossier_payload)
        md_body = agent_output_text or json.dumps(dossier_payload, indent=2, ensure_ascii=False, default=str)
        (dossier_dir / f"{slug}.md").write_text(md_body + ("\n" if not md_body.endswith("\n") else ""), encoding="utf-8")
        usage_dir = run_dir / "usage"
        usage_dir.mkdir(parents=True, exist_ok=True)
        usage_payload = {
            "run_id": run_dir.name,
            "name": person.get("name"),
            "email": person.get("email"),
            "requested_model": args.claude_model,
            "web_mode": resolve_tool_policy(args)[0],
            "phases": phase_usage,
            "aggregate": aggregate_usage_telemetry(phase_usage),
        }
        json_dump(usage_dir / f"{slug}.json", usage_payload)
        braindump_dir = run_dir / "research_memos"
        braindump_dir.mkdir(parents=True, exist_ok=True)
        (braindump_dir / f"{slug}.md").write_text(
            render_research_braindump(
                person,
                dossier_payload,
                context,
                person.get("_seed_evidence"),
            ),
            encoding="utf-8",
        )

        subject = dossier_payload.get("subject") or {}
        index_rows.append(
            {
                "name": person.get("name"),
                "email": person.get("email"),
                "latest_checked_in_event": person.get("latest_checked_in_event"),
                "identity_status": subject.get("identity_status"),
                "dossier_json": str((dossier_dir / f"{slug}.json").relative_to(run_dir)),
                "dossier_md": str((dossier_dir / f"{slug}.md").relative_to(run_dir)),
                "usage_json": str((usage_dir / f"{slug}.json").relative_to(run_dir)),
            }
        )

    json_dump(run_dir / "index.json", index_rows)
    telemetry_dir = run_dir / "telemetry"
    telemetry_dir.mkdir(parents=True, exist_ok=True)
    usage_files = sorted((run_dir / "usage").glob("*.json")) if (run_dir / "usage").exists() else []
    usage_payloads: list[dict[str, Any]] = []
    for path in usage_files:
        try:
            usage_payloads.append(json.loads(path.read_text(encoding="utf-8")))
        except Exception:
            continue
    json_dump(
        telemetry_dir / "run_summary.json",
        {
            "run_id": run_dir.name,
            "requested_model": args.claude_model,
            "web_mode": resolve_tool_policy(args)[0],
            "attendee_count": len(people),
            "index": index_rows,
            "usage": usage_payloads,
        },
    )
    print(f"run_dir: {run_dir}")
    print(f"attendees: {len(people)}")
    for row in index_rows:
        print(f"- {row['name']} <{row['email']}> [{row['identity_status']}]")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
