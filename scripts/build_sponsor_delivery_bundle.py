#!/usr/bin/env python3
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, Iterable, List, Tuple
from urllib.parse import urlparse

DEFAULT_BASE = Path(__file__).resolve().parents[1]

BASE = DEFAULT_BASE
MARKETING_CSV = BASE / 'marketing_master.csv'
ENRICHED_JSON = BASE / 'results' / 'run_20260306_185829' / 'enriched_complete_gemini_nyc.json'
Q18_MD = BASE / 'subagent_outputs' / 'sectionC_agent5_q18.md'
EVENT_LABEL = os.getenv('CVRANK_EVENT_LABEL', 'Gemini 3 NYC')

OUT_REPORT = BASE / 'sponsor_report_pii_safe.md'
OUT_METRICS = BASE / 'sponsor_metrics_long.csv'
OUT_METRICS_FULL = BASE / 'sponsor_metrics_long_full.csv'
OUT_LEADS = BASE / 'sponsor_leads_internal.csv'
OUT_EVIDENCE = BASE / 'project_tech_evidence.csv'

OUT_METRICS_MD = BASE / 'sponsor_metrics_long.md'
OUT_LEADS_MD = BASE / 'sponsor_leads_internal.md'
OUT_EVIDENCE_MD = BASE / 'project_tech_evidence.md'
OUT_TASK_LIST = BASE / 'TASK_LIST_SPONSOR_EXPORT.md'
OUT_HEADLINE_BANK = BASE / 'SPONSOR_HEADLINE_BANK.md'

SPONSOR_TOOLS = [
    'Gemini',
    'Antigravity',
    'LlamaIndex',
    'Temporal',
    'Agno',
    'Composio',
    'CrewAI',
    'Skyfire',
    'Snowflake',
    'MongoDB',
    'Fireworks',
    'Vercel',
    'Voyage AI',
    'Coinbase',
    'NVIDIA',
]

TOOL_MENTION_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ('Gemini', re.compile(r'\bgemini(?:\s*[23](?:\.\d+)?)?\b', re.I)),
    ('Antigravity', re.compile(r'\bantigravity\b', re.I)),
    ('LlamaIndex', re.compile(r'\bllama\s*index\b|\bllamaindex\b', re.I)),
    ('Temporal', re.compile(r'\btemporal\b', re.I)),
    ('Agno', re.compile(r'\bagno\b', re.I)),
    ('Composio', re.compile(r'\bcomposio\b|\bcomposio\s*toolset\b', re.I)),
    ('CrewAI', re.compile(r'\bcrew\s*ai\b|\bcrewai\b', re.I)),
    ('Skyfire', re.compile(r'\bskyfire\b', re.I)),
    ('Snowflake', re.compile(r'\bsnowflake\b|\bsnowpark\b|\bsnowflake[-_ ]connector\b|\bsnowflake\s+cortex\b', re.I)),
    ('MongoDB', re.compile(r'\bmongodb\b|\batlas\b', re.I)),
    ('Fireworks', re.compile(r'\bfireworks?(?:\s*ai)?\b', re.I)),
    ('Vercel', re.compile(r'\bvercel\b', re.I)),
    ('Voyage AI', re.compile(r'\bvoyage(?:\s*ai)?\b|\bvoyagerai\b|\bvoyageai\b', re.I)),
    ('Coinbase', re.compile(r'\bcoinbase\b|\bcoin\s*base\b|\bcdp\b', re.I)),
    ('NVIDIA', re.compile(r'\bnvidia\b|\bnemo\b', re.I)),
)


def _find_latest_enriched_json(base: Path) -> Path | None:
    candidates: list[Path] = []
    results_root = base / 'results'
    if not results_root.exists():
        return None
    for run_dir in results_root.glob('run_*'):
        if not run_dir.is_dir():
            continue
        candidates.extend(run_dir.glob('enriched_complete*.json'))
        fallback = run_dir / 'enriched.json'
        if fallback.exists():
            candidates.append(fallback)
    if not candidates:
        return None
    return max(candidates, key=lambda p: p.stat().st_mtime)


def _resolve_enriched_json(base: Path, run_id: str | None, explicit_path: str | None) -> Path:
    if explicit_path:
        path = Path(explicit_path).expanduser().resolve()
        if not path.exists():
            raise FileNotFoundError(f'--enriched-json not found: {path}')
        return path

    if run_id:
        run_dir = base / 'results' / run_id
        if run_dir.exists():
            candidates = sorted(run_dir.glob('enriched_complete*.json'))
            if candidates:
                return max(candidates, key=lambda p: p.stat().st_mtime)
            fallback = run_dir / 'enriched.json'
            if fallback.exists():
                return fallback.resolve()
            raise FileNotFoundError(
                f'No enriched_complete*.json or enriched.json found for run_id={run_id} under {run_dir}'
            )

    latest = _find_latest_enriched_json(base)
    if latest is None:
        if run_id:
            raise FileNotFoundError(
                f'No enriched_complete*.json found for run_id={run_id}, '
                f'and no fallback enriched file found under {base / "results"}'
            )
        raise FileNotFoundError(f'No enriched_complete*.json found under {base / "results"}')
    return latest


def validate_snapshot_alignment(
    enriched_rows: List[dict],
    snapshot: dict,
    *,
    allow_mismatch: bool = False,
) -> None:
    applicant_emails = {
        safe_str(a.get('email')).lower()
        for a in snapshot.get('applicants', [])
        if safe_str(a.get('email'))
    }
    enriched_emails = {
        safe_str(p.get('email')).lower()
        for p in enriched_rows
        if safe_str(p.get('email'))
    }
    overlap = len(applicant_emails & enriched_emails)
    expected = len(snapshot.get('applicants', []))
    observed = len(enriched_rows)
    overlap_pct = (overlap / max(len(applicant_emails), 1)) * 100.0

    if observed == expected and overlap_pct >= 95.0:
        return

    msg = (
        'Snapshot alignment failed: enriched profiles do not match the event snapshot closely enough. '
        f'enriched_rows={observed}, snapshot_applicants={expected}, email_overlap={overlap}/{len(applicant_emails)} '
        f'({overlap_pct:.1f}%).'
    )
    if allow_mismatch:
        print(f'WARNING: {msg}', file=sys.stderr)
    else:
        raise RuntimeError(msg)


def configure_paths(
    *,
    base: Path,
    run_id: str | None,
    enriched_json: str | None,
    event_label: str | None,
    q18_md: str | None,
) -> None:
    global BASE
    global MARKETING_CSV
    global ENRICHED_JSON
    global Q18_MD
    global OUT_REPORT
    global OUT_METRICS
    global OUT_METRICS_FULL
    global OUT_LEADS
    global OUT_EVIDENCE
    global OUT_METRICS_MD
    global OUT_LEADS_MD
    global OUT_EVIDENCE_MD
    global OUT_TASK_LIST
    global OUT_HEADLINE_BANK
    global EVENT_LABEL

    BASE = base.resolve()
    MARKETING_CSV = BASE / 'marketing_master.csv'
    ENRICHED_JSON = _resolve_enriched_json(BASE, run_id, enriched_json)
    Q18_MD = Path(q18_md).expanduser().resolve() if q18_md else (BASE / 'subagent_outputs' / '__none__q18__.md')

    OUT_REPORT = BASE / 'sponsor_report_pii_safe.md'
    OUT_METRICS = BASE / 'sponsor_metrics_long.csv'
    OUT_METRICS_FULL = BASE / 'sponsor_metrics_long_full.csv'
    OUT_LEADS = BASE / 'sponsor_leads_internal.csv'
    OUT_EVIDENCE = BASE / 'project_tech_evidence.csv'

    OUT_METRICS_MD = BASE / 'sponsor_metrics_long.md'
    OUT_LEADS_MD = BASE / 'sponsor_leads_internal.md'
    OUT_EVIDENCE_MD = BASE / 'project_tech_evidence.md'
    OUT_TASK_LIST = BASE / 'TASK_LIST_SPONSOR_EXPORT.md'
    OUT_HEADLINE_BANK = BASE / 'SPONSOR_HEADLINE_BANK.md'

    if event_label:
        EVENT_LABEL = event_label


@dataclass
class MetricRow:
    section: str
    metric: str
    value: str
    numerator: str
    denominator: str
    denominator_cohort: str
    coverage_pct: str
    source: str
    pii_classification: str
    confidence: str
    claim_label: str
    notes: str


def read_master_metrics() -> List[MetricRow]:
    rows: List[MetricRow] = []
    with MARKETING_CSV.open(newline='') as f:
        reader = csv.DictReader(f)
        for r in reader:
            rows.append(
                MetricRow(
                    section=r['section'],
                    metric=r['metric'],
                    value=r['value'],
                    numerator=r['numerator'],
                    denominator=r['denominator'],
                    denominator_cohort=r['denominator_cohort'],
                    coverage_pct=r['coverage_pct'],
                    source=r['source'],
                    pii_classification=r['pii_classification'],
                    confidence=r['confidence'],
                    claim_label=r['claim_label'],
                    notes=r['notes'],
                )
            )
    return rows


def _safe_int_str(value: str) -> int | None:
    if value is None:
        return None
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def _funnel_applicant_denominator(rows: List[MetricRow]) -> int | None:
    for r in rows:
        if r.section == 'funnel' and r.metric == 'applied_to_approved':
            return _safe_int_str(r.denominator)
    return None


def validate_event_alignment(
    master_rows: List[MetricRow],
    enriched_rows: List[dict],
    *,
    allow_mismatch: bool = False,
) -> None:
    funnel_den = _funnel_applicant_denominator(master_rows)
    enriched_n = len(enriched_rows)
    if funnel_den is None:
        return
    if funnel_den == enriched_n:
        return

    msg = (
        'Event alignment failed: marketing_master.csv appears to represent a different event. '
        f'Funnel applicant denominator={funnel_den}, enriched profile count={enriched_n}. '
        'Regenerate event-level metrics for this event before building sponsor outputs.'
    )
    if allow_mismatch:
        print(f'WARNING: {msg}', file=sys.stderr)
    else:
        raise RuntimeError(msg)


