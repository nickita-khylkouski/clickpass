"""
Configuration loading, validation, and logging setup.

Loads YAML config with ${ENV_VAR} interpolation.
Hierarchy: CLI flags > env vars > YAML file > DEFAULT_CONFIG.
"""

import copy
import logging
import os
import re
import sys
from pathlib import Path
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

_DEFAULT_MODEL = os.environ.get("OPENAI_MODEL", "gpt-5-mini")

DEFAULT_CONFIG: dict[str, Any] = {
    "event": {"name": "", "type": "community_meetup", "target_accepts": 0},
    "models": {
        "scoring": _DEFAULT_MODEL,
        "swiss": _DEFAULT_MODEL,
        "quality_check": _DEFAULT_MODEL,
        "borderline": "gpt-5.2",  # accuracy matters most at the cutline
    },
    "swiss": {
        "rounds": 20,
        "use_bradley_terry": True,
        "inject_pointwise": True,
        "bt_pointwise_prior": True,
        "bt_prior_strength": 2,
        "auto_rounds": {
            "enabled": True,
            "offset": -1,
            "min_rounds": 5,
            "max_guard_offset": 2,
        },
        "early_stop": {
            "enabled": True,
            "min_rounds": 5,
            "jaccard_accept_set_min": 0.98,
            "max_boundary_rank_shift": 2,
            "consecutive_rounds": 2,
            "boundary_width_pct": 0.10,
            "boundary_width_min": 20,
        },
    },
    "weights": {"swiss": 0.50, "pointwise": 0.50, "auto": True},
    "pointwise": {
        "chain_of_thought": False,
        "require_decimal": True,
        "temperature": 0,
        "scoring_mode": "rubric",
    },
    "enrichment": {
        "supabase": {"enabled": True, "url": "", "key": "", "batch_size": 50},
        "github": {"enabled": True, "token": "", "batch_size": 20},
        "local_csv": {"enabled": False, "paths": []},
        "platform_db": {"enabled": True, "dsn": "", "batch_size": 100},
        "exa": {
            "enabled": False,
            "api_key": "",
            "gemini_api_key": "",
            "linkedin_urls": 20,
            "github_urls": 10,
            "devpost_urls": 30,
            "publication_urls": 30,
            "web_urls": 15,
        },
    },
    "anonymize": {"enabled": True},
    "debiasing": {"enabled": True, "strength": 0.5},
    "borderline": {"enabled": True, "band_pct": 0.15, "extra_rounds": 5},
    "concurrency": {"scoring": 50, "swiss": 50, "quality_check": 50},
    "save_every": 50,
    "max_retries": 5,
    "guardrails": {
        "max_exclusion_rate": 0.02,
        "require_failed_to_rank_report": True,
        "needs_review": {
            "enabled": True,
            "boundary_width_pct": 0.10,
            "boundary_width_min": 20,
            "disagreement_threshold": 0.25,
        },
    },
    "baselines": [],
}

# Keys that MUST be present (dot-separated paths).
REQUIRED_KEYS = [
    "event.name",
    "event.type",
    "models.scoring",
]

