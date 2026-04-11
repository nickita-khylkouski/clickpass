from __future__ import annotations

import csv
import importlib.util
import math
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

from cv_rank.sponsor_automation.io import load_enriched_json

THEME_RULES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("DevTools/Infra", ("developer tool", "devtool", "infra", "infrastructure", "observability", "deployment", "orchestration", "sdk", "api", "agent ops", "workflow engine")),
    ("Fintech/Payments", ("payment", "wallet", "finance", "fintech", "trading", "bank", "credit", "defi", "coinbase", "invoice")),
    ("Creative Media/Gaming", ("music", "video", "creator", "media", "art", "design", "gaming", "game", "avatar", "audio", "film")),
    ("Productivity/Collaboration", ("collaboration", "workspace", "meeting", "notes", "document", "knowledge base", "productivity", "task manager", "assistant for teams")),
    ("Security/Compliance", ("security", "compliance", "fraud", "auth", "identity", "risk", "governance")),
    ("Health/Bio", ("health", "medical", "patient", "therapy", "clinic", "biotech", "bio")),
    ("Real Estate/Location", ("real estate", "property", "housing", "rent", "apartment", "location", "map", "travel")),
    ("Education/Knowledge", ("education", "tutor", "learning", "study", "student", "course", "knowledge")),
    ("Enterprise Ops/Sales", ("sales", "crm", "support", "operations", "back office", "recruiting", "workflow automation")),
)

MANGO_KEYWORDS = (
    "google",
    "microsoft",
    "meta",
    "amazon",
    "aws",
    "apple",
    "netflix",
)

GENERIC_EVIDENCE_TOOL_NAMES = {
    "",
    "https",
    "http",
    "github.com",
    "youtu.be",
    "www.youtube.com",
    "youtube.com",
    "www.loom.com",
    "loom.com",
}

EVIDENCE_TOOL_ALIAS_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
    (re.compile(r"\bcomposio\b|\bcomposio\s*toolset\b", re.I), "Composio"),
    (re.compile(r"\bcrew\s*ai\b|\bcrewai\b", re.I), "CrewAI"),
    (re.compile(r"\bskyfire\b", re.I), "Skyfire"),
    (re.compile(r"\bsnowflake\b|\bsnowpark\b|\bsnowflake[-_ ]connector\b|\bsnowflake\s+cortex\b", re.I), "Snowflake"),
    (re.compile(r"\bmongodb\b|\batlas\b", re.I), "MongoDB"),
    (re.compile(r"\bfireworks?\b", re.I), "Fireworks"),
    (re.compile(r"\bvoyage(?:\s*ai)?\b|\bvoyagerai\b|\bvoyageai\b", re.I), "Voyage AI"),
    (re.compile(r"\bcoinbase\b|\bcoin\s*base\b|\bcdp\b", re.I), "Coinbase"),
    (re.compile(r"\bvercel\b", re.I), "Vercel"),
    (re.compile(r"\bnvidia\b|\bnemo\b", re.I), "NVIDIA"),
    (re.compile(r"\bthesys\b", re.I), "Thesys"),
    (re.compile(r"\bgalileo\b", re.I), "Galileo"),
)