def _load_platform_dsn() -> str:
    dsn = os.getenv('PLATFORM_DATABASE_URL', '').strip()
    if dsn:
        return dsn
    try:
        from dotenv import load_dotenv

        load_dotenv(BASE / '.env', override=False)
    except Exception:
        pass
    return os.getenv('PLATFORM_DATABASE_URL', '').strip()


def _safe_float(value, default: float = 0.0) -> float:
    if value in (None, ''):
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _as_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return False
    text = str(value).strip().lower()
    return text in {'1', 'true', 'yes', 'y'}


def _normalize_tool_name(raw: str) -> str:
    t = (raw or '').strip()
    if not t:
        return ''
    t_low = re.sub(r'\s+', ' ', t.lower())
    replacements = {
        'fireworks ai': 'Fireworks',
        'fireworks': 'Fireworks',
        'voyage ai': 'Voyage AI',
        'voyage': 'Voyage AI',
        'mongodb atlas': 'MongoDB',
        'mongodb': 'MongoDB',
        'gemini': 'Gemini',
        'agno': 'Agno',
        'llamaindex': 'LlamaIndex',
        'llama index': 'LlamaIndex',
        'temporal': 'Temporal',
        'crewai': 'CrewAI',
        'crew ai': 'CrewAI',
        'composio': 'Composio',
        'composio toolset': 'Composio',
        'skyfire': 'Skyfire',
        'snowflake': 'Snowflake',
        'snowflake cortex': 'Snowflake',
        'snowpark': 'Snowflake',
        'vercel': 'Vercel',
        'openai': 'OpenAI',
        'anthropic': 'Anthropic',
    }
    if t_low in replacements:
        return replacements[t_low]
    compact = re.sub(r'[^a-z0-9\+\- ]', '', t_low).strip()
    if compact in replacements:
        return replacements[compact]
    words = [w for w in compact.split() if w and w not in {'and', 'with', 'using', 'tool', 'tools'}]
    if not words:
        return ''
    return ' '.join(w.capitalize() for w in words[:3])


def parse_partner_tools_generic(text: str) -> List[str]:
    normalized = (text or '').replace('\n', ',')
    if not re.search(r'[,\|;/]', normalized):
        return []
    parts = re.split(r'[,\|;/]+', normalized)
    found: List[str] = []
    seen = set()
    for part in parts:
        if len(part.split()) > 4:
            continue
        name = _normalize_tool_name(part)
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(name)
    return found


def scan_known_tool_mentions(text: str) -> List[str]:
    found: List[str] = []
    seen: set[str] = set()
    haystack = safe_str(text)
    if not haystack:
        return found
    for canonical, pattern in TOOL_MENTION_PATTERNS:
        if not pattern.search(haystack):
            continue
        key = canonical.lower()
        if key in seen:
            continue
        seen.add(key)
        found.append(canonical)
    return found


def collect_submission_tool_text(fields: Dict[str, str]) -> str:
    parts: List[str] = []
    for field_name, value in (fields or {}).items():
        text = safe_str(value).strip()
        if not text:
            continue
        key = safe_str(field_name).lower()
        if any(token in key for token in ('github', 'repo', 'video', 'demo url', 'public url', 'link', 'website')):
            continue
        if any(token in key for token in ('description', 'project', 'summary', 'overview', 'problem', 'solution', 'stack', 'technology', 'tool', 'sponsor', 'why')):
            parts.append(text)
    return '\n'.join(parts).strip()


def load_event_snapshot(event_name: str) -> dict | None:
    dsn = _load_platform_dsn()
    if not dsn:
        return None
    try:
        import psycopg2
        import psycopg2.extras
    except Exception:
        return None

    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor)
        cur.execute(
            '''
            SELECT id, title, description, details
            FROM "PlatformEvent"
            WHERE title = %s
            LIMIT 1
            ''',
            (event_name,),
        )
        event_row = cur.fetchone()
        if not event_row:
            return None
        event_id = event_row['id']
        event_tool_text = '\n'.join(
            part for part in [safe_str(event_row.get('description')), safe_str(event_row.get('details'))] if part
        )
        event_partner_tools = scan_known_tool_mentions(event_tool_text)

        cur.execute(
            '''
            SELECT
              a."userId" AS user_id,
              LOWER(up.email) AS email,
              a.status::text AS status,
              a."checkedIn" AS checked_in
            FROM "EventApplicant" a
            JOIN "UserProfile" up ON up."userId" = a."userId"
            WHERE a."eventId" = %s
            ''',
            (event_id,),
        )
        applicants = [dict(r) for r in cur.fetchall()]

        cur.execute(
            '''
            SELECT
              hs.id::text AS submission_id,
              hs."teamName" AS team_name,
              hs.placement::text AS placement,
              htm."userId" AS member_user_id,
              LOWER(up.email) AS member_email,
              sf.field AS field_name,
              sv.value AS field_value
            FROM "HackathonSubmission" hs
            LEFT JOIN "HackathonTeamMember" htm ON htm."submissionId" = hs.id
            LEFT JOIN "UserProfile" up ON up."userId" = htm."userId"
            LEFT JOIN "HackathonSubmissionValue" sv ON sv."hackathonSubmissionId" = hs.id
            LEFT JOIN "HackathonSubmissionField" sf ON sf.id = sv."hackathonSubmissionFieldId"
            WHERE hs."eventId" = %s
            ''',
            (event_id,),
        )
        submission_rows = cur.fetchall()

        cur.execute(
            '''
            SELECT
              hjs."submissionId"::text AS submission_id,
              (
                SUM(hjs.score * COALESCE(hjc.weight, 1)::float)
                / NULLIF(SUM(COALESCE(hjc.weight, 1)::float), 0)
              ) * 10.0 AS weighted_pct
            FROM "HackathonJudgingScore" hjs
            JOIN "HackathonJudgingCriteria" hjc ON hjc.id = hjs."judgingCriteriaId"
            JOIN "HackathonSubmission" hs ON hs.id = hjs."submissionId"
            WHERE hs."eventId" = %s
            GROUP BY hjs."submissionId"
            ''',
            (event_id,),
        )
        judging_rows = cur.fetchall()

    finally:
        conn.close()

    submissions_by_id: Dict[str, dict] = {}
    for row in submission_rows:
        sid = safe_str(row.get('submission_id'))
        if not sid:
            continue
        sub = submissions_by_id.setdefault(
            sid,
            {
                'submission_id': sid,
                'team_name': safe_str(row.get('team_name')),
                'placement': safe_str(row.get('placement')),
                'member_user_ids': set(),
                'member_emails': set(),
                'fields': {},
            },
        )
        member_uid = safe_str(row.get('member_user_id'))
        member_email = safe_str(row.get('member_email')).lower()
        if member_uid:
            sub['member_user_ids'].add(member_uid)
        if member_email:
            sub['member_emails'].add(member_email)
        field_name = safe_str(row.get('field_name'))
        field_value = safe_str(row.get('field_value'))
        if field_name and field_value:
            sub['fields'][field_name] = field_value

    judging_by_submission = {safe_str(r['submission_id']): _safe_float(r['weighted_pct']) for r in judging_rows}
    submissions: List[dict] = []
    tool_counts: Counter = Counter()
    for sid, sub in submissions_by_id.items():
        row = {
            'submission_id': sid,
            'team_name': safe_str(sub.get('team_name')),
            'placement': safe_str(sub.get('placement')),
            'member_user_ids': sorted(sub.get('member_user_ids') or []),
            'member_emails': sorted(sub.get('member_emails') or []),
        }
        for k, v in (sub.get('fields') or {}).items():
            row[k] = v
        partner_blob = ''
        for k, v in (sub.get('fields') or {}).items():
            key = k.lower()
            if ('partner' in key or 'sponsor' in key) and any(token in key for token in ('tool', 'tech', 'stack', 'technology')):
                partner_blob = safe_str(v)
                break
        fallback_blob = collect_submission_tool_text(sub.get('fields') or {})
        detection_blob = '\n'.join(part for part in [partner_blob, fallback_blob] if part).strip()
        parsed_tools = parse_partner_tools(detection_blob)
        if event_partner_tools:
            allowed = set(event_partner_tools)
            parsed_tools = [tool for tool in parsed_tools if tool in allowed]
        row['Partner Technologies Used'] = partner_blob or ', '.join(parsed_tools)
        row['tool_detection_text'] = fallback_blob
        row['current_event_judging_weighted_avg'] = round(judging_by_submission.get(sid, 0.0), 2)
        row['parsed_partner_tools'] = parsed_tools
        for tool in row['parsed_partner_tools']:
            tool_counts[tool] += 1
        submissions.append(row)

    return {
        'event_id': safe_str(event_id),
        'event_name': event_name,
        'event_partner_tools': event_partner_tools,
        'applicants': applicants,
        'submissions': submissions,
        'judging_by_submission': judging_by_submission,
        'tool_counts': dict(tool_counts),
    }


def hydrate_enriched_with_event_snapshot(enriched: List[dict], snapshot: dict) -> None:
    by_email: Dict[str, dict] = {}
    for p in enriched:
        e = safe_str(p.get('email')).lower()
        if e:
            by_email[e] = p

    applicant_by_email: Dict[str, dict] = {}
    for a in snapshot.get('applicants', []):
        e = safe_str(a.get('email')).lower()
        if e:
            applicant_by_email[e] = a

    submissions_by_email: Dict[str, List[dict]] = defaultdict(list)
    for sub in snapshot.get('submissions', []):
        for e in sub.get('member_emails', []):
            if e:
                submissions_by_email[e.lower()].append(sub)

    for p in enriched:
        e = safe_str(p.get('email')).lower()
        applicant = applicant_by_email.get(e, {})
        p['current_event_status'] = safe_str(applicant.get('status')).lower()
        p['current_event_checked_in'] = bool(applicant.get('checked_in'))

        subs = submissions_by_email.get(e, [])
        p['current_event_submissions'] = [dict(s) for s in subs]
        if subs:
            scores = [_safe_float(s.get('current_event_judging_weighted_avg')) for s in subs if _safe_float(s.get('current_event_judging_weighted_avg')) > 0]
            if scores:
                p['current_event_judging_weighted_avg'] = round(sum(scores) / len(scores), 2)