# Expected types for selected keys (dot-separated path -> type or tuple of types).
KEY_TYPES: dict[str, type | tuple[type, ...]] = {
    "event.target_accepts": int,
    "swiss.rounds": int,
    "swiss.bt_pointwise_prior": bool,
    "swiss.bt_prior_strength": int,
    "swiss.use_bradley_terry": bool,
    "swiss.auto_rounds.enabled": bool,
    "swiss.auto_rounds.offset": int,
    "swiss.auto_rounds.min_rounds": int,
    "swiss.auto_rounds.max_guard_offset": int,
    "swiss.early_stop.enabled": bool,
    "swiss.early_stop.min_rounds": int,
    "swiss.early_stop.jaccard_accept_set_min": (int, float),
    "swiss.early_stop.max_boundary_rank_shift": int,
    "swiss.early_stop.consecutive_rounds": int,
    "swiss.early_stop.boundary_width_pct": (int, float),
    "swiss.early_stop.boundary_width_min": int,
    "weights.swiss": (int, float),
    "weights.pointwise": (int, float),
    "weights.auto": bool,
    "pointwise.temperature": (int, float),
    "concurrency.scoring": int,
    "concurrency.swiss": int,
    "concurrency.quality_check": int,
    "save_every": int,
    "max_retries": int,
    "guardrails.max_exclusion_rate": (int, float),
    "guardrails.require_failed_to_rank_report": bool,
    "guardrails.needs_review.enabled": bool,
    "guardrails.needs_review.boundary_width_pct": (int, float),
    "guardrails.needs_review.boundary_width_min": int,
    "guardrails.needs_review.disagreement_threshold": (int, float),
    "baselines": list,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _deep_merge(base: dict, override: dict) -> dict:
    """Recursively merge *override* into a deep copy of *base*.

    Values in *override* always win.  Nested dicts are merged recursively;
    everything else is replaced outright.
    """
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if key in merged and isinstance(merged[key], dict) and isinstance(value, dict):
            merged[key] = _deep_merge(merged[key], value)
        else:
            merged[key] = copy.deepcopy(value)
    return merged


def _interpolate_env(raw: str) -> str:
    """Replace ``${ENV_VAR}`` placeholders with their environment values.

    If an env var is not set the placeholder is left as-is so that
    ``validate_config`` can catch it later (or the caller can supply it
    via CLI flags).
    """

    def _sub(match: re.Match) -> str:
        return os.environ.get(match.group(1), match.group(0))

    return re.sub(r"\$\{(\w+)\}", _sub, raw)


def _get_nested(d: dict, dotted_key: str) -> Any:
    """Retrieve a value from a nested dict using a dot-separated key.

    Returns ``_MISSING`` sentinel when the key does not exist.
    """
    parts = dotted_key.split(".")
    cur: Any = d
    for p in parts:
        if not isinstance(cur, dict) or p not in cur:
            return _MISSING
        cur = cur[p]
    return cur


class _MissingSentinel:
    """Unique sentinel for 'key not found' so we can distinguish from None."""

    def __repr__(self) -> str:
        return "<MISSING>"


_MISSING = _MissingSentinel()


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def load_config(path: str | Path | None = None) -> dict:
    """Load a YAML config file, interpolate env vars, merge with defaults.

    Parameters
    ----------
    path:
        Path to a YAML config file.  If ``None`` the function looks for
        ``config.yaml`` in the current working directory, then falls back
        to ``DEFAULT_CONFIG`` alone.

    Returns
    -------
    dict
        Fully merged configuration dictionary.
    """
    if path is not None:
        config_path = Path(path)
    else:
        config_path = Path.cwd() / "config.yaml"

    file_config: dict = {}
    if config_path.is_file():
        with open(config_path) as fh:
            raw = fh.read()
        raw = _interpolate_env(raw)
        loaded = yaml.safe_load(raw)
        if isinstance(loaded, dict):
            file_config = loaded

    return _deep_merge(DEFAULT_CONFIG, file_config)


def validate_config(config: dict) -> list[str]:
    """Validate *config* against required keys and expected types.

    Returns a list of human-readable error strings.  An empty list means
    the config is valid.
    """
    errors: list[str] = []

    # Required keys
    for key in REQUIRED_KEYS:
        val = _get_nested(config, key)
        if val is _MISSING:
            errors.append(f"Missing required key: {key}")
        elif isinstance(val, str) and not val.strip():
            errors.append(f"Required key is empty: {key}")
        elif isinstance(val, str) and val.startswith("${"):
            errors.append(f"Unresolved env var in required key: {key} = {val!r}")

    # Type checks
    for key, expected in KEY_TYPES.items():
        val = _get_nested(config, key)
        if val is _MISSING:
            continue  # optional key not present -- that is fine
        if not isinstance(val, expected):
            expected_name = expected.__name__ if isinstance(expected, type) else str(expected)
            errors.append(f"Wrong type for {key}: expected {expected_name}, got {type(val).__name__}")

    # Weight sanity — skip when auto-weighting is enabled (manual weights are ignored)
    w_auto = _get_nested(config, "weights.auto")
    auto_enabled = isinstance(w_auto, bool) and w_auto
    w_swiss = _get_nested(config, "weights.swiss")
    w_point = _get_nested(config, "weights.pointwise")
    if not auto_enabled and isinstance(w_swiss, (int, float)) and isinstance(w_point, (int, float)):
        total = w_swiss + w_point
        if abs(total - 1.0) > 0.01:
            errors.append(f"weights.swiss + weights.pointwise should sum to 1.0, got {total:.3f}")

    # Range sanity for adaptive Swiss and guardrails
    exclusion = _get_nested(config, "guardrails.max_exclusion_rate")
    if isinstance(exclusion, (int, float)) and not (0.0 <= exclusion <= 1.0):
        errors.append(f"guardrails.max_exclusion_rate must be in [0,1], got {exclusion}")

    jaccard = _get_nested(config, "swiss.early_stop.jaccard_accept_set_min")
    if isinstance(jaccard, (int, float)) and not (0.0 <= jaccard <= 1.0):
        errors.append(f"swiss.early_stop.jaccard_accept_set_min must be in [0,1], got {jaccard}")

    boundary_pct = _get_nested(config, "swiss.early_stop.boundary_width_pct")
    if isinstance(boundary_pct, (int, float)) and not (0.0 <= boundary_pct <= 1.0):
        errors.append(f"swiss.early_stop.boundary_width_pct must be in [0,1], got {boundary_pct}")

    review_boundary_pct = _get_nested(config, "guardrails.needs_review.boundary_width_pct")
    if isinstance(review_boundary_pct, (int, float)) and not (0.0 <= review_boundary_pct <= 1.0):
        errors.append(
            "guardrails.needs_review.boundary_width_pct must be in [0,1], "
            f"got {review_boundary_pct}"
        )

    disagreement_threshold = _get_nested(config, "guardrails.needs_review.disagreement_threshold")
    if isinstance(disagreement_threshold, (int, float)) and not (0.0 <= disagreement_threshold <= 1.0):
        errors.append(
            "guardrails.needs_review.disagreement_threshold must be in [0,1], "
            f"got {disagreement_threshold}"
        )

    # Enrichment credential checks
    supa = _get_nested(config, "enrichment.supabase")
    if isinstance(supa, dict) and supa.get("enabled"):
        if not supa.get("url") or (isinstance(supa["url"], str) and supa["url"].startswith("${")):
            errors.append("Supabase enabled but URL not set. Set SUPABASE_URL env var or config enrichment.supabase.url")
        if not supa.get("key") or (isinstance(supa["key"], str) and supa["key"].startswith("${")):
            errors.append("Supabase enabled but key not set. Set SUPABASE_KEY env var or config enrichment.supabase.key")

    exa = _get_nested(config, "enrichment.exa")
    if isinstance(exa, dict) and exa.get("enabled"):
        if not exa.get("api_key") or (isinstance(exa["api_key"], str) and exa["api_key"].startswith("${")):
            errors.append("Exa enabled but API key not set. Set EXA_API_KEY env var or config enrichment.exa.api_key")
        if not exa.get("gemini_api_key") or (isinstance(exa["gemini_api_key"], str) and exa["gemini_api_key"].startswith("${")):
            errors.append("Exa enabled but Gemini API key not set. Set GEMINI_API_KEY env var or config enrichment.exa.gemini_api_key")

    return errors


def setup_logging(name: str = "cv_rank", output_dir: str | Path | None = None) -> logging.Logger:
    """Create and return a logger with console and (optionally) file handlers.

    Console handler logs at INFO and above.
    File handler (if *output_dir* is given) logs at DEBUG and above.

    Parameters
    ----------
    name:
        Logger name.
    output_dir:
        If provided, a ``{name}.log`` file is created in this directory.

    Returns
    -------
    logging.Logger
    """
    logger = logging.getLogger(name)

    # Avoid adding duplicate handlers when called multiple times.
    if logger.handlers:
        return logger

    logger.setLevel(logging.DEBUG)

    fmt = logging.Formatter(
        fmt="%(asctime)s | %(levelname)-7s | %(name)s | %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Console handler — INFO+
    console = logging.StreamHandler(sys.stderr)
    console.setLevel(logging.INFO)
    console.setFormatter(fmt)
    logger.addHandler(console)

    # File handler — DEBUG+ (optional)
    if output_dir is not None:
        out = Path(output_dir)
        out.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(out / f"{name}.log", encoding="utf-8")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(fmt)
        logger.addHandler(fh)

        from cv_rank.utils import configure_llm_trace

        trace_path = configure_llm_trace(out, enabled=True)
        if trace_path is not None:
            logger.debug("LLM trace enabled: %s", trace_path)

    return logger


def parse_criteria(criteria_str: str | None, config: dict) -> dict[str, float]:
    """Parse criteria from CLI string or config preset.

    Accepts either:
    - A comma-separated string: ``"technical_depth:0.3,creativity:0.2"``
    - A plain description: ``"AI builders"`` (uses config preset for event type)
    """
    if criteria_str and ":" in criteria_str:
        # Parse explicit weights
        result: dict[str, float] = {}
        for item in criteria_str.split(","):
            item = item.strip()
            if ":" in item:
                name, weight = item.rsplit(":", 1)
                try:
                    result[name.strip()] = float(weight)
                except ValueError:
                    print(
                        f"  Warning: invalid weight '{weight}' for criterion"
                        f" '{name.strip()}', skipping",
                        file=sys.stderr,
                    )
        if result:
            return result

    if criteria_str:
        # Plain text like "AI builders" — not parsed, using config preset
        print(
            f"  Note: --criteria \"{criteria_str}\" is a label, not scored directly.\n"
            f"        Using config preset for event type. For custom weights use:\n"
            f"        --criteria \"technical_depth:0.3,creativity:0.2\"",
            file=sys.stderr,
        )

    # Fall back to config preset
    event_type = config.get("event", {}).get("type", "community_meetup")
    presets = config.get("criteria", {})
    if event_type in presets:
        return presets[event_type]

    # Ultimate fallback
    return {
        "technical_depth": 0.30,
        "industry_experience": 0.25,
        "open_source": 0.20,
        "network_engagement": 0.15,
        "publications": 0.10,
    }
