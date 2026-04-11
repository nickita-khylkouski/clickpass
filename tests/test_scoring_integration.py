"""Integration tests for scoring logic: Swiss pairing, compare_one, combine_rankings."""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock, patch


from cv_rank.scoring.combine import combine_rankings
from cv_rank.scoring.swiss import compare_one, swiss_pair


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _person(name: str) -> dict:
    return {"name": name}


def _records(*entries: tuple[str, int, int]) -> dict[str, dict]:
    return {
        name: {"wins": wins, "losses": losses, "byes": 0}
        for name, wins, losses in entries
    }


def _format_profile(p: dict) -> str:
    return f"Profile: {p['name']}"


def _mock_openai_response(winner_label: str, why: str = "better overall") -> AsyncMock:
    """Build an AsyncMock client whose chat.completions.create returns winner_label."""
    resp = SimpleNamespace(
        choices=[
            SimpleNamespace(
                message=SimpleNamespace(
                    content=f'{{"winner": "{winner_label}", "why": "{why}"}}'
                ),
                finish_reason="stop",
            )
        ],
        usage=SimpleNamespace(total_tokens=42),
    )
    client = AsyncMock()
    client.chat.completions.create = AsyncMock(return_value=resp)
    return client


# ---------------------------------------------------------------------------
# swiss_pair
# ---------------------------------------------------------------------------


class TestSwissPairAvoidRepeats:

    def test_no_repeat_when_alternatives_exist(self):
        records = _records(("A", 1, 0), ("B", 1, 0), ("C", 0, 1), ("D", 0, 1))
        past = {tuple(sorted(["A", "B"]))}

        matches, byes = swiss_pair(records, past)

        paired = {tuple(sorted(m)) for m in matches}
        assert tuple(sorted(["A", "B"])) not in paired

    def test_allows_repeat_when_no_alternative(self):
        records = _records(("A", 1, 0), ("B", 0, 1))
        past = {tuple(sorted(["A", "B"]))}

        matches, byes = swiss_pair(records, past)

        assert len(matches) == 1
        assert set(matches[0]) == {"A", "B"}


class TestSwissPairByes:

    def test_odd_number_gives_one_bye(self):
        records = _records(("A", 0, 0), ("B", 0, 0), ("C", 0, 0))
        matches, byes = swiss_pair(records, set())

        assert len(byes) == 1
        assert len(matches) == 1
        all_names = {n for m in matches for n in m} | set(byes)
        assert all_names == {"A", "B", "C"}

    def test_even_number_no_bye(self):
        records = _records(("A", 0, 0), ("B", 0, 0), ("C", 0, 0), ("D", 0, 0))
        matches, byes = swiss_pair(records, set())

        assert len(byes) == 0
        assert len(matches) == 2


class TestSwissPairGrouping:

    def test_similar_records_paired_together(self):
        records = _records(
            ("W1", 3, 0), ("W2", 3, 0),
            ("M1", 1, 2), ("M2", 1, 2),
            ("L1", 0, 3), ("L2", 0, 3),
        )
        matches, _ = swiss_pair(records, set())

        match_sets = [set(m) for m in matches]
        assert {"W1", "W2"} in match_sets
        assert {"M1", "M2"} in match_sets
        assert {"L1", "L2"} in match_sets


# ---------------------------------------------------------------------------
# compare_one (mocked OpenAI)
# ---------------------------------------------------------------------------


class TestCompareOneReturnsWinner:

    async def test_alpha_winner_unswapped(self):
        client = _mock_openai_response("ALPHA")
        p1, p2 = _person("Alice"), _person("Bob")

        with patch("cv_rank.scoring.swiss.random.random", return_value=0.1):
            result = await compare_one(
                client, p1, p2, {}, "gpt-4o", _format_profile, config={"max_retries": 1}
            )

        assert result["winner"] == "Alice"
        assert result["swapped"] is False
        assert result["tokens"] == 42

    async def test_beta_winner_unswapped(self):
        client = _mock_openai_response("BETA")
        p1, p2 = _person("Alice"), _person("Bob")

        with patch("cv_rank.scoring.swiss.random.random", return_value=0.1):
            result = await compare_one(
                client, p1, p2, {}, "gpt-4o", _format_profile, config={"max_retries": 1}
            )

        assert result["winner"] == "Bob"