def _ratio(num: int, den: int) -> float:
    if den <= 0:
        return 0.0
    return num / den


def _bucket_years(v: float) -> str:
    if v < 2:
        return '0-1'
    if v < 5:
        return '2-4'
    if v < 9:
        return '5-8'
    return '9+'


def _metric_row(
    section: str,
    metric: str,
    *,
    numerator: int,
    denominator: int,
    denominator_cohort: str,
    source: str,
    confidence: str = 'high',
    claim_label: str = 'observed_in_code',
    notes: str = '',
) -> MetricRow:
    return MetricRow(
        section=section,
        metric=metric,
        value=f'{_ratio(numerator, denominator):.4f}',
        numerator=str(numerator),
        denominator=str(denominator),
        denominator_cohort=denominator_cohort,
        coverage_pct=f'{_ratio(numerator, denominator) * 100:.1f}',
        source=source,
        pii_classification='PII-safe',
        confidence=confidence,
        claim_label=claim_label,
        notes=notes,
    )


def build_event_metrics_rows(enriched: List[dict], snapshot: dict) -> List[MetricRow]:
    source = f'platform_db.EventApplicant(event={snapshot["event_name"]})'
    applicant_emails = {safe_str(a.get('email')).lower() for a in snapshot.get('applicants', []) if safe_str(a.get('email'))}
    approved_emails = {
        safe_str(a.get('email')).lower()
        for a in snapshot.get('applicants', [])
        if safe_str(a.get('email')) and safe_str(a.get('status')).lower() in {'approved', 'accepted'}
    }
    checked_in_emails = {
        safe_str(a.get('email')).lower()
        for a in snapshot.get('applicants', [])
        if safe_str(a.get('email')) and _as_bool(a.get('checked_in'))
    }

    submitter_emails: set[str] = set()
    placed_emails: set[str] = set()
    submissions = snapshot.get('submissions', [])
    for sub in submissions:
        members = {safe_str(e).lower() for e in sub.get('member_emails', []) if safe_str(e)}
        submitter_emails.update(members)
        if safe_str(sub.get('placement')):
            placed_emails.update(members)

    applied = len(applicant_emails)
    approved = len(approved_emails)
    checked = len(checked_in_emails)
    submitters = len(submitter_emails)
    placed = len(placed_emails)

    rows: List[MetricRow] = []
    rows.extend(
        [
            _metric_row('funnel', 'applied_to_approved', numerator=approved, denominator=applied, denominator_cohort='all_applicants', source=source),
            _metric_row('funnel', 'approved_to_checked_in', numerator=checked, denominator=max(approved, 1), denominator_cohort='approved', source=source),
            _metric_row('funnel', 'checked_in_to_submitted', numerator=submitters, denominator=max(checked, 1), denominator_cohort='checked_in', source=source),
            _metric_row('funnel', 'submitted_to_placed', numerator=placed, denominator=max(submitters, 1), denominator_cohort='submitters', source=source),
        ]
    )

    stage_pairs = [
        ('applied_to_approved', applied, approved),
        ('approved_to_checked_in', approved, checked),
        ('checked_in_to_submitted', checked, submitters),
        ('submitted_to_placed', submitters, placed),
    ]
    drops = [(name, max(a - b, 0), _ratio(max(a - b, 0), max(a, 1)), a) for name, a, b in stage_pairs]
    if drops:
        largest_abs = max(drops, key=lambda x: x[1])
        largest_pct = max(drops, key=lambda x: x[2])
        rows.append(
            MetricRow(
                section='Q2',
                metric='largest_dropoff_absolute_count',
                value=str(largest_abs[1]),
                numerator=str(largest_abs[1]),
                denominator=str(largest_abs[3]),
                denominator_cohort='all_applicants',
                coverage_pct='100.0',
                source=source,
                pii_classification='PII-safe',
                confidence='high',
                claim_label='observed_in_code',
                notes=f'stage={largest_abs[0]}',
            )
        )
        rows.append(
            MetricRow(
                section='Q2',
                metric='largest_dropoff_percentage',
                value=f'{largest_pct[2]:.4f}',
                numerator=str(largest_pct[1]),
                denominator=str(largest_pct[3]),
                denominator_cohort='all_applicants',
                coverage_pct='100.0',
                source=source,
                pii_classification='PII-safe',
                confidence='high',
                claim_label='observed_in_code',
                notes=f'stage={largest_pct[0]}',
            )
        )

    profiles = {safe_str(p.get('email')).lower(): p for p in enriched if safe_str(p.get('email'))}
    checked_profiles = [profiles[e] for e in checked_in_emails if e in profiles]
    submitter_profiles = [profiles[e] for e in submitter_emails if e in profiles]

    def _is_founder(p: dict) -> bool:
        return _as_bool(p.get('is_founder'))

    def _is_student(p: dict) -> bool:
        return _as_bool(p.get('is_student'))

    def _is_decision(p: dict) -> bool:
        return _as_bool(p.get('is_decision_maker'))

    def _is_ic(p: dict) -> bool:
        role = safe_str(p.get('role')).lower()
        return any(x in role for x in ['engineer', 'developer', 'scientist', 'research']) and not _is_founder(p)

    role_flags = {
        'founders': _is_founder,
        'students': _is_student,
        'decision_makers': _is_decision,
        'ics': _is_ic,
    }
    for seg, pred in role_flags.items():
        app_seg = sum(1 for e in applicant_emails if e in profiles and pred(profiles[e]))
        chk_seg = sum(1 for p in checked_profiles if pred(p))
        sub_seg = sum(1 for p in submitter_profiles if pred(p))
        rows.append(_metric_row('q3_segment', f'{seg}.checked_in_count', numerator=chk_seg, denominator=max(app_seg, 1), denominator_cohort='all_applicants', source=source))
        rows.append(_metric_row('q3_segment', f'{seg}.submitters_count', numerator=sub_seg, denominator=max(chk_seg, 1), denominator_cohort='checked_in', source=source))

    # Hiring section
    hiring_ready = [p for p in checked_profiles if normalize_yes_no_other(safe_str(p.get('looking_for_job'))) == 'yes']
    hiring_ready_both = [p for p in hiring_ready if safe_str(p.get('github_url')) and safe_str(p.get('linkedin_url'))]
    rows.append(_metric_row('sectionB_exec', 'q1_hiring_ready_checked_in', numerator=len(hiring_ready), denominator=max(len(checked_profiles), 1), denominator_cohort='checked_in', source=source))
    rows.append(_metric_row('sectionB_exec', 'q2_hiring_ready_with_gh_li', numerator=len(hiring_ready_both), denominator=max(len(hiring_ready), 1), denominator_cohort='checked_in', source=source))
    github_cov = sum(1 for p in checked_profiles if _safe_float(p.get('github_total_score')) > 0 or safe_str(p.get('github_url')))
    lang_cov = sum(1 for p in checked_profiles if p.get('github_best_languages'))
    rows.append(_metric_row('sectionB_exec', 'q4_github_quality_coverage_checked_in', numerator=github_cov, denominator=max(len(checked_profiles), 1), denominator_cohort='checked_in', source=source))
    rows.append(_metric_row('sectionB_exec', 'q3_language_signal_coverage_checked_in', numerator=lang_cov, denominator=max(len(checked_profiles), 1), denominator_cohort='checked_in', source=source))
    judged_submitters = sum(1 for p in submitter_profiles if _safe_float(p.get('current_event_judging_weighted_avg')) > 0)
    rows.append(_metric_row('sectionB_exec', 'q5_judging_score_coverage_submitters', numerator=judged_submitters, denominator=max(len(submitter_profiles), 1), denominator_cohort='submitters', source=source))

    # Sales/BD
    founder_checked = sum(1 for p in checked_profiles if _is_founder(p))
    founder_all = sum(1 for e in applicant_emails if e in profiles and _is_founder(profiles[e]))
    decision_checked = sum(1 for p in checked_profiles if _is_decision(p))
    decision_all = sum(1 for e in applicant_emails if e in profiles and _is_decision(profiles[e]))
    rows.append(_metric_row('sectionC_exec', 'founder.checked_in_count', numerator=founder_checked, denominator=max(founder_all, 1), denominator_cohort='all_applicants', source=source))
    rows.append(_metric_row('sectionC_exec', 'decision_maker.checked_in_count', numerator=decision_checked, denominator=max(decision_all, 1), denominator_cohort='all_applicants', source=source))

    # Market research
    years = [_safe_float(p.get('years_experience'), -1) for e, p in profiles.items() if e in applicant_emails and _safe_float(p.get('years_experience'), -1) >= 0]
    if years:
        buckets = Counter(_bucket_years(v) for v in years)
        top_bucket, top_count = buckets.most_common(1)[0]
        rows.append(
            _metric_row(
                'sectionD_exec',
                f'q1_largest_experience_bucket_{top_bucket.replace("+", "plus").replace("-", "_")}',
                numerator=top_count,
                denominator=max(len(years), 1),
                denominator_cohort='all_applicants',
                source=source,
            )
        )
    student_all = sum(1 for e in applicant_emails if e in profiles and _is_student(profiles[e]))
    rows.append(_metric_row('sectionD_exec', 'q2_largest_exclusive_role_bucket_student', numerator=student_all, denominator=max(applied, 1), denominator_cohort='all_applicants', source=source))
    us_all = sum(1 for e in applicant_emails if e in profiles and 'united states' in safe_str(profiles[e].get('li_country')).lower())
    rows.append(_metric_row('sectionD_exec', 'q3_us_country_share_all_applicants', numerator=us_all, denominator=max(applied, 1), denominator_cohort='all_applicants', source=source))
    startup_all = sum(1 for e in applicant_emails if e in profiles and safe_str(profiles[e].get('employment_category')).lower() == 'startup')
    rows.append(_metric_row('sectionD_exec', 'q4_startup_employer_share_all_applicants', numerator=startup_all, denominator=max(applied, 1), denominator_cohort='all_applicants', source=source))
    lang_signal = sum(1 for e in applicant_emails if e in profiles and profiles[e].get('github_best_languages'))
    rows.append(_metric_row('sectionD_exec', 'q6_language_signal_coverage_all_applicants', numerator=lang_signal, denominator=max(applied, 1), denominator_cohort='all_applicants', source=source))

    # Sponsor tools (dynamic from partner-tool field)
    submitter_tools: Dict[str, set[str]] = defaultdict(set)
    team_tools: Dict[str, set[str]] = {}
    for sub in submissions:
        sid = safe_str(sub.get('submission_id'))
        tools = set(sub.get('parsed_partner_tools') or [])
        if sid:
            team_tools[sid] = tools
        for e in sub.get('member_emails', []):
            if e:
                submitter_tools[e.lower()].update(tools)

    tool_counts = Counter()
    for e in submitter_emails:
        tool_counts.update(submitter_tools.get(e, set()))
    top_tools = [name for name, _ in tool_counts.most_common(6)]
    for tool in top_tools:
        slug = re.sub(r'[^a-z0-9]+', '_', tool.lower()).strip('_')
        num = sum(1 for e in submitter_emails if tool in submitter_tools.get(e, set()))
        rows.append(_metric_row('sectionE_exec', f'submitter_tool_adoption_{slug}', numerator=num, denominator=max(submitters, 1), denominator_cohort='submitters', source=source, claim_label='self_reported'))
        team_num = sum(1 for _, tools in team_tools.items() if tool in tools)
        rows.append(_metric_row('sectionE_exec', f'team_tool_adoption_{slug}', numerator=team_num, denominator=max(len(team_tools), 1), denominator_cohort='submitters', source=source, claim_label='self_reported'))

    multi_tool_teams = sum(1 for tools in team_tools.values() if len(tools) >= 2)
    rows.append(_metric_row('sectionE_exec', 'q8_teams_using_multiple_tools_2plus', numerator=multi_tool_teams, denominator=max(len(team_tools), 1), denominator_cohort='submitters', source=source, claim_label='self_reported'))

    pair_counts = Counter()
    for tools in team_tools.values():
        lst = sorted(tools)
        for i in range(len(lst)):
            for j in range(i + 1, len(lst)):
                pair_counts[(lst[i], lst[j])] += 1
    if pair_counts:
        (a, b), cnt = pair_counts.most_common(1)[0]
        pair_slug = re.sub(r'[^a-z0-9]+', '_', f'{a}_{b}'.lower()).strip('_')
        rows.append(_metric_row('sectionE_exec', f'q7_top_pair_{pair_slug}_team_frequency', numerator=cnt, denominator=max(len(team_tools), 1), denominator_cohort='submitters', source=source, claim_label='self_reported'))

    # Marketing-level proof inventory
    rows.append(_metric_row('sectionF_exec', 'q13_partner_story_anchor', numerator=multi_tool_teams, denominator=max(len(team_tools), 1), denominator_cohort='submitters', source=source, claim_label='self_reported'))
    rows.append(
        _metric_row(
            'sectionF_exec',
            'q14_project_inventory_submissions',
            numerator=len(team_tools),
            denominator=max(submitters, 1),
            denominator_cohort='submitters',
            source=source,
            claim_label='observed_in_code',
        )
    )

    return rows


