from __future__ import annotations

import json
import os
import re
from pathlib import Path
from threading import Lock
from typing import Any
from urllib.parse import parse_qs, urlparse

try:
    from exa_py import Exa

    EXA_AVAILABLE = True
except ImportError:
    EXA_AVAILABLE = False


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


def clip_text(value: str, limit: int) -> str:
    normalized = re.sub(r"\s+", " ", value or "").strip()
    if len(normalized) <= limit:
        return normalized
    return normalized[: limit - 1].rstrip() + "…"


def normalize_text(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).strip()


def split_tokens(value: str) -> list[str]:
    return [token for token in normalize_text(value).split() if token]


LOW_QUALITY_DOMAINS = {
    "contactout.com",
    "moneyinc.com",
    "eboona.com",
    "web3.bio",
}


PREFERRED_HOST_HINTS = (
    "linkedin.com",
    "github.com",
    "medium.com",
    "devpost.com",
    "huggingface.co",
    "scholar.google.com",
    "theorg.com",
    "about.me",
    "substack.com",
)

_EXA_KEY_CURSOR = 0
_EXA_KEY_CURSOR_LOCK = Lock()


def _split_exa_key_blob(raw: str) -> list[str]:
    keys: list[str] = []
    for line in re.split(r"[\n,]+", raw or ""):
        value = str(line or "").strip().strip("'\"")
        if not value or value.startswith("#"):
            continue
        if re.match(r"^EXA_API_KEY(?:S|_\d+)?=", value):
            value = value.split("=", 1)[1].strip().strip("'\"")
        if value and value not in keys:
            keys.append(value)
    return keys


def _read_exa_key_file(path: Path) -> list[str]:
    try:
        if not path.exists() or not path.is_file():
            return []
        return _split_exa_key_blob(path.read_text(encoding="utf-8"))
    except Exception:
        return []


def discover_exa_api_keys() -> list[str]:
    keys: list[str] = []
    for env_name in ("EXA_API_KEYS", "EXA_API_KEY"):
        for key in _split_exa_key_blob(os.environ.get(env_name, "")):
            if key not in keys:
                keys.append(key)
    indexed_env = sorted(name for name in os.environ if re.fullmatch(r"EXA_API_KEY_\d+", name))
    for env_name in indexed_env:
        for key in _split_exa_key_blob(os.environ.get(env_name, "")):
            if key not in keys:
                keys.append(key)
    key_file_candidates = [
        Path(os.environ.get("EXA_API_KEYS_FILE", "")).expanduser() if os.environ.get("EXA_API_KEYS_FILE") else None,
        Path.home() / ".claude-wafer" / "exa_keys.env",
        Path.home() / ".claude-zai" / "exa_keys.env",
    ]
    for candidate in key_file_candidates:
        if candidate is None:
            continue
        for key in _read_exa_key_file(candidate):
            if key not in keys:
                keys.append(key)

    candidates = [
        Path(os.environ.get("CLAUDE_WAFER_ROOT", "")).expanduser() / "state" / "config" / ".claude.json",
        Path.home() / ".claude-wafer" / "state" / "config" / ".claude.json",
        Path.home().parent / ".claude-wafer" / "state" / "config" / ".claude.json",
    ]
    seen: set[Path] = set()
    for config_path in candidates:
        if not str(config_path):
            continue
        config_path = config_path.resolve()
        if config_path in seen or not config_path.exists():
            continue
        seen.add(config_path)
        try:
            payload = json.loads(config_path.read_text(encoding="utf-8"))
        except Exception:
            continue

        projects = payload.get("projects") or {}
        for project_cfg in projects.values():
            if not isinstance(project_cfg, dict):
                continue
            exa_cfg = (project_cfg.get("mcpServers") or {}).get("exa")
            if not isinstance(exa_cfg, dict):
                continue
            env = exa_cfg.get("env") or {}
            for key in _split_exa_key_blob(str(env.get("EXA_API_KEY") or "")):
                if key and key not in keys:
                    keys.append(key)
            url = str(exa_cfg.get("url") or "").strip()
            if not url or "exaApiKey=" not in url:
                continue
            values = parse_qs(urlparse(url).query).get("exaApiKey") or []
            for value in values:
                value = str(value or "").strip()
                if value and value not in keys:
                    keys.append(value)
    return keys


