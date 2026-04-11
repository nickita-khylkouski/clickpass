"""
Shared type definitions for cv-rank.

Provides TypedDict definitions for the core data structures passed through
the pipeline.  Using ``total=False`` because enrichment adds fields
progressively — a person loaded from CSV has fewer fields than one enriched
from Supabase + GitHub.
"""

from __future__ import annotations

from typing import TypedDict


class PersonRecord(TypedDict, total=False):
    """Core person record flowing through the pipeline.

    Fields are added progressively by enrichment.  Only ``name`` is guaranteed
    to be present after CSV load.
    """

    # --- Identity (from CSV) ---
    name: str
    first_name: str
    last_name: str
    email: str

    # --- Social profiles (from CSV or enrichment) ---
    linkedin_url: str
    github_url: str
    x_handle: str

    # --- LinkedIn enrichment ---
    linkedin_headline: str
    linkedin_followers: int
    linkedin_connections: int

    # --- GitHub enrichment ---
    github_bio: str
    github_stars: int
    github_followers: int
    github_repos: int
    github_commits_year: int
    github_total_score: float
    github_implementation_score: float
    github_difficulty_score: float
    github_language_control_score: float
    github_best_languages: list[str]

    # --- Application data ---
    self_description: str
    ai_project: str
    looking_for_job: str
    years_experience: str
    is_founder: str
    is_big_tech: str
    is_student: str
    top_school: str
    education_level: str
    employment_category: str
    company: str
    role: str
    location: str
    publications: int
    certifications: int
    notable_achievements: str

    # --- Event-specific ---
    total_cv_events: int
    hackathon_submissions: int
    judging_scores_avg: str


class RankingRecord(TypedDict, total=False):
    """A single entry in the combined rankings output."""

    name: str
    rank: int
    final_score: float
    pointwise_score: float
    swiss_wins: int
    swiss_losses: int
    bt_strength: float
    verdict: str
    why: str
    best_number: str
    confidence: str


class PointwiseResult(TypedDict, total=False):
    """Result from scoring a single person."""

    name: str
    score: float | None
    score_raw: float | None  # preserved before debiasing
    confidence: str
    why: str
    strongest_signal: str
    concerns: str
    tokens_used: int
    error: str | None