def build_dense_packet_markdown(
    *,
    repo_root: Path,
    event_name: str | None,
    run_id: str | None,
    enriched_json: str | None,
    metrics_csv: Path,
    metrics_full_csv: Path,
    evidence_csv: Path,
) -> str:
    resolved_event_name = event_name or _resolve_event_name(repo_root, run_id)
    enriched_path = _resolve_enriched_json(repo_root, run_id, enriched_json)
    bundle_module = _load_bundle_script_module(repo_root)

    profiles = load_enriched_json(enriched_path)
    snapshot = bundle_module.load_event_snapshot(resolved_event_name)
    if snapshot is None:
        raise RuntimeError(f"Could not load event snapshot for event={resolved_event_name!r}")
    bundle_module.hydrate_enriched_with_event_snapshot(profiles, snapshot)
    bundle_module.validate_snapshot_alignment(profiles, snapshot, allow_mismatch=False)

    checked = [p for p in profiles if _is_checked_in(p)]
    approved = [p for p in profiles if _is_approved(p)]
    submitter_people = [p for p in profiles if _is_submitter(p)]
    placed_people = [p for p in profiles if _is_placed(p)]
    teams = list(snapshot.get("submissions", []))
    finalist_teams = [sub for sub in teams if _safe_str(sub.get("placement")).strip()]
    audience_context = _resolve_audience_context(profiles)
    audience_profiles = audience_context["profiles"]
    audience_count = len(audience_profiles)
    audience_stage_title = audience_context["stage_title"]
    audience_label = audience_context["label"]
    audience_label_title = audience_context["label_title"]
    attendance_is_suspect = audience_context["suspect"]

    metrics = _load_metrics(metrics_csv)
    metrics_full_rows = _load_csv_rows(metrics_full_csv)
    evidence_rows = _load_csv_rows(evidence_csv)

    sponsor_people_counts, sponsor_team_counts, tool_pair_rows = _parse_tool_metric_rows(metrics_full_rows)
    event_sponsor_tools = set(snapshot.get("event_partner_tools") or [])
    if event_sponsor_tools:
        sponsor_people_counts = {name: count for name, count in sponsor_people_counts.items() if name in event_sponsor_tools}
        sponsor_team_counts = {name: count for name, count in sponsor_team_counts.items() if name in event_sponsor_tools}
        tool_pair_rows = [row for row in tool_pair_rows if all(part.strip() in event_sponsor_tools for part in row[0].split("+"))]
    sponsor_tools = tuple(sorted(sponsor_people_counts, key=lambda name: (-sponsor_people_counts[name], name)))
    allowed_sponsor_tools = set(sponsor_tools) or event_sponsor_tools
    evidence_counts, evidence_categories, model_counts = _parse_evidence_rows(evidence_rows, allowed_sponsor_tools)
    sponsor_summary_rows = _build_sponsor_summary_rows(teams, sponsor_people_counts, sponsor_team_counts, evidence_counts, allowed_sponsor_tools)
    tool_breadth_rows = _build_tool_breadth_rows(teams, allowed_sponsor_tools)
    finalist_stack_rows = _build_finalist_stack_rows(finalist_teams, allowed_sponsor_tools)

    segment_rows = _build_segment_rows(profiles, audience_filter=_is_approved if attendance_is_suspect else _is_checked_in)
    experience_rows, experience_coverage = _bucket_experience(audience_profiles)
    stars_rows, stars_coverage = _bucket_numeric(audience_profiles, "gh_api_stars", ((0, 1, "0-1 stars"), (2, 10, "2-10 stars"), (11, 50, "11-50 stars"), (51, 200, "51-200 stars"), (201, None, "200+ stars")))
    follower_rows, follower_coverage = _bucket_numeric(audience_profiles, "li_follower_count", ((0, 499, "0-499"), (500, 1999, "500-1,999"), (2000, 9999, "2,000-9,999"), (10000, None, "10,000+")))
    connection_rows, connection_coverage = _bucket_numeric(audience_profiles, "li_connection_count", ((0, 499, "0-499"), (500, 999, "500-999"), (1000, 4999, "1,000-4,999"), (5000, None, "5,000+")))

    top_companies = Counter(
        _clean_display_label(_safe_str(p.get("company")))
        for p in audience_profiles
        if _clean_display_label(_safe_str(p.get("company")))
    ).most_common(6)
    top_schools = Counter(
        _clean_display_label(_extract_primary_school(p))
        for p in audience_profiles
        if _clean_display_label(_extract_primary_school(p))
    ).most_common(6)

    theme_rows = _theme_score_rows(teams)[:5]
    top_project_rows = _build_top_project_rows(teams, allowed_sponsor_tools)
    big_numbers = _build_big_numbers(audience_profiles)

    hiring_ready_num = sum(1 for p in audience_profiles if _is_hiring_ready(p))
    hiring_ready_den = audience_count
    documented_team_num, documented_team_den = _metric_fraction(metrics, "q14_project_inventory_submissions")
    multi_tool_num = sum(count for label, count in tool_breadth_rows if label != "1 tool")
    multi_tool_den = len(teams)
    partner_story_num, partner_story_den = multi_tool_num, multi_tool_den

    top_pair_name, top_pair_count, top_pair_den = _top_pair(tool_pair_rows)
    top_country, top_country_count = _top_country(profiles)
    top_countries = Counter(
        _clean_display_label(_safe_str(profile.get("li_country")))
        for profile in profiles
        if _clean_display_label(_safe_str(profile.get("li_country")))
    ).most_common(5)

    lines = [
        "## Executive Summary",
        "",
    ]
    if attendance_is_suspect:
        lines.extend(
            [
                f"- The event generated a large top-of-funnel: **{len(profiles)} applicants**, **{len(approved)} approved ({_pct(len(approved), len(profiles))})**, **{len(submitter_people)} submitter participants ({_pct(len(submitter_people), len(approved))} of approved)**, and **{len(placed_people)} finalist/winner participants ({_pct(len(placed_people), len(submitter_people))} of submitters)**.",
                f"- Platform DB attendance labels are incomplete for this event, so this dossier uses the **approved participant cohort ({audience_count})** as the main audience base instead of the raw checked-in count of **{len(checked)}**.",
            ]
        )
    else:
        lines.append(
            f"- The event generated a large top-of-funnel: **{len(profiles)} applicants**, **{len(approved)} approved ({_pct(len(approved), len(profiles))})**, **{len(checked)} checked in ({_pct(len(checked), len(approved))} of approved)**, **{len(submitter_people)} submitter participants ({_pct(len(submitter_people), len(checked))} of checked-in)**, and **{len(placed_people)} placed participants ({_pct(len(placed_people), len(submitter_people))} of submitters)**."
        )
    lines.extend([
        f"- The usable project surface was much larger than the winner list: **{documented_team_num} documented teams** were available in the project set, while only **{len(finalist_teams)} finalist teams** and **{len(placed_people)} finalist/winner participants** sat at the very top of the competition.",
    ])
    if attendance_is_suspect:
        lines.append(
            f"- The reliable operating base was the approved cohort: **{len(submitter_people)}/{len(approved)}** approved participants converted into project submission activity, and **{len(finalist_teams)}/{len(teams)}** teams reached finalist status."
        )
    else:
        lines.append(
            f"- Attendance was the main operational constraint, not project follow-through. The biggest loss was **{len(approved) - len(checked)} approved people who never checked in**, while once people arrived the event still converted **{len(submitter_people)}/{len(checked)}** into submission activity."
        )
    lines.extend([
        f"- The audience was commercially relevant as well as technical: **{hiring_ready_num}/{hiring_ready_den} {audience_label} ({_pct(hiring_ready_num, hiring_ready_den)})** were hiring-ready, **{segment_rows['decision_makers']['audience']}** {audience_label} were decision-makers, and **{segment_rows['founders']['audience']}** were founders.",
        f"- Sponsor-tool usage was broad and stack-oriented rather than single-product. Across documented teams, **{multi_tool_num}/{multi_tool_den} ({_pct(multi_tool_num, multi_tool_den)})** used **2+ tools**, and the most common pair was **{top_pair_name} in {top_pair_count}/{top_pair_den} teams ({_pct(top_pair_count, top_pair_den)})**.",
    ])
    if big_numbers:
        bn = big_numbers
        lines.append(
            f"- The room also had real amplification and engineering depth. {audience_label_title} brought **{bn.get('linkedin_followers', '0')} LinkedIn followers**, **{bn.get('linkedin_connections', '0')} first-degree LinkedIn connections**, **{bn.get('github_private_contributions', '0')} private GitHub contributions**, and **{bn.get('github_commits_year', '0')} annual GitHub commits** across covered profiles."
        )
    lines.extend([
        "",
        "## Sponsor Tech Adoption",
        "",
        "### Sponsor-By-Sponsor Summary",
        "",
        "| Sponsor tool | Submitter people | Documented teams | Evidence-backed projects | Finalist teams | Avg judging score |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for row in sponsor_summary_rows:
        lines.append(
            f"| {row['tool_name']} | {row['submitter_people']}/{len(submitter_people)} | {row['documented_teams']}/{len(teams)} | {row['evidence_projects']}/{len(teams)} | {row['finalist_teams']}/{len(finalist_teams) or 1} | {row['avg_judging_score']} |"
        )
    if sponsor_summary_rows:
        top_tool = sponsor_summary_rows[0]
        lines.append("")
        lines.append(f"- **{top_tool['tool_name']}** was the base layer of the event: it showed up in **{top_tool['submitter_people']}/{len(submitter_people)}** submitter participants, **{top_tool['documented_teams']}/{len(teams)}** documented teams, and **{top_tool['finalist_teams']}/{len(finalist_teams) or 1}** finalist teams.")
        if len(sponsor_summary_rows) > 1:
            second = sponsor_summary_rows[1]
            lines.append(f"- **{second['tool_name']}** formed the clearest secondary sponsor lane, reaching **{second['submitter_people']}/{len(submitter_people)}** submitter participants and **{second['documented_teams']}/{len(teams)}** teams, with the strongest recurring pair in the event: **{top_pair_name} ({top_pair_count}/{top_pair_den})**.")
        strongest_outcome = max(
            sponsor_summary_rows,
            key=lambda row: float(row["avg_judging_score"]) if int(row["documented_teams"]) >= 3 else 0.0,
        )
        lines.append(f"- On outcome quality, **{strongest_outcome['tool_name']}** had the strongest average judging score among teams that reported it at **{strongest_outcome['avg_judging_score']}**, which is directionally useful for sponsor storytelling even when finalist counts stay small.")

    lines.extend([
        "",
        "### Tool Breadth Across Documented Teams",
        "",
        "| Tool breadth | Team count |",
        "|---|---:|",
    ])
    for label, count in tool_breadth_rows:
        lines.append(f"| {label} | {count} |")
    lines.extend([
        "",
        f"- Multi-tool behavior was the norm, not the exception. Only **{tool_breadth_rows[0][1] if tool_breadth_rows else 0}** teams stayed on a one-tool stack, while **{sum(count for label, count in tool_breadth_rows if label in {'3 tools', '4 tools', '5+ tools'})}** teams used **3+ tools**.",
        "",
        "### Top Sponsor Pairings Across Teams",
        "",
        "| Sponsor pair | Team count |",
        "|---|---:|",
    ])
    for pair_name, count, _ in tool_pair_rows[:5]:
        lines.append(f"| {pair_name} | {count} |")
    lines.extend([
        "",
        "### Evidence Coverage Across Documented Teams",
        "",
        "| Evidence type | Project count |",
        "|---|---:|",
    ])
    for category, count in evidence_categories:
        lines.append(f"| {category} | {count} |")
    if model_counts:
        lines.extend([
            "",
            "### Top Model Signals Across Projects",
            "",
            "| Model signal | Project count |",
            "|---|---:|",
        ])
        for model_name, count in model_counts[:8]:
            lines.append(f"| {model_name} | {count} |")

    lines.extend([
        "",
        "### Finalist Stack Inventory",
        "",
        "| Finalist team | Tool stack |",
        "|---|---|",
    ])
    for team_name, stack in finalist_stack_rows:
        lines.append(f"| {team_name} | {stack or 'No sponsor tools listed'} |")

    lines.extend([
        "",
        "## Audience And Talent Buckets",
        "",
        f"### Role Mix Across The Funnel ({audience_stage_title} View)",
        "",
        f"| Role segment | Applicants | {audience_stage_title} | Submitters |",
        "|---|---:|---:|---:|",
    ])
    for key, label in (("decision_makers", "Decision Makers"), ("ics", "ICs"), ("founders", "Founders"), ("students", "Students")):
        row = segment_rows[key]
        lines.append(f"| {label} | {row['applied']} | {row['audience']} | {row['submitters']} |")
    lines.extend([
        "",
        f"- The audience was not dominated by any single bucket. Decision-makers were the largest segment in raw volume, while founders, ICs, and students all stayed material through the {audience_stage_title.lower()} and submitter stages.",
        "",
        f"### Experience Buckets Among {audience_label_title}",
        f"Coverage: **{experience_coverage}/{audience_count} {audience_label}**",
        "",
        "| Experience bucket | Count | Share |",
        "|---|---:|---:|",
    ])
    for label, count, pct in experience_rows:
        lines.append(f"| {label} | {count} | {pct} |")
    lines.extend([
        "",
        f"### GitHub Stars Buckets Among {audience_label_title}",
        f"Coverage: **{stars_coverage}/{audience_count} {audience_label}**",
        "",
        "| GitHub stars bucket | Count | Share |",
        "|---|---:|---:|",
    ])
    for label, count, pct in stars_rows:
        lines.append(f"| {label} | {count} | {pct} |")
    lines.extend([
        "",
        f"### LinkedIn Audience Buckets Among {audience_label_title}",
        f"Followers coverage: **{follower_coverage}/{audience_count}**",
        "",
        "| LinkedIn followers bucket | Count | Share |",
        "|---|---:|---:|",
    ])
    for label, count, pct in follower_rows:
        lines.append(f"| {label} | {count} | {pct} |")
    lines.extend([
        "",
        f"Connections coverage: **{connection_coverage}/{audience_count}**",
        "",
        "| LinkedIn connections bucket | Count | Share |",
        "|---|---:|---:|",
    ])
    for label, count, pct in connection_rows:
        lines.append(f"| {label} | {count} | {pct} |")

    lines.extend([
        "",
        f"### Top {audience_stage_title} Company Clusters",
        "",
        "| Company | Count |",
        "|---|---:|",
    ])
    for name, count in top_companies[:6]:
        lines.append(f"| {name} | {count} |")
    lines.extend([
        "",
        f"### Top {audience_stage_title} School Clusters",
        "",
        "| School | Count |",
        "|---|---:|",
    ])
    for name, count in top_schools[:6]:
        lines.append(f"| {name} | {count} |")
    lines.extend([
        "",
        "### Top Applicant Countries",
        "",
        "| Country | Count | Share |",
        "|---|---:|---:|",
    ])
    for name, count in top_countries:
        lines.append(f"| {name} | {count} | {_pct(count, len(profiles))} |")

    lines.extend([
        "",
        "## Hiring And Commercial Signals",
        "",
        f"- The event produced a meaningful recruiting pool: **{hiring_ready_num}/{hiring_ready_den} {audience_label} ({_pct(hiring_ready_num, hiring_ready_den)})** were hiring-ready.",
        f"- **Students** were the highest-throughput builder segment within the dossier audience: **{segment_rows['students']['submitters']}/{segment_rows['students']['audience']}** student submitters (**{_pct(segment_rows['students']['submitters'], segment_rows['students']['audience'])}**).",
        f"- **Founders** stayed one of the strongest sponsor-relevant operator segments: **{segment_rows['founders']['audience']}/{segment_rows['founders']['applied']} ({_pct(segment_rows['founders']['audience'], segment_rows['founders']['applied'])})** were in the dossier audience and **{segment_rows['founders']['submitters']}/{segment_rows['founders']['audience']} ({_pct(segment_rows['founders']['submitters'], segment_rows['founders']['audience'])})** converted into submitters.",
        f"- **Decision-makers** were the largest business-relevant group by audience volume: **{segment_rows['decision_makers']['audience']}/{segment_rows['decision_makers']['applied']} ({_pct(segment_rows['decision_makers']['audience'], segment_rows['decision_makers']['applied'])})** were in the audience base and **{segment_rows['decision_makers']['submitters']}/{segment_rows['decision_makers']['audience']} ({_pct(segment_rows['decision_makers']['submitters'], segment_rows['decision_makers']['audience'])})** submitted.",
    ])
    if top_companies:
        lines.append(f"- Top company clusters in the dossier audience were small but credible: {', '.join(f'**{name} ({count})**' for name, count in top_companies[:4])}.")
    if top_schools:
        lines.append(f"- Top school clusters in the dossier audience were concentrated enough to be reusable in outreach and community follow-up: {', '.join(f'**{name} ({count})**' for name, count in top_schools[:5])}.")
    lines.extend([
        "",
        "### Segment Conversion Snapshot",
        "",
        f"| Segment | {audience_stage_title} | Submitters | Finalists/Winners | Submit rate from {audience_stage_title.lower()} | Finalist/Winner rate from submitters |",
        "|---|---:|---:|---:|---:|---:|",
    ])
    for key, label in (("students", "Students"), ("founders", "Founders"), ("decision_makers", "Decision Makers"), ("ics", "ICs")):
        row = segment_rows[key]
        lines.append(
            f"| {label} | {row['audience']} | {row['submitters']} | {row['placed']} | {_pct(row['submitters'], row['audience'])} | {_pct(row['placed'], row['submitters'])} |"
        )

    lines.extend([
        "",
        "## Project And Theme Inventory",
        "",
        f"- The project set contained **{documented_team_num} documented teams**, which is the main unit to use for sponsor storytelling, stack analysis, and project examples.",
        f"- The event was primarily {top_country.lower()}-based at the applicant level: **{top_country_count}/{len(profiles)} ({_pct(top_country_count, len(profiles))})** applicants came from that country, which is useful for sponsor follow-up planning and geo expectations.",
        f"- Partner-story density was high in the documented project set: **{partner_story_num}/{partner_story_den} ({_pct(partner_story_num, partner_story_den)})** documented teams already carried a multi-sponsor angle.",
        "",
        "### Theme Distribution",
        "",
        "| Theme | Team count | Finalist teams | Avg judging score |",
        "|---|---:|---:|---:|",
    ])
    for row in theme_rows:
        lines.append(f"| {row['theme']} | {row['count']} | {row['placed']} | {row['avg_score']} |")
    if theme_rows:
        lines.append("")
        best_finalist_theme = None
        finalist_theme_candidates = [row for row in theme_rows if int(row["placed"]) > 0]
        if finalist_theme_candidates:
            best_finalist_theme = max(
                finalist_theme_candidates,
                key=lambda row: int(row["placed"]) / max(int(row["count"]), 1),
            )
        if best_finalist_theme is not None:
            if best_finalist_theme["theme"] == theme_rows[0]["theme"]:
                lines.append(
                    f"- **{theme_rows[0]['theme']}** had both the highest raw project volume and the strongest finalist conversion among major themes at **{best_finalist_theme['placed']}/{best_finalist_theme['count']}**."
                )
            else:
                lines.append(
                    f"- **{theme_rows[0]['theme']}** had the highest raw project volume, while **{best_finalist_theme['theme']}** converted the strongest share of teams into finalist outcomes at **{best_finalist_theme['placed']}/{best_finalist_theme['count']}**."
                )
        best_avg = max(theme_rows, key=lambda row: float(row['avg_score']))
        lines.append(f"- **{best_avg['theme']}** posted the strongest average judging score among the top theme clusters at **{best_avg['avg_score']}**, which makes it a useful sponsor-story angle even when it is not the largest theme by count.")

    lines.extend([
        "",
        "### Top Project Examples For Sponsor Review",
        "",
        "| Team | Score | Placement | Sponsor stack |",
        "|---|---:|---|---|",
    ])
    for row in top_project_rows:
        lines.append(f"| {row['team_name']} | {row['score']} | {row['placement']} | {row['tools']} |")

    if attendance_is_suspect:
        lines.extend(
            [
                "",
                "## Funnel",
                "",
                f"- **Applied -> Approved:** **{len(approved)}/{len(profiles)} ({_pct(len(approved), len(profiles))})**",
                f"- **Attendance tracking note:** Platform DB check-in labels are incomplete for this event, so audience analysis is anchored on **approved participants ({len(approved)})** rather than the raw checked-in count of **{len(checked)}**.",
                f"- **Approved -> Submitter participants:** **{len(submitter_people)}/{len(approved)} ({_pct(len(submitter_people), len(approved))})**",
                f"- **Submitted teams -> Finalist teams:** **{len(finalist_teams)}/{len(teams)} ({_pct(len(finalist_teams), len(teams))})**",
                f"- **Submitter participants -> Finalist/winner participants:** **{len(placed_people)}/{len(submitter_people)} ({_pct(len(placed_people), len(submitter_people))})**",
                f"- Largest reliable drop: **{len(approved) - len(submitter_people)} people** between approved participants and submitter participants.",
                f"- Largest percentage drop: **{len(submitter_people) - len(placed_people)} of {len(submitter_people)} submitter participants ({_pct(len(submitter_people) - len(placed_people), len(submitter_people))})** did not finish on finalist/winner teams.",
                "",
                "## Sponsor Headline Options (Big-Number Angles)",
                "",
            ]
        )
    else:
        lines.extend(
            [
                "",
                "## Funnel",
                "",
                f"- **Applied -> Approved:** **{len(approved)}/{len(profiles)} ({_pct(len(approved), len(profiles))})**",
                f"- **Approved -> Checked-in:** **{len(checked)}/{len(approved)} ({_pct(len(checked), len(approved))})**",
                f"- **Checked-in -> Submitted:** **{len(submitter_people)}/{len(checked)} ({_pct(len(submitter_people), len(checked))})**",
                f"- **Submitted -> Placed:** **{len(placed_people)}/{len(submitter_people)} ({_pct(len(placed_people), len(submitter_people))})**",
                f"- Largest absolute drop: **{len(approved) - len(checked)} people** between approved and checked-in.",
                f"- Largest percentage drop: **{len(submitter_people) - len(placed_people)} of {len(submitter_people)} submissions ({_pct(len(submitter_people) - len(placed_people), len(submitter_people))})** did not place.",
                "",
                "## Sponsor Headline Options (Big-Number Angles)",
                "",
            ]
        )
    headline_lines = _headline_lines(big_numbers, segment_rows, documented_team_num, multi_tool_num, sponsor_summary_rows, len(finalist_teams), audience_label=audience_label)
    for item in headline_lines[:8]:
        lines.append(f"- {item}")

    return "\n".join(lines).strip() + "\n"


def _load_bundle_script_module(repo_root: Path):
    script_path = repo_root / "scripts" / "build_sponsor_delivery_bundle.py"
    spec = importlib.util.spec_from_file_location("cv_rank.bundle_builder_dense", script_path)
    if spec is None or spec.loader is None:
        raise ImportError(f"Could not import bundle builder from {script_path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def _resolve_event_name(repo_root: Path, run_id: str | None) -> str:
    if run_id:
        meta_path = repo_root / "results" / run_id / "meta.json"
        if meta_path.exists():
            import json

            meta = json.loads(meta_path.read_text(encoding="utf-8"))
            csv_path = _safe_str(meta.get("csv_path"))
            prefix = "Platform DB event: "
            if csv_path.startswith(prefix):
                return csv_path[len(prefix):].strip()
    raise ValueError("Event name could not be resolved. Provide event_name or a run_id with Platform DB event metadata.")


def _resolve_enriched_json(repo_root: Path, run_id: str | None, explicit_path: str | None) -> Path:
    if explicit_path:
        path = Path(explicit_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f"Explicit enriched JSON not found: {path}")
        return path
    if run_id:
        run_dir = repo_root / "results" / run_id
        candidates = sorted(run_dir.glob("enriched_complete*.json"))
        if candidates:
            return max(candidates, key=lambda p: p.stat().st_mtime)
        fallback = run_dir / "enriched.json"
        if fallback.exists():
            return fallback.resolve()
    raise FileNotFoundError("Could not resolve enriched JSON for dense sponsor packet")


def _load_csv_rows(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return [{k: (v or "").strip() for k, v in row.items()} for row in csv.DictReader(handle)]


def _load_metrics(path: Path) -> dict[str, dict[str, str]]:
    rows = _load_csv_rows(path)
    out: dict[str, dict[str, str]] = {}
    for row in rows:
        metric_name = row.get("metric_name", "")
        if metric_name and metric_name not in out:
            out[metric_name] = row
    return out


def _metric_fraction(metrics: dict[str, dict[str, str]], metric_name: str) -> tuple[int, int]:
    row = metrics.get(metric_name, {})
    return int(_safe_float(row.get("numerator"))), int(_safe_float(row.get("denominator")))


def _parse_tool_metric_rows(metrics_full_rows: list[dict[str, str]]) -> tuple[dict[str, int], dict[str, int], list[tuple[str, int, int]]]:
    submitter_people: dict[str, int] = {}
    documented_teams: dict[str, int] = {}
    tool_pairs: list[tuple[str, int, int]] = []
    for row in metrics_full_rows:
        raw_metric = row.get("raw_metric", "")
        numerator = int(_safe_float(row.get("numerator")))
        denominator = int(_safe_float(row.get("denominator")))
        if raw_metric.startswith("submitter_tool_adoption_"):
            submitter_people[_humanize_metric_suffix(raw_metric.removeprefix("submitter_tool_adoption_"))] = numerator
        elif raw_metric.startswith("team_tool_adoption_"):
            documented_teams[_humanize_metric_suffix(raw_metric.removeprefix("team_tool_adoption_"))] = numerator
        elif raw_metric.startswith("q7_top_pair_") and raw_metric.endswith("_team_frequency"):
            pair_name = raw_metric.removeprefix("q7_top_pair_").removesuffix("_team_frequency")
            tool_pairs.append((_humanize_metric_suffix(pair_name, pair=True), numerator, denominator))
    tool_pairs.sort(key=lambda item: (-item[1], item[0]))
    return submitter_people, documented_teams, tool_pairs


def _parse_evidence_rows(evidence_rows: list[dict[str, str]], sponsor_tools: set[str]) -> tuple[dict[str, int], list[tuple[str, int]], list[tuple[str, int]]]:
    by_tool_project: defaultdict[str, set[str]] = defaultdict(set)
    by_category_project: defaultdict[str, set[str]] = defaultdict(set)
    by_model_project: defaultdict[str, set[str]] = defaultdict(set)
    for row in evidence_rows:
        project_id = row.get("project_id", "")
        category = row.get("tool_category", "")
        tool_name = row.get("tool_name", "")
        if project_id and category:
            by_category_project[category].add(project_id)
        if category == "model" and tool_name:
            model_name = _normalize_model_name(tool_name)
            if model_name:
                by_model_project[model_name].add(project_id)
        if category in {"provider", "sdk", "infra"} and tool_name and project_id:
            normalized = _normalize_evidence_tool_name(tool_name, sponsor_tools)
            if normalized:
                by_tool_project[normalized].add(project_id)
    evidence_counts = {tool: len(project_ids) for tool, project_ids in by_tool_project.items()}
    evidence_categories = sorted(
        ((category.replace("_", " ").title(), len(project_ids)) for category, project_ids in by_category_project.items()),
        key=lambda item: (-item[1], item[0]),
    )
    model_counts = sorted(((name, len(project_ids)) for name, project_ids in by_model_project.items()), key=lambda item: (-item[1], item[0]))
    return evidence_counts, evidence_categories, model_counts


def _build_sponsor_summary_rows(
    teams: list[dict[str, Any]],
    sponsor_people_counts: dict[str, int],
    sponsor_team_counts: dict[str, int],
    evidence_counts: dict[str, int],
    allowed_tools: set[str],
) -> list[dict[str, Any]]:
    scores_by_tool: defaultdict[str, list[float]] = defaultdict(list)
    finalists_by_tool = Counter()
    for submission in teams:
        tools = _normalized_submission_tools(submission, allowed_tools)
        score = _safe_float(submission.get("current_event_judging_weighted_avg"), math.nan)
        is_finalist = bool(_safe_str(submission.get("placement")).strip())
        for tool_name in tools:
            if not math.isnan(score) and score > 0:
                scores_by_tool[tool_name].append(score)
            if is_finalist:
                finalists_by_tool[tool_name] += 1
    rows: list[dict[str, Any]] = []
    for tool_name, people_count in sorted(sponsor_people_counts.items(), key=lambda item: (-item[1], item[0])):
        scores = scores_by_tool.get(tool_name, [])
        avg = sum(scores) / len(scores) if scores else 0.0
        rows.append(
            {
                "tool_name": tool_name,
                "submitter_people": people_count,
                "documented_teams": sponsor_team_counts.get(tool_name, 0),
                "evidence_projects": evidence_counts.get(tool_name, 0),
                "finalist_teams": finalists_by_tool.get(tool_name, 0),
                "avg_judging_score": f"{avg:.2f}",
            }
        )
    return rows


def _build_tool_breadth_rows(teams: list[dict[str, Any]], allowed_tools: set[str]) -> list[tuple[str, int]]:
    buckets = Counter()
    for submission in teams:
        size = len(_normalized_submission_tools(submission, allowed_tools))
        if size <= 1:
            label = "1 tool"
        elif size == 2:
            label = "2 tools"
        elif size == 3:
            label = "3 tools"
        elif size == 4:
            label = "4 tools"
        else:
            label = "5+ tools"
        buckets[label] += 1
    ordered = ["1 tool", "2 tools", "3 tools", "4 tools", "5+ tools"]
    return [(label, buckets.get(label, 0)) for label in ordered]


def _build_finalist_stack_rows(finalist_teams: list[dict[str, Any]], allowed_tools: set[str]) -> list[tuple[str, str]]:
    ordered = sorted(finalist_teams, key=lambda sub: _safe_float(sub.get("current_event_judging_weighted_avg")), reverse=True)
    return [(_safe_str(sub.get("team_name")) or "Unnamed team", ", ".join(_normalized_submission_tools(sub, allowed_tools))) for sub in ordered[:10]]


def _build_segment_rows(
    profiles: list[dict[str, Any]],
    *,
    audience_filter,
) -> dict[str, dict[str, int]]:
    rows: dict[str, dict[str, int]] = {}
    for segment in ("students", "founders", "decision_makers", "ics"):
        members = [p for p in profiles if _segment_match(p, segment)]
        rows[segment] = {
            "applied": len(members),
            "audience": sum(1 for p in members if audience_filter(p)),
            "submitters": sum(1 for p in members if _is_submitter(p)),
            "placed": sum(1 for p in members if _is_placed(p)),
        }
    return rows


def _bucket_experience(profiles: list[dict[str, Any]]) -> tuple[list[tuple[str, int, str]], int]:
    buckets = Counter()
    coverage = 0
    for profile in profiles:
        years = _safe_float(profile.get("years_experience"), math.nan)
        if math.isnan(years):
            continue
        coverage += 1
        if years <= 1:
            buckets["0-1 years"] += 1
        elif years <= 4:
            buckets["2-4 years"] += 1
        elif years <= 8:
            buckets["5-8 years"] += 1
        else:
            buckets["9+ years"] += 1
    ordered = ["0-1 years", "2-4 years", "5-8 years", "9+ years"]
    return [(label, buckets.get(label, 0), _pct(buckets.get(label, 0), coverage)) for label in ordered], coverage


def _bucket_numeric(
    profiles: list[dict[str, Any]],
    field_name: str,
    ranges: tuple[tuple[int, int | None, str], ...],
) -> tuple[list[tuple[str, int, str]], int]:
    buckets = Counter()
    coverage = 0
    for profile in profiles:
        value = _safe_float(profile.get(field_name), math.nan)
        if math.isnan(value):
            continue
        coverage += 1
        for lower, upper, label in ranges:
            if upper is None and value >= lower:
                buckets[label] += 1
                break
            if upper is not None and lower <= value <= upper:
                buckets[label] += 1
                break
    ordered = [label for _, _, label in ranges]
    return [(label, buckets.get(label, 0), _pct(buckets.get(label, 0), coverage)) for label in ordered], coverage


def _build_top_project_rows(teams: list[dict[str, Any]], allowed_tools: set[str]) -> list[dict[str, str]]:
    finalists = [team for team in teams if _safe_str(team.get("placement")).strip()]
    ordered = finalists or teams
    ordered = sorted(ordered, key=lambda sub: _safe_float(sub.get("current_event_judging_weighted_avg")), reverse=True)
    rows: list[dict[str, str]] = []
    for sub in ordered[:8]:
        rows.append(
            {
                "team_name": _safe_str(sub.get("team_name")) or "Unnamed team",
                "score": f"{_safe_float(sub.get('current_event_judging_weighted_avg')):.2f}",
                "placement": _safe_str(sub.get("placement")) or "Not placed",
                "tools": ", ".join(_normalized_submission_tools(sub, allowed_tools)) or "No sponsor tools listed",
            }
        )
    return rows


def _build_big_numbers(checked_profiles: list[dict[str, Any]]) -> dict[str, str]:
    fields = {
        "linkedin_followers": "li_follower_count",
        "linkedin_connections": "li_connection_count",
        "github_private_contributions": "gh_api_private_contributions",
        "github_commits_year": "gh_api_commits_year",
        "github_repos": "gh_api_repos",
        "github_stars": "gh_api_stars",
        "years_experience_sum": "years_experience",
    }
    out: dict[str, str] = {}
    for key, field_name in fields.items():
        usable = [
            _safe_float(profile.get(field_name), math.nan)
            for profile in checked_profiles
            if not math.isnan(_safe_float(profile.get(field_name), math.nan))
        ]
        if usable:
            out[key] = _format_total(sum(usable))
    return out


def _resolve_audience_context(profiles: list[dict[str, Any]]) -> dict[str, Any]:
    approved = [p for p in profiles if _is_approved(p)]
    checked = [p for p in profiles if _is_checked_in(p)]
    submitters = [p for p in profiles if _is_submitter(p)]
    suspect = _has_suspect_attendance_labels(len(approved), len(checked), len(submitters))
    audience_profiles = approved if suspect else checked
    return {
        "profiles": audience_profiles,
        "suspect": suspect,
        "stage_title": "Approved" if suspect else "Checked-in",
        "label": "approved participants" if suspect else "checked-in attendees",
        "label_title": "Approved participants" if suspect else "Checked-in attendees",
    }


def _has_suspect_attendance_labels(approved_count: int, checked_count: int, submitter_count: int) -> bool:
    if approved_count >= 50 and checked_count <= 1:
        return True
    return checked_count > 0 and submitter_count > checked_count


def _headline_lines(
    big_numbers: dict[str, str],
    segment_rows: dict[str, dict[str, int]],
    documented_team_num: int,
    multi_tool_num: int,
    sponsor_summary_rows: list[dict[str, Any]],
    finalist_count: int,
    *,
    audience_label: str,
) -> list[str]:
    lines: list[str] = []
    if big_numbers.get("linkedin_followers"):
        lines.append(f"The {audience_label} brought **{big_numbers['linkedin_followers']} LinkedIn followers** across covered profiles.")
    if big_numbers.get("linkedin_connections"):
        lines.append(f"The same cohort brought **{big_numbers['linkedin_connections']} first-degree LinkedIn connections** for direct sponsor reach.")
    if big_numbers.get("github_private_contributions") and big_numbers.get("github_commits_year"):
        lines.append(f"Builders in the {audience_label} logged **{big_numbers['github_private_contributions']} private GitHub contributions** and **{big_numbers['github_commits_year']} annual commits**.")
    if big_numbers.get("github_repos") and big_numbers.get("github_stars"):
        lines.append(f"Covered GitHub profiles accounted for **{big_numbers['github_repos']} repositories** and **{big_numbers['github_stars']} stars**.")
    lines.append(f"The room included **{segment_rows['decision_makers']['audience']} decision-makers** and **{segment_rows['founders']['audience']} founders**, not just students.")
    lines.append(f"The project set contained **{documented_team_num} documented teams**, with **{multi_tool_num}** of them using **2+ tools**.")
    if sponsor_summary_rows:
        top_tool = sponsor_summary_rows[0]
        lines.append(f"**{top_tool['tool_name']}** appeared in **{top_tool['finalist_teams']}/{finalist_count or 1} finalist teams** and stayed the base sponsor layer across the event.")
    return lines


def _top_pair(rows: list[tuple[str, int, int]]) -> tuple[str, int, int]:
    if not rows:
        return ("No dominant pair", 0, 0)
    return rows[0]


def _top_country(profiles: list[dict[str, Any]]) -> tuple[str, int]:
    counter = Counter(
        _clean_display_label(_safe_str(profile.get("li_country")))
        for profile in profiles
        if _clean_display_label(_safe_str(profile.get("li_country")))
    )
    if not counter:
        return ("Unknown", 0)
    return counter.most_common(1)[0]


def _normalized_submission_tools(submission: dict[str, Any], allowed_tools: set[str]) -> list[str]:
    tools: set[str] = set()
    for raw_tool in submission.get("parsed_partner_tools") or []:
        canonical = _normalize_evidence_tool_name(_safe_str(raw_tool), allowed_tools)
        if canonical:
            tools.add(canonical)
    return sorted(tools)


def _humanize_metric_suffix(value: str, *, pair: bool = False) -> str:
    if pair:
        parts = [part for part in value.split("_") if part]
        midpoint = len(parts) // 2
        if midpoint > 0:
            left = _clean_display_label(" ".join(parts[:midpoint]))
            right = _clean_display_label(" ".join(parts[midpoint:]))
            return f"{left} + {right}"
    special = {
        "composio": "Composio",
        "crewai": "CrewAI",
        "crew_ai": "CrewAI",
        "skyfire": "Skyfire",
        "snowflake": "Snowflake",
        "mongodb": "MongoDB",
        "fireworks": "Fireworks",
        "vercel": "Vercel",
        "voyage_ai": "Voyage AI",
        "coinbase": "Coinbase",
        "nvidia": "NVIDIA",
    }
    return special.get(value, value.replace("_", " ").title())


def _clean_display_label(value: str) -> str:
    text = " ".join(_safe_str(value).split())
    if not text:
        return ""
    special = {
        "composio": "Composio",
        "crewai": "CrewAI",
        "crew ai": "CrewAI",
        "skyfire": "Skyfire",
        "snowflake": "Snowflake",
        "mongodb": "MongoDB",
        "mongo db": "MongoDB",
        "voyage ai": "Voyage AI",
        "voyagerai": "Voyage AI",
        "coinbase": "Coinbase",
        "fireworks": "Fireworks",
        "nvidia": "NVIDIA",
        "openai": "OpenAI",
    }
    lowered = text.lower()
    return special.get(lowered, text)


def _normalize_evidence_tool_name(raw_name: str, allowed_tools: set[str]) -> str:
    text = _clean_display_label(raw_name)
    lowered = text.lower()
    if lowered in GENERIC_EVIDENCE_TOOL_NAMES:
        return ""
    for pattern, canonical in EVIDENCE_TOOL_ALIAS_PATTERNS:
        if pattern.search(text):
            if not allowed_tools or canonical in allowed_tools:
                return canonical
    if text in allowed_tools:
        return text
    return ""


def _normalize_model_name(raw_name: str) -> str:
    text = _clean_display_label(raw_name)
    lowered = text.lower()
    if not lowered or lowered in GENERIC_EVIDENCE_TOOL_NAMES or len(lowered) > 48:
        return ""
    return lowered


def _normalize_loose_tool_name(raw_name: str) -> str:
    text = _clean_display_label(raw_name)
    if not text:
        return ""
    canonical = _normalize_evidence_tool_name(text, set())
    return canonical or text


def _segment_match(profile: dict[str, Any], segment: str) -> bool:
    if segment == "founders":
        return _as_bool(profile.get("is_founder"))
    if segment == "decision_makers":
        return _as_bool(profile.get("is_decision_maker"))
    if segment == "students":
        return _as_bool(profile.get("is_student"))
    if segment == "ics":
        return _is_ic(profile)
    return False


def _is_ic(profile: dict[str, Any]) -> bool:
    if _as_bool(profile.get("is_founder")):
        return False
    role = _safe_str(profile.get("role")).lower()
    return any(token in role for token in ("engineer", "developer", "scientist", "research"))


def _is_approved(profile: dict[str, Any]) -> bool:
    return _safe_str(profile.get("current_event_status")).lower() in {"approved", "accepted"}


def _is_checked_in(profile: dict[str, Any]) -> bool:
    return _as_bool(profile.get("current_event_checked_in"))


def _is_hiring_ready(profile: dict[str, Any]) -> bool:
    return _safe_str(profile.get("looking_for_job")).lower() in {"1", "true", "yes", "y"}


def _is_submitter(profile: dict[str, Any]) -> bool:
    return bool(profile.get("current_event_submissions"))


def _is_placed(profile: dict[str, Any]) -> bool:
    return any(_safe_str(sub.get("placement")).strip() for sub in (profile.get("current_event_submissions") or []))


def _as_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    return _safe_str(value).lower() in {"1", "true", "yes", "y"}


def _safe_str(value: Any) -> str:
    return "" if value is None else str(value)


def _safe_float(value: Any, default: float = 0.0) -> float:
    text = _safe_str(value).strip().replace("%", "")
    if not text:
        return default
    try:
        return float(text)
    except ValueError:
        return default


def _pct(numerator: int, denominator: int) -> str:
    if denominator <= 0:
        return "0.0%"
    return f"{(numerator / denominator) * 100:.1f}%"


def _format_total(value: float) -> str:
    if value >= 1000 or float(value).is_integer():
        return f"{int(round(value)):,}"
    return f"{value:,.1f}"


def _extract_primary_school(profile: dict[str, Any]) -> str:
    for school in profile.get("education_with_schools") or []:
        name = _safe_str(school.get("school_name")).strip()
        if name:
            return name
    return ""


def _project_text(submission: dict[str, Any]) -> str:
    parts = []
    for key, value in submission.items():
        key_norm = _safe_str(key).lower()
        if any(token in key_norm for token in ("description", "project", "idea", "summary")):
            parts.append(_safe_str(value))
    return " ".join(part for part in parts if part).strip()


def _classify_theme(text: str) -> str:
    lowered = _safe_str(text).lower()
    for theme, keywords in THEME_RULES:
        if any(keyword in lowered for keyword in keywords):
            return theme
    return "Other"


def _theme_score_rows(submissions: list[dict[str, Any]]) -> list[dict[str, str]]:
    scores_by_theme: defaultdict[str, list[float]] = defaultdict(list)
    placement_counts = Counter()
    theme_counts = Counter()
    for submission in submissions:
        theme = _classify_theme(_project_text(submission))
        theme_counts[theme] += 1
        score = _safe_float(submission.get("current_event_judging_weighted_avg"), math.nan)
        if not math.isnan(score) and score > 0:
            scores_by_theme[theme].append(score)
        if _safe_str(submission.get("placement")).strip():
            placement_counts[theme] += 1
    rows: list[dict[str, str]] = []
    for theme, count in theme_counts.most_common():
        scores = scores_by_theme.get(theme, [])
        avg = sum(scores) / len(scores) if scores else 0.0
        rows.append(
            {
                "theme": theme,
                "count": str(count),
                "placed": str(placement_counts.get(theme, 0)),
                "avg_score": f"{avg:.2f}",
            }
        )
    return rows