def discover_exa_api_key() -> str:
    keys = discover_exa_api_keys()
    return keys[0] if keys else ""


def get_exa_api_key() -> str:
    keys = get_exa_api_keys()
    return keys[0] if keys else ""


def get_exa_api_keys() -> list[str]:
    return discover_exa_api_keys()


def is_exa_rate_limit_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "rate limit" in message
        or "too many requests" in message
        or '"code":-32000' in message
        or "429" in message
        or "quota" in message
    )


def is_exa_retryable_key_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return (
        "invalid api key" in message
        or "invalid_api_key" in message
        or "no_more_credits" in message
        or "credits limit" in message
        or "401" in message
        or "402" in message
        or "403" in message
        or "unauthorized" in message
        or "forbidden" in message
    )


def _rotated_exa_keys() -> list[str]:
    keys = get_exa_api_keys()
    if len(keys) <= 1:
        return keys
    global _EXA_KEY_CURSOR
    with _EXA_KEY_CURSOR_LOCK:
        start = _EXA_KEY_CURSOR % len(keys)
        _EXA_KEY_CURSOR += 1
    return keys[start:] + keys[:start]


def call_exa_with_rotation(operation_name: str, fn: Any) -> Any:
    keys = _rotated_exa_keys()
    if not keys:
        raise RuntimeError("EXA_API_KEY unavailable")
    last_exc: Exception | None = None
    for idx, key in enumerate(keys):
        try:
            return fn(Exa(api_key=key))
        except Exception as exc:
            last_exc = exc
            if idx < len(keys) - 1 and (is_exa_rate_limit_error(exc) or is_exa_retryable_key_error(exc)):
                continue
            raise
    if last_exc is not None:
        raise last_exc
    raise RuntimeError(f"{operation_name} failed without an Exa response")


def company_aliases(company: str, email_domain: str) -> list[str]:
    aliases: list[str] = []
    normalized_company = company.strip().lower()
    normalized_domain = email_domain.strip().lower()
    if "cerebral valley" in normalized_company or normalized_domain == "cerebralvalley.ai":
        aliases.extend(["Cerebral Valley", "CV", "AI Valley"])
    return aliases


