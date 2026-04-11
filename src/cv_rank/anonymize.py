"""
Name anonymization for bias reduction in LLM scoring.

Replaces candidate names with deterministic anonymous IDs (CANDIDATE_001, etc.)
to prevent demographic bias from names like "Fatima Al-Rashid" or "Wei Zhang".

Evidence: 85% racial bias in resume screening (UW 2024).
"""

from __future__ import annotations

import json
import logging
import re
from pathlib import Path
from typing import Callable

logger = logging.getLogger("cv_rank.anonymize")


class Anonymizer:
    """Deterministic name anonymization for the ranking pipeline.

    Creates a stable mapping from real names to anonymous IDs
    (CANDIDATE_001, CANDIDATE_002, ...) sorted alphabetically for
    reproducibility. The mapping is saved to disk for checkpoint resume.

    Parameters
    ----------
    people:
        List of person dicts, each with a ``"name"`` key.
    """

    def __init__(self, people: list[dict]) -> None:
        names = sorted({p.get("name", "?") for p in people})
        self._name_to_id: dict[str, str] = {}
        self._id_to_name: dict[str, str] = {}

        for i, name in enumerate(names, 1):
            anon_id = f"CANDIDATE_{i:03d}"
            self._name_to_id[name] = anon_id
            self._id_to_name[anon_id] = name

        logger.info("Anonymizer: mapped %d names to anonymous IDs", len(self._name_to_id))

    @classmethod
    def from_map_file(cls, path: Path) -> Anonymizer:
        """Restore an Anonymizer from a saved mapping file."""
        instance = cls.__new__(cls)
        data = json.loads(path.read_text())
        instance._name_to_id = data["name_to_id"]
        instance._id_to_name = {v: k for k, v in data["name_to_id"].items()}
        return instance

    def save(self, path: Path) -> None:
        """Save the mapping to a JSON file for checkpoint resume."""
        data = {"name_to_id": self._name_to_id}
        path.write_text(json.dumps(data, indent=2))

    def get_id(self, name: str) -> str:
        """Get the anonymous ID for a real name."""
        return self._name_to_id.get(name, name)

    def get_name(self, anon_id: str) -> str:
        """Get the real name for an anonymous ID."""
        return self._id_to_name.get(anon_id, anon_id)

    def anonymize_profile(self, text: str, name: str) -> str:
        """Replace name references and email addresses in profile text.

        Replaces:
        - Full name
        - First name (if multi-word name)
        - Last name (if multi-word name)
        - Email addresses
        """
        anon_id = self.get_id(name)
        if anon_id == name:
            logger.warning("Anonymizer: name %r not in mapping, profile will not be anonymized", name)

        # Replace full name first (longest match, case-insensitive)
        result = re.sub(re.escape(name), anon_id, text, flags=re.IGNORECASE)

        # Replace first/last name parts
        parts = name.split()
        if len(parts) >= 2:
            first = parts[0]
            last = parts[-1]
            # Only replace standalone words >2 chars to avoid matching "Al" in "Algorithm"
            if len(first) > 2:
                result = re.sub(rf'\b{re.escape(first)}\b', anon_id, result, flags=re.IGNORECASE)
            if len(last) > 2:
                result = re.sub(rf'\b{re.escape(last)}\b', anon_id, result, flags=re.IGNORECASE)

        # Replace email addresses
        result = re.sub(r'[\w.+-]+@[\w.-]+\.\w+', f'{anon_id.lower()}@anon.example', result)

        return result

    def wrap_format_fn(self, format_fn: Callable[[dict], str]) -> Callable[[dict], str]:
        """Wrap a format_profile function to anonymize its output.

        Returns a new callable with the same signature that:
        1. Calls the original format_fn
        2. Anonymizes the resulting text
        """
        def anonymized_format(person: dict) -> str:
            original = format_fn(person)
            name = person.get("name", "?")
            result = self.anonymize_profile(original, name)
            # Check if any real name fragments leaked through
            if name != "?" and name.lower() in result.lower():
                logger.warning("Anonymize leak: %r still appears in anonymized profile", name)
            logger.debug("Anonymized: %s → %s (%d chars)", name, self.get_id(name), len(result))
            return result

        return anonymized_format
