"""Tests for cv_rank.checkpoint — checkpoint/resume for pipeline runs."""

from __future__ import annotations

import json
import time
from pathlib import Path


from cv_rank.checkpoint import (
    create_run_dir,
    find_latest_run,
    get_completed_phases,
    load_checkpoint,
    save_checkpoint,
    save_meta,
)


class TestSaveLoadCheckpoint:
    """Tests for save_checkpoint() and load_checkpoint() round-trip."""

    def test_save_and_load_roundtrip(self, tmp_run_dir: Path) -> None:
        """Data saved to a checkpoint can be loaded back unchanged."""
        data = {
            "people": [
                {"name": "Alice", "score": 90},
                {"name": "Bob", "score": 75},
            ],
            "metadata": {"count": 2, "timestamp": "2026-02-22"},
        }

        save_checkpoint(tmp_run_dir, "enrichment", data)
        loaded = load_checkpoint(tmp_run_dir, "enrichment")

        assert loaded == data

    def test_load_nonexistent_phase_returns_none(self, tmp_run_dir: Path) -> None:
        """Loading a phase that has not been saved returns None."""
        result = load_checkpoint(tmp_run_dir, "nonexistent_phase")
        assert result is None

    def test_save_creates_json_file(self, tmp_run_dir: Path) -> None:
        """save_checkpoint creates a JSON file named after the phase."""
        save_checkpoint(tmp_run_dir, "pointwise", [{"name": "Alice", "score": 85}])

        phase_file = tmp_run_dir / "pointwise.json"
        assert phase_file.exists()

        content = json.loads(phase_file.read_text())
        assert len(content) == 1
        assert content[0]["name"] == "Alice"

    def test_save_updates_meta(self, tmp_run_dir: Path) -> None:
        """save_checkpoint updates the meta.json file with phase info."""
        save_checkpoint(tmp_run_dir, "enrichment", {"key": "value"})

        meta_path = tmp_run_dir / "meta.json"
        assert meta_path.exists()

        meta = json.loads(meta_path.read_text())
        assert "phases" in meta
        assert "enrichment" in meta["phases"]
        assert "completed_at" in meta["phases"]["enrichment"]
        assert "last_updated" in meta

    def test_save_multiple_phases(self, tmp_run_dir: Path) -> None:
        """Multiple phases can be saved sequentially, all tracked in meta."""
        save_checkpoint(tmp_run_dir, "enrichment", {"phase": 1})
        save_checkpoint(tmp_run_dir, "pointwise", {"phase": 2})
        save_checkpoint(tmp_run_dir, "swiss", {"phase": 3})

        meta = json.loads((tmp_run_dir / "meta.json").read_text())
        assert set(meta["phases"].keys()) == {"enrichment", "pointwise", "swiss"}

    def test_overwrite_checkpoint(self, tmp_run_dir: Path) -> None:
        """Saving the same phase again overwrites the previous data."""
        save_checkpoint(tmp_run_dir, "enrichment", {"version": 1})
        save_checkpoint(tmp_run_dir, "enrichment", {"version": 2})

        loaded = load_checkpoint(tmp_run_dir, "enrichment")
        assert loaded["version"] == 2

    def test_save_handles_non_serializable_via_default_str(self, tmp_run_dir: Path) -> None:
        """Non-JSON-serializable types are converted via default=str."""
        from pathlib import PurePosixPath

        data = {"path": PurePosixPath("/some/path"), "count": 42}
        save_checkpoint(tmp_run_dir, "test_phase", data)

        loaded = load_checkpoint(tmp_run_dir, "test_phase")
        assert loaded["path"] == "/some/path"
        assert loaded["count"] == 42


class TestGetCompletedPhases:
    """Tests for get_completed_phases()."""

    def test_no_meta_returns_empty(self, tmp_run_dir: Path) -> None:
        """If meta.json does not exist, return empty list."""
        # tmp_run_dir is fresh, no meta.json
        phases = get_completed_phases(tmp_run_dir)
        assert phases == []

    def test_returns_saved_phases(self, tmp_run_dir: Path) -> None:
        """Returns the list of phases that have been checkpointed."""
        save_checkpoint(tmp_run_dir, "enrichment", {})
        save_checkpoint(tmp_run_dir, "pointwise", {})

        phases = get_completed_phases(tmp_run_dir)
        assert "enrichment" in phases
        assert "pointwise" in phases
        assert len(phases) == 2

    def test_order_preserved(self, tmp_run_dir: Path) -> None:
        """Phases are returned in insertion order."""
        save_checkpoint(tmp_run_dir, "alpha", {})
        save_checkpoint(tmp_run_dir, "beta", {})
        save_checkpoint(tmp_run_dir, "gamma", {})

        phases = get_completed_phases(tmp_run_dir)
        assert phases == ["alpha", "beta", "gamma"]