def build_overview_queries(person: dict[str, Any]) -> list[str]:
    queries: list[str] = []
    name = str(person.get("name") or "").strip()
    company = str(person.get("company") or "").strip()
    title = str(person.get("title") or person.get("current_job") or "").strip()
    email = str(person.get("email") or "").strip().lower()
    email_domain = email.split("@", 1)[1] if "@" in email else ""
    linkedin_url = str(person.get("linkedin_url") or "").strip()
    github_url = str(person.get("github_url") or "").strip()
    x_handle = str(person.get("x_handle") or "").strip().lstrip("@")
    event_name = str(person.get("latest_checked_in_event") or "").strip()
    aliases = company_aliases(company, email_domain)
    linkedin_username = linkedin_url.rstrip("/").rsplit("/", 1)[-1] if linkedin_url else ""
    github_username = github_url.rstrip("/").rsplit("/", 1)[-1] if github_url else ""

    if name:
        queries.append(f'"{name}" profile bio portfolio resume')
        queries.append(f'"{name}" career background projects research')
    if linkedin_username:
        queries.append(f'"{linkedin_username}"')
        if name:
            queries.append(f'"{name}" "{linkedin_username}" profile')
    if github_username:
        queries.append(f'"{github_username}"')
        if name:
            queries.append(f'"{name}" "{github_username}" projects')
    if x_handle:
        queries.append(f'"{x_handle}"')
        if name:
            queries.append(f'"{name}" "{x_handle}"')
    if name and title:
        queries.append(f'"{name}" "{title}" profile')
    if name and company:
        queries.append(f'"{name}" "{company}" profile bio team')
        queries.append(f'"{name}" "{company}" projects research')
    if name and email_domain and email_domain not in FREE_EMAIL_DOMAINS:
        queries.append(f'"{name}" site:{email_domain} profile team about')
    if name and event_name:
        queries.append(f'"{name}" "{event_name}" builder researcher engineer')
    for alias in aliases:
        queries.append(f'"{name}" "{alias}" profile bio team')

    deduped: list[str] = []
    seen: set[str] = set()
    for query in queries:
        if query and query not in seen:
            seen.add(query)
            deduped.append(query)
    return deduped[:6]


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
        seeds.append(f'"{linkedin_username}"')
    if github_username:
        seeds.append(f'"{github_username}"')
    if name:
        seeds.append(f'"{name}"')
    if name and company:
        seeds.append(f'"{name}" "{company}"')
    if name and email_domain and email_domain not in FREE_EMAIL_DOMAINS:
        seeds.append(f'"{name}" site:{email_domain}')
    if email_local_part and email_domain and email_domain not in FREE_EMAIL_DOMAINS:
        seeds.append(f'"{email_local_part}" site:{email_domain}')
    if name and company:
        seeds.append(f'"{name}" "{company}" LinkedIn')
        seeds.append(f'"{name}" "{company}" team')
        seeds.append(f'"{name}" "{company}" about')
        seeds.append(f'"{name}" "{company}" post')
    if name and github_username:
        seeds.append(f'"{name}" "{github_username}"')
        seeds.append(f'"{name}" site:github.com')
    if name and linkedin_username:
        seeds.append(f'"{name}" "{linkedin_username}"')
        seeds.append(f'"{name}" site:linkedin.com')
    if name:
        seeds.append(f'"{name}" site:medium.com')
        seeds.append(f'"{name}" site:devpost.com')
        seeds.append(f'"{name}" site:substack.com')
        seeds.append(f'"{name}" site:huggingface.co')
    if name and event_name:
        seeds.append(f'"{name}" "{event_name}"')
        seeds.append(f'"{name}" "{event_name}" project')
        seeds.append(f'"{name}" "{event_name}" hackathon')
    for alias in aliases:
        seeds.append(f'"{name}" "{alias}"')
        seeds.append(f'"{name}" "{alias}" LinkedIn')
        if email_domain and email_domain not in FREE_EMAIL_DOMAINS:
            seeds.append(f'"{name}" site:{email_domain} "{alias}"')
        if alias.lower().replace(" ", "") == "aivalley":
            seeds.append(f'"{name}" site:aivalley.ai')

    deduped: list[str] = []
    seen: set[str] = set()
    for seed in seeds:
        if seed and seed not in seen:
            seen.add(seed)
            deduped.append(seed)
    return deduped[:16]


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


def build_identity_anchor_bundle(person: dict[str, Any]) -> dict[str, Any]:
    name = str(person.get("name") or "").strip()
    name_tokens = split_tokens(name)
    first_name = name_tokens[0] if name_tokens else ""
    last_name = name_tokens[-1] if len(name_tokens) >= 2 else ""
    email = str(person.get("email") or "").strip().lower()
    email_local_part = email.split("@", 1)[0] if "@" in email else ""
    email_domain = email.split("@", 1)[1] if "@" in email else ""
    email_domain_stem = email_domain.split(".", 1)[0].replace("-", " ").strip()
    linkedin_url = str(person.get("linkedin_url") or "").strip()
    github_url = str(person.get("github_url") or "").strip()
    x_handle = str(person.get("x_handle") or "").strip().lstrip("@")
    personal_website = str(person.get("personal_website") or "").strip()
    company = str(person.get("company") or "").strip()
    aliases = company_aliases(company, email_domain)

    linkedin_username = linkedin_url.rstrip("/").rsplit("/", 1)[-1] if linkedin_url else ""
    github_username = github_url.rstrip("/").rsplit("/", 1)[-1] if github_url else ""
    website_host = urlparse(personal_website).netloc.lower().replace("www.", "") if personal_website else ""

    return {
        "full_name": normalize_text(name),
        "first_name": first_name,
        "last_name": last_name,
        "name_tokens": name_tokens,
        "email_local_part": normalize_text(email_local_part),
        "email_domain": email_domain,
        "email_domain_stem": normalize_text(email_domain_stem),
        "linkedin_url": linkedin_url,
        "github_url": github_url,
        "linkedin_username": normalize_text(linkedin_username),
        "github_username": normalize_text(github_username),
        "x_handle": normalize_text(x_handle),
        "personal_website": personal_website,
        "website_host": website_host,
        "company": normalize_text(company),
        "aliases": [normalize_text(alias) for alias in aliases if alias],
    }


