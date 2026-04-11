"""Tests for cv_rank.debiasing — profile-length debiasing."""

from __future__ import annotations

import pytest

from cv_rank.debiasing import compute_profile_lengths, debias_scores


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _people(*names: str) -> list[dict]:
    return [{"name": n} for n in names]


def _format_fn_varied(person: dict) -> str:
    """Format function that produces different lengths per person name."""
    name = person.get("name", "")
    # Simulate longer profiles for later-alphabet names
    words = name.split()
    return " ".join(words * (ord(name[0]) - ord("A") + 1) * 10)


def _format_fn_constant(person: dict) -> str:
    return f"Profile for {person.get('name', '?')}"


def _scores(data: dict[str, float]) -> list[dict]:
    return [{"name": n, "score": s} for n, s in data.items()]


# ---------------------------------------------------------------------------
# compute_profile_lengths
# ---------------------------------------------------------------------------

class TestComputeProfileLengths:
    def test_returns_dict_with_names(self):
        people = _people("Alice", "Bob")
        lengths = compute_profile_lengths(people, _format_fn_constant)

        assert "Alice" in lengths
        assert "Bob" in lengths
        assert isinstance(lengths["Alice"], int)
        assert isinstance(lengths["Bob"], int)

    def test_longer_profile_larger_count(self):
        """Longer formatted profile produces larger token estimate."""
        people = [
            {"name": "Short", "bio": "x"},
            {"name": "Long", "bio": "x " * 100},
        ]

        def fmt(p: dict) -> str:
            return f"Name: {p['name']}\nBio: {p.get('bio', '')}"

        lengths = compute_profile_lengths(people, fmt)
        assert lengths["Long"] > lengths["Short"]

    def test_empty_people(self):
        lengths = compute_profile_lengths([], _format_fn_constant)
        assert lengths == {}


# ---------------------------------------------------------------------------
# debias_scores
# ---------------------------------------------------------------------------

class TestDebiasScores:
    def test_reduces_length_score_correlation(self):
        """Debiasing should reduce the correlation between length and score."""
        # Create scores that correlate with length (longer → higher)
        scores = _scores({
            "A": 40, "B": 50, "C": 60, "D": 70, "E": 80,
            "F": 45, "G": 55, "H": 65, "I": 75, "J": 85,
        })
        lengths = {
            "A": 100, "B": 150, "C": 200, "D": 250, "E": 300,
            "F": 120, "G": 170, "H": 220, "I": 270, "J": 320,
        }

        result = debias_scores(scores, lengths, strength=1.0)

        # Compute correlation of adjusted scores with lengths
        from cv_rank.metrics import length_bias_correlation
        r_before = length_bias_correlation(scores, lengths)
        r_after = length_bias_correlation(result, lengths)

        assert abs(r_after) < abs(r_before)

    def test_preserves_score_raw(self):
        """Original scores are preserved in score_raw."""
        scores = _scores({
            "A": 40, "B": 50, "C": 60, "D": 70, "E": 80,
        })
        lengths = {"A": 100, "B": 200, "C": 300, "D": 400, "E": 500}

        result = debias_scores(scores, lengths, strength=0.5)

        for r in result:
            if "score_raw" in r:
                original = next(s for s in scores if s["name"] == r["name"])
                assert r["score_raw"] == original["score"]

    def test_skips_when_no_correlation(self):
        """When |r| < 0.1, scores are returned unchanged."""
        # All same score → zero variance → r=0.0
        scores = _scores({
            "A": 50, "B": 50, "C": 50, "D": 50, "E": 50,
        })
        lengths = {"A": 100, "B": 200, "C": 300, "D": 400, "E": 500}

        result = debias_scores(scores, lengths)

        # Scores should be unchanged (no score_raw added)
        for res, s in zip(result, scores):
            assert res["score"] == s["score"]

    def test_too_few_pairs_skips(self):
        """With < 5 valid pairs, debiasing is skipped."""
        scores = _scores({"A": 50, "B": 80})
        lengths = {"A": 100, "B": 200}

        result = debias_scores(scores, lengths)

        assert result[0]["score"] == 50
        assert result[1]["score"] == 80

    def test_strength_zero_no_change(self):
        """strength=0.0 means no adjustment."""
        scores = _scores({
            "A": 40, "B": 50, "C": 60, "D": 70, "E": 80,
        })
        lengths = {"A": 100, "B": 200, "C": 300, "D": 400, "E": 500}

        result = debias_scores(scores, lengths, strength=0.0)

        for r, s in zip(result, scores):
            assert r["score"] == s["score"]

    def test_preserves_error_entries(self):
        """Score entries with score=None are passed through unchanged."""
        scores = [
            {"name": "A", "score": 50},
            {"name": "B", "score": None, "error": "timeout"},
            {"name": "C", "score": 60},
            {"name": "D", "score": 70},
            {"name": "E", "score": 80},
            {"name": "F", "score": 90},
        ]
        lengths = {"A": 100, "B": 200, "C": 300, "D": 400, "E": 500, "F": 600}

        result = debias_scores(scores, lengths)

        error_entry = next(r for r in result if r["name"] == "B")
        assert error_entry["score"] is None
        assert error_entry["error"] == "timeout"
