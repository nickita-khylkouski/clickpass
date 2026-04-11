"""
GitHub API enrichment via GraphQL.

Pulls repo stats, contribution data, and language info for people
with GitHub URLs who are missing GitHub data from the Supabase DB.

Ported from hackathon-elo/pipeline_v2/02_enrich_github.py.
"""

import json
import logging
import os
import time
from typing import Any

from cv_rank.utils import parse_github_username

logger = logging.getLogger("cv_rank")

try:
    import httpx
    _HAS_HTTPX = True
except ImportError:
    import urllib.request
    _HAS_HTTPX = False


# ---------------------------------------------------------------------------
# GraphQL query
# ---------------------------------------------------------------------------

GITHUB_GRAPHQL_QUERY = """
query($login: String!) {
  user(login: $login) {
    name
    bio
    followers { totalCount }
    repositories(first: 10, ownerAffiliations: OWNER, orderBy: {field: STARGAZERS, direction: DESC}) {
      totalCount
      nodes {
        name
        description
        stargazerCount
        forkCount
        primaryLanguage { name }
        updatedAt
      }
    }
    contributionsCollection {
      totalCommitContributions
      totalPullRequestContributions
      totalIssueContributions
      restrictedContributionsCount
    }
  }
}
"""


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------

def _github_graphql(token: str, query: str, variables: dict[str, Any]) -> dict:
    """Execute a GitHub GraphQL query using httpx (preferred) or urllib."""
    headers = {
        "Authorization": f"Bearer {token}",
        "Content-Type": "application/json",
    }
    body = json.dumps({"query": query, "variables": variables}).encode()

    if _HAS_HTTPX:
        resp = httpx.post(
            "https://api.github.com/graphql",
            headers=headers,
            content=body,
            timeout=30,
        )
        resp.raise_for_status()
        return resp.json()
    else:
        req = urllib.request.Request(
            "https://api.github.com/graphql",
            data=body,
            headers=headers,
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read())


def _github_rest_user(token: str, username: str) -> dict | None:
    """Fetch user profile from GitHub REST API for fields not in GraphQL.

    Returns a subset of fields: created_at, company, twitter_username, hireable.
    Returns None on any error (best-effort).
    """
    headers = {
        "Authorization": f"Bearer {token}",
        "Accept": "application/vnd.github+json",
    }
    url = f"https://api.github.com/users/{username}"

    try:
        if _HAS_HTTPX:
            resp = httpx.get(url, headers=headers, timeout=15)
            if resp.status_code != 200:
                return None
            data = resp.json()
        else:
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as resp:
                data = json.loads(resp.read())
        return {
            "created_at": data.get("created_at", ""),
            "company": (data.get("company") or "").strip().lstrip("@"),
            "twitter_username": data.get("twitter_username") or "",
            "hireable": data.get("hireable"),
        }
    except Exception:
        return None


