from __future__ import annotations

import argparse
import csv
import gzip
import json
import random
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import pandas as pd


BASE_DIR = Path(__file__).resolve().parent.parent
LOCAL_DATA_DIR = BASE_DIR / "local_data"
AGENT_ANALYSIS_DIR = LOCAL_DATA_DIR / "agent_analysis"
PLATFORM_TABLES_DIR = LOCAL_DATA_DIR / "platform" / "tables"
PORTAL_DIR = LOCAL_DATA_DIR / "agent_portal"
PROFILES_DIR = PORTAL_DIR / "profiles"
SAMPLES_DIR = PORTAL_DIR / "samples"

MAX_VALUE_LEN = 140
DEFAULT_SAMPLE_ROWS = 10
DEFAULT_EXAMPLE_VALUES = 10


def trim_value(value: Any, *, max_len: int = MAX_VALUE_LEN) -> str:
    if value is None:
        return ""
    text = str(value).replace("\r", " ").replace("\n", " ").strip()
    if len(text) > max_len:
        return text[: max_len - 3] + "..."
    return text


def infer_type(example_values: list[str]) -> str:
    if not example_values:
        return "unknown"
    lowered = [value.lower() for value in example_values]
    if all(value in {"true", "false"} for value in lowered):
        return "bool_like"
    try:
        for value in example_values:
            int(value)
        return "int_like"
    except Exception:
        pass
    try:
        for value in example_values:
            float(value)
        return "float_like"
    except Exception:
        pass
    datetime_hits = 0
    for value in example_values:
        try:
            pd.to_datetime(value)
            datetime_hits += 1
        except Exception:
            pass
    if datetime_hits >= max(2, len(example_values) // 2):
        return "datetime_like"
    if all(value.startswith("{") or value.startswith("[") for value in example_values[: min(3, len(example_values))]):
        return "json_like"
    if max(len(value) for value in example_values) > 80:
        return "long_text"
    return "string"


def choose_open(path: Path):
    if path.suffix == ".gz":
        return gzip.open
    return open


def profile_csv(
    path: Path,
    *,
    sample_rows: int = DEFAULT_SAMPLE_ROWS,
    example_values: int = DEFAULT_EXAMPLE_VALUES,
) -> dict[str, Any]:
    opener = choose_open(path)
    rng = random.Random(0)
    total_rows = 0
    sample_records: list[dict[str, str]] = []
    column_stats: dict[str, dict[str, Any]] = {}
    columns: list[str] | None = None

    with opener(path, "rt", newline="") as fh:
        reader = csv.DictReader(fh)
        columns = reader.fieldnames or []
        for column in columns:
            column_stats[column] = {
                "non_null_rows": 0,
                "blank_rows": 0,
                "example_values": [],
                "_seen_examples": set(),
                "max_value_length": 0,
            }

        for row in reader:
            total_rows += 1
            cleaned_row = {key: trim_value(value) for key, value in row.items()}
            if len(sample_records) < sample_rows:
                sample_records.append(cleaned_row)
            else:
                choice = rng.randint(1, total_rows)
                if choice <= sample_rows:
                    sample_records[choice - 1] = cleaned_row

            for column in columns:
                raw = row.get(column)
                text = trim_value(raw)
                stats = column_stats[column]
                stats["max_value_length"] = max(stats["max_value_length"], len(text))
                if text == "":
                    stats["blank_rows"] += 1
                    continue
                stats["non_null_rows"] += 1
                seen_examples = stats["_seen_examples"]
                if len(seen_examples) < example_values and text not in seen_examples:
                    seen_examples.add(text)
                    stats["example_values"].append(text)

    sample_path = SAMPLES_DIR / f"{path.name}.sample.csv"
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)
    if sample_records:
        pd.DataFrame(sample_records).to_csv(sample_path, index=False)

    profiled_columns: list[dict[str, Any]] = []
    for column in columns or []:
        stats = column_stats[column]
        examples = stats["example_values"]
        profiled_columns.append(
            {
                "name": column,
                "non_null_rows": stats["non_null_rows"],
                "blank_rows": stats["blank_rows"],
                "null_fraction": round(
                    (stats["blank_rows"] / total_rows) if total_rows else 0.0,
                    4,
                ),
                "max_value_length": stats["max_value_length"],
                "example_values": examples,
                "inferred_type": infer_type(examples),
            }
        )

    return {
        "path": str(path.relative_to(BASE_DIR)),
        "bytes": path.stat().st_size,
        "modified_at": datetime.fromtimestamp(path.stat().st_mtime, tz=timezone.utc).isoformat(),
        "rows": total_rows,
        "columns": len(columns or []),
        "column_profiles": profiled_columns,
        "sample_rows_path": str(sample_path.relative_to(BASE_DIR)) if sample_records else None,
        "sample_rows": sample_records,
    }


