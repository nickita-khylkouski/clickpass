"""Tests for cv_rank.config — configuration loading and validation."""

from __future__ import annotations

from pathlib import Path
from typing import Any


from cv_rank.config import (
    DEFAULT_CONFIG,
    _interpolate_env,
    load_config,
    validate_config,
)


# ---------------------------------------------------------------------------
# load_config
# ---------------------------------------------------------------------------


class TestLoadConfig:
    """Tests for load_config()."""

    def test_defaults_only(self, tmp_path: Path, monkeypatch) -> None:
        """When no config file exists, returns DEFAULT_CONFIG values."""
        # Change cwd to tmp_path where there is no config.yaml
        monkeypatch.chdir(tmp_path)

        config = load_config()

        assert config["event"]["type"] == "community_meetup"
        assert config["models"]["scoring"]  # model is set (from env or default)
        assert config["swiss"]["rounds"] == 20
        assert config["weights"]["swiss"] == 0.50
        assert config["weights"]["pointwise"] == 0.50
        assert config["pointwise"]["chain_of_thought"] is False
        assert config["pointwise"]["scoring_mode"] == "rubric"

    def test_load_with_yaml_file(self, tmp_path: Path) -> None:
        """A YAML file overrides defaults where specified."""
        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text(
            "event:\n"
            "  name: My Cool Event\n"
            "  target_accepts: 200\n"
            "swiss:\n"
            "  rounds: 30\n"
        )

        config = load_config(yaml_path)

        # Overridden values
        assert config["event"]["name"] == "My Cool Event"
        assert config["event"]["target_accepts"] == 200
        assert config["swiss"]["rounds"] == 30

        # Defaults still present for unspecified keys
        assert config["models"]["scoring"]  # model is set (from env or default)
        assert config["weights"]["swiss"] == 0.50

    def test_load_config_deep_merge(self, tmp_path: Path) -> None:
        """Nested dicts are merged recursively, not replaced entirely."""
        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text(
            "models:\n"
            "  scoring: claude-3-opus\n"
        )

        config = load_config(yaml_path)

        # Overridden
        assert config["models"]["scoring"] == "claude-3-opus"
        # Other model keys preserved from the resolved module defaults.
        assert config["models"]["swiss"] == DEFAULT_CONFIG["models"]["swiss"]
        assert config["models"]["quality_check"] == DEFAULT_CONFIG["models"]["quality_check"]

    def test_load_config_nonexistent_path_uses_defaults(self, tmp_path: Path, monkeypatch) -> None:
        """When explicit path does not exist, use defaults (no error)."""
        monkeypatch.chdir(tmp_path)
        config = load_config(tmp_path / "nonexistent.yaml")

        # Should get pure defaults
        assert config["event"]["name"] == ""
        assert config["swiss"]["rounds"] == 20

    def test_load_config_env_interpolation(self, tmp_path: Path, monkeypatch) -> None:
        """${ENV_VAR} placeholders in YAML are replaced with env values."""
        monkeypatch.setenv("TEST_EVENT_NAME", "EnvEvent2026")

        yaml_path = tmp_path / "config.yaml"
        yaml_path.write_text(
            "event:\n"
            "  name: ${TEST_EVENT_NAME}\n"
        )

        config = load_config(yaml_path)
        assert config["event"]["name"] == "EnvEvent2026"


# ---------------------------------------------------------------------------
# validate_config
# ---------------------------------------------------------------------------


