"""Helpers for ranking-v2 golden/oracle tests.

This module is intentionally test-only. It provides a lightweight scaffold for
comparing fixed pipeline artifacts without needing live model calls.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Iterable


FIXTURE_DIR = Path(__file__).resolve().parent / "fixtures" / "ranking_v2"
ORACLE_PATH = FIXTURE_DIR / "oracle.json"


def load_oracle(path: Path | str = ORACLE_PATH) -> dict[str, Any]:
    """Load the canonical synthetic ranking oracle fixture."""
    return json.loads(Path(path).read_text())


def index_by_applicant_id(rows: Iterable[dict[str, Any]]) -> dict[str, dict[str, Any]]:
    """Index stage rows by stable applicant_id."""
    indexed: dict[str, dict[str, Any]] = {}
    for row in rows:
        applicant_id = row["applicant_id"]
        if applicant_id in indexed:
            raise AssertionError(f"Duplicate applicant_id in fixture: {applicant_id}")
        indexed[applicant_id] = row
    return indexed


def assert_stage_matches(
    actual_rows: Iterable[dict[str, Any]],
    expected_rows: Iterable[dict[str, Any]],
    *,
    keys: Iterable[str],
) -> None:
    """Compare two stage outputs using applicant_id as the join key.

    This intentionally ignores row order. It is meant for future v2 parity
    tests where the stable identity is the thing that matters, not incidental
    list ordering from a specific stage implementation.
    """
    keys = tuple(keys)
    actual = index_by_applicant_id(actual_rows)
    expected = index_by_applicant_id(expected_rows)
    assert set(actual) == set(expected), "applicant_id sets differ"

    for applicant_id, expected_row in expected.items():
        actual_row = actual[applicant_id]
        for key in keys:
            assert actual_row.get(key) == expected_row.get(key), (
                f"Mismatch for {applicant_id!r} field {key!r}: "
                f"expected {expected_row.get(key)!r}, got {actual_row.get(key)!r}"
            )


def assert_ranked_order(rows: Iterable[dict[str, Any]], *, key: str = "rank") -> None:
    """Assert the ranked rows are sorted by the requested order key."""
    values = [row[key] for row in rows]
    assert values == sorted(values), f"Rows are not sorted by {key}"
