"""Checkpoint/resume for long-running cv-rank pipeline runs."""

import json
import time
from pathlib import Path
from typing import Any


def create_run_dir(output_dir: Path, run_id: str | None = None) -> Path:
    """Create a run directory. Auto-generates run_id if not provided."""
    if not run_id:
        from datetime import datetime
        run_id = f"run_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    run_dir = output_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    return run_dir


def save_checkpoint(run_dir: Path, phase: str, data: Any) -> Path:
    """Save checkpoint data for a pipeline phase."""
    path = run_dir / f"{phase}.json"
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, default=str)

    # Update meta
    meta_path = run_dir / "meta.json"
    meta = {}
    if meta_path.exists():
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)

    meta.setdefault("phases", {})
    meta["phases"][phase] = {
        "completed_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "file": str(path.name),
    }
    meta["last_updated"] = time.strftime("%Y-%m-%dT%H:%M:%S")

    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2)

    return path


def load_checkpoint(run_dir: Path, phase: str) -> Any | None:
    """Load checkpoint data for a phase. Returns None if not found."""
    path = run_dir / f"{phase}.json"
    if not path.exists():
        return None
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def get_completed_phases(run_dir: Path) -> list[str]:
    """Get list of completed phases for a run."""
    meta_path = run_dir / "meta.json"
    if not meta_path.exists():
        return []
    with open(meta_path, encoding="utf-8") as f:
        meta = json.load(f)
    return list(meta.get("phases", {}).keys())


def find_latest_run(output_dir: Path) -> Path | None:
    """Find the most recent run directory."""
    if not output_dir.exists():
        return None
    runs = sorted(output_dir.glob("run_*"), key=lambda p: p.stat().st_mtime, reverse=True)
    return runs[0] if runs else None


def save_meta(run_dir: Path, **kwargs):
    """Save/update run metadata."""
    meta_path = run_dir / "meta.json"
    meta = {}
    if meta_path.exists():
        with open(meta_path, encoding="utf-8") as f:
            meta = json.load(f)
    meta.update(kwargs)
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, default=str)
