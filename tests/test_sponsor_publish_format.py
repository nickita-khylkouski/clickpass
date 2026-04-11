from __future__ import annotations

from pathlib import Path

from cv_rank.sponsor_automation.publish import _format_headline_bank_for_packet, _render_packet
from cv_rank.sponsor_automation.dense_packet import _build_tool_breadth_rows, _resolve_audience_context


def test_headline_bank_table_is_rendered_as_sentence_bullets() -> None:
    table = """# Sponsor Headline Bank (Event-Specific)

Event: Sample Event

| # | Headline | Numbers | Why this matters |
|---|---|---|---|
| 1 | **Headline A** | A: `100`; B: `200`. | Strong signal. |
| 2 | **Headline B** | C: `55/80`. | Useful proof. |
"""
    rendered = _format_headline_bank_for_packet(table)
    assert "Event: Sample Event" in rendered
    assert "| # | Headline | Numbers | Why this matters |" not in rendered
    assert "- Headline A." in rendered
    assert "- Headline B." in rendered
    assert "`" not in rendered


def test_headline_bank_filters_zero_and_small_value_rows() -> None:
    table = """# Sponsor Headline Bank (Event-Specific)

Event: Sample Event

| # | Headline | Numbers | Why this matters |
|---|---|---|---|
| 1 | **Small line** | value: `10`. | Low signal. |
| 2 | **Zero line** | value: `0`. | Low signal. |
| 3 | **Big line** | value: `250`. | High signal. |
"""
    rendered = _format_headline_bank_for_packet(table)
    assert "- Big line." in rendered
    assert "Small line" not in rendered
    assert "Zero line" not in rendered


def test_render_packet_skips_appending_headline_bank_when_section_already_present() -> None:
    rendered = _render_packet(
        event_name="Sample Event",
        event_slug="sample-event",
        run_id="run_123",
        narrative_markdown="## Executive Summary\n\n- Hello\n\n## Sponsor Headline Options (Big-Number Angles)\n\n- Existing headline",
        headline_bank_text="Event: Sample Event\n\n- New headline",
        copied_csv_paths=[],
        claims_path=Path("/tmp/claims.csv"),
    )
    assert rendered.count("## Sponsor Headline Options (Big-Number Angles)") == 1
    assert "New headline" not in rendered


def test_build_tool_breadth_rows_buckets_documented_teams() -> None:
    teams = [
        {"parsed_partner_tools": ["MongoDB"]},
        {"parsed_partner_tools": ["MongoDB", "Fireworks"]},
        {"parsed_partner_tools": ["MongoDB", "Fireworks", "Vercel"]},
        {"parsed_partner_tools": ["MongoDB", "Fireworks", "Vercel", "Voyage AI"]},
        {"parsed_partner_tools": ["MongoDB", "Fireworks", "Vercel", "Voyage AI", "Coinbase"]},
    ]
    rows = _build_tool_breadth_rows(teams, {"MongoDB", "Fireworks", "Vercel", "Voyage AI", "Coinbase"})
    assert rows == [
        ("1 tool", 1),
        ("2 tools", 1),
        ("3 tools", 1),
        ("4 tools", 1),
        ("5+ tools", 1),
    ]


def test_resolve_audience_context_falls_back_to_approved_when_checkins_are_suspect() -> None:
    profiles = [
        {"current_event_status": "approved", "current_event_checked_in": False, "current_event_submissions": [{"submission_id": "s1"}]},
        {"current_event_status": "approved", "current_event_checked_in": True, "current_event_submissions": [{"submission_id": "s2"}]},
        {"current_event_status": "approved", "current_event_checked_in": False, "current_event_submissions": [{"submission_id": "s3"}]},
    ] * 30

    context = _resolve_audience_context(profiles)

    assert context["suspect"] is True
    assert context["stage_title"] == "Approved"
    assert context["label"] == "approved participants"
    assert len(context["profiles"]) == 90
