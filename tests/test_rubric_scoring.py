"""Tests for rubric-based (v2) pointwise scoring."""


from cv_rank.scoring.pointwise import (
    DEFAULT_RUBRIC_WEIGHTS,
    RUBRIC_DEFINITIONS,
    _build_rubric_text,
    _build_system_prompt_v2,
    _build_user_prompt_v2,
    _compute_composite_score,
)


class TestRubricDefinitions:
    """Test that rubric definitions are well-formed."""

    def test_all_dimensions_have_5_levels(self):
        for dim, defn in RUBRIC_DEFINITIONS.items():
            levels = defn["levels"]
            assert set(levels.keys()) == {1, 2, 3, 4, 5}, f"{dim} missing levels"

    def test_all_dimensions_have_question(self):
        for dim, defn in RUBRIC_DEFINITIONS.items():
            assert "question" in defn and len(defn["question"]) > 10, f"{dim} missing question"

    def test_weights_sum_to_one(self):
        total = sum(DEFAULT_RUBRIC_WEIGHTS.values())
        assert abs(total - 1.0) < 0.001, f"Weights sum to {total}, expected 1.0"

    def test_all_weight_dims_in_rubric(self):
        for dim in DEFAULT_RUBRIC_WEIGHTS:
            assert dim in RUBRIC_DEFINITIONS, f"Weight dim '{dim}' not in rubric definitions"


class TestBuildRubricText:
    """Test rubric text formatting."""

    def test_includes_all_dimensions(self):
        text = _build_rubric_text()
        for dim in DEFAULT_RUBRIC_WEIGHTS:
            assert dim.replace("_", " ").title() in text

    def test_includes_score_levels(self):
        text = _build_rubric_text()
        for level in range(1, 6):
            assert f"Score {level}:" in text

    def test_includes_weight_percentages(self):
        text = _build_rubric_text()
        assert "35%" in text  # builder_signal
        assert "30%" in text  # technical_depth
        assert "15%" in text  # professional_standing or uniqueness
        assert "15%" in text  # uniqueness
        assert "5%" in text   # community_impact


class TestCompositeScore:
    """Test composite score computation from dimension scores."""

    def test_all_fives_nested(self):
        result = {"dimensions": {d: {"score": 5} for d in DEFAULT_RUBRIC_WEIGHTS}}
        assert _compute_composite_score(result) == 100.0

    def test_all_ones_nested(self):
        result = {"dimensions": {d: {"score": 1} for d in DEFAULT_RUBRIC_WEIGHTS}}
        assert _compute_composite_score(result) == 1.0

    def test_all_threes_nested(self):
        result = {"dimensions": {d: {"score": 3} for d in DEFAULT_RUBRIC_WEIGHTS}}
        score = _compute_composite_score(result)
        assert 49.0 < score < 52.0  # should be around 50.5

    def test_mixed_scores_nested(self):
        result = {"dimensions": {
            "technical_depth": {"score": 5},
            "builder_signal": {"score": 4},
            "professional_standing": {"score": 3},
            "community_impact": {"score": 2},
            "uniqueness": {"score": 1},
        }}
        score = _compute_composite_score(result)
        # Weighted avg: 5*0.30 + 4*0.35 + 3*0.15 + 2*0.05 + 1*0.15 = 3.60
        # Mapped: (3.60-1)*24.75 + 1 = 65.35
        assert abs(score - 65.4) < 0.2

    def test_flat_keys(self):
        """Test flat format: technical_depth_score=4, etc."""
        result = {f"{d}_score": 4 for d in DEFAULT_RUBRIC_WEIGHTS}
        score = _compute_composite_score(result)
        assert score > 70.0

    def test_string_scores_handled(self):
        result = {"dimensions": {d: {"score": "4"} for d in DEFAULT_RUBRIC_WEIGHTS}}
        score = _compute_composite_score(result)
        assert score > 70.0

    def test_out_of_range_clamped(self):
        result = {"dimensions": {d: {"score": 10} for d in DEFAULT_RUBRIC_WEIGHTS}}
        assert _compute_composite_score(result) == 100.0  # clamped to 5

        result2 = {"dimensions": {d: {"score": -1} for d in DEFAULT_RUBRIC_WEIGHTS}}
        assert _compute_composite_score(result2) == 1.0  # clamped to 1

    def test_missing_dimensions_default_to_3(self):
        result = {"dimensions": {"technical_depth": {"score": 5}}}
        score = _compute_composite_score(result)
        # Only technical_depth=5, rest default to 3
        # (5*0.30 + 3*0.70) / 1.0 = 3.6 → 65.35
        assert 64.0 < score < 66.5

    def test_flat_plain_numbers(self):
        """Test when dimensions in nested are plain numbers, not dicts."""
        result = {"dimensions": {d: 4 for d in DEFAULT_RUBRIC_WEIGHTS}}
        score = _compute_composite_score(result)
        assert score > 70.0

    def test_custom_weights(self):
        custom = {"technical_depth": 0.5, "builder_signal": 0.5}
        result = {"dimensions": {"technical_depth": {"score": 5}, "builder_signal": {"score": 1}}}
        score = _compute_composite_score(result, custom)
        # (5*0.5 + 1*0.5) / 1.0 = 3.0 → (3-1)*24.75+1 = 50.5
        assert abs(score - 50.5) < 0.2

    def test_mixed_flat_and_nested(self):
        """Flat keys take precedence when dimensions dict is empty."""
        result = {
            "technical_depth_score": 5,
            "builder_signal_score": 5,
            "professional_standing_score": 5,
            "community_impact_score": 5,
            "uniqueness_score": 5,
        }
        assert _compute_composite_score(result) == 100.0


class TestV2Prompts:
    """Test v2 prompt construction."""

    def test_system_prompt_v2_contains_rubric(self):
        prompt = _build_system_prompt_v2(
            criteria={"technical_depth": 0.3},
            baselines_text="(No baselines)",
            total=20,
            accept_count=5,
        )
        assert "Technical Depth" in prompt
        assert "Builder Signal" in prompt
        assert "Score 1:" in prompt
        assert "Score 5:" in prompt
        assert "impartial judge" in prompt.lower() or "fair" in prompt.lower()
        assert "builder-first" in prompt.lower()

    def test_system_prompt_v2_contains_accept_info(self):
        prompt = _build_system_prompt_v2(
            criteria={},
            baselines_text="(None)",
            total=100,
            accept_count=20,
        )
        assert "20" in prompt
        assert "100" in prompt

    def test_user_prompt_v2_contains_profile(self):
        prompt = _build_user_prompt_v2("Name: John Doe\nGitHub: 50 stars")
        assert "John Doe" in prompt
        assert "50 stars" in prompt

    def test_user_prompt_v2_requests_dimensions(self):
        prompt = _build_user_prompt_v2("Test profile")
        assert "technical_depth_score" in prompt
        assert "builder_signal_score" in prompt
        assert "confidence" in prompt
        assert '"why"' in prompt

    def test_user_prompt_v2_custom_weights(self):
        custom = {"skill_a": 0.5, "skill_b": 0.5}
        prompt = _build_user_prompt_v2("Test profile", custom)
        assert "skill_a" in prompt
        assert "skill_b" in prompt