class TestCompareOnePositionBias:

    async def test_swap_reverses_alpha_beta_assignment(self):
        client = _mock_openai_response("ALPHA")
        p1, p2 = _person("Alice"), _person("Bob")

        with patch("cv_rank.scoring.swiss.random.random", return_value=0.9):
            result = await compare_one(
                client, p1, p2, {}, "gpt-4o", _format_profile, config={"max_retries": 1}
            )

        assert result["swapped"] is True
        assert result["winner"] == "Bob"

    async def test_swap_beta_winner_maps_to_first_person(self):
        client = _mock_openai_response("BETA")
        p1, p2 = _person("Alice"), _person("Bob")

        with patch("cv_rank.scoring.swiss.random.random", return_value=0.9):
            result = await compare_one(
                client, p1, p2, {}, "gpt-4o", _format_profile, config={"max_retries": 1}
            )

        assert result["swapped"] is True
        assert result["winner"] == "Alice"


class TestCompareOneApiError:

    async def test_generic_error_returns_none_winner(self):
        client = AsyncMock()
        client.chat.completions.create = AsyncMock(
            side_effect=RuntimeError("connection failed")
        )
        p1, p2 = _person("Alice"), _person("Bob")

        result = await compare_one(
            client, p1, p2, {}, "gpt-4o", _format_profile, config={"max_retries": 1}
        )

        assert result["winner"] is None
        assert "error" in result

    async def test_empty_response_returns_none_winner(self):
        resp = SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content=""))],
            usage=SimpleNamespace(total_tokens=0),
        )
        client = AsyncMock()
        client.chat.completions.create = AsyncMock(return_value=resp)
        p1, p2 = _person("Alice"), _person("Bob")

        result = await compare_one(
            client, p1, p2, {}, "gpt-4o", _format_profile, config={"max_retries": 1}
        )

        assert result["winner"] is None


# ---------------------------------------------------------------------------
# combine_rankings
# ---------------------------------------------------------------------------


def _scores(*entries: tuple[str, float]) -> list[dict]:
    return [{"name": n, "score": s} for n, s in entries]


# ---------------------------------------------------------------------------
# PRePair: pointwise context injection
# ---------------------------------------------------------------------------


