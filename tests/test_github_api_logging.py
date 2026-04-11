"""Tests for verbose logging in cv_rank.enrichment.github_api module."""

from __future__ import annotations

import logging
from typing import Any
from unittest.mock import patch


from cv_rank.enrichment.github_api import enrich_from_github_api


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

from helpers import make_person


def _make_person(name: str, github_url: str = "", has_data: bool = False) -> dict[str, Any]:
    extra = {}
    if has_data:
        extra["gh_api_stars"] = 100
        extra["gh_api_commits_year"] = 200
    return make_person(name, github_url=github_url, **extra)


def _make_config(token: str = "ghp_test123") -> dict[str, Any]:
    return {
        "enrichment": {
            "github": {
                "token": token,
                "batch_size": 5,
            }
        }
    }


def _mock_graphql_response(
    login: str = "testuser",
    total_stars: int = 500,
    total_repos: int = 25,
    commits: int = 300,
    language: str = "Python",
) -> dict:
    """Create a realistic GitHub GraphQL response."""
    return {
        "data": {
            "user": {
                "name": login,
                "bio": "Builder of things",
                "followers": {"totalCount": 100},
                "repositories": {
                    "totalCount": total_repos,
                    "nodes": [
                        {
                            "name": "awesome-repo",
                            "description": "An awesome repo",
                            "stargazerCount": total_stars,
                            "forkCount": 50,
                            "primaryLanguage": {"name": language},
                            "updatedAt": "2026-01-01T00:00:00Z",
                        }
                    ],
                },
                "contributionsCollection": {
                    "totalCommitContributions": commits,
                    "totalPullRequestContributions": 40,
                    "totalIssueContributions": 15,
                    "restrictedContributionsCount": 10,
                },
            }
        }
    }


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestEnrichFromGithubApiLogging:
    """Verify enrich_from_github_api logs per-person details and summary stats."""

    def test_logs_each_person_being_enriched(self, caplog):
        """Logs each person with their GitHub username during enrichment."""
        people = [
            _make_person("Alice Chen", "https://github.com/alicechen"),
            _make_person("Bob Martinez", "https://github.com/bobm"),
        ]
        config = _make_config()

        with patch("cv_rank.enrichment.github_api._github_graphql") as mock_gql:
            mock_gql.return_value = _mock_graphql_response()
            with caplog.at_level(logging.DEBUG, logger="cv_rank"):
                enrich_from_github_api(people, config)

        log_text = caplog.text
        # Should log each username
        assert "alicechen" in log_text
        assert "bobm" in log_text

    def test_logs_what_was_found(self, caplog):
        """After each person, logs stars, repos, commits, top language."""
        people = [_make_person("Alice Chen", "https://github.com/alicechen")]
        config = _make_config()

        with patch("cv_rank.enrichment.github_api._github_graphql") as mock_gql:
            mock_gql.return_value = _mock_graphql_response(
                total_stars=1500, total_repos=42, commits=890, language="Rust"
            )
            with caplog.at_level(logging.DEBUG, logger="cv_rank"):
                enrich_from_github_api(people, config)

        log_text = caplog.text
        # Should log stars found
        assert "1500" in log_text or "1,500" in log_text
        # Should log repos
        assert "42" in log_text
        # Should log commits
        assert "890" in log_text
        # Should log language
        assert "Rust" in log_text

    def test_logs_error_with_username(self, caplog):
        """On error, logs the specific GitHub username and error message."""
        people = [_make_person("Failing User", "https://github.com/failuser")]
        config = _make_config()

        with patch("cv_rank.enrichment.github_api._github_graphql") as mock_gql:
            mock_gql.side_effect = RuntimeError("API rate limit exceeded")
            with caplog.at_level(logging.DEBUG, logger="cv_rank"):
                enrich_from_github_api(people, config)

        log_text = caplog.text
        assert "failuser" in log_text
        assert "rate limit" in log_text.lower() or "error" in log_text.lower()

    def test_logs_enrichment_summary_stats(self, caplog):
        """At end, logs enrichment stats: count with repos, avg stars, top by stars."""
        people = [
            _make_person("Alice", "https://github.com/alice1"),
            _make_person("Bob", "https://github.com/bob1"),
            _make_person("Carol", "https://github.com/carol1"),
        ]
        config = _make_config()

        responses = [
            _mock_graphql_response(total_stars=5000, total_repos=30),
            _mock_graphql_response(total_stars=200, total_repos=10),
            _mock_graphql_response(total_stars=1000, total_repos=20),
        ]

        with patch("cv_rank.enrichment.github_api._github_graphql") as mock_gql:
            mock_gql.side_effect = responses
            with caplog.at_level(logging.DEBUG, logger="cv_rank"):
                enrich_from_github_api(people, config)

        log_text = caplog.text
        # Should log how many had repos
        assert "repos" in log_text.lower()
        # Should log average stars or top by stars
        assert "stars" in log_text.lower()
        # Should log top people by stars
        assert "top" in log_text.lower() or "Top" in log_text

    def test_logs_user_not_found(self, caplog):
        """When GitHub user is not found, logs this with username."""
        people = [_make_person("Ghost User", "https://github.com/ghostuser")]
        config = _make_config()

        with patch("cv_rank.enrichment.github_api._github_graphql") as mock_gql:
            mock_gql.return_value = {"data": {"user": None}}
            with caplog.at_level(logging.DEBUG, logger="cv_rank"):
                enrich_from_github_api(people, config)

        log_text = caplog.text
        assert "ghostuser" in log_text
        assert "not_found" in log_text.lower() or "not found" in log_text.lower()
