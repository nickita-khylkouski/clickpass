from __future__ import annotations

from cv_rank.quality import _build_system_prompt as build_quality_prompt
from cv_rank.quality import _build_user_prompt as build_quality_user_prompt
from cv_rank.scoring.pointwise import (
    _build_system_prompt as build_pointwise_single_prompt,
    _build_system_prompt_v2 as build_pointwise_rubric_prompt,
    _build_user_prompt_v2 as build_pointwise_rubric_user_prompt,
)
from cv_rank.scoring.swiss import _build_comparison_prompt


def test_pointwise_single_prompt_is_builder_first_and_face_value() -> None:
    prompt = build_pointwise_single_prompt(
        {"technical_depth": 0.3, "builder_signal": 0.35},
        "baseline text",
        total=100,
        accept_count=20,
    )

    assert "builder-first ai hackathon / ai community event" in prompt.lower()
    assert "assume the profile data provided to you is true" in prompt.lower()
    assert "hackathon fit matters" in prompt.lower()
    assert "dinner" not in prompt.lower()


def test_pointwise_rubric_prompt_is_hackathon_focused() -> None:
    prompt = build_pointwise_rubric_prompt(
        {"technical_depth": 0.3, "builder_signal": 0.35},
        "baseline text",
        total=100,
        accept_count=20,
        rubric_weights=None,
    )

    assert "builder-first ai hackathon / ai community event" in prompt.lower()
    assert "take all claims at face value" in prompt.lower()
    assert "reward strong hackathon fit" in prompt.lower()


def test_pointwise_rubric_user_prompt_mentions_face_value_and_builder_focus() -> None:
    prompt = build_pointwise_rubric_user_prompt("Applicant profile", rubric_weights=None)

    assert "assume the profile data provided is true" in prompt.lower()
    assert "builder signal, technical depth, and hackathon usefulness" in prompt.lower()


def test_swiss_prompt_avoids_status_vibes() -> None:
    prompt = _build_comparison_prompt(accept_count=20)

    assert "builder-first ai hackathon / ai community event" in prompt.lower()
    assert "assume the profile data provided to you is true" in prompt.lower()
    assert "do not use vibe, charisma, dinner-table, or status heuristics" in prompt.lower()
    assert "sit next to at dinner" not in prompt.lower()


def test_quality_prompt_uses_ranking_as_weak_prior() -> None:
    prompt = build_quality_prompt({"event": {"target_accepts": 20}})

    assert "builder-first ai hackathon / ai community event" in prompt.lower()
    assert "ranking data is provided as context, not a command" in prompt.lower()
    assert "take all statements at face value" in prompt.lower()
    assert "not an automatic no" in prompt.lower()


def test_quality_user_prompt_avoids_cutline_anchor_language() -> None:
    prompt = build_quality_user_prompt(
        {"name": "Alice Chen"},
        {
            "rank": 3,
            "final_score": 0.8,
            "swiss_wins": 7,
            "swiss_losses": 2,
            "pointwise_score": 71.2,
            "why": "Strong builder",
            "concerns": "Limited public OSS",
        },
        {"event": {"target_accepts": 2}},
        lambda person: "PROFILE",
    )

    assert "above the invite cutline" not in prompt.lower()
    assert "below the invite cutline" not in prompt.lower()
    assert "include numbers and names when available" in prompt.lower()