def score_hit(person: dict[str, Any], url: str, title: str, snippet: str) -> int:
    anchors = build_identity_anchor_bundle(person)
    combined = normalize_text(" ".join(filter(None, [url, title, snippet])))
    host = urlparse(url).netloc.lower().replace("www.", "")
    score = 0

    if url and url in {anchors["linkedin_url"], anchors["github_url"], anchors["personal_website"]}:
        score += 120
    if host and any(hint in host for hint in PREFERRED_HOST_HINTS):
        score += 10
    if host in LOW_QUALITY_DOMAINS:
        score -= 35

    full_name = anchors["full_name"]
    if full_name and full_name in combined:
        score += 30
    elif anchors["first_name"] and anchors["last_name"] and anchors["first_name"] in combined and anchors["last_name"] in combined:
        score += 22

    email_local_part = anchors["email_local_part"]
    usable_email_local_part = (
        email_local_part
        and email_local_part != anchors["first_name"]
        and len(email_local_part) >= 5
    )

    for key in ("linkedin_username", "github_username", "x_handle"):
        value = anchors[key]
        if value and value in combined:
            score += 45 if key in {"linkedin_username", "github_username"} else 30
    if usable_email_local_part and email_local_part in combined:
        score += 18

    if anchors["website_host"] and anchors["website_host"] in host:
        score += 35
    if anchors["email_domain"] and anchors["email_domain"] in url:
        score += 10
    if anchors["email_domain_stem"] and anchors["email_domain_stem"] in combined:
        score += 8
    if anchors["company"] and anchors["company"] in combined:
        score += 12
    for alias in anchors["aliases"]:
        if alias and alias in combined:
            score += 10

    strong_handle_anchor = any(
        value and value in combined
        for value in (
            anchors["linkedin_username"],
            anchors["github_username"],
            anchors["x_handle"],
        )
    ) or (usable_email_local_part and email_local_part in combined) or (anchors["website_host"] and anchors["website_host"] in host)

    weak_context_anchor = any(
        value and value in combined
        for value in (
            anchors["email_domain_stem"],
            anchors["company"],
            *anchors["aliases"],
        )
    ) or (anchors["email_domain"] and anchors["email_domain"] in url)

    rich_anchor_count = sum(
        1
        for value in (
            anchors["linkedin_username"],
            anchors["github_username"],
            anchors["x_handle"],
            anchors["email_local_part"],
            anchors["email_domain_stem"],
            anchors["company"],
            anchors["website_host"],
        )
        if value
    )

    if rich_anchor_count >= 2 and not strong_handle_anchor and not (full_name and full_name in combined and weak_context_anchor):
        score -= 70

    if anchors["last_name"] and anchors["last_name"] not in combined and not strong_handle_anchor:
        score -= 45
    if anchors["first_name"] and anchors["first_name"] not in combined and not strong_handle_anchor:
        score -= 20

    return score


def filter_and_rank_hits(person: dict[str, Any], hits: list[dict[str, Any]], *, limit: int) -> list[dict[str, Any]]:
    ranked: list[dict[str, Any]] = []
    for item in hits:
        score = score_hit(person, str(item.get("url") or ""), str(item.get("title") or ""), str(item.get("snippet") or item.get("summary") or ""))
        item = dict(item)
        item["score"] = score
        if score >= 10:
            ranked.append(item)

    ranked.sort(key=lambda item: (item.get("score") or 0, "query_type" in item and item.get("query_type") == "overview"), reverse=True)

    deduped: list[dict[str, Any]] = []
    seen_urls: set[str] = set()
    for item in ranked:
        url = str(item.get("url") or "")
        if not url or url in seen_urls:
            continue
        seen_urls.add(url)
        deduped.append(item)
        if len(deduped) >= limit:
            break
    return deduped