def load_agent_catalog() -> dict[str, Any]:
    catalog_path = AGENT_ANALYSIS_DIR / "catalog.json"
    if not catalog_path.exists():
        return {}
    try:
        return json.loads(catalog_path.read_text())
    except Exception:
        return {}


def file_group(path: Path) -> str:
    rel = path.relative_to(LOCAL_DATA_DIR)
    if len(rel.parts) >= 2 and rel.parts[0] == "platform" and rel.parts[1] == "tables":
        return "platform_tables"
    return rel.parts[0] if rel.parts else "unknown"


def build_index() -> dict[str, Any]:
    PROFILES_DIR.mkdir(parents=True, exist_ok=True)
    SAMPLES_DIR.mkdir(parents=True, exist_ok=True)

    agent_catalog = load_agent_catalog().get("files", {})

    files_to_profile: list[Path] = []
    files_to_profile.extend(sorted(AGENT_ANALYSIS_DIR.glob("*.csv")))
    files_to_profile.extend(sorted(PLATFORM_TABLES_DIR.glob("*.csv.gz")))

    profiles: dict[str, Any] = {}
    by_group: dict[str, list[str]] = {"agent_analysis": [], "platform_tables": []}
    warnings: list[dict[str, str]] = []
    for path in files_to_profile:
        key = path.name
        try:
            profile = profile_csv(path)
        except Exception as exc:
            warnings.append({"path": str(path.relative_to(BASE_DIR)), "error": str(exc)})
            continue
        catalog_entry = agent_catalog.get(key) or agent_catalog.get(path.stem) or {}
        merged = {
            "group": file_group(path),
            "filename": path.name,
            "catalog": catalog_entry,
            **profile,
        }
        profile_path = PROFILES_DIR / f"{path.name}.json"
        profile_path.write_text(json.dumps(merged, indent=2) + "\n")
        profiles[key] = {
            "path": merged["path"],
            "rows": merged["rows"],
            "columns": merged["columns"],
            "bytes": merged["bytes"],
            "profile_path": str(profile_path.relative_to(BASE_DIR)),
            "sample_rows_path": merged["sample_rows_path"],
            "group": merged["group"],
        }
        by_group.setdefault(merged["group"], []).append(key)

    index = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "profiles": profiles,
        "groups": by_group,
        "recommended_load_order": [
            "agent_analysis/events_core.csv",
            "agent_analysis/applicants_core.csv",
            "agent_analysis/event_acquisition_rollup.csv",
            "agent_analysis/signup_velocity.csv",
            "agent_analysis/posthog_event_velocity.csv",
        ],
        "notes": [
            "Use the profile JSONs for column-level examples and sample rows.",
            "Use the sample CSVs for quick inspection without loading the full file.",
            "Platform table snapshots are optional fallbacks when the compact pack is insufficient.",
        ],
        "warnings": warnings,
    }
    (PORTAL_DIR / "index.json").write_text(json.dumps(index, indent=2) + "\n")

    lines = [
        "# Agent Data Portal",
        "",
        "Fast entrypoint for analysis agents.",
        "",
        "## Recommended Start",
        "",
        "1. Read `index.json`.",
        "2. Open `agent_analysis/events_core.csv` for event-level work.",
        "3. Open `agent_analysis/applicants_core.csv` for person-level work.",
        "4. Use the per-file profile JSON for column examples and sample rows.",
        "5. Use the live query scripts when you need data beyond the local snapshots.",
        "",
        "## Files",
        "",
    ]
    for key, meta in profiles.items():
        lines.extend(
            [
                f"### {key}",
                f"- path: `{meta['path']}`",
                f"- rows: {meta['rows']}",
                f"- columns: {meta['columns']}",
                f"- sample rows: `{meta['sample_rows_path']}`",
                f"- profile: `{meta['profile_path']}`",
                "",
            ]
        )
    (PORTAL_DIR / "README.md").write_text("\n".join(lines) + "\n")
    return index


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Build agent-friendly data portal metadata for local_data.")
    return parser.parse_args()


def main() -> None:
    parse_args()
    PORTAL_DIR.mkdir(parents=True, exist_ok=True)
    build_index()
    print(f"Wrote {PORTAL_DIR / 'index.json'}")


if __name__ == "__main__":
    main()
