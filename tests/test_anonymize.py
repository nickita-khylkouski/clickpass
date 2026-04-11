"""Tests for cv_rank.anonymize — name anonymization for bias reduction."""

from __future__ import annotations

import json
from pathlib import Path

from cv_rank.anonymize import Anonymizer


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _people(*names: str) -> list[dict]:
    return [{"name": n} for n in names]


# ---------------------------------------------------------------------------
# Anonymizer
# ---------------------------------------------------------------------------

class TestAnonymizer:
    def test_deterministic_mapping(self):
        """Same input produces same mapping regardless of input order."""
        people_a = _people("Zara", "Alice", "Bob")
        people_b = _people("Bob", "Zara", "Alice")

        anon_a = Anonymizer(people_a)
        anon_b = Anonymizer(people_b)

        assert anon_a.get_id("Alice") == anon_b.get_id("Alice")
        assert anon_a.get_id("Bob") == anon_b.get_id("Bob")
        assert anon_a.get_id("Zara") == anon_b.get_id("Zara")

    def test_sequential_ids(self):
        """IDs are sequential CANDIDATE_001, 002, 003 in alphabetical order."""
        people = _people("Charlie", "Alice", "Bob")
        anon = Anonymizer(people)

        assert anon.get_id("Alice") == "CANDIDATE_001"
        assert anon.get_id("Bob") == "CANDIDATE_002"
        assert anon.get_id("Charlie") == "CANDIDATE_003"

    def test_roundtrip_id_name(self):
        """Can convert name → ID → name."""
        people = _people("Alice Chen", "Bob Martinez")
        anon = Anonymizer(people)

        for p in people:
            name = p["name"]
            anon_id = anon.get_id(name)
            assert anon.get_name(anon_id) == name

    def test_unknown_name_returns_original(self):
        anon = Anonymizer(_people("Alice"))
        assert anon.get_id("Unknown") == "Unknown"

    def test_unknown_id_returns_original(self):
        anon = Anonymizer(_people("Alice"))
        assert anon.get_name("CANDIDATE_999") == "CANDIDATE_999"


class TestAnonymizeProfile:
    def test_replaces_full_name(self):
        anon = Anonymizer(_people("Alice Chen"))
        text = "Name: Alice Chen\nTitle: Engineer"
        result = anon.anonymize_profile(text, "Alice Chen")

        assert "Alice Chen" not in result
        assert "CANDIDATE_001" in result

    def test_replaces_name_parts(self):
        """First and last name parts are replaced when >2 chars."""
        anon = Anonymizer(_people("Alice Chen"))
        text = "Alice is a great engineer. Her last name is Chen."
        result = anon.anonymize_profile(text, "Alice Chen")

        assert "Alice" not in result
        assert "Chen" not in result

    def test_short_name_parts_preserved(self):
        """Short name parts (<= 2 chars) are NOT replaced to avoid false matches."""
        anon = Anonymizer(_people("Li Bo"))
        text = "Li Bo works in AI research. Li is a common name."
        result = anon.anonymize_profile(text, "Li Bo")

        # Full name should be replaced
        assert "Li Bo" not in result
        # But standalone short parts may remain (to avoid replacing
        # "Li" in "Linear" etc.)

    def test_replaces_emails(self):
        anon = Anonymizer(_people("Alice Chen"))
        text = "Contact: alice.chen@example.com"
        result = anon.anonymize_profile(text, "Alice Chen")

        assert "alice.chen@example.com" not in result
        assert "@anon.example" in result

    def test_preserves_non_name_content(self):
        anon = Anonymizer(_people("Alice Chen"))
        text = "Name: Alice Chen\nCompany: Google\nRole: ML Engineer"
        result = anon.anonymize_profile(text, "Alice Chen")

        assert "Google" in result
        assert "ML Engineer" in result


class TestWrapFormatFn:
    def test_wraps_format_function(self):
        anon = Anonymizer(_people("Alice Chen"))

        def format_fn(person: dict) -> str:
            return f"Name: {person['name']}\nEmail: {person.get('email', 'n/a')}"

        wrapped = anon.wrap_format_fn(format_fn)
        result = wrapped({"name": "Alice Chen", "email": "alice@test.com"})

        assert "Alice Chen" not in result
        assert "alice@test.com" not in result
        assert "CANDIDATE_001" in result

    def test_wrapped_fn_preserves_signature(self):
        """Wrapped function accepts same args as original."""
        anon = Anonymizer(_people("Alice"))

        def format_fn(person: dict) -> str:
            return person.get("name", "")

        wrapped = anon.wrap_format_fn(format_fn)
        # Should accept a dict and return a string
        result = wrapped({"name": "Alice"})
        assert isinstance(result, str)


class TestSaveAndLoad:
    def test_roundtrip(self, tmp_path: Path):
        people = _people("Alice Chen", "Bob Martinez", "Carol Okafor")
        anon = Anonymizer(people)

        map_file = tmp_path / "anonymizer_map.json"
        anon.save(map_file)

        assert map_file.exists()

        restored = Anonymizer.from_map_file(map_file)

        for name in ["Alice Chen", "Bob Martinez", "Carol Okafor"]:
            assert restored.get_id(name) == anon.get_id(name)
            anon_id = anon.get_id(name)
            assert restored.get_name(anon_id) == name

    def test_saved_file_is_valid_json(self, tmp_path: Path):
        anon = Anonymizer(_people("Alice"))
        map_file = tmp_path / "map.json"
        anon.save(map_file)

        data = json.loads(map_file.read_text())
        assert "name_to_id" in data