class TestFindLatestRun:
    """Tests for find_latest_run()."""

    def test_no_runs_returns_none(self, tmp_path: Path) -> None:
        """If output dir has no run_* directories, returns None."""
        output_dir = tmp_path / "results"
        output_dir.mkdir()

        result = find_latest_run(output_dir)
        assert result is None

    def test_nonexistent_dir_returns_none(self, tmp_path: Path) -> None:
        """If the output directory does not exist, returns None."""
        result = find_latest_run(tmp_path / "nonexistent")
        assert result is None

    def test_finds_latest_by_mtime(self, tmp_path: Path) -> None:
        """With multiple run dirs, returns the most recently modified one."""
        output_dir = tmp_path / "results"
        output_dir.mkdir()

        # Create runs in order
        run1 = output_dir / "run_20260101_000000"
        run1.mkdir()
        # Write a file to set mtime
        (run1 / "meta.json").write_text("{}")

        # Small delay to ensure different mtime
        time.sleep(0.05)

        run2 = output_dir / "run_20260201_000000"
        run2.mkdir()
        (run2 / "meta.json").write_text("{}")

        time.sleep(0.05)

        run3 = output_dir / "run_20260301_000000"
        run3.mkdir()
        (run3 / "meta.json").write_text("{}")

        result = find_latest_run(output_dir)
        assert result is not None
        assert result.name == "run_20260301_000000"

    def test_ignores_non_run_dirs(self, tmp_path: Path) -> None:
        """Directories not matching run_* are ignored."""
        output_dir = tmp_path / "results"
        output_dir.mkdir()

        (output_dir / "other_dir").mkdir()
        (output_dir / "notes.txt").write_text("hello")

        run = output_dir / "run_test"
        run.mkdir()

        result = find_latest_run(output_dir)
        assert result is not None
        assert result.name == "run_test"


class TestCreateRunDir:
    """Tests for create_run_dir()."""

    def test_auto_generated_name(self, tmp_path: Path) -> None:
        """Without run_id, auto-generates run_YYYYMMDD_HHMMSS name."""
        output_dir = tmp_path / "results"
        output_dir.mkdir()

        run_dir = create_run_dir(output_dir)

        assert run_dir.exists()
        assert run_dir.parent == output_dir
        assert run_dir.name.startswith("run_")

    def test_explicit_run_id(self, tmp_path: Path) -> None:
        """When run_id is provided, use it as the directory name."""
        output_dir = tmp_path / "results"
        output_dir.mkdir()

        run_dir = create_run_dir(output_dir, run_id="run_custom_001")

        assert run_dir.name == "run_custom_001"
        assert run_dir.exists()

    def test_creates_parent_dirs(self, tmp_path: Path) -> None:
        """Parent directories are created if they do not exist."""
        output_dir = tmp_path / "deep" / "nested" / "results"

        run_dir = create_run_dir(output_dir, run_id="run_001")

        assert run_dir.exists()
        assert run_dir.parent == output_dir

    def test_idempotent_on_existing_dir(self, tmp_path: Path) -> None:
        """Creating a run dir that already exists does not error."""
        output_dir = tmp_path / "results"
        output_dir.mkdir()

        run1 = create_run_dir(output_dir, run_id="run_001")
        # Write a file inside
        (run1 / "data.json").write_text("{}")

        # Create again -- should not fail or lose data
        run2 = create_run_dir(output_dir, run_id="run_001")
        assert run2 == run1
        assert (run2 / "data.json").exists()


class TestSaveMeta:
    """Tests for save_meta()."""

    def test_save_meta_creates_file(self, tmp_run_dir: Path) -> None:
        """save_meta creates meta.json with provided kwargs."""
        save_meta(tmp_run_dir, csv_path="/path/to/file.csv", people_count=42)

        meta = json.loads((tmp_run_dir / "meta.json").read_text())
        assert meta["csv_path"] == "/path/to/file.csv"
        assert meta["people_count"] == 42

    def test_save_meta_updates_existing(self, tmp_run_dir: Path) -> None:
        """save_meta merges new values into existing meta."""
        save_meta(tmp_run_dir, key1="value1")
        save_meta(tmp_run_dir, key2="value2")

        meta = json.loads((tmp_run_dir / "meta.json").read_text())
        assert meta["key1"] == "value1"
        assert meta["key2"] == "value2"