def build_fallback_top50(enriched: List[dict]) -> List[dict]:
    ranked = []
    for p in enriched:
        email = safe_str(p.get('email')).lower()
        if not email:
            continue
        checked = _as_bool(p.get('current_event_checked_in'))
        submitter = bool(p.get('current_event_submissions'))
        job = normalize_yes_no_other(safe_str(p.get('looking_for_job'))) == 'yes'
        gh = _safe_float(p.get('github_total_score'))
        judging = _safe_float(p.get('current_event_judging_weighted_avg'))
        founder = _as_bool(p.get('is_founder'))
        decision = _as_bool(p.get('is_decision_maker'))

        score = 0.0
        score += 20.0 if checked else 0.0
        score += 20.0 if submitter else 0.0
        score += 20.0 if job else 0.0
        score += min(20.0, gh * 0.2)
        score += min(20.0, judging * 0.2)
        score += 5.0 if founder else 0.0
        score += 5.0 if decision else 0.0

        reasons = []
        if checked:
            reasons.append('CHECKED_IN')
        if submitter:
            reasons.append('EVENT_SUBMITTER')
        if job:
            reasons.append('JOB_SEEKING_YES')
        if gh >= 60:
            reasons.append('HIGH_GITHUB_SIGNAL')
        if judging >= 70:
            reasons.append('HIGH_JUDGING_SIGNAL')
        if founder:
            reasons.append('ROLE_FOUNDER')
        if decision:
            reasons.append('DECISION_MAKER')
        if not reasons:
            reasons.append('GENERAL_PROFILE_SIGNAL')

        role_bucket = 'founder' if founder else ('decision_maker' if decision else ('student' if _as_bool(p.get('is_student')) else 'ic'))
        ranked.append(
            {
                'name': safe_str(p.get('name')),
                'email': email,
                'company': safe_str(p.get('company')),
                'role_bucket': role_bucket,
                'lead_score': f'{score:.1f}',
                'reason_codes': ';'.join(reasons),
            }
        )

    ranked.sort(key=lambda x: float(x['lead_score']), reverse=True)
    out = []
    for idx, row in enumerate(ranked[:50], start=1):
        out.append(
            {
                'rank': idx,
                'name': row['name'],
                'email': row['email'],
                'company': row['company'],
                'role_bucket': row['role_bucket'],
                'metric_name': 'fallback_top50',
                'lead_score': row['lead_score'],
                'numerator': str(idx),
                'denominator': str(len(ranked)),
                'denominator_cohort': 'all_applicants',
                'coverage_pct': '100.0',
                'source': f'platform_db.EventApplicant ({EVENT_LABEL})',
                'confidence': 'medium',
                'claim_label': 'observed_in_code',
                'reason_codes': row['reason_codes'],
            }
        )
    return out


def split_metric(metric: str) -> Tuple[str, str]:
    if '.' in metric:
        left, right = metric.split('.', 1)
        if left and right:
            return right, left
    return metric, 'all'


def normalize_coverage_pct(value: str) -> str:
    v = (value or '').strip()
    if not v:
        return ''
    if v.endswith('%'):
        v = v[:-1].strip()
    return v


def write_sponsor_metrics_long(rows: List[MetricRow]) -> List[dict]:
    out_rows: List[dict] = []
    for r in rows:
        metric_name, segment = split_metric(r.metric)
        out_rows.append(
            {
                'section': r.section,
                'metric_name': metric_name,
                'segment': segment,
                'value': r.value,
                'numerator': r.numerator,
                'denominator': r.denominator,
                'denominator_cohort': r.denominator_cohort,
                'coverage_pct': normalize_coverage_pct(r.coverage_pct),
                'source_table_or_file': r.source,
                'calculation_note': r.notes,
                'confidence': r.confidence,
                'pii_classification': r.pii_classification,
                'claim_label': r.claim_label,
                'raw_metric': r.metric,
            }
        )

    fieldnames_exact = [
        'section',
        'metric_name',
        'segment',
        'value',
        'numerator',
        'denominator',
        'denominator_cohort',
        'coverage_pct',
        'source_table_or_file',
        'calculation_note',
        'confidence',
    ]
    with OUT_METRICS.open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames_exact)
        writer.writeheader()
        for row in out_rows:
            writer.writerow({k: row[k] for k in fieldnames_exact})

    fieldnames_full = fieldnames_exact + ['pii_classification', 'claim_label', 'raw_metric']
    with OUT_METRICS_FULL.open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames_full)
        writer.writeheader()
        writer.writerows(out_rows)
    return out_rows


def metric_lookup(rows: List[MetricRow]) -> Dict[Tuple[str, str], MetricRow]:
    return {(r.section, r.metric): r for r in rows}


def sanitize_source(source: str) -> str:
    s = (source or '').strip()
    s = re.sub(
        r'platform_db\.EventApplicant\(eventId=[0-9a-f\-]+\)',
        f'platform_db.EventApplicant ({EVENT_LABEL})',
        s,
        flags=re.I,
    )
    s = re.sub(
        r'platform_db\.HackathonSubmission\+HackathonTeamMember\(eventId=[0-9a-f\-]+;placement!=NULL\)',
        f'platform_db.HackathonSubmission+HackathonTeamMember ({EVENT_LABEL})',
        s,
        flags=re.I,
    )
    s = re.sub(
        r'platform_db\.EventApplicant\+HackathonSubmission\+HackathonTeamMember\(eventId=[0-9a-f\-]+\)',
        f'platform_db.EventApplicant+HackathonSubmission+HackathonTeamMember ({EVENT_LABEL})',
        s,
        flags=re.I,
    )
    s = s.replace('/Users/nickita/cv-rank/', '')
    return s


def fmt_metric_line(r: MetricRow) -> str:
    return (
        f"- `{r.section}.{r.metric}`: value={r.value}, numerator={r.numerator}, "
        f"denominator={r.denominator}, denominator_cohort={r.denominator_cohort}, "
        f"coverage_pct={normalize_coverage_pct(r.coverage_pct)}, confidence={r.confidence}, source={sanitize_source(r.source)}"
    )


def parse_num(s: str) -> float:
    if not s:
        return 0.0
    t = s.strip().replace('%', '')
    try:
        return float(t)
    except ValueError:
        return 0.0


def top_rows(rows: Iterable[MetricRow], n: int = 6) -> List[MetricRow]:
    sortable = []
    for r in rows:
        sortable.append((parse_num(r.numerator), parse_num(r.value), r))
    sortable.sort(key=lambda x: (x[0], x[1]), reverse=True)
    return [x[2] for x in sortable[:n]]