def collect_person_evidence(
    person: dict[str, Any],
    *,
    overview_queries_limit: int = 4,
    max_queries: int = 10,
    results_per_query: int = 5,
    max_hits: int = 18,
    max_fetch_urls: int = 12,
    fetch_excerpt_chars: int = 1200,
    fetch_excerpt_long_chars: int = 2600,
) -> dict[str, Any]:
    if not EXA_AVAILABLE:
        return {"queries": [], "hits": [], "fetched": [], "error": "exa_py unavailable"}

    exa_api_keys = get_exa_api_keys()
    if not exa_api_keys:
        return {"queries": [], "hits": [], "fetched": [], "error": "EXA_API_KEY unavailable"}
    overview_queries = build_overview_queries(person)
    queries = build_query_seeds(person)
    direct_urls = build_direct_url_hypotheses(person)
    raw_hits: list[dict[str, Any]] = []
    overview_hits: list[dict[str, Any]] = []
    seen_urls: set[str] = set()

    for query in overview_queries[:overview_queries_limit]:
        try:
            result = call_exa_with_rotation(
                "search",
                lambda exa: exa.search(query, type="auto", num_results=max(3, results_per_query - 1)),
            )
        except Exception:
            continue
        for item in getattr(result, "results", []) or []:
            url = getattr(item, "url", "") or ""
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            record = {
                "query": query,
                "query_type": "overview",
                "url": url,
                "title": getattr(item, "title", "") or "",
                "snippet": clip_text(getattr(item, "snippet", "") or getattr(item, "text", "") or "", 320),
                "summary": clip_text(getattr(item, "summary", "") or "", 320),
            }
            overview_hits.append(record)
            raw_hits.append(record)
            if len(raw_hits) >= max_hits:
                break
        if len(raw_hits) >= max_hits:
            break

    for query in queries[:max_queries]:
        try:
            result = call_exa_with_rotation(
                "search",
                lambda exa: exa.search(query, type="auto", num_results=results_per_query),
            )
        except Exception:
            continue
        for item in getattr(result, "results", []) or []:
            url = getattr(item, "url", "") or ""
            if not url or url in seen_urls:
                continue
            seen_urls.add(url)
            raw_hits.append(
                {
                    "query": query,
                    "query_type": "targeted",
                    "url": url,
                    "title": getattr(item, "title", "") or "",
                    "snippet": clip_text(getattr(item, "snippet", "") or getattr(item, "text", "") or "", 220),
                    "summary": clip_text(getattr(item, "summary", "") or "", 220),
                }
            )
            if len(raw_hits) >= max_hits:
                break
        if len(raw_hits) >= max_hits:
            break

    hits = filter_and_rank_hits(person, raw_hits, limit=max_hits)
    overview_hits = [item for item in hits if item.get("query_type") == "overview"][:8]

    fetch_urls: list[str] = []
    for url in direct_urls + [item["url"] for item in hits]:
        if url and url not in fetch_urls:
            fetch_urls.append(url)
        if len(fetch_urls) >= max_fetch_urls:
            break

    fetched: list[dict[str, Any]] = []
    if fetch_urls:
        try:
            content = call_exa_with_rotation(
                "get_contents",
                lambda exa: exa.get_contents(fetch_urls, text=True),
            )
            for idx, item in enumerate(getattr(content, "results", []) or []):
                text = getattr(item, "text", "") or ""
                if not text:
                    continue
                excerpt = clip_text(text, fetch_excerpt_chars)
                excerpt_long = clip_text(text, fetch_excerpt_long_chars)
                fetched.append(
                    {
                        "url": getattr(item, "url", "") or (fetch_urls[idx] if idx < len(fetch_urls) else ""),
                        "excerpt": excerpt,
                        "excerpt_long": excerpt_long,
                        "text_char_count": len(re.sub(r"\s+", " ", text).strip()),
                    }
                )
        except Exception as exc:
            return {
                "overview_queries": overview_queries[:overview_queries_limit],
                "overview_hits": overview_hits,
                "queries": queries[:max_queries],
                "hits": hits,
                "fetched": fetched,
                "error": str(exc),
            }

    return {
        "overview_queries": overview_queries[:overview_queries_limit],
        "overview_hits": overview_hits,
        "queries": queries[:max_queries],
        "hits": hits,
        "fetched": fetched,
    }