class TestCompareOneWithPointwiseContext:

    async def test_context_injected_in_prompt(self):
        """When pointwise_context is provided, it appears in the prompt."""
        client = _mock_openai_response("ALPHA")
        p1, p2 = _person("Alice"), _person("Bob")
        pw_context = {
            "Alice": {"score": 85, "strongest_signal": "ML expertise", "concerns": "no OSS"},
            "Bob": {"score": 60, "strongest_signal": "startup founder", "concerns": "thin profile"},
        }

        with patch("cv_rank.scoring.swiss.random.random", return_value=0.1):
            result = await compare_one(
                client, p1, p2, {}, "gpt-4o", _format_profile,
                config={"max_retries": 1},
                pointwise_context=pw_context,
            )

        # Check the prompt sent to the API
        call_args = client.chat.completions.create.call_args
        messages = call_args.kwargs.get("messages") or call_args[1].get("messages", [])
        user_msg = messages[-1]["content"]

        assert "PRIOR ASSESSMENT" in user_msg
        assert "85/100" not in user_msg
        assert "ML expertise" in user_msg

    async def test_context_follows_swap(self):
        """Context is applied to the correct candidate after position swap."""
        client = _mock_openai_response("ALPHA")
        p1, p2 = _person("Alice"), _person("Bob")
        pw_context = {
            "Alice": {"score": 85, "strongest_signal": "ML", "concerns": "none"},
            "Bob": {"score": 60, "strongest_signal": "startup", "concerns": "thin"},
        }

        # Force swap: Bob becomes ALPHA, Alice becomes BETA
        with patch("cv_rank.scoring.swiss.random.random", return_value=0.9):
            result = await compare_one(
                client, p1, p2, {}, "gpt-4o", _format_profile,
                config={"max_retries": 1},
                pointwise_context=pw_context,
            )

        call_args = client.chat.completions.create.call_args
        messages = call_args.kwargs.get("messages") or call_args[1].get("messages", [])
        user_msg = messages[-1]["content"]

        # ALPHA is Bob (swapped), so Bob's qualitative context should appear first
        alpha_idx = user_msg.index("CANDIDATE ALPHA")
        beta_idx = user_msg.index("CANDIDATE BETA")

        # Bob's context ("startup") should be between ALPHA and BETA markers
        bob_context_idx = user_msg.index("startup")
        assert alpha_idx < bob_context_idx < beta_idx

    async def test_no_context_when_none(self):
        """Without pointwise_context, no PRIOR ASSESSMENT appears."""
        client = _mock_openai_response("ALPHA")
        p1, p2 = _person("Alice"), _person("Bob")

        with patch("cv_rank.scoring.swiss.random.random", return_value=0.1):
            result = await compare_one(
                client, p1, p2, {}, "gpt-4o", _format_profile,
                config={"max_retries": 1},
                pointwise_context=None,
            )

        call_args = client.chat.completions.create.call_args
        messages = call_args.kwargs.get("messages") or call_args[1].get("messages", [])
        user_msg = messages[-1]["content"]

        assert "PRIOR ASSESSMENT" not in user_msg

    async def test_partial_context(self):
        """If only one candidate has context, only that one gets annotation."""
        client = _mock_openai_response("ALPHA")
        p1, p2 = _person("Alice"), _person("Bob")
        pw_context = {
            "Alice": {"score": 85, "strongest_signal": "ML", "concerns": "none"},
            # Bob not in context
        }

        with patch("cv_rank.scoring.swiss.random.random", return_value=0.1):
            result = await compare_one(
                client, p1, p2, {}, "gpt-4o", _format_profile,
                config={"max_retries": 1},
                pointwise_context=pw_context,
            )

        call_args = client.chat.completions.create.call_args
        messages = call_args.kwargs.get("messages") or call_args[1].get("messages", [])
        user_msg = messages[-1]["content"]

        assert "85/100" not in user_msg
        assert "ML" in user_msg
        # Only one PRIOR ASSESSMENT
        assert user_msg.count("PRIOR ASSESSMENT") == 1


class TestCombineEqualWeights:

    def test_balanced_ranking_50_50(self):
        scores = _scores(("Alice", 90), ("Bob", 70), ("Carol", 50))
        swiss = _records(("Alice", 5, 5), ("Bob", 8, 2), ("Carol", 3, 7))

        result = combine_rankings(scores, swiss, {}, swiss_weight=0.5, pointwise_weight=0.5)

        names = [r["name"] for r in result]
        assert len(names) == 3
        assert result[0]["rank"] == 1
        assert result[-1]["rank"] == 3
        assert all(result[i]["final_score"] >= result[i + 1]["final_score"] for i in range(len(result) - 1))

    def test_dominant_pointwise_lifts_rank(self):
        scores = _scores(("A", 100), ("B", 0))
        swiss = _records(("A", 0, 10), ("B", 10, 0))

        result = combine_rankings(scores, swiss, {}, swiss_weight=0.5, pointwise_weight=0.5)

        assert result[0]["final_score"] == result[1]["final_score"]


class TestCombineWithBTStrengths:

    def test_bt_breaks_tie_in_win_count(self):
        scores = _scores(("Alice", 80), ("Bob", 80))
        swiss = _records(("Alice", 5, 5), ("Bob", 5, 5))
        bt = {"Alice": 0.8, "Bob": 0.2}

        result = combine_rankings(scores, swiss, bt, swiss_weight=0.5, pointwise_weight=0.5)

        assert result[0]["name"] == "Alice"
        assert result[0]["bt_strength"] is not None

    def test_bt_absent_falls_back_to_win_rate(self):
        scores = _scores(("Alice", 80), ("Bob", 80))
        swiss = _records(("Alice", 8, 2), ("Bob", 2, 8))

        result = combine_rankings(scores, swiss, {}, swiss_weight=0.5, pointwise_weight=0.5)

        assert result[0]["name"] == "Alice"
        assert result[0]["bt_strength"] is None