def write_report(rows: List[MetricRow]) -> None:
    lk = metric_lookup(rows)
    pii_rows = [r for r in rows if r.pii_classification == 'PII-safe']

    funnel_keys = [
        ('funnel', 'applied_to_approved'),
        ('funnel', 'approved_to_checked_in'),
        ('funnel', 'checked_in_to_submitted'),
        ('funnel', 'submitted_to_placed'),
        ('Q2', 'largest_dropoff_absolute_count'),
        ('Q2', 'largest_dropoff_percentage'),
    ]
    hiring_keys = [
        ('sectionB_exec', 'q1_hiring_ready_checked_in'),
        ('sectionB_exec', 'q2_hiring_ready_with_gh_li'),
        ('sectionB_exec', 'q6_high_judging_strong_github_overlap_submitters'),
        ('sectionC_exec', 'q17_top25_hiring_leads_with_reason_codes'),
        ('sectionC_exec', 'q18_top50_hiring_leads_with_reason_codes'),
    ]
    sales_keys = [
        ('sectionD_exec', 'q2_largest_exclusive_role_bucket_student'),
        ('sectionD_exec', 'q4_startup_employer_share_all_applicants'),
        ('sectionD_exec', 'q14_student_mix_ratio_current_vs_baseline_proxy'),
    ]
    market_keys = [
        ('sectionD_exec', 'q10_top_theme_media_music_creator_share'),
        ('sectionD_exec', 'q11_top_avg_score_theme_climate_sustainability'),
        ('sectionD_exec', 'q13_underrepresented_high_performing_segment_count'),
        ('sectionH_exec', 'g3_missing_judging_weighted_avg_share'),
    ]
    marketing_keys = [
        ('sectionF_exec', 'q13_gemini_plus_any_partner_story_anchor'),
        ('sectionF_exec', 'q14_high_confidence_claim_share'),
        ('sectionH_exec', 'h25_proof_point_projects_with_sdk_evidence_share'),
    ]
    adoption_keys = [
        ('sectionE_exec', 'q1_submitter_gemini_adoption'),
        ('sectionE_exec', 'q2_submitter_antigravity_adoption'),
        ('sectionE_exec', 'q3_submitter_llamaindex_adoption'),
        ('sectionE_exec', 'q4_submitter_temporal_adoption'),
        ('sectionE_exec', 'q5_submitter_agno_adoption'),
        ('sectionE_exec', 'q8_teams_using_multiple_tools_2plus'),
    ]
    rec_keys = [
        ('sectionF_exec', 'q15_program_week2_outreach_total_target'),
        ('sectionH_exec', 'h24_docs_enablement_target_llamaindex_share'),
        ('sectionH_exec', 'q23_projects_without_endpoint_evidence_share'),
        ('sectionI_exec', 'i9_global_consistency_score'),
    ]

    def pick(keys: List[Tuple[str, str]]) -> List[MetricRow]:
        out = []
        for k in keys:
            r = lk.get(k)
            if r and r.pii_classification == 'PII-safe':
                out.append(r)
        return out

    exec_summary = []
    for k in [('funnel', 'applied_to_approved'), ('funnel', 'approved_to_checked_in'), ('funnel', 'checked_in_to_submitted'), ('funnel', 'submitted_to_placed')]:
        r = lk.get(k)
        if r:
            exec_summary.append(r)
    exec_summary.extend(pick([('sectionE_exec', 'q1_submitter_gemini_adoption'), ('sectionE_exec', 'q2_submitter_antigravity_adoption'), ('sectionH_exec', 'h25_proof_point_projects_with_sdk_evidence_share')]))

    appendix_rows = top_rows([r for r in pii_rows if r.denominator_cohort in {'all_applicants', 'approved', 'checked_in', 'submitters', 'placed'}], n=30)

    lines: List[str] = []
    lines.append('# sponsor_report_pii_safe')
    lines.append('')
    lines.append('Report classification: **PII-safe (external-safe)**')
    lines.append('')

    lines.append('## Executive Summary')
    for r in exec_summary:
        lines.append(fmt_metric_line(r))
    lines.append('')

    lines.append('## Funnel')
    for r in pick(funnel_keys):
        lines.append(fmt_metric_line(r))
    lines.append('')

    lines.append('## Hiring Insights')
    for r in pick(hiring_keys):
        lines.append(fmt_metric_line(r))
    lines.append('')

    lines.append('## Sales/BD Insights')
    for r in pick(sales_keys):
        lines.append(fmt_metric_line(r))
    lines.append('')

    lines.append('## Market Research Insights')
    for r in pick(market_keys):
        lines.append(fmt_metric_line(r))
    lines.append('')

    lines.append('## Marketing Insights')
    for r in pick(marketing_keys):
        lines.append(fmt_metric_line(r))
    lines.append('')

    lines.append('## Sponsor Tech Adoption')
    for r in pick(adoption_keys):
        lines.append(fmt_metric_line(r))
    lines.append('')

    lines.append('## Recommendations (next 30/60 days)')
    for r in pick(rec_keys):
        lines.append(fmt_metric_line(r))
    lines.append('- 30-day: run sponsor follow-up outreach on top leads and high-intent submitters first.')
    lines.append('- 60-day: re-run this exact export pipeline after follow-up touchpoints and compare deltas.')
    lines.append('')

    lines.append('## Appendix: Metric Definitions + Denominators + Coverage')
    lines.append('Allowed denominator cohorts: `all_applicants`, `approved`, `checked_in`, `submitters`, `placed`.')
    lines.append('Coverage interpretation: `coverage_pct` is inherited from source metric lineage.')
    lines.append('Claim interpretation: `self_reported` means participant-provided text/flags; `observed_in_code` means deterministic extraction from available artifacts.')
    lines.append('')
    lines.append('| section | metric | value | numerator | denominator | denominator_cohort | coverage_pct | confidence | source |')
    lines.append('|---|---|---:|---:|---:|---|---:|---|---|')
    for r in appendix_rows:
        lines.append(
            f"| {r.section} | {r.metric} | {r.value} | {r.numerator} | {r.denominator} | {r.denominator_cohort} | {normalize_coverage_pct(r.coverage_pct)} | {r.confidence} | {sanitize_source(r.source)} |"
        )

    OUT_REPORT.write_text('\n'.join(lines) + '\n')


def load_enriched() -> List[dict]:
    with ENRICHED_JSON.open() as f:
        return json.load(f)


def parse_top50_from_q18_md() -> List[dict]:
    if not Q18_MD.exists():
        return []
    lines = Q18_MD.read_text().splitlines()
    rows: List[dict] = []
    in_table = False
    for line in lines:
        if line.startswith('| rank | name | email | company | role_bucket |'):
            in_table = True
            continue
        if in_table and line.startswith('|---'):
            continue
        if in_table:
            if not line.startswith('|'):
                break
            content = line.strip().strip('|').strip()
            if not content:
                continue
            parts_right = content.rsplit(' | ', 10)
            if len(parts_right) != 11:
                continue
            left = parts_right[0]
            tail = parts_right[1:]
            left_parts = [p.strip() for p in left.split(' | ')]
            if len(left_parts) < 5:
                continue
            rank = left_parts[0]
            name = left_parts[1]
            email = left_parts[2]
            company = ' | '.join(left_parts[3:-1]).strip()
            role_bucket = left_parts[-1]
            metric_name, value, numerator, denominator, denominator_cohort, coverage_pct, source, confidence, claim_label, reason_codes = [x.strip() for x in tail]
            if not rank.isdigit():
                continue
            rows.append(
                {
                    'rank': int(rank),
                    'name': name,
                    'email': email.lower(),
                    'company': company,
                    'role_bucket': role_bucket,
                    'metric_name': metric_name,
                    'lead_score': value,
                    'numerator': numerator,
                    'denominator': denominator,
                    'denominator_cohort': denominator_cohort,
                    'coverage_pct': coverage_pct,
                    'source': source,
                    'confidence': confidence,
                    'claim_label': claim_label,
                    'reason_codes': reason_codes,
                }
            )
    rows.sort(key=lambda r: r['rank'])
    return rows[:50]


def normalize_yes_no_other(value: str) -> str:
    v = (value or '').strip().lower()
    if v in {'yes', 'y', 'true', '1', 'open', 'looking'}:
        return 'yes'
    if v in {'no', 'n', 'false', '0', 'not looking'}:
        return 'no'
    return 'other'


def parse_partner_tools(text: str) -> List[str]:
    found: List[str] = []
    seen: set[str] = set()
    allowed = {tool.lower() for tool in SPONSOR_TOOLS}
    for tool in parse_partner_tools_generic(text) + scan_known_tool_mentions(text):
        key = tool.lower()
        if key not in allowed:
            continue
        if key in seen:
            continue
        seen.add(key)
        found.append(tool)
    return found


def safe_str(x) -> str:
    return '' if x is None else str(x)


def pick_profile_urls(profile: dict) -> Tuple[str, str]:
    github = safe_str(profile.get('qa_github_url') or profile.get('github_url'))
    linkedin = safe_str(profile.get('qa_linkedin_url') or profile.get('linkedin_url'))
    return github, linkedin