def _enrich_one(person: dict, token: str, idx: int = 0, total: int = 0) -> bool:
    """Enrich a single person from GitHub API. Returns True if enriched."""
    name = person.get("name", "?")
    url = person.get("github_url", "")
    username = parse_github_username(url)
    if not username:
        return False

    # All three fields must be present to skip.  DB enrichment (supabase.py)
    # only sets gh_api_stars — followers and commits require a live API call.
    # Checking just stars would skip DB-enriched people who still need live data.
    if (
        person.get("gh_api_stars") is not None
        and person.get("gh_api_commits_year") is not None
        and person.get("gh_api_followers") is not None
    ):
        logger.debug(
            "    [%d/%d] %s (@%s): skipped -- already has full data",
            idx, total, name, username,
        )
        return True

    logger.info("    [%d/%d] Enriching: %s (@%s)", idx, total, name, username)

    try:
        result = _github_graphql(token, GITHUB_GRAPHQL_QUERY, {"login": username})
    except Exception as e:
        person["gh_api_error"] = str(e)[:100]
        logger.warning(
            "    [%d/%d] %s (@%s): error -- %s: %s",
            idx, total, name, username, type(e).__name__, str(e)[:150],
        )
        return False

    user = result.get("data", {}).get("user")
    if not user:
        person["gh_api_error"] = "user_not_found"
        logger.warning(
            "    [%d/%d] %s (@%s): user_not_found on GitHub",
            idx, total, name, username,
        )
        return False

    # Basic stats
    person["github_bio"] = user.get("bio") or person.get("github_bio", "")
    person["gh_api_followers"] = user["followers"]["totalCount"]

    # DB runs first and has richer repo data (500 non-fork repos, LLM-rated
    # quality scores).  API runs second for live stats (followers, commits/yr).
    # Without these guards, API's shallow data (top-10 only, forks included)
    # would overwrite the DB values.
    repos = user["repositories"]
    if person.get("gh_api_repos") is None:
        person["gh_api_repos"] = repos["totalCount"]
    else:
        logger.debug(
            "    %s: keeping DB gh_api_repos=%d (API would set %d)",
            name, person["gh_api_repos"], repos["totalCount"],
        )

    top_repos = []
    total_stars = 0
    for r in repos["nodes"]:
        stars = r["stargazerCount"]
        total_stars += stars
        top_repos.append({
            "name": r["name"],
            "description": r.get("description", ""),
            "stars": stars,
            "forks": r["forkCount"],
            "language": (
                r["primaryLanguage"]["name"]
                if r.get("primaryLanguage")
                else None
            ),
        })
    # DB sums stars from up to 500 non-fork repos; API only sums top-10.
    # Keep the higher (DB) value when present.
    if person.get("gh_api_stars") is None or total_stars > person["gh_api_stars"]:
        person["gh_api_stars"] = total_stars
    else:
        logger.debug(
            "    %s: keeping DB gh_api_stars=%d (API top-10 sum=%d)",
            name, person["gh_api_stars"], total_stars,
        )

    # Only set top repos if not already populated from DB (DB has richer data)
    if not person.get("gh_api_top_repos"):
        person["gh_api_top_repos"] = top_repos

    # Languages (dedupe, preserve order)
    if not person.get("github_best_languages"):
        langs = [r["language"] for r in top_repos if r.get("language")]
        person["github_best_languages"] = list(dict.fromkeys(langs))

    # Contributions
    contrib = user.get("contributionsCollection", {})
    person["gh_api_commits_year"] = contrib.get("totalCommitContributions", 0)
    person["gh_api_prs_year"] = contrib.get("totalPullRequestContributions", 0)
    person["gh_api_issues_year"] = contrib.get("totalIssueContributions", 0)
    person["gh_api_private_contributions"] = contrib.get("restrictedContributionsCount", 0)

    # Supplement with REST API for fields not in GraphQL
    rest = _github_rest_user(token, username)
    if rest:
        if rest["created_at"]:
            person["gh_account_created"] = rest["created_at"]
        if rest["company"] and not person.get("company"):
            person["company"] = rest["company"]
        if rest["twitter_username"] and not person.get("x_handle"):
            person["x_handle"] = rest["twitter_username"]

    top_lang = person.get("github_best_languages", [""])[0] if person.get("github_best_languages") else "N/A"
    logger.info(
        "    [%d/%d] %s (@%s): stars=%d, repos=%d, commits=%d, top_lang=%s",
        idx, total, name, username,
        total_stars, repos["totalCount"],
        person["gh_api_commits_year"], top_lang,
    )

    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def enrich_from_github_api(people: list[dict], config: dict) -> list[dict]:
    """Enrich *people* with GitHub profile data via the GraphQL API.

    Only enriches people who have a ``github_url`` but are missing
    GitHub data (e.g. ``gh_api_stars`` or ``gh_api_commits_year``).
    This avoids overwriting richer data already loaded from the
    Supabase DB.

    Parameters
    ----------
    people:
        List of person dicts (mutated in place and returned).
    config:
        Full configuration dict.  GitHub token is read from
        ``config["enrichment"]["github"]["token"]`` or falls back to
        the ``GITHUB_TOKEN`` environment variable.

    Returns
    -------
    list[dict]
        The same *people* list, enriched with GitHub API data.
    """
    gh_cfg = config.get("enrichment", {}).get("github", {})

    # Resolve token
    token = gh_cfg.get("token", "")
    if not token or (isinstance(token, str) and token.startswith("$")):
        token = os.environ.get("GITHUB_TOKEN", "")
    if not token:
        # Try gh CLI as last resort
        try:
            import subprocess
            r = subprocess.run(
                ["gh", "auth", "token"],
                capture_output=True,
                text=True,
            )
            if r.returncode == 0:
                token = r.stdout.strip()
        except Exception:
            pass

    if not token:
        logger.warning(
            "GitHub token not configured and GITHUB_TOKEN not set -- "
            "skipping GitHub API enrichment"
        )
        return people

    batch_size = gh_cfg.get("batch_size", 20)

    # Identify people who need API enrichment
    # Include DB-enriched people who are missing live stats (followers, commits)
    need_api = [
        (i, p)
        for i, p in enumerate(people)
        if parse_github_username(p.get("github_url"))
        and not p.get("gh_api_error")
        and (
            p.get("gh_api_stars") is None
            or p.get("gh_api_followers") is None
            or p.get("gh_api_commits_year") is None
        )
    ]

    has_github = sum(
        1 for p in people if parse_github_username(p.get("github_url", ""))
    )
    logger.info("  GitHub API: %d with URLs, %d need fresh data", has_github, len(need_api))

    if not need_api:
        return people

    enriched = 0
    errors = 0
    t_start = time.time()

    concurrency = gh_cfg.get("concurrency", 10)
    if concurrency > 1 and len(need_api) > 5:
        # Parallel enrichment using threads (I/O-bound HTTP requests)
        from concurrent.futures import ThreadPoolExecutor, as_completed

        logger.info("  GitHub API: parallel enrichment (concurrency=%d)", concurrency)

        def _enrich_worker(args: tuple) -> bool:
            count, (idx, person) = args
            return _enrich_one(person, token, idx=count, total=len(need_api))

        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = {}
            for count, item in enumerate(need_api, 1):
                # Stagger submission to avoid thundering herd
                if count > concurrency:
                    time.sleep(0.15)
                f = executor.submit(_enrich_worker, (count, item))
                futures[f] = count

            for f in as_completed(futures):
                count = futures[f]
                try:
                    if f.result():
                        enriched += 1
                    else:
                        errors += 1
                except Exception as e:
                    errors += 1
                    logger.warning("    GitHub API worker error: %s", e)

                done = enriched + errors
                if done % batch_size == 0:
                    elapsed = time.time() - t_start
                    logger.info(
                        "    GitHub API: %d/%d enriched=%d errors=%d (%.0fs)",
                        done, len(need_api), enriched, errors, elapsed,
                    )
    else:
        # Sequential fallback for small batches
        for count, (idx, person) in enumerate(need_api, 1):
            success = _enrich_one(person, token, idx=count, total=len(need_api))
            if success:
                enriched += 1
            else:
                errors += 1

            if count % batch_size == 0:
                elapsed = time.time() - t_start
                logger.info(
                    "    GitHub API: %d/%d enriched=%d errors=%d (%.0fs)",
                    count, len(need_api), enriched, errors, elapsed,
                )
                time.sleep(0.5)
            else:
                time.sleep(0.3)

    elapsed = time.time() - t_start
    pct = enriched * 100 // has_github if has_github > 0 else 0
    logger.info(
        "  GitHub API: %d/%d (%d%%) in %.0fs, errors=%d",
        enriched, has_github, pct, elapsed, errors,
    )

    # Log enrichment summary stats
    enriched_people = [
        p for p in people
        if p.get("gh_api_stars") is not None and not p.get("gh_api_error")
    ]
    if enriched_people:
        stars_list = [(p.get("name", "?"), p.get("gh_api_stars", 0)) for p in enriched_people]
        with_repos = sum(1 for p in enriched_people if (p.get("gh_api_repos") or 0) > 0)
        avg_stars = sum(s for _, s in stars_list) / len(stars_list) if stars_list else 0
        top_by_stars = sorted(stars_list, key=lambda x: x[1], reverse=True)[:5]

        logger.info(
            "  GitHub enrichment stats: %d with repos, avg stars=%.0f",
            with_repos, avg_stars,
        )
        if top_by_stars:
            top_str = ", ".join(f"{name} ({stars})" for name, stars in top_by_stars)
            logger.info("  Top by stars: %s", top_str)

    return people
