from __future__ import annotations

from cv_rank.sponsor_automation.orchestrator import (
    Claim,
    _claim_reviewer_block_reasons,
    _is_noise_row,
    _sanitize_narrative,
)


def _claim(*, section: str, metric_name: str, numerator: str, denominator: str) -> Claim:
    return Claim(
        claim_id="claim_test",
        section=section,
        metric_name=metric_name,
        segment="all",
        value="0",
        numerator=numerator,
        denominator=denominator,
        denominator_cohort="submitters",
        coverage_pct="100",
        confidence="high",
        source_table_or_file="platform_db.EventApplicant(event=test)",
        claim_label="observed_in_code",
        notes="",
        claim_text="",
    )


def test_reviewer_blocks_tautology_external_url_metric() -> None:
    claim = _claim(
        section="adoption",
        metric_name="q10_projects_with_external_base_urls",
        numerator="75",
        denominator="75",
    )
    reasons = _claim_reviewer_block_reasons(claim)
    assert "tautology_metric_external_urls" in reasons


def test_reviewer_blocks_zero_signal_story_metrics() -> None:
    claim = _claim(
        section="marketing",
        metric_name="q8_teams_using_multiple_tools_2plus",
        numerator="0",
        denominator="75",
    )
    reasons = _claim_reviewer_block_reasons(claim)
    assert "zero_signal_story_metric" in reasons


def test_noise_filter_blocks_trivial_metrics_before_claim_generation() -> None:
    row = {
        "section": "sectionE_exec",
        "metric_name": "q10_projects_with_external_base_urls",
        "segment": "all",
        "numerator": "58",
        "denominator": "58",
        "source_table_or_file": "platform_db.HackathonSubmission",
    }
    assert _is_noise_row(row) is True

    row_zero_story = {
        "section": "sectionF_exec",
        "metric_name": "q13_partner_story_anchor",
        "segment": "all",
        "numerator": "0",
        "denominator": "75",
        "source_table_or_file": "platform_db.EventApplicant",
    }
    assert _is_noise_row(row_zero_story) is True


def test_reviewer_keeps_nontrivial_funnel_claim() -> None:
    claim = _claim(
        section="funnel",
        metric_name="approved_to_checked_in",
        numerator="269",
        denominator="466",
    )
    reasons = _claim_reviewer_block_reasons(claim)
    assert not reasons


def test_reviewer_blocks_tiny_denominator_hiring_claim() -> None:
    claim = _claim(
        section="hiring",
        metric_name="q1_hiring_ready_pool_checked_in",
        numerator="2",
        denominator="3",
    )
    reasons = _claim_reviewer_block_reasons(claim)
    assert "tiny_denominator_for_business_narrative" in reasons


def test_sanitize_removes_tiny_delta_wording() -> None:
    raw = """## Sales/BD Insights

- Founders checked in at 60.9% versus 58.4% for decision-makers, with founders slightly higher.
- Decision-makers converted after check-in at 53.4% vs 49.4% for founders.
"""
    cleaned = _sanitize_narrative(raw)
    assert "slightly higher" not in cleaned.lower()
    assert "53.4%" in cleaned
