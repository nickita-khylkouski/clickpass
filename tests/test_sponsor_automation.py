"""Offline tests for sponsor automation bundle generation."""

from __future__ import annotations

import csv
import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[1] / "scripts" / "build_sponsor_delivery_bundle.py"

EXPECTED_METRICS_COLUMNS = [
    "section",
    "metric_name",
    "segment",
    "value",
    "numerator",
    "denominator",
    "denominator_cohort",
    "coverage_pct",
    "source_table_or_file",
    "calculation_note",
    "confidence",
]

EXPECTED_REPORT_SECTION_ORDER = [
    "## Executive Summary",
    "## Funnel",
    "## Hiring Insights",
    "## Sales/BD Insights",
    "## Market Research Insights",
    "## Marketing Insights",
    "## Sponsor Tech Adoption",
    "## Recommendations (next 30/60 days)",
    "## Appendix: Metric Definitions + Denominators + Coverage",
]


@pytest.fixture(scope="module")
def sponsor_module() -> ModuleType:
    """Load the sponsor bundle script as a module for direct function testing."""
    spec = importlib.util.spec_from_file_location("build_sponsor_delivery_bundle", SCRIPT_PATH)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    try:
        yield module
    finally:
        sys.modules.pop(spec.name, None)


def _row(mod: ModuleType, **overrides):
    base = {
        "section": "funnel",
        "metric": "applied_to_approved",
        "value": "65.93",
        "numerator": "360",
        "denominator": "546",
        "denominator_cohort": "all_applicants",
        "coverage_pct": "100.0%",
        "source": "platform_db.EventApplicant(eventId=abc123)",
        "pii_classification": "PII-safe",
        "confidence": "high",
        "claim_label": "observed_in_code",
        "notes": "deterministic formula",
    }
    base.update(overrides)
    return mod.MetricRow(**base)


def test_write_sponsor_metrics_long_enforces_strict_schema(
    sponsor_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    metrics_path = tmp_path / "sponsor_metrics_long.csv"
    metrics_full_path = tmp_path / "sponsor_metrics_long_full.csv"
    monkeypatch.setattr(sponsor_module, "OUT_METRICS", metrics_path)
    monkeypatch.setattr(sponsor_module, "OUT_METRICS_FULL", metrics_full_path)

    rows = [
        _row(
            sponsor_module,
            metric="funnel.applied_to_approved",
            coverage_pct="87.5%",
            denominator_cohort="approved",
            numerator="14",
            denominator="16",
        ),
        _row(
            sponsor_module,
            metric="sectionI_exec.i8_small_sample_caveat_claim_count",
            value="12.0",
            numerator="12",
            denominator="121",
            denominator_cohort="submitters",
            confidence="medium",
            claim_label="heuristic_observed",
        ),
    ]

    written_rows = sponsor_module.write_sponsor_metrics_long(rows)
    assert len(written_rows) == 2
    assert written_rows[0]["metric_name"] == "applied_to_approved"
    assert written_rows[0]["segment"] == "funnel"
    assert written_rows[0]["coverage_pct"] == "87.5"
    assert int(written_rows[0]["numerator"]) <= int(written_rows[0]["denominator"])
    assert written_rows[1]["metric_name"] == "i8_small_sample_caveat_claim_count"
    assert written_rows[1]["segment"] == "sectionI_exec"

    with metrics_path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        strict_rows = list(reader)
        assert reader.fieldnames == EXPECTED_METRICS_COLUMNS
    assert strict_rows[1]["denominator_cohort"] == "submitters"

    with metrics_full_path.open(newline="") as handle:
        reader = csv.DictReader(handle)
        full_rows = list(reader)
    assert reader.fieldnames == EXPECTED_METRICS_COLUMNS + ["pii_classification", "claim_label", "raw_metric"]
    assert full_rows[1]["raw_metric"] == "sectionI_exec.i8_small_sample_caveat_claim_count"


def test_write_report_filters_disallowed_denominator_cohorts_from_appendix(
    sponsor_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "sponsor_report_pii_safe.md"
    monkeypatch.setattr(sponsor_module, "OUT_REPORT", report_path)

    rows = [
        _row(sponsor_module, section="audit", metric="allowed_row", denominator_cohort="submitters"),
        _row(sponsor_module, section="audit", metric="disallowed_row", denominator_cohort="unknown_bucket"),
    ]
    sponsor_module.write_report(rows)

    report_text = report_path.read_text()
    assert "| audit | allowed_row |" in report_text
    assert "| audit | disallowed_row |" not in report_text


def test_write_report_keeps_small_sample_flags(
    sponsor_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "sponsor_report_pii_safe.md"
    monkeypatch.setattr(sponsor_module, "OUT_REPORT", report_path)

    rows = [
        _row(
            sponsor_module,
            section="I8",
            metric="caveat_claim_count.small_sample",
            value="49.569",
            numerator="2185",
            denominator="4408",
            denominator_cohort="all_applicants",
            confidence="medium",
        )
    ]
    sponsor_module.write_report(rows)

    report_text = report_path.read_text()
    assert "small_sample" in report_text
    assert "| I8 | caveat_claim_count.small_sample | 49.569 | 2185 | 4408 | all_applicants |" in report_text


def test_write_report_uses_stable_packet_section_order(
    sponsor_module: ModuleType,
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
) -> None:
    report_path = tmp_path / "sponsor_report_pii_safe.md"
    monkeypatch.setattr(sponsor_module, "OUT_REPORT", report_path)

    sponsor_module.write_report([])
    report_text = report_path.read_text()
    headings = [line for line in report_text.splitlines() if line.startswith("## ")]

    assert headings == EXPECTED_REPORT_SECTION_ORDER


def test_parse_partner_tools_detects_sentence_mentions_for_snowflake_event_tools(
    sponsor_module: ModuleType,
) -> None:
    text = (
        "DocRight Context uses CrewAI agents with Composio Slack tools, "
        "routes secure payments through Skyfire, and stores workflow state in Snowflake."
    )

    parsed = sponsor_module.parse_partner_tools(text)

    assert parsed == ["Composio", "CrewAI", "Skyfire", "Snowflake"]