class TestValidateConfig:
    """Tests for validate_config()."""

    def test_valid_config_no_errors(self, sample_config: dict) -> None:
        """A properly filled config produces no validation errors."""
        errors = validate_config(sample_config)
        assert errors == []

    def test_missing_required_keys(self) -> None:
        """Missing required keys produce errors."""
        config: dict[str, Any] = {
            "event": {},  # Missing name, type
            "models": {},  # Missing scoring
        }

        errors = validate_config(config)

        # Should flag event.name, event.type, models.scoring as missing or empty
        assert len(errors) >= 3
        error_text = " ".join(errors)
        assert "event.name" in error_text
        # event.type is missing from the dict
        assert "event.type" in error_text
        assert "models.scoring" in error_text

    def test_empty_required_key(self) -> None:
        """An empty string for a required key produces an error."""
        config = {
            "event": {"name": "", "type": "community_meetup"},
            "models": {"scoring": "gpt-5-nano-2025-08-07"},
        }

        errors = validate_config(config)
        assert any("event.name" in e and "empty" in e.lower() for e in errors)

    def test_unresolved_env_var(self) -> None:
        """An unresolved ${VAR} placeholder in a required key is flagged."""
        config = {
            "event": {"name": "${UNSET_VAR}", "type": "community_meetup"},
            "models": {"scoring": "gpt-5-nano-2025-08-07"},
        }

        errors = validate_config(config)
        assert any("unresolved" in e.lower() and "event.name" in e for e in errors)

    def test_wrong_types(self) -> None:
        """Wrong types for typed keys produce errors."""
        config = {
            "event": {"name": "Test", "type": "meetup", "target_accepts": "not_an_int"},
            "models": {"scoring": "gpt-5-nano-2025-08-07"},
            "swiss": {"rounds": "twenty", "use_bradley_terry": "yes"},
            "weights": {"swiss": 0.5, "pointwise": 0.5},
            "concurrency": {"scoring": 30, "swiss": 30, "quality_check": 15},
            "save_every": 50,
            "max_retries": 3,
            "baselines": [],
            "pointwise": {"temperature": 0},
        }

        errors = validate_config(config)

        error_text = " ".join(errors)
        assert "target_accepts" in error_text
        assert "swiss.rounds" in error_text

    def test_weight_sum_not_one(self) -> None:
        """Weights that do not sum to 1.0 produce an error."""
        config = {
            "event": {"name": "Test", "type": "meetup"},
            "models": {"scoring": "gpt-5-nano-2025-08-07"},
            "weights": {"swiss": 0.7, "pointwise": 0.5},  # sums to 1.2
        }

        errors = validate_config(config)
        assert any("sum to 1.0" in e for e in errors)

    def test_weight_sum_exactly_one(self, sample_config: dict) -> None:
        """Weights that sum to exactly 1.0 produce no weight error."""
        sample_config["weights"]["swiss"] = 0.3
        sample_config["weights"]["pointwise"] = 0.7

        errors = validate_config(sample_config)
        assert not any("sum to 1.0" in e for e in errors)

    def test_weight_sum_within_tolerance(self, sample_config: dict) -> None:
        """Weights within 0.01 tolerance of 1.0 are accepted."""
        sample_config["weights"]["swiss"] = 0.505
        sample_config["weights"]["pointwise"] = 0.5

        errors = validate_config(sample_config)
        # 0.505 + 0.5 = 1.005, within tolerance of 0.01
        assert not any("sum to 1.0" in e for e in errors)

    def test_optional_keys_absent_ok(self) -> None:
        """Optional typed keys that are absent do not produce errors."""
        config = {
            "event": {"name": "Test", "type": "meetup"},
            "models": {"scoring": "gpt-5-nano-2025-08-07"},
            # No swiss, weights, concurrency, etc.
        }

        errors = validate_config(config)
        # Only weight-related might appear since weights is missing entirely
        # But _get_nested returns _MISSING and the code skips it
        type_errors = [e for e in errors if "Wrong type" in e]
        assert type_errors == []

    def test_guardrails_needs_review_boundary_pct_range(self, sample_config: dict) -> None:
        sample_config["guardrails"]["needs_review"]["boundary_width_pct"] = 1.5
        errors = validate_config(sample_config)
        assert any("guardrails.needs_review.boundary_width_pct" in e for e in errors)

    def test_guardrails_needs_review_disagreement_threshold_range(self, sample_config: dict) -> None:
        sample_config["guardrails"]["needs_review"]["disagreement_threshold"] = -0.1
        errors = validate_config(sample_config)
        assert any("guardrails.needs_review.disagreement_threshold" in e for e in errors)


# ---------------------------------------------------------------------------
# _interpolate_env
# ---------------------------------------------------------------------------


class TestInterpolateEnv:
    """Tests for _interpolate_env()."""

    def test_replaces_set_env_var(self, monkeypatch) -> None:
        """A set environment variable is substituted."""
        monkeypatch.setenv("MY_VAR", "hello_world")

        result = _interpolate_env("prefix_${MY_VAR}_suffix")
        assert result == "prefix_hello_world_suffix"

    def test_unset_var_left_as_is(self, monkeypatch) -> None:
        """An unset environment variable leaves the placeholder intact."""
        monkeypatch.delenv("DEFINITELY_UNSET_VAR", raising=False)

        result = _interpolate_env("value=${DEFINITELY_UNSET_VAR}")
        assert result == "value=${DEFINITELY_UNSET_VAR}"

    def test_multiple_vars(self, monkeypatch) -> None:
        """Multiple placeholders in one string are all replaced."""
        monkeypatch.setenv("A_VAR", "aaa")
        monkeypatch.setenv("B_VAR", "bbb")

        result = _interpolate_env("${A_VAR} and ${B_VAR}")
        assert result == "aaa and bbb"

    def test_no_placeholders(self) -> None:
        """A string with no ${...} is returned unchanged."""
        result = _interpolate_env("plain text with no vars")
        assert result == "plain text with no vars"

    def test_empty_string(self) -> None:
        """An empty string returns empty."""
        result = _interpolate_env("")
        assert result == ""