def build_leads_internal(enriched: List[dict], top50: List[dict]) -> List[dict]:
    by_email: Dict[str, dict] = {}
    by_name: Dict[str, List[dict]] = defaultdict(list)
    for p in enriched:
        email = safe_str(p.get('email')).lower()
        if email and email not in by_email:
            by_email[email] = p
        name = safe_str(p.get('name')).strip().lower()
        if name:
            by_name[name].append(p)

    leads: List[dict] = []
    for r in top50:
        profile = by_email.get(r['email'])
        if profile is None:
            candidates = by_name.get(r['name'].strip().lower(), [])
            profile = candidates[0] if candidates else {}

        github_url, linkedin_url = pick_profile_urls(profile)
        submissions = profile.get('current_event_submissions') or []

        sub_names = []
        sponsor_union = set()
        for sub in submissions:
            team_name = safe_str(sub.get('team_name')).strip()
            if team_name:
                sub_names.append(team_name)
            sponsor_union.update(parse_partner_tools(safe_str(sub.get('Partner Technologies Used'))))
            for qfield, tool in [
                ('Did you try out Gemini 3? If so, what was your experience', 'Gemini'),
                ('Did you try out Antigravity? If so, what was your experience', 'Antigravity'),
                ('Did you try out LlamaIndex? If so, what was your experience?', 'LlamaIndex'),
                ('Did you try out Temporal? If so, what was your experience?', 'Temporal'),
                ('Did you try out Agno? If so, what was your experience', 'Agno'),
            ]:
                txt = safe_str(sub.get(qfield)).strip().lower()
                if txt and txt not in {'no', 'n/a', 'none', 'not used'}:
                    sponsor_union.add(tool)

        project_name = sorted(set(sub_names))[0] if sub_names else ''
        sponsor_used = ', '.join(sorted(sponsor_union))

        reasons = [x for x in r['reason_codes'].split(';') if x][:3]
        while len(reasons) < 3:
            reasons.append('')

        rc = r['reason_codes']
        if 'JOB_SEEKING_YES' in rc:
            goal_fit = 'hiring'
        elif 'DECISION_MAKER' in rc or 'ROLE_FOUNDER' in rc:
            goal_fit = 'sales_bd'
        elif 'EVENT_SUBMITTER' in rc:
            goal_fit = 'marketing'
        else:
            goal_fit = 'research'

        rank = r['rank']
        if rank <= 25:
            tier = 'A'
        elif rank <= 50:
            tier = 'B'
        else:
            tier = 'C'

        leads.append(
            {
                'lead_id': f'{re.sub(r"[^a-z0-9]+", "_", EVENT_LABEL.lower()).strip("_")}_lead_{rank:03d}',
                'name': r['name'],
                'email': r['email'],
                'linkedin_url': linkedin_url,
                'github_url': github_url,
                'company': r['company'] or safe_str(profile.get('company')),
                'role': r['role_bucket'],
                'goal_fit': goal_fit,
                'lead_score': r['lead_score'],
                'reason_code_1': reasons[0],
                'reason_code_2': reasons[1],
                'reason_code_3': reasons[2],
                'sponsor_tech_used': sponsor_used,
                'project_name': project_name,
                'judging_weighted_avg': safe_str(profile.get('current_event_judging_weighted_avg')),
                'priority_tier': tier,
            }
        )

    fieldnames = [
        'lead_id',
        'name',
        'email',
        'linkedin_url',
        'github_url',
        'company',
        'role',
        'goal_fit',
        'lead_score',
        'reason_code_1',
        'reason_code_2',
        'reason_code_3',
        'sponsor_tech_used',
        'project_name',
        'judging_weighted_avg',
        'priority_tier',
    ]
    with OUT_LEADS.open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        for lead in leads:
            writer.writerow(lead)

    return leads


def iter_unique_projects(enriched: List[dict]) -> List[dict]:
    by_id: Dict[str, dict] = {}
    for person in enriched:
        for sub in person.get('current_event_submissions') or []:
            sid = safe_str(sub.get('submission_id'))
            if sid and sid not in by_id:
                by_id[sid] = dict(sub)
    return list(by_id.values())


def extract_urls(text: str) -> List[str]:
    if not text:
        return []
    urls = re.findall(r'https?://[^\s\)\]\|>\"]+', text)
    clean = []
    for u in urls:
        clean.append(u.rstrip('.,;'))
    return clean


def make_snippet(text: str, match: str, width: int = 140) -> str:
    low = text.lower()
    idx = low.find(match.lower())
    if idx < 0:
        return match
    start = max(0, idx - 40)
    end = min(len(text), idx + len(match) + 40)
    snippet = text[start:end].replace('\n', ' ').strip()
    return snippet[:width]


def build_project_evidence(enriched: List[dict]) -> List[dict]:
    projects = iter_unique_projects(enriched)

    provider_patterns = {
        'Gemini': re.compile(r'\bgemini\b', re.I),
        'Antigravity': re.compile(r'\bantigravity\b', re.I),
        'LlamaIndex': re.compile(r'\bllama\s*index\b|\bllamaindex\b', re.I),
        'Temporal': re.compile(r'\btemporal\b', re.I),
        'Agno': re.compile(r'\bagno\b', re.I),
        'Composio': re.compile(r'\bcomposio\b|\bcomposio\s*toolset\b', re.I),
        'CrewAI': re.compile(r'\bcrew\s*ai\b|\bcrewai\b', re.I),
        'Skyfire': re.compile(r'\bskyfire\b', re.I),
        'Snowflake': re.compile(r'\bsnowflake\b|\bsnowpark\b|\bsnowflake[-_ ]connector\b|\bsnowflake\s+cortex\b', re.I),
        'Veo': re.compile(r'\bveo\b', re.I),
        'Lyria': re.compile(r'\blyria\b', re.I),
    }
    dynamic_tools: set[str] = set()
    for sub in projects:
        dynamic_tools.update(parse_partner_tools(safe_str(sub.get('Partner Technologies Used'))))
        for t in sub.get('parsed_partner_tools') or []:
            dynamic_tools.add(safe_str(t))
    for tool in sorted(dynamic_tools):
        if not tool:
            continue
        if tool not in provider_patterns:
            provider_patterns[tool] = re.compile(re.escape(tool), re.I)
    sdk_patterns = {
        'Genkit': re.compile(r'\bgenkit\b', re.I),
        'React': re.compile(r'\breact\b', re.I),
        'Next.js': re.compile(r'\bnext\.?(?:js)?\b', re.I),
        'FastAPI': re.compile(r'\bfastapi\b', re.I),
        'LangChain': re.compile(r'\blangchain\b', re.I),
        'Vite': re.compile(r'\bvite\b', re.I),
        'CrewAI': re.compile(r'\bcrew\s*ai\b|\bcrewai\b', re.I),
        'Composio': re.compile(r'\bcomposio\b|\bcomposio\s*toolset\b', re.I),
        'Snowpark': re.compile(r'\bsnowpark\b', re.I),
        'Snowflake Connector': re.compile(r'\bsnowflake[-_ ]connector\b', re.I),
    }
    infra_patterns = {
        'Supabase': re.compile(r'\bsupabase\b', re.I),
        'Firebase': re.compile(r'\bfirebase\b', re.I),
        'Vercel': re.compile(r'\bvercel\b', re.I),
        'Railway': re.compile(r'\brailway\b', re.I),
        'Cloud Run': re.compile(r'\bcloud\s*run\b', re.I),
        'Docker': re.compile(r'\bdocker\b', re.I),
        'Postgres': re.compile(r'\bpostgres(?:ql)?\b', re.I),
        'MongoDB': re.compile(r'\bmongodb\b', re.I),
        'Snowflake': re.compile(r'\bsnowflake\b|\bsnowpark\b|\bsnowflake[-_ ]connector\b', re.I),
    }
    model_patterns = {
        'gemini-3-flash': re.compile(r'gemini\s*3(?:\.0)?\s*flash', re.I),
        'gemini-2.5-flash': re.compile(r'gemini\s*2\.5\s*flash', re.I),
        'gemini-3.1-pro': re.compile(r'gemini\s*3\.1\s*pro', re.I),
        'gemini-3': re.compile(r'gemini\s*3(?!\.1)', re.I),
        'gemini-2.0-flash': re.compile(r'gemini\s*2\.0\s*flash', re.I),
        'gemini-pro': re.compile(r'gemini\s*(?:pro|ultra)', re.I),
        'claude': re.compile(r'\bclaude\b', re.I),
        'llama': re.compile(r'\bllama\b', re.I),
        'whisper': re.compile(r'\bwhisper\b', re.I),
        'imagen': re.compile(r'\bimagen\b', re.I),
        'veo': re.compile(r'\bveo\b', re.I),
        'lyria': re.compile(r'\blyria\b', re.I),
    }

    evidence_rows: List[dict] = []
    seen = set()

    for sub in projects:
        project_id = safe_str(sub.get('submission_id'))
        project_name = safe_str(sub.get('team_name'))
        repo_url = safe_str(sub.get('Public GitHub Repository'))

        fields = {
            'Project Description': safe_str(sub.get('Project Description')),
            'Partner Technologies Used': safe_str(sub.get('Partner Technologies Used')),
            'Gemini Response': safe_str(sub.get('Did you try out Gemini 3? If so, what was your experience')),
            'Antigravity Response': safe_str(sub.get('Did you try out Antigravity? If so, what was your experience')),
            'LlamaIndex Response': safe_str(sub.get('Did you try out LlamaIndex? If so, what was your experience?')),
            'Temporal Response': safe_str(sub.get('Did you try out Temporal? If so, what was your experience?')),
            'Agno Response': safe_str(sub.get('Did you try out Agno? If so, what was your experience')),
            'Demo Video': safe_str(sub.get('Demo Video')),
            'Public GitHub Repository': repo_url,
        }
        for key, val in sub.items():
            if key in fields or key in {'submission_id', 'team_name', 'placement', 'member_user_ids', 'member_emails', 'current_event_judging_weighted_avg', 'parsed_partner_tools'}:
                continue
            txt = safe_str(val)
            if txt:
                fields[key] = txt

        self_reported = set(parse_partner_tools(fields['Partner Technologies Used']))
        self_reported.update(parse_partner_tools(safe_str(','.join(sub.get('parsed_partner_tools') or []))))
        for k, tool in [
            ('Gemini Response', 'Gemini'),
            ('Antigravity Response', 'Antigravity'),
            ('LlamaIndex Response', 'LlamaIndex'),
            ('Temporal Response', 'Temporal'),
            ('Agno Response', 'Agno'),
        ]:
            txt = fields[k].strip().lower()
            if txt and txt not in {'no', 'n/a', 'none', 'not used'}:
                self_reported.add(tool)

        def emit(tool_category: str, tool_name: str, evidence_type: str, evidence_file: str, evidence_snippet: str, confidence: str, self_report_match: str) -> None:
            key = (project_id, tool_category, tool_name, evidence_type, evidence_file, evidence_snippet)
            if key in seen:
                return
            seen.add(key)
            evidence_rows.append(
                {
                    'project_id': project_id,
                    'project_name': project_name,
                    'repo_url': repo_url,
                    'tool_category': tool_category,
                    'tool_name': tool_name,
                    'evidence_type': evidence_type,
                    'evidence_file': evidence_file,
                    'evidence_line': '',
                    'evidence_snippet': evidence_snippet,
                    'confidence': confidence,
                    'self_report_match': self_report_match,
                }
            )

        for field_name, text in fields.items():
            if not text:
                continue
            scan_mentions = field_name not in {'Demo Video', 'Public GitHub Repository'}
            if scan_mentions:
                for tool, pat in provider_patterns.items():
                    m = pat.search(text)
                    if m:
                        sr = 'yes' if tool in self_reported else 'no'
                        conf = 'high' if field_name in {'Partner Technologies Used', 'Gemini Response', 'Antigravity Response', 'LlamaIndex Response', 'Temporal Response', 'Agno Response'} else 'medium'
                        emit('provider', tool, 'string', f'submission:{field_name}', make_snippet(text, m.group(0)), conf, sr)

                for tool, pat in sdk_patterns.items():
                    m = pat.search(text)
                    if m:
                        emit('sdk', tool, 'dependency', f'submission:{field_name}', make_snippet(text, m.group(0)), 'medium', 'unknown')

                for tool, pat in infra_patterns.items():
                    m = pat.search(text)
                    if m:
                        emit('infra', tool, 'config', f'submission:{field_name}', make_snippet(text, m.group(0)), 'medium', 'unknown')

                for model, pat in model_patterns.items():
                    m = pat.search(text)
                    if m:
                        conf = 'high' if re.search(r'\d', m.group(0)) else 'medium'
                        emit('model', model, 'string', f'submission:{field_name}', make_snippet(text, m.group(0)), conf, 'unknown')

            for url in extract_urls(text):
                host = urlparse(url).netloc or url
                emit('endpoint', host, 'http_call', f'submission:{field_name}', url[:180], 'high', 'unknown')

            for m in re.finditer(r'/api/[A-Za-z0-9_\-/]+', text):
                emit('app_endpoint', m.group(0), 'string', f'submission:{field_name}', make_snippet(text, m.group(0)), 'medium', 'unknown')

            if re.search(r'\bwebhook\b', text, re.I):
                emit('app_endpoint', 'webhook', 'string', f'submission:{field_name}', make_snippet(text, 'webhook'), 'medium', 'unknown')
            if re.search(r'\bworker(s)?\b', text, re.I):
                emit('app_endpoint', 'worker', 'string', f'submission:{field_name}', make_snippet(text, 'worker'), 'medium', 'unknown')

    fieldnames = [
        'project_id',
        'project_name',
        'repo_url',
        'tool_category',
        'tool_name',
        'evidence_type',
        'evidence_file',
        'evidence_line',
        'evidence_snippet',
        'confidence',
        'self_report_match',
    ]
    with OUT_EVIDENCE.open('w', newline='') as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames)
        writer.writeheader()
        writer.writerows(evidence_rows)

    return evidence_rows


