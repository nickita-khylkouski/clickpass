"""Tests for the print_data_quality_report function in cv_rank.display."""

from __future__ import annotations

from typing import Any


from cv_rank.display import print_data_quality_report


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

from helpers import make_person


def _make_person(name: str, **kwargs: Any) -> dict[str, Any]:
    return make_person(name, **kwargs)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------

class TestPrintDataQualityReport:
    """Verify print_data_quality_report outputs correct quality summary."""

    def test_function_exists(self):
        """print_data_quality_report is importable from cv_rank.display."""
        assert callable(print_data_quality_report)

    def test_prints_total_people(self, capsys):
        """Reports total number of people."""
        people = [
            _make_person("Alice", email="a@x.com"),
            _make_person("Bob"),
            _make_person("Carol", email="c@x.com"),
        ]
        print_data_quality_report(people)
        output = capsys.readouterr().err  # display.py prints to stderr
        assert "3" in output

    def test_prints_field_counts(self, capsys):
        """Reports how many people have email, linkedin, github, etc."""
        people = [
            _make_person("Alice", email="a@x.com", linkedin_url="https://linkedin.com/in/alice",
                         github_url="https://github.com/alice", company="Acme",
                         role="Engineer", location="NYC", x_handle="@alice"),
            _make_person("Bob"),  # No fields at all
        ]
        print_data_quality_report(people)
        output = capsys.readouterr().err
        # Should show counts for various fields
        assert "email" in output.lower()
        assert "linkedin" in output.lower()
        assert "github" in output.lower()

    def test_flags_unverifiable_people(self, capsys):
        """Flags people with NO linkedin AND NO github as unverifiable."""
        people = [
            _make_person("Alice", linkedin_url="https://linkedin.com/in/alice"),
            _make_person("Bob", github_url="https://github.com/bob"),
            _make_person("Carol"),  # No linkedin AND no github -> unverifiable
            _make_person("Dave"),   # Also unverifiable
        ]
        print_data_quality_report(people)
        output = capsys.readouterr().err
        assert "unverifiable" in output.lower()
        assert "2" in output  # 2 unverifiable people

    def test_prints_percentages(self, capsys):
        """Output includes percentage values."""
        people = [
            _make_person("Alice", email="a@x.com", linkedin_url="https://linkedin.com/in/alice"),
            _make_person("Bob", email="b@x.com"),
            _make_person("Carol"),
            _make_person("Dave", email="d@x.com"),
        ]
        print_data_quality_report(people)
        output = capsys.readouterr().err
        # Should show percentages (e.g., "75%" for email coverage: 3 of 4)
        assert "%" in output

    def test_empty_people_list(self, capsys):
        """Handles empty people list without error."""
        print_data_quality_report([])
        output = capsys.readouterr().err
        assert "0" in output or "no" in output.lower() or "empty" in output.lower()

    def test_all_fields_populated(self, capsys):
        """When all fields present, shows 100% coverage."""
        people = [
            _make_person("Alice", email="a@x.com", linkedin_url="https://linkedin.com/in/alice",
                         github_url="https://github.com/alice", company="Acme",
                         role="Eng", location="SF", x_handle="@a"),
        ]
        print_data_quality_report(people)
        output = capsys.readouterr().err
        assert "100" in output  # 100%
