from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

from cv_rank.sponsor_automation.analysis_helper import (
    _build_tool_summary_rows,
    _resolve_event_name,
    resolve_enriched_json,
)


def _load_bundle_builder_module():
    repo_root = Path(__file__).resolve().parents[1]
    script_path = repo_root / "scripts" / "build_sponsor_delivery_bundle.py"
    spec = importlib.util.spec_from_file_location("bundle_builder_test", script_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"Could not load {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_analysis_helper_resolve_enriched_json_prefers_run_local_fallback(tmp_path: Path) -> None:
    base = tmp_path
    run_target = base / "results" / "run_target"
    run_target.mkdir(parents=True)
    run_other = base / "results" / "run_other"
    run_other.mkdir(parents=True)

    target_enriched = run_target / "enriched.json"
    target_enriched.write_text("[]", encoding="utf-8")
    other_complete = run_other / "enriched_complete_other.json"
    other_complete.write_text("[]", encoding="utf-8")

    resolved = resolve_enriched_json(base, "run_target", None)
    assert resolved == target_enriched.resolve()


def test_bundle_builder_resolve_enriched_json_prefers_run_local_fallback(tmp_path: Path) -> None:
    module = _load_bundle_builder_module()
    base = tmp_path
    run_target = base / "results" / "run_target"
    run_target.mkdir(parents=True)
    run_other = base / "results" / "run_other"
    run_other.mkdir(parents=True)

    target_enriched = run_target / "enriched.json"
    target_enriched.write_text("[]", encoding="utf-8")
    other_complete = run_other / "enriched_complete_other.json"
    other_complete.write_text("[]", encoding="utf-8")

    resolved = module._resolve_enriched_json(base, "run_target", None)
    assert resolved == target_enriched.resolve()


def test_resolve_event_name_from_platform_meta(tmp_path: Path) -> None:
    repo_root = tmp_path
    run_dir = repo_root / "results" / "run_123"
    run_dir.mkdir(parents=True)
    meta_path = run_dir / "meta.json"
    meta_path.write_text(
        json.dumps({"csv_path": "Platform DB event: Agentic Orchestration and Collaboration Hackathon"}),
        encoding="utf-8",
    )

    event_name = _resolve_event_name(repo_root, "run_123", None)
    assert event_name == "Agentic Orchestration and Collaboration Hackathon"


def test_build_tool_summary_rows_normalizes_evidence_to_allowed_tools(tmp_path: Path) -> None:
    metrics_csv = tmp_path / "metrics.csv"
    metrics_csv.write_text(
        "\n".join(
            [
                "raw_metric,metric_name,numerator,denominator,coverage_pct",
                "submitter_tool_adoption_mongodb,mongodb,10,20,50.0",
                "team_tool_adoption_mongodb,mongodb,6,10,60.0",
                "team_tool_adoption_fireworks,fireworks,4,10,40.0",
                "q8_teams_using_multiple_tools_2plus,multi,7,10,70.0",
            ]
        ),
        encoding="utf-8",
    )
    evidence_csv = tmp_path / "evidence.csv"
    evidence_csv.write_text(
        "\n".join(
            [
                "project_id,tool_category,tool_name",
                "p1,provider,Https",
                "p1,provider,Atlas",
                "p1,provider,MongoDB",
                "p2,provider,Firework",
                "p2,provider,Fireworks",
                "p3,provider,Voyage Ai",
                "p4,model,gemini-2.5-flash",
            ]
        ),
        encoding="utf-8",
    )

    rows = _build_tool_summary_rows(metrics_csv, evidence_csv)
    evidence_rows = [row for row in rows if row["group"] == "evidence_project"]
    assert {"name": "MongoDB", "count": "1"} in [{k: row[k] for k in ("name", "count")} for row in evidence_rows]
    assert {"name": "Fireworks", "count": "1"} in [{k: row[k] for k in ("name", "count")} for row in evidence_rows]
    assert all(row["name"] != "Https" for row in evidence_rows)
    assert all(row["name"] != "Voyage AI" for row in evidence_rows)