def append_derived_metrics(
    master_rows: List[MetricRow],
    enriched: List[dict],
    evidence_rows: List[dict],
    snapshot: dict | None,
    top50: List[dict],
) -> List[MetricRow]:
    rows = list(master_rows)
    by_project: Dict[str, List[dict]] = defaultdict(list)
    for r in evidence_rows:
        pid = safe_str(r.get('project_id'))
        if pid:
            by_project[pid].append(r)

    project_ids = set(by_project.keys())
    projects_n = len(project_ids)
    sdk_projects = sum(1 for pid in project_ids if any(e.get('tool_category') == 'sdk' for e in by_project[pid]))
    model_projects = sum(1 for pid in project_ids if any(e.get('tool_category') == 'model' for e in by_project[pid]))
    endpoint_projects = sum(1 for pid in project_ids if any(e.get('tool_category') == 'endpoint' for e in by_project[pid]))

    source = f'platform_db.HackathonSubmission ({EVENT_LABEL})'
    if projects_n > 0:
        rows.append(_metric_row('sectionE_exec', 'q9_projects_with_sdk_package_evidence', numerator=sdk_projects, denominator=projects_n, denominator_cohort='submitters', source=source))
        rows.append(_metric_row('sectionE_exec', 'q11_projects_with_model_ids', numerator=model_projects, denominator=projects_n, denominator_cohort='submitters', source=source))
        rows.append(_metric_row('sectionE_exec', 'q10_projects_with_external_base_urls', numerator=endpoint_projects, denominator=projects_n, denominator_cohort='submitters', source=source))
        rows.append(_metric_row('sectionF_exec', 'q9_projects_with_sdk_package_evidence', numerator=sdk_projects, denominator=projects_n, denominator_cohort='submitters', source=source))
        rows.append(_metric_row('sectionF_exec', 'q11_projects_with_model_ids', numerator=model_projects, denominator=projects_n, denominator_cohort='submitters', source=source))

    applicants_n = len(enriched)
    top25 = min(25, len(top50))
    top50_n = min(50, len(top50))
    if applicants_n > 0:
        rows.append(_metric_row('sectionC_exec', 'q17_top25_hiring_leads_with_reason_codes', numerator=top25, denominator=applicants_n, denominator_cohort='all_applicants', source='helper.top_leads'))
        rows.append(_metric_row('sectionC_exec', 'q18_top50_hiring_leads_with_reason_codes', numerator=top50_n, denominator=applicants_n, denominator_cohort='all_applicants', source='helper.top_leads'))
        rows.append(_metric_row('sectionB_exec', 'q17_top25_hiring_leads_with_reason_codes', numerator=top25, denominator=applicants_n, denominator_cohort='all_applicants', source='helper.top_leads'))
        rows.append(_metric_row('sectionB_exec', 'q18_top50_hiring_leads_with_reason_codes', numerator=top50_n, denominator=applicants_n, denominator_cohort='all_applicants', source='helper.top_leads'))

    if snapshot:
        team_sizes = []
        for sub in snapshot.get('submissions', []):
            members = sub.get('member_user_ids') or sub.get('member_emails') or []
            team_sizes.append(len(members))
        if team_sizes:
            avg_team = sum(team_sizes) / len(team_sizes)
            rows.append(
                MetricRow(
                    section='sectionF_exec',
                    metric='q14_avg_team_size',
                    value=f'{avg_team:.4f}',
                    numerator=str(int(round(avg_team * 100))),
                    denominator='100',
                    denominator_cohort='submitters',
                    coverage_pct='100.0',
                    source=source,
                    pii_classification='PII-safe',
                    confidence='high',
                    claim_label='observed_in_code',
                    notes='average team size',
                )
            )

    return rows


def write_event_headline_bank(enriched: List[dict], snapshot: dict | None) -> None:
    checked = [p for p in enriched if _as_bool(p.get('current_event_checked_in'))]
    checked_n = len(checked)
    if checked_n == 0:
        return

    def sum_with_cov(field: str) -> tuple[int, int]:
        vals = []
        for p in checked:
            v = p.get(field)
            if v in (None, ''):
                continue
            vals.append(int(_safe_float(v)))
        return sum(vals), len(vals)

    li_followers_sum, li_followers_cov = sum_with_cov('li_follower_count')
    li_connections_sum, li_connections_cov = sum_with_cov('li_connection_count')
    gh_private_sum, gh_private_cov = sum_with_cov('gh_api_private_contributions')
    gh_commits_sum, gh_commits_cov = sum_with_cov('gh_api_commits_year')
    gh_repos_sum, gh_repos_cov = sum_with_cov('gh_api_repos')
    gh_stars_sum, gh_stars_cov = sum_with_cov('gh_api_stars')

    founder_checked = sum(1 for p in checked if _as_bool(p.get('is_founder')))
    decision_checked = sum(1 for p in checked if _as_bool(p.get('is_decision_maker')))
    bigtech_checked = sum(1 for p in checked if _as_bool(p.get('is_in_big_tech')))

    submissions = snapshot.get('submissions', []) if snapshot else []
    submitter_users = set()
    placed_users = set()
    tool_team_counts = Counter()
    for sub in submissions:
        members = {safe_str(u) for u in sub.get('member_user_ids', []) if safe_str(u)}
        submitter_users.update(members)
        if safe_str(sub.get('placement')):
            placed_users.update(members)
        for tool in set(sub.get('parsed_partner_tools') or []):
            tool_team_counts[tool] += 1

    lines = [
        '# Sponsor Headline Bank (Event-Specific)',
        '',
        f'Event: {EVENT_LABEL}',
        '',
        '| # | Headline | Numbers | Why this matters |',
        '|---|---|---|---|',
        f'| 1 | **{checked_n} builders showed up and {len(submissions)} teams shipped, creating a large post-event follow-up window.** | Checked-in participants: `{checked_n}`; team submissions: `{len(submissions)}`. | Demonstrates real execution volume, not just registrations. |',
        f'| 2 | **The checked-in audience brought {li_followers_sum:,} LinkedIn followers that can amplify sponsor stories fast.** | Followers captured: `{li_followers_sum:,}` from `{li_followers_cov}` checked-in profiles. | Large organic distribution potential for launches and recaps. |',
        f'| 3 | **This cohort also carried {li_connections_sum:,} first-degree LinkedIn connections for direct GTM reach.** | First-degree connections: `{li_connections_sum:,}` from `{li_connections_cov}` checked-in profiles. | Useful reach layer for partner announcements and co-marketing. |',
        f'| 4 | **Checked-in builders logged {gh_private_sum:,} private GitHub contributions, signaling sustained build activity.** | Private contributions: `{gh_private_sum:,}` from `{gh_private_cov}` checked-in profiles. | Strong proxy for execution velocity in production-like work. |',
        f'| 5 | **Across the room, builders represented {gh_commits_sum:,} annual GitHub commits and sustained coding throughput.** | Annual commits: `{gh_commits_sum:,}` from `{gh_commits_cov}` checked-in profiles. | Reinforces technical depth for hiring and developer-focused sponsors. |',
        f'| 6 | **Participants collectively touched {gh_repos_sum:,} repositories and earned {gh_stars_sum:,} stars across GitHub.** | Repositories: `{gh_repos_sum:,}`; stars: `{gh_stars_sum:,}` from checked-in profiles with GitHub data. | Indicates breadth of project exposure and public technical credibility. |',
        f'| 7 | **Business-side signal was meaningful with {founder_checked} founders and {decision_checked} decision-makers in attendance.** | Founders: `{founder_checked}/{checked_n}`; decision-makers: `{decision_checked}/{checked_n}`. | Valuable sponsor-facing audience for product and partnership conversations. |',
        f'| 8 | **Enterprise-context experience showed up too, with {bigtech_checked} attendees listing big-tech backgrounds.** | Big-tech-affiliated attendees: `{bigtech_checked}/{checked_n}`. | Adds credibility for enterprise-oriented sponsor narratives. |',
        f'| 9 | **Execution carried through to outcomes: {len(submitter_users)} submitter participants and {len(placed_users)} placed participants.** | Submitter participants: `{len(submitter_users)}`; placed participants: `{len(placed_users)}`. | Gives sponsors a clear high-signal cohort for deeper follow-up. |',
    ]
    if tool_team_counts:
        top_tools = ', '.join([f'{t} ({n})' for t, n in tool_team_counts.most_common(4)])
        lines.append(
            f'| 10 | **Top submission tools by team usage were {top_tools}.** | Team-level tool counts across `{len(submissions)}` submissions. | Concrete sponsor adoption proof points from project builds. |'
        )

    OUT_HEADLINE_BANK.write_text('\n'.join(lines) + '\n')


def write_companion_docs(metrics_rows: List[dict], leads_rows: List[dict], evidence_rows: List[dict]) -> None:
    pii_safe = sum(1 for r in metrics_rows if r['pii_classification'] == 'PII-safe')
    internal = sum(1 for r in metrics_rows if r['pii_classification'] == 'internal-only')

    OUT_METRICS_MD.write_text(
        '\n'.join(
            [
                '# sponsor_metrics_long',
                '',
                'Purpose: normalized long-form metric export for sponsor analysis.',
                f'- Total metric rows exported: {len(metrics_rows)}',
                f'- PII-safe rows: {pii_safe}',
                f'- Internal-only rows: {internal}',
                '- Source: event-native deterministic metrics + enriched dataset (fallback: marketing_master.csv when event DB context unavailable).',
                '- Required columns included: section, metric_name, segment, value, numerator, denominator, denominator_cohort, coverage_pct, source_table_or_file, calculation_note, confidence.',
                '- Strict file: `sponsor_metrics_long.csv` (exact required columns only).',
                '- Full preservation file: `sponsor_metrics_long_full.csv` (adds pii_classification, claim_label, raw_metric).',
            ]
        )
        + '\n'
    )

    tier_counts = defaultdict(int)
    goal_counts = defaultdict(int)
    for r in leads_rows:
        tier_counts[r['priority_tier']] += 1
        goal_counts[r['goal_fit']] += 1

    OUT_LEADS_MD.write_text(
        '\n'.join(
            [
                '# sponsor_leads_internal',
                '',
                'Classification: **internal-only** (contains person-level contact fields).',
                f'- Lead rows exported: {len(leads_rows)} (top-50 ranked list).',
                f"- Priority tiers: A={tier_counts.get('A',0)}, B={tier_counts.get('B',0)}, C={tier_counts.get('C',0)}.",
                '- Goal fit categories: hiring, sales_bd, marketing, research.',
                f"- Goal-fit counts: hiring={goal_counts.get('hiring',0)}, sales_bd={goal_counts.get('sales_bd',0)}, marketing={goal_counts.get('marketing',0)}, research={goal_counts.get('research',0)}.",
                '- Ranking source: Q18 markdown if provided; otherwise deterministic fallback scoring from event check-in/submission/job-seeking/GitHub/judging signals.',
            ]
        )
        + '\n'
    )

    cat_counts = defaultdict(int)
    for r in evidence_rows:
        cat_counts[r['tool_category']] += 1

    OUT_EVIDENCE_MD.write_text(
        '\n'.join(
            [
                '# project_tech_evidence',
                '',
                'Classification: mixed evidence table; safe for internal analysis by default.',
                f'- Evidence rows exported: {len(evidence_rows)}.',
                '- Project base: deduped unique submissions (`submission_id`) from enriched dataset.',
                f"- Tool category row counts: provider={cat_counts.get('provider',0)}, sdk={cat_counts.get('sdk',0)}, endpoint={cat_counts.get('endpoint',0)}, model={cat_counts.get('model',0)}, infra={cat_counts.get('infra',0)}, app_endpoint={cat_counts.get('app_endpoint',0)}.",
                '- Evidence source is submission text and URLs (no repo cloning in this export).',
            ]
        )
        + '\n'
    )


def write_task_list(metrics_rows: List[dict], leads_rows: List[dict], evidence_rows: List[dict]) -> None:
    lines = [
        '# Sponsor Export Task List',
        '',
        '- [x] Map source artifacts and required schemas.',
        '- [x] Generate `sponsor_metrics_long.csv` with exact required columns.',
        '- [x] Generate `sponsor_metrics_long_full.csv` for no-loss extra fields.',
        '- [x] Generate `sponsor_report_pii_safe.md` with required section order.',
        '- [x] Generate `sponsor_leads_internal.csv` with required lead columns.',
        '- [x] Generate `project_tech_evidence.csv` with required evidence columns.',
        '- [x] Add companion explainers (`sponsor_metrics_long.md`, `sponsor_leads_internal.md`, `project_tech_evidence.md`).',
        '- [x] Validate row counts and required columns.',
        '',
        '## Validation Snapshot',
        f'- sponsor_metrics_long.csv rows: {len(metrics_rows)}',
        f'- sponsor_leads_internal.csv rows: {len(leads_rows)}',
        f'- project_tech_evidence.csv rows: {len(evidence_rows)}',
        '- sponsor_report_pii_safe.md created with 9 required sections + appendix.',
    ]
    OUT_TASK_LIST.write_text('\n'.join(lines) + '\n')


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description='Build deterministic sponsor delivery bundle from normalized CSVs + enriched JSON.',
    )
    parser.add_argument('--base', default=str(DEFAULT_BASE), help='cv-rank repo root (default: script parent repo)')
    parser.add_argument('--run-id', default=os.getenv('CVRANK_RUN_ID'), help='results run folder (example: run_20260306_185829)')
    parser.add_argument('--enriched-json', default=os.getenv('CVRANK_ENRICHED_JSON'), help='override enriched_complete*.json path')
    parser.add_argument('--q18-md', default=os.getenv('CVRANK_Q18_MD'), help='optional sectionC_agent5_q18.md path')
    parser.add_argument('--event-label', default=os.getenv('CVRANK_EVENT_LABEL'), help='event label for source normalization text')
    parser.add_argument(
        '--use-legacy-master',
        action='store_true',
        help='force legacy marketing_master.csv metrics instead of event-native DB-derived metrics',
    )
    parser.add_argument(
        '--allow-metrics-mismatch',
        action='store_true',
        help='allow output generation even when marketing_master funnel cohort does not match enriched dataset size',
    )
    return parser


def main(argv: list[str] | None = None) -> None:
    args = build_parser().parse_args(argv)
    configure_paths(
        base=Path(args.base),
        run_id=args.run_id,
        enriched_json=args.enriched_json,
        event_label=args.event_label,
        q18_md=args.q18_md,
    )

    enriched = load_enriched()
    snapshot = load_event_snapshot(EVENT_LABEL) if EVENT_LABEL else None
    if snapshot:
        hydrate_enriched_with_event_snapshot(enriched, snapshot)
        validate_snapshot_alignment(
            enriched,
            snapshot,
            allow_mismatch=bool(args.allow_metrics_mismatch),
        )

    if snapshot and not args.use_legacy_master:
        master_rows = build_event_metrics_rows(enriched, snapshot)
    else:
        master_rows = read_master_metrics()
        validate_event_alignment(
            master_rows,
            enriched,
            allow_mismatch=bool(args.allow_metrics_mismatch),
        )

    top50 = parse_top50_from_q18_md()
    if not top50:
        top50 = build_fallback_top50(enriched)
    leads_rows = build_leads_internal(enriched, top50)
    evidence_rows = build_project_evidence(enriched)
    master_rows = append_derived_metrics(master_rows, enriched, evidence_rows, snapshot, top50)
    write_event_headline_bank(enriched, snapshot)

    metrics_rows = write_sponsor_metrics_long(master_rows)
    write_report(master_rows)

    write_companion_docs(metrics_rows, leads_rows, evidence_rows)
    write_task_list(metrics_rows, leads_rows, evidence_rows)

    print('base', BASE)
    print('event_label', EVENT_LABEL)
    print('event_snapshot', 'loaded' if snapshot else 'not_loaded')
    print('enriched_json', ENRICHED_JSON)
    print('q18_md', Q18_MD if Q18_MD.exists() else '(missing, leads fallback)')
    print('wrote', OUT_REPORT)
    print('wrote', OUT_METRICS, 'rows', len(metrics_rows))
    print('wrote', OUT_METRICS_FULL, 'rows', len(metrics_rows))
    print('wrote', OUT_LEADS, 'rows', len(leads_rows))
    print('wrote', OUT_EVIDENCE, 'rows', len(evidence_rows))
    print('wrote companion docs + task list')


if __name__ == '__main__':
    main()
