"""
CLI entry point for cv-rank.

Usage::

    cv-rank run --csv applicants.csv --accept 50
    cv-rank validate --csv applicants.csv
    cv-rank init
    cv-rank --version
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import json
import logging
import shlex
import subprocess
import sys
import time
from collections import Counter, defaultdict
from pathlib import Path

from dotenv import load_dotenv

from cv_rank import __version__

REPO_ROOT = Path(__file__).resolve().parents[2]
ATTENDEE_QUEUE_SCRIPT = REPO_ROOT / "scripts" / "attendee_dossier_queue.py"
MINIMAX_DOSSIER_WORKERS = REPO_ROOT / "ops" / "scripts" / "start_daytona_minimax_queue_workers.py"
DEFAULT_DAYTONA_ENV = REPO_ROOT / ".env.daytona"


# ---------------------------------------------------------------------------
# Pipeline phase helpers (extracted from _cmd_run for readability)
# ---------------------------------------------------------------------------

def _run_enrichment(people, config, run_dir, logger, args):
    """Phase 1: enrich profiles from supabase, github, local CSV.

    Returns (people, should_stop) where should_stop=True for --enrich-only.
    """
    from cv_rank.checkpoint import get_completed_phases, load_checkpoint, save_checkpoint
    from cv_rank.display import print_banner

    completed = get_completed_phases(run_dir)

    if "enrichment" not in completed:
        print_banner("Phase 1: Enrichment")
        logger.info("Starting Phase 1 (Enrichment) with %d people", len(people))
        t0 = time.time()

        if config["enrichment"]["supabase"]["enabled"]:
            from cv_rank.enrichment.supabase import enrich_from_supabase
            people = enrich_from_supabase(people, config)

        if config["enrichment"].get("exa", {}).get("enabled"):
            from cv_rank.enrichment.exa_enricher import enrich_from_exa
            people = enrich_from_exa(people, config)

        if config["enrichment"]["github"]["enabled"]:
            from cv_rank.enrichment.github_api import enrich_from_github_api
            people = enrich_from_github_api(people, config)

        if config["enrichment"]["local_csv"]["enabled"]:
            from cv_rank.enrichment.local_csv import enrich_from_local_csv
            people = enrich_from_local_csv(people, config)

        if config["enrichment"].get("platform_db", {}).get("enabled"):
            from cv_rank.enrichment.platform_db import enrich_from_platform_db
            people = enrich_from_platform_db(people, config)

        save_checkpoint(run_dir, "enrichment", people)
        elapsed_enrich = time.time() - t0
        logger.info("Enrichment complete in %.0fs", elapsed_enrich)

        # Post-enrichment data quality summary
        _post = {
            "linkedin_profile": sum(1 for p in people if p.get("linkedin_headline")),
            "work_history": sum(1 for p in people if p.get("positions_with_companies")),
            "education": sum(1 for p in people if p.get("education_with_schools")),
            "github_db": sum(1 for p in people if p.get("github_total_score") or p.get("gh_api_stars")),
            "event_history": sum(1 for p in people if p.get("event_history")),
            "judging_scores": sum(1 for p in people if p.get("judging_scores_received")),
            "hackathon_subs": sum(1 for p in people if p.get("hackathon_submissions")),
            "x_handle": sum(1 for p in people if p.get("x_handle")),
        }
        logger.info("POST-ENRICHMENT DATA QUALITY:")
        for field, count in _post.items():
            pct = count * 100 // len(people) if people else 0
            bar = "#" * (pct // 5)
            logger.info("  %-20s %3d/%d (%3d%%) %s", field, count, len(people), pct, bar)
    else:
        people = load_checkpoint(run_dir, "enrichment") or people
        logger.info("Enrichment: loaded from checkpoint")

    if args.enrich_only:
        out = run_dir / "enriched.json"
        with open(out, "w") as f:
            json.dump(people, f, indent=2, default=str)
        logger.info("Enriched data saved to %s (--enrich-only)", out)
        return people, True

    return people, False


def _compute_p_show(people, config, args, logger):
    """Compute per-person P(show) after enrichment.

    Uses the trained logistic regression model from waves.model.
    When platform DB is available and we know the event name, fetches
    per-applicant timing and timezone data for accurate predictions.
    Otherwise falls back to enrichment data with defaults.

    Mutates people in-place (adds 'p_show' key).
    """
    import os
    from datetime import datetime

    from cv_rank.waves.model import predict_show_probability
    from cv_rank.waves.predict import (
        city_to_timezone,
        compute_tz_offset_diff,
        tz_distance_bucket,
    )

    event_name = getattr(args, "event", None)
    event_date_str = getattr(args, "event_date", None)
    event_city = getattr(args, "city", None)

    # Try to get per-applicant data from platform DB
    pdb = config.get("enrichment", {}).get("platform_db", {})
    dsn = pdb.get("dsn") or os.environ.get("PLATFORM_DATABASE_URL", "")
    applicant_data = {}  # email -> {days_before, tz_distance}

    if dsn and not dsn.startswith("${") and pdb.get("enabled", True):
        try:
            import psycopg2
            import psycopg2.extras

            conn = psycopg2.connect(dsn)
            try:
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    # Find the event by name or title
                    event_title = event_name
                    if not event_title:
                        # For --csv mode, try config event name
                        event_title = config.get("event", {}).get("name", "")

                    if event_title:
                        event_slug_guess = event_title.strip().lower().replace("_", "-")
                        # Query event info + per-applicant timing/tz/page views/notification read
                        cur.execute("""
                            WITH page_view_counts AS (
                                SELECT i."userId", i.properties->>'eventId' AS event_id, COUNT(*) AS view_count
                                FROM "Insight" i
                                JOIN "PlatformEvent" pe_pv ON pe_pv.id::text = i.properties->>'eventId'
                                WHERE i."eventName" = 'PAGE_VIEW'
                                AND i.properties->>'eventId' IS NOT NULL
                                AND i."createdAt" < pe_pv."startDateTime"
                                GROUP BY i."userId", i.properties->>'eventId'
                            ),
                            notif_reads AS (
                                SELECT DISTINCT ON (pn."userId", pn.data->>'eventId')
                                    pn."userId", pn.data->>'eventId' AS event_id, pn.read AS notif_read
                                FROM "PlatformNotification" pn
                                WHERE pn.type = 'event_application_status_change'
                                AND pn.data->>'status' = 'approved'
                                ORDER BY pn."userId", pn.data->>'eventId', pn."createdAt" DESC
                            )
                            SELECT
                                LOWER(up.email) AS email,
                                EXTRACT(EPOCH FROM (pe."startDateTime" - ea."createdAt")) / 86400.0 AS days_before,
                                ea."appliedFromTimeZone" AS applicant_tz,
                                pe."startDateTime" AS event_start,
                                pe.city AS event_city,
                                COALESCE(pv.view_count, 0) AS page_views,
                                COALESCE(nr.notif_read, false) AS notif_read
                            FROM "EventApplicant" ea
                            JOIN "PlatformEvent" pe ON ea."eventId" = pe.id
                            JOIN "UserProfile" up ON ea."userId" = up."userId"
                            LEFT JOIN page_view_counts pv ON pv."userId" = ea."userId" AND pv.event_id = pe.id::text
                            LEFT JOIN notif_reads nr ON nr."userId" = ea."userId" AND nr.event_id = pe.id::text
                            WHERE pe.title = %s OR pe.slug = %s
                              AND ea."createdAt" IS NOT NULL
                              AND pe."startDateTime" IS NOT NULL
                        """, (event_title, event_slug_guess))
                        rows = cur.fetchall()

                        if rows:
                            # Get event info from first row
                            if not event_city:
                                event_city = rows[0].get("event_city") or ""
                            if not event_date_str and rows[0].get("event_start"):
                                event_date_str = str(rows[0]["event_start"].date())

                            event_tz = city_to_timezone(event_city, event_title)

                            for row in rows:
                                email = row["email"]
                                days = float(row["days_before"] or 0)
                                app_tz = row.get("applicant_tz") or ""
                                if app_tz:
                                    offset = compute_tz_offset_diff(app_tz, event_tz)
                                    tz_dist = tz_distance_bucket(offset)
                                else:
                                    tz_dist = "medium"  # unknown tz → conservative default
                                applicant_data[email] = {
                                    "days_before": max(days, 0),
                                    "tz_distance": tz_dist,
                                    "page_views": int(row.get("page_views") or 0),
                                    "notification_read": bool(row.get("notif_read")),
                                }

                            logger.info(
                                "P_Show: loaded timing/tz/views/notif data for %d applicants from platform DB",
                                len(applicant_data),
                            )
                        else:
                            logger.warning(
                                "P_Show: no platform event match for %r (tried title/slug), skipping DB timing lookup",
                                event_title,
                            )
            finally:
                conn.close()
        except Exception as exc:
            logger.warning("P_Show: platform DB query failed: %s", exc)

    # Parse event date once before the loop
    evt_dt = None
    if event_date_str:
        try:
            evt_dt = datetime.strptime(event_date_str, "%Y-%m-%d")
        except ValueError:
            logger.warning("P_Show: invalid --event-date format %r (expected YYYY-MM-DD), skipping fallback timing", event_date_str)

    # Build a name-based fallback index for applicant_data
    # so people with mismatched emails can still get P_Show from platform data
    _name_to_ad: dict[str, dict] = {}
    if applicant_data and dsn and not dsn.startswith("${"):
        try:
            import psycopg2
            import psycopg2.extras as _extras

            _conn = psycopg2.connect(dsn)
            try:
                with _conn.cursor(cursor_factory=_extras.RealDictCursor) as _cur:
                    _cur.execute("""
                        SELECT LOWER(email) as email,
                               TRIM(LOWER(COALESCE("firstName", '') || ' ' || COALESCE("lastName", ''))) as full_name
                        FROM "UserProfile"
                        WHERE LOWER(email) = ANY(%s)
                    """, (list(applicant_data.keys()),))
                    for _row in _cur.fetchall():
                        fn = (_row.get("full_name") or "").strip()
                        if fn and _row["email"] in applicant_data:
                            if fn in _name_to_ad:
                                _name_to_ad[fn] = None  # ambiguous — multiple DB rows share this name
                            else:
                                _name_to_ad[fn] = applicant_data[_row["email"]]
            finally:
                _conn.close()
        except Exception:
            pass  # fallback index is best-effort

    # Compute p_show for each person
    computed = 0
    for person in people:
        email = (person.get("email") or "").lower().strip()
        ad = applicant_data.get(email)

        # Fallback: match by name if email didn't match
        if not ad:
            pname = f"{(person.get('first_name') or '').lower().strip()} {(person.get('last_name') or '').lower().strip()}".strip()
            if pname:
                ad = _name_to_ad.get(pname)

        if ad:
            days_before = ad["days_before"]
            tz_dist = ad["tz_distance"]
            pv = ad.get("page_views", 0)
            nr = ad.get("notification_read", False)
        else:
            # Fallback: use per-person application timestamp if available,
            # otherwise fall back to event_date - now().
            if evt_dt:
                try:
                    # prefer per-person created_at from enrichment data
                    created_at = person.get("created_at") or person.get("applied_at")
                    app_dt = None
                    if created_at:
                        if isinstance(created_at, str):
                            for fmt in ("%Y-%m-%dT%H:%M:%S", "%Y-%m-%d %H:%M:%S", "%Y-%m-%d"):
                                try:
                                    app_dt = datetime.strptime(created_at[:19], fmt)
                                    break
                                except ValueError:
                                    continue
                            if app_dt is None:
                                logger.warning("Unrecognized date format for %s: %r", email, created_at)
                        elif isinstance(created_at, datetime):
                            # strip tzinfo so subtraction with naive evt_dt works
                            app_dt = created_at.replace(tzinfo=None)

                    if app_dt is not None:
                        days_before = (evt_dt - app_dt).total_seconds() / 86400.0
                        days_before = max(days_before, 0)
                    else:
                        days_before = 7.0  # neutral default — avoids inflating score
                except (ValueError, TypeError):
                    days_before = 7.0  # reasonable default
            else:
                # No timing data at all — can't compute meaningful p_show
                continue
            tz_dist = "medium"  # unknown tz → conservative default
            pv = 0
            nr = False

        # Prior attendance from enrichment
        try:
            total_events = int(float(person.get("total_cv_events", 0) or 0))
        except (ValueError, TypeError):
            total_events = 0
        try:
            checkin_count = int(float(person.get("checkin_count", 0) or 0))
        except (ValueError, TypeError):
            checkin_count = 0
        if total_events > 0:
            # Use total_events as-is — in CSV ranking the current event is
            # typically not yet in the user's DB history, so subtracting 1
            # would incorrectly deflate the denominator.
            prior_rate = checkin_count / total_events
        else:
            prior_rate = None

        p_show = predict_show_probability(
            days_before_event=days_before,
            prior_attendance_rate=prior_rate,
            tz_distance=tz_dist,
            page_views=pv,
            notification_read=nr,
        )
        person["p_show"] = round(p_show, 3)
        computed += 1

    logger.info("P_Show: computed for %d / %d people", computed, len(people))


def _run_pointwise(people, criteria, config, run_dir, logger, format_profile_fn=None):
    """Phase 2: LLM pointwise scoring for each candidate."""
    from cv_rank.checkpoint import get_completed_phases, load_checkpoint, save_checkpoint
    from cv_rank.display import print_banner, print_histogram

    if format_profile_fn is None:
        from cv_rank.profile import format_profile
        format_profile_fn = format_profile

    completed = get_completed_phases(run_dir)
    accept_count = config["event"]["target_accepts"]

    if "pointwise" not in completed:
        print_banner("Phase 2: Pointwise Scoring")
        logger.info("Starting Phase 2 (Pointwise Scoring) with %d people", len(people))
        t_pw = time.time()
        from cv_rank.scoring.pointwise import score_all

        scores = asyncio.run(score_all(
            people, criteria, config["models"]["scoring"],
            accept_count, config, run_dir, format_profile_fn,
        ))
        save_checkpoint(run_dir, "pointwise", scores)
        logger.info("Phase 2 (Pointwise) complete in %.1fs", time.time() - t_pw)
    else:
        scores = load_checkpoint(run_dir, "pointwise") or []
        logger.info("Pointwise: loaded %d scores from checkpoint", len(scores))

    if scores:
        score_vals = [s.get("score", 0) or 0 for s in scores]
        print_histogram(score_vals, label="Pointwise Score")

    return scores


def _run_swiss(people, criteria, config, run_dir, logger,
               format_profile_fn=None, pointwise_scores=None):
    """Phase 3: Swiss-system tournament with Bradley-Terry ranking."""
    from cv_rank.checkpoint import get_completed_phases, load_checkpoint, save_checkpoint
    from cv_rank.display import print_banner

    if format_profile_fn is None:
        from cv_rank.profile import format_profile
        format_profile_fn = format_profile

    completed = get_completed_phases(run_dir)
    accept_count = config["event"]["target_accepts"]

    if "swiss" not in completed:
        print_banner("Phase 3: Swiss Tournament")
        logger.info("Starting Phase 3 (Swiss Tournament) with %d people", len(people))
        t_sw = time.time()
        from cv_rank.scoring.swiss import run_swiss

        swiss_records, bt_strengths, all_matches = asyncio.run(run_swiss(
            people, criteria, config["models"]["swiss"],
            config["swiss"]["rounds"], accept_count,
            config, run_dir, format_profile_fn,
            pointwise_scores=pointwise_scores,
        ))
        save_checkpoint(run_dir, "swiss", {
            "records": swiss_records,
            "bt_strengths": bt_strengths,
            "match_count": len(all_matches),
        })
        logger.info("Phase 3 (Swiss) complete in %.1fs, %d matches", time.time() - t_sw, len(all_matches))
    else:
        swiss_data = load_checkpoint(run_dir, "swiss") or {}
        swiss_records = swiss_data.get("records", {})
        bt_strengths = swiss_data.get("bt_strengths", {})
        logger.info("Swiss: loaded %d records from checkpoint", len(swiss_records))

    return swiss_records, bt_strengths


def _run_combine(scores, swiss_records, bt_strengths, config, run_dir, logger):
    """Phase 4: merge pointwise + Swiss into final rankings."""
    from cv_rank.checkpoint import get_completed_phases, load_checkpoint, save_checkpoint
    from cv_rank.display import print_banner

    completed = get_completed_phases(run_dir)

    if "combine" not in completed:
        print_banner("Phase 4: Combining Rankings")
        logger.info("Starting Phase 4 (Combine Rankings)")
        t_comb = time.time()
        from cv_rank.scoring.combine import combine_rankings

        rankings = combine_rankings(
            scores, swiss_records, bt_strengths,
            swiss_weight=config["weights"]["swiss"],
            pointwise_weight=config["weights"]["pointwise"],
            auto_weight=config.get("weights", {}).get("auto", False),
        )
        save_checkpoint(run_dir, "combine", rankings)
        logger.info("Phase 4 (Combine) complete in %.1fs", time.time() - t_comb)
    else:
        rankings = load_checkpoint(run_dir, "combine") or []
        logger.info("Combine: loaded %d rankings from checkpoint", len(rankings))

    return rankings


def _run_quality(people, rankings, config, run_dir, logger):
    """Phase 5: LLM quality-check on top-ranked candidates."""
    from cv_rank.checkpoint import get_completed_phases, load_checkpoint, save_checkpoint
    from cv_rank.display import print_banner
    from cv_rank.profile import format_profile

    completed = get_completed_phases(run_dir)

    if "quality" not in completed:
        print_banner("Phase 5: Quality Check")
        logger.info("Starting Phase 5 (Quality Check) with %d people", len(people))
        t_qc = time.time()
        from cv_rank.quality import run_quality_check

        qc_results = asyncio.run(run_quality_check(
            people, rankings, config, run_dir, format_profile,
        ))
        save_checkpoint(run_dir, "quality", qc_results)
        logger.info("Phase 5 (Quality Check) complete in %.1fs", time.time() - t_qc)
    else:
        qc_results = load_checkpoint(run_dir, "quality") or []
        logger.info("Quality: loaded %d results from checkpoint", len(qc_results))

    return qc_results


def _run_export(people, rankings, scores, swiss_records, qc_results,
                config, run_dir, logger):
    """Phase 6: write RANKED.csv and finalize checkpoint."""
    from cv_rank.checkpoint import get_completed_phases, save_checkpoint
    from cv_rank.csv_io import export_ranked_csv, export_clean_csv
    from cv_rank.display import print_banner

    completed = get_completed_phases(run_dir)

    if "export" not in completed:
        print_banner("Phase 6: Export")

        # Build lookup dicts for export_ranked_csv
        score_map = {r["name"]: r.get("final_score", 0) for r in rankings}
        qc_map = {r["name"]: r for r in qc_results}
        pw_map = {s["name"]: s.get("score", 0) for s in scores}

        output_csv = run_dir / "RANKED.csv"
        export_ranked_csv(
            people=people,
            scores=score_map,
            swiss_records=swiss_records,
            pointwise_scores=pw_map,
            quality_results=qc_map,
            config=config,
            output_path=output_csv,
        )
        save_checkpoint(run_dir, "export", {"csv": str(output_csv)})
    else:
        # Rebuild qc_map for summary display
        qc_map = {r["name"]: r for r in qc_results}
        output_csv = run_dir / "RANKED.csv"
        logger.info("Export: already complete (use --no-resume to re-export)")

    # Always generate the clean CSV alongside the full one
    clean_csv = export_clean_csv(output_csv)
    logger.info("Clean CSV: %s", clean_csv)

    return output_csv, qc_map


def _require_supabase_enrichment(config: dict, args: argparse.Namespace) -> bool:
    """Check Supabase is enabled — called by incremental command."""
    supa = config["enrichment"]["supabase"]
    if not supa.get("enabled"):
        print(
            "  Error: Supabase enrichment is required for this command.\n"
            "         Provide SUPABASE_URL/SUPABASE_KEY in .env.",
            file=sys.stderr,
        )
        return False
    return True


# ---------------------------------------------------------------------------
# Subcommand: run
# ---------------------------------------------------------------------------

def _cmd_run(args: argparse.Namespace) -> int:
    """Execute the full ranking pipeline."""
    # Require exactly one of --csv or --event
    if not args.csv and not args.event:
        print("Error: must provide --csv or --event", file=sys.stderr)
        return 1
    if args.csv and args.event:
        print("Error: --csv and --event are mutually exclusive", file=sys.stderr)
        return 1

    from cv_rank.checkpoint import (
        create_run_dir,
        find_latest_run,
        get_completed_phases,
        save_meta,
    )
    from cv_rank.config import load_config, setup_logging, validate_config
    from cv_rank.csv_io import load_csv
    from cv_rank.enrichment.supabase import load_from_supabase
    from cv_rank.display import print_banner, print_summary_table

    # ── Load config ──────────────────────────────────────────────────
    config = load_config(args.config)

    # CLI overrides
    if args.accept is not None:
        config["event"]["target_accepts"] = args.accept
    if args.model:
        config["models"]["scoring"] = args.model
        config["models"]["swiss"] = args.model
        config["models"]["quality_check"] = args.model
    if args.rounds is not None:
        config["swiss"]["rounds"] = args.rounds
    if args.event_name:
        config["event"]["name"] = args.event_name
    if args.swiss_weight is not None:
        config["weights"]["swiss"] = args.swiss_weight
        # round() avoids IEEE 754 noise (1.0 - 0.3 = 0.7000000000000001).
        config["weights"]["pointwise"] = round(1.0 - args.swiss_weight, 10)
    if args.scoring_mode:
        config.setdefault("pointwise", {})["scoring_mode"] = args.scoring_mode

    # Event type: explicit flag > auto-detect from event name > config default
    if getattr(args, "event_type", None):
        config["event"]["type"] = args.event_type
    elif config["event"]["type"] == "community_meetup":
        # Auto-detect hackathon from event name
        name_lower = (args.event_name or args.event or (args.csv and Path(args.csv).stem) or "").lower()
        if "hackathon" in name_lower or "hack" in name_lower:
            config["event"]["type"] = "hackathon"

    # Enrichment: all sources ON by default, --no-X flags to disable.
    # Inject env vars when config values are empty.
    import os
    supa = config["enrichment"]["supabase"]
    if not supa.get("url"):
        supa["url"] = os.environ.get("SUPABASE_URL", "")
    if not supa.get("key"):
        supa["key"] = os.environ.get("SUPABASE_KEY", "")

    gh = config["enrichment"]["github"]
    if not gh.get("token"):
        gh["token"] = os.environ.get("GITHUB_TOKEN", "")

    pdb = config["enrichment"].get("platform_db", {})
    if not pdb.get("dsn"):
        pdb["dsn"] = os.environ.get("PLATFORM_DATABASE_URL", "")

    # Supabase is mandatory for csv-backed runs.
    if args.no_supabase:
        print(
            "  Error: Supabase enrichment is required for this command.\n"
            "         Remove --no-supabase and provide SUPABASE_URL/SUPABASE_KEY in .env.",
            file=sys.stderr,
        )
        return 1
    if args.no_github:
        gh["enabled"] = False
    if getattr(args, "no_platform_db", False):
        pdb["enabled"] = False

    url = (supa.get("url") or "").strip()
    key = (supa.get("key") or "").strip()
    if not url or not key or url.startswith("${") or key.startswith("${"):
        print(
            "  Error: Supabase enrichment is required for this command.\n"
            "         Provide SUPABASE_URL/SUPABASE_KEY in .env.",
            file=sys.stderr,
        )
        return 1

    # Optional enrichers auto-disable when credentials are absent.
    token = (gh.get("token") or "").strip()
    if gh.get("enabled") and (not token or token.startswith("${")):
        gh["enabled"] = False

    dsn = (pdb.get("dsn") or "").strip()
    if pdb.get("enabled") and (not dsn or dsn.startswith("${")):
        pdb["enabled"] = False

    # Set event name — --event flag always overrides config
    if args.event:
        config["event"]["name"] = args.event.replace(" ", "_")
    elif not config["event"]["name"] and args.csv:
        config["event"]["name"] = Path(args.csv).stem

    # Require target_accepts to be explicitly set
    if config["event"]["target_accepts"] <= 0:
        print(
            "  Error: --accept is required. Tell cv-rank how many people to accept.\n"
            "         Example: cv-rank run --csv applicants.csv --accept 50\n"
            "         Or set event.target_accepts in config.yaml",
            file=sys.stderr,
        )
        return 1

    # Validate
    errors = validate_config(config)
    if errors:
        for e in errors:
            print(f"  Config error: {e}", file=sys.stderr)
        return 1

    # ── Output directory / resume ────────────────────────────────────
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.resume:
        run_dir = find_latest_run(output_dir)
        if run_dir is None:
            print("No previous run found to resume.", file=sys.stderr)
            return 1
        completed = get_completed_phases(run_dir)
        print(f"Resuming from {run_dir.name} (completed: {', '.join(completed) or 'none'})",
              file=sys.stderr)
    else:
        run_dir = create_run_dir(output_dir)

    # ── Logging ──────────────────────────────────────────────────────
    logger = setup_logging("cv_rank", run_dir)

    # Adjust console log level based on --verbose / --quiet
    if getattr(args, "verbose", False):
        for h in logger.handlers:
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                h.setLevel(logging.DEBUG)
    elif getattr(args, "quiet", False):
        for h in logger.handlers:
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                h.setLevel(logging.WARNING)

    # Save config to run dir for reproducibility (redact secrets)
    import copy
    safe_config = copy.deepcopy(config)
    for source in safe_config.get("enrichment", {}).values():
        if isinstance(source, dict):
            for k in ("key", "token", "dsn"):
                if k in source and source[k]:
                    source[k] = "***REDACTED***"
    with open(run_dir / "config.json", "w") as f:
        json.dump(safe_config, f, indent=2, default=str)

    # ── Load applicants ──────────────────────────────────────────────
    print_banner(f"cv-rank {__version__} — {config['event']['name']}")

    if args.csv:
        csv_path = Path(args.csv)
        if not csv_path.exists():
            logger.error("CSV file not found: %s", csv_path)
            return 1
        people = load_csv(csv_path)
        source_label = str(csv_path)
    else:
        # --event: try platform DB first, fall back to old Supabase
        people = []
        source_label = f"event: {args.event}"
        platform_dsn = config["enrichment"].get("platform_db", {}).get("dsn") or os.environ.get("PLATFORM_DATABASE_URL", "")
        if platform_dsn and not platform_dsn.startswith("${"):
            from cv_rank.enrichment.platform_db import load_from_platform_db
            logger.info("Loading event %r from platform DB ...", args.event)
            people = load_from_platform_db(args.event, platform_dsn)
            source_label = f"Platform DB event: {args.event}"
        if not people:
            # Fall back to old Supabase
            if supa.get("url") and supa.get("key"):
                people = load_from_supabase(args.event, supa["url"], supa["key"])
                source_label = f"Supabase event: {args.event}"
                config["enrichment"]["supabase"]["enabled"] = True
            elif not platform_dsn:
                logger.error("--event requires PLATFORM_DATABASE_URL or SUPABASE_URL/KEY (set in .env or config)")
                return 1

    if not people:
        logger.error("No valid applicants from %s", source_label)
        return 1

    logger.info("Loaded %d people from %s", len(people), source_label)

    # ── Per-person data quality summary ───────────────────────────
    _n_email = sum(1 for p in people if p.get("email"))
    _n_linkedin = sum(1 for p in people if p.get("linkedin_url"))
    _n_github = sum(1 for p in people if p.get("github_url"))
    _n_company = sum(1 for p in people if p.get("company"))
    _n_role = sum(1 for p in people if p.get("role"))
    logger.info(
        "Data quality: %d/%d have email, %d/%d have LinkedIn, "
        "%d/%d have GitHub, %d/%d have company, %d/%d have role",
        _n_email, len(people), _n_linkedin, len(people),
        _n_github, len(people), _n_company, len(people),
        _n_role, len(people),
    )
    for p in people:
        _flags = []
        if p.get("email"):
            _flags.append("email")
        if p.get("linkedin_url"):
            _flags.append("linkedin")
        if p.get("github_url"):
            _flags.append("github")
        if p.get("company"):
            _flags.append("company")
        if p.get("role"):
            _flags.append("role")
        logger.debug(
            "  %s: %s",
            p.get("name", "?"),
            ", ".join(_flags) if _flags else "(no data)",
        )

    pipeline_t0 = time.time()
    # source_label is set in both --csv and --event branches above.
    # Can't use csv_path here — it only exists in the --csv branch.
    save_meta(run_dir, csv_path=source_label, people_count=len(people))

    # ── Parse criteria ───────────────────────────────────────────────
    from cv_rank.config import parse_criteria
    criteria = parse_criteria(args.criteria, config)
    logger.info("Criteria: %s", criteria)

    accept_count = config["event"]["target_accepts"]
    logger.info("Target accepts: %d / %d", accept_count, len(people))

    # Log model routing
    models = config["models"]
    logger.info(
        "Models: scoring=%s, swiss=%s, borderline=%s, quality=%s",
        models.get("scoring"), models.get("swiss"),
        models.get("borderline", models.get("swiss")),
        models.get("quality_check"),
    )

    # Auto-size Swiss rounds if not explicitly set via CLI
    if args.rounds is None:
        import math
        n = len(people)
        auto_cfg = config.get("swiss", {}).get("auto_rounds", {})
        auto_enabled = bool(auto_cfg.get("enabled", True))
        if auto_enabled:
            base = math.ceil(math.log2(max(n, 2)))
            min_rounds = max(1, int(auto_cfg.get("min_rounds", 5)))
            offset = int(auto_cfg.get("offset", -1))
            max_guard_offset = int(auto_cfg.get("max_guard_offset", 2))
            auto_rounds = max(min_rounds, base + offset)
            max_rounds = max(min_rounds, base + max_guard_offset)
            auto_rounds = min(auto_rounds, max_rounds)
            if config["swiss"]["rounds"] != auto_rounds:
                logger.info(
                    "Auto-sizing Swiss rounds: %d (for %d people, ceil(log2(N))%+d, min=%d, guard_max=%d)",
                    auto_rounds,
                    n,
                    offset,
                    min_rounds,
                    max_rounds,
                )
                config["swiss"]["rounds"] = auto_rounds

    if getattr(args, "dry_run", False):
        from cv_rank.cost import print_dry_run
        print_dry_run(people, config)
        return 0

    # ── Phase 1: Enrichment ──────────────────────────────────────────
    people, should_stop = _run_enrichment(people, config, run_dir, logger, args)
    if should_stop:
        pipeline_elapsed = time.time() - pipeline_t0
        logger.info(
            "Total pipeline elapsed: %.1fs (%.1f min)",
            pipeline_elapsed, pipeline_elapsed / 60,
        )
        return 0

    # ── Phase 1.5: P(show) prediction ────────────────────────────────
    _compute_p_show(people, config, args, logger)

    # ── Anonymization setup ─────────────────────────────────────────
    from cv_rank.profile import format_profile

    # Include extra CSV columns (like RL experience) that aren't mapped to canonical fields
    def format_profile_with_raw(person, _orig=format_profile):
        return _orig(person, include_raw_csv=True)

    anon_format = format_profile_with_raw  # default: no anonymization
    if config.get("anonymize", {}).get("enabled", True):
        from cv_rank.anonymize import Anonymizer

        anon_map_path = run_dir / "anonymizer_map.json"
        if anon_map_path.exists():
            anonymizer = Anonymizer.from_map_file(anon_map_path)
            logger.info("Anonymizer: loaded mapping from checkpoint")
        else:
            anonymizer = Anonymizer(people)
            anonymizer.save(anon_map_path)

        anon_format = anonymizer.wrap_format_fn(format_profile_with_raw)
        logger.info("Anonymization enabled: %d candidates anonymized", len(people))
    else:
        logger.info("Anonymization disabled by config")

    # ── Phase 2: Pointwise Scoring ───────────────────────────────────
    scores = _run_pointwise(people, criteria, config, run_dir, logger,
                            format_profile_fn=anon_format)
    n_scored = sum(1 for s in scores if s.get("score") is not None)
    n_errors = sum(1 for s in scores if s.get("error"))
    logger.info(
        "Phase 2 → 3 handoff: %d people, %d scored, %d errors",
        len(people), n_scored, n_errors,
    )

    # ── Phase 2.5: Length debiasing ──────────────────────────────────
    if config.get("debiasing", {}).get("enabled", True):
        from cv_rank.debiasing import compute_profile_lengths, debias_scores

        strength = config.get("debiasing", {}).get("strength", 0.5)
        logger.info("Phase 2.5: Length debiasing (strength=%.2f)", strength)

        profile_lengths = compute_profile_lengths(people, anon_format)

        # Log pre-debiasing score distribution
        pre_scores = [s.get("score", 0) or 0 for s in scores if s.get("score") is not None]
        if pre_scores:
            logger.info(
                "Pre-debiasing scores: min=%.1f, max=%.1f, mean=%.1f",
                min(pre_scores), max(pre_scores), sum(pre_scores) / len(pre_scores),
            )

        scores = debias_scores(scores, profile_lengths, strength=strength)

        # Log post-debiasing score distribution
        post_scores = [s.get("score", 0) or 0 for s in scores if s.get("score") is not None]
        if post_scores:
            logger.info(
                "Post-debiasing scores: min=%.1f, max=%.1f, mean=%.1f",
                min(post_scores), max(post_scores), sum(post_scores) / len(post_scores),
            )

        # Show biggest adjustments
        adjusted = [(s.get("name", "?"), s.get("score_raw", s.get("score", 0)), s.get("score", 0))
                     for s in scores if "score_raw" in s]
        if adjusted:
            adjusted.sort(key=lambda x: abs(x[1] - x[2]), reverse=True)
            logger.info("Biggest debiasing adjustments:")
            for name, raw, adj in adjusted[:5]:
                logger.info("  %s: %.1f → %.1f (delta=%+.1f)", name, raw, adj, adj - raw)
    else:
        logger.info("Length debiasing disabled by config")

    # ── Phase 3: Swiss Tournament ────────────────────────────────────
    swiss_records, bt_strengths = _run_swiss(
        people, criteria, config, run_dir, logger,
        format_profile_fn=anon_format, pointwise_scores=scores,
    )
    logger.info(
        "Phase 3 → 4 handoff: %d Swiss records, %d BT strengths",
        len(swiss_records), len(bt_strengths),
    )

    # ── Phase 4: Combine Rankings ────────────────────────────────────
    rankings = _run_combine(scores, swiss_records, bt_strengths, config, run_dir, logger)
    logger.info(
        "Phase 4 → 5 handoff: %d final rankings (from %d people)",
        len(rankings), len(people),
    )

    # ── Phase 4.5: Borderline Re-evaluation ──────────────────────────
    from cv_rank.checkpoint import get_completed_phases as _get_phases_bl
    from cv_rank.checkpoint import save_checkpoint as _save_bl

    completed_bl = _get_phases_bl(run_dir)
    if config.get("borderline", {}).get("enabled", True) and "borderline" not in completed_bl:
        from cv_rank.display import print_banner as _print_banner_bl
        from cv_rank.scoring.borderline import run_borderline_reeval

        _print_banner_bl("Phase 4.5: Borderline Re-evaluation")
        bl_records, bl_bt, bl_matches = asyncio.run(run_borderline_reeval(
            people, rankings, criteria, config, run_dir, anon_format,
            pointwise_scores=scores,
            swiss_records=swiss_records,
            bt_strengths=bt_strengths,
        ))
        # Mark borderline as completed BEFORE overwriting combine.json.
        # This prevents double-blending on crash+resume: if we crash after
        # marking done but before saving combine, resume skips borderline
        # and uses the original (un-blended) combine — safe, not corrupted.
        _save_bl(run_dir, "borderline", {"applied": True})
        if bl_records:
            from cv_rank.scoring.combine import recombine_with_borderline
            rankings = recombine_with_borderline(
                scores, swiss_records, bt_strengths,
                bl_records, bl_bt, config,
            )
            _save_bl(run_dir, "combine", rankings)
            logger.info("Borderline re-evaluation updated %d rankings", len(rankings))
        else:
            logger.info("Borderline re-evaluation: no changes (too few candidates or skipped)")
    elif "borderline" in completed_bl:
        logger.info("Borderline: already completed (checkpoint), skipping")
    else:
        logger.info("Borderline re-evaluation disabled by config")

    # ── Track dropped people across phases ────────────────────────────
    _track_drops(people, scores, swiss_records, rankings, logger)
    require_failed_report = bool(config.get("guardrails", {}).get("require_failed_to_rank_report", True))
    failed_path = _write_failed_to_rank_report(
        run_dir, people, scores, swiss_records, rankings, logger, force_write=require_failed_report,
    )
    needs_review_path = _write_needs_review_report(
        run_dir, people, rankings, scores, swiss_records, bt_strengths, accept_count, config, logger,
    )

    if failed_path:
        logger.info("Failed-to-rank report: %s", failed_path)
    if needs_review_path:
        logger.info("Needs-review report: %s", needs_review_path)

    if not _check_exclusion_guardrail(people, rankings, config, logger):
        logger.error("Stopping run due to exclusion guardrail violation")
        return 1

    # ── Phase 5: Quality Check ───────────────────────────────────────
    qc_results = _run_quality(people, rankings, config, run_dir, logger)

    # ── Phase 6: Export ──────────────────────────────────────────────
    output_csv, qc_map = _run_export(
        people, rankings, scores, swiss_records, qc_results,
        config, run_dir, logger,
    )

    # ── Summary ──────────────────────────────────────────────────────
    print_banner("Complete!")

    top_n = min(20, len(rankings))
    rows = []
    for r in rankings[:top_n]:
        name = r["name"]
        qc = qc_map.get(name, {})
        rows.append([
            r.get("rank", "?"),
            name[:25],
            f"{r.get('final_score', 0):.3f}",
            f"{r.get('pointwise_score', 0):.1f}",
            f"{r.get('swiss_wins', 0)}W-{r.get('swiss_losses', 0)}L",
            qc.get("verdict", "?"),
        ])

    print_summary_table(rows, ["Rank", "Name", "Score", "PW", "Swiss", "Verdict"])

    pipeline_elapsed = time.time() - pipeline_t0
    logger.info("Results: %s", output_csv)
    logger.info("Run dir: %s", run_dir)
    logger.info(
        "Total pipeline elapsed: %.1fs (%.1f min)",
        pipeline_elapsed, pipeline_elapsed / 60,
    )

    # Print cost summary
    from cv_rank.cost import print_cost_summary
    print_cost_summary(run_dir, config, logger)

    return 0


# ---------------------------------------------------------------------------
# Subcommand: validate
# ---------------------------------------------------------------------------

def _cmd_validate(args: argparse.Namespace) -> int:
    """Validate a CSV file and optional config."""
    import os

    from cv_rank.config import load_config, validate_config
    from cv_rank.csv_io import validate_csv

    # CSV validation
    is_valid, messages = validate_csv(args.csv)
    for msg in messages:
        print(f"  {msg}", file=sys.stderr)

    # Config validation (if config file exists)
    config = load_config(args.config)

    # Apply the same auto-fill that _cmd_run does so validation matches runtime
    if not config["event"]["name"] and args.csv:
        config["event"]["name"] = Path(args.csv).stem

    supa = config["enrichment"]["supabase"]
    if not supa.get("url"):
        supa["url"] = os.environ.get("SUPABASE_URL", "")
    if not supa.get("key"):
        supa["key"] = os.environ.get("SUPABASE_KEY", "")

    cfg_errors = validate_config(config)
    if cfg_errors:
        for e in cfg_errors:
            print(f"  Config: {e}", file=sys.stderr)

    if is_valid and not cfg_errors:
        print("Validation passed.", file=sys.stderr)
        return 0
    return 1


# ---------------------------------------------------------------------------
# Subcommand: init
# ---------------------------------------------------------------------------

_INIT_CONFIG_TEMPLATE = """\
# cv-rank configuration
# Full docs: https://github.com/cerebralvalley/cv-rank
#
# Config hierarchy: CLI flags > env vars ($OPENAI_MODEL etc.) > this file > built-in defaults
# Environment variables: ${VAR_NAME} is interpolated at load time.

# ── Event ────────────────────────────────────────────────────────────────────
event:
  name: ""                       # Auto-filled from CSV filename if empty
  type: "community_meetup"       # community_meetup | hackathon (auto-detected from name)
  # target_accepts: 50           # REQUIRED — how many people to ACCEPT (or use --accept)

# ── Models ───────────────────────────────────────────────────────────────────
# Per-phase model selection. --model CLI flag overrides scoring/swiss/quality
# but NOT borderline (where cutline accuracy matters most).
models:
  scoring: "gpt-5-mini"          # Pointwise scoring (bulk phase, cost-sensitive)
  swiss: "gpt-5-mini"            # Swiss tournament comparisons
  quality_check: "gpt-5-mini"    # Final verdict generation
  borderline: "gpt-5.2"          # Borderline re-eval (accuracy > cost here)

# ── Swiss Tournament ─────────────────────────────────────────────────────────
swiss:
  rounds: 20                     # Ignored when auto_rounds.enabled=true and --rounds unset
  use_bradley_terry: true        # Requires choix package; falls back to win rate
  inject_pointwise: true         # PRePair: inject pointwise scores into comparison prompts
  bt_pointwise_prior: true       # Use pointwise as BT prior for low-match stability
  bt_prior_strength: 2           # Strength of BT prior regularization
  auto_rounds:
    enabled: true
    offset: -1                   # rounds = ceil(log2(N)) + offset
    min_rounds: 5
    max_guard_offset: 2          # rounds never exceed ceil(log2(N)) + this
  early_stop:
    enabled: true
    min_rounds: 5
    jaccard_accept_set_min: 0.98
    max_boundary_rank_shift: 2
    consecutive_rounds: 2
    boundary_width_pct: 0.10
    boundary_width_min: 20

# ── Weights ──────────────────────────────────────────────────────────────────
weights:
  swiss: 0.50                    # Ignored when auto: true
  pointwise: 0.50                # Ignored when auto: true
  auto: true                     # Entropy-based auto-weighting (recommended)

# ── Pointwise Scoring ───────────────────────────────────────────────────────
pointwise:
  chain_of_thought: false
  require_decimal: true
  temperature: 0
  scoring_mode: "rubric"         # "single" (1-100) or "rubric" (builder-first 5-dimension)

# ── Bias Reduction (all on by default) ───────────────────────────────────────
anonymize:
  enabled: true                  # Replace names with CANDIDATE_001 during scoring

debiasing:
  enabled: true                  # Remove profile-length bias from pointwise scores
  strength: 0.5                  # 0.0 = no change, 1.0 = full correction

borderline:
  enabled: true                  # Extra Swiss rounds near the accept/reject cutline
  band_pct: 0.15                 # Fraction of pool on each side of cutline
  extra_rounds: 5                # Additional Swiss rounds for borderline subset

# ── Enrichment ───────────────────────────────────────────────────────────────
enrichment:
  supabase:
    enabled: false               # Set true + provide URL/KEY for directory DB
    url: "${SUPABASE_URL}"
    key: "${SUPABASE_KEY}"
    batch_size: 50
  github:
    enabled: true                # Warns and auto-disables if GITHUB_TOKEN not set
    token: "${GITHUB_TOKEN}"
    batch_size: 20
  local_csv:
    enabled: false
    paths: []

# ── Criteria Presets (used when --criteria not specified) ────────────────────
criteria:
  community_meetup:
    technical_depth: 0.30
    industry_experience: 0.25
    open_source: 0.20
    network_engagement: 0.15
    publications: 0.10
  hackathon:
    shipping_ability: 0.35
    technical_depth: 0.25
    creativity: 0.20
    past_projects: 0.15
    collaboration: 0.05

# ── Calibration Baselines ───────────────────────────────────────────────────
# Match by name in your CSV. Anchors the LLM's scoring distribution.
baselines: []
  # - score: 85
  #   label: "ELITE"
  #   name: "Jane Doe"
  #   why: "20yr veteran, created major OSS project, 1K+ GitHub stars"
  # - score: 55
  #   label: "AVERAGE"
  #   name: "John Smith"
  #   why: "Fresh grad, solid skills but no shipped products"

# ── Performance ──────────────────────────────────────────────────────────────
concurrency:
  scoring: 50                    # Concurrent API calls per phase (Tier 5: up to 10K RPM)
  swiss: 50                      # Lower to 5-10 if hitting rate limits on lower tiers
  quality_check: 50

save_every: 50                   # Checkpoint every N people
max_retries: 5                   # API retries per request

# ── Guardrails ───────────────────────────────────────────────────────────────
guardrails:
  max_exclusion_rate: 0.02       # Fail run if >2% of candidates are excluded
  require_failed_to_rank_report: true
  needs_review:
    enabled: true
    boundary_width_pct: 0.10
    boundary_width_min: 20
    disagreement_threshold: 0.25
"""

_INIT_ENV_TEMPLATE = """\
# Required
OPENAI_API_KEY=sk-...

# Optional — model selection (default: gpt-5-mini)
# OPENAI_MODEL=gpt-5-mini

# Optional — GitHub API enrichment (auto-disabled if not set)
GITHUB_TOKEN=ghp_...

# Optional — Supabase directory DB enrichment
# SUPABASE_URL=https://your-project.supabase.co
# SUPABASE_KEY=eyJ...
"""


def _cmd_init(args: argparse.Namespace) -> int:
    """Generate config.yaml and .env with sensible defaults."""
    target = Path(args.dir or ".")
    target.mkdir(parents=True, exist_ok=True)

    config_dst = target / "config.yaml"
    env_dst = target / ".env"

    created = []
    if config_dst.exists():
        print(f"  Skipping {config_dst.name} (already exists)", file=sys.stderr)
    else:
        config_dst.write_text(_INIT_CONFIG_TEMPLATE)
        created.append(config_dst.name)
        print(f"  Created {config_dst.name}", file=sys.stderr)

    if env_dst.exists():
        print(f"  Skipping {env_dst.name} (already exists)", file=sys.stderr)
    else:
        env_dst.write_text(_INIT_ENV_TEMPLATE)
        created.append(env_dst.name)
        print(f"  Created {env_dst.name}", file=sys.stderr)

    if created:
        print(f"\nCreated: {', '.join(created)}", file=sys.stderr)
        print("Next steps:", file=sys.stderr)
        print("  1. Add your OPENAI_API_KEY to .env", file=sys.stderr)
        print("  2. (Optional) Add GITHUB_TOKEN for richer profiles", file=sys.stderr)
        print("  3. Run: cv-rank run --csv applicants.csv --accept 50", file=sys.stderr)
    return 0


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _track_drops(people, scores, swiss_records, rankings, logger) -> dict[str, set[str]]:
    """Log people lost between pipeline phases and return drop sets."""
    # Indexed fallback ("unnamed_3") so multiple nameless people don't
    # collapse to a single "?" in the set.
    all_names = {p.get("name", f"unnamed_{i}") for i, p in enumerate(people)}
    # Error results have score=None — only count successful scores.
    scored_names = {s["name"] for s in scores if s.get("score") is not None}
    swiss_names = set(swiss_records.keys()) if isinstance(swiss_records, dict) else set()
    ranked_names = {r["name"] for r in rankings}

    dropped_pw = all_names - scored_names
    dropped_swiss = all_names - swiss_names
    dropped_final = all_names - ranked_names
    summary = {
        "all_names": all_names,
        "scored_names": scored_names,
        "swiss_names": swiss_names,
        "ranked_names": ranked_names,
        "dropped_pointwise": dropped_pw,
        "dropped_swiss": dropped_swiss,
        "dropped_final": dropped_final,
    }

    if dropped_pw or dropped_swiss or dropped_final:
        logger.warning("--- Dropped People Report ---")
        logger.warning("  Loaded: %d | Pointwise: %d | Swiss: %d | Final: %d",
                        len(all_names), len(scored_names), len(swiss_names), len(ranked_names))
        if dropped_pw:
            logger.warning("  Dropped after pointwise (%d): %s",
                            len(dropped_pw), ", ".join(sorted(dropped_pw)[:10]))
        if dropped_swiss:
            logger.warning("  Dropped after Swiss (%d): %s",
                            len(dropped_swiss), ", ".join(sorted(dropped_swiss)[:10]))
        if dropped_final:
            logger.warning("  Dropped in final rankings (%d): %s",
                            len(dropped_final), ", ".join(sorted(dropped_final)[:10]))
    else:
        logger.info("All %d people survived all phases (no drops)", len(all_names))
    return summary


def _write_csv(path: Path, headers: list[str], rows: list[dict], logger, *, force_write: bool = False) -> Path | None:
    """Write rows to CSV when non-empty, otherwise skip file creation."""
    if not rows and not force_write:
        return None
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=headers)
        writer.writeheader()
        writer.writerows(rows)
    logger.info("Wrote %d rows to %s", len(rows), path)
    return path


def _write_failed_to_rank_report(
    run_dir: Path,
    people: list[dict],
    scores: list[dict],
    swiss_records: dict[str, dict],
    rankings: list[dict],
    logger,
    *,
    force_write: bool = False,
) -> Path | None:
    """Write FAILED_TO_RANK.csv with explicit reasons."""
    name_counts: Counter[str] = Counter(
        p.get("name", f"unnamed_{i}") for i, p in enumerate(people)
    )
    scores_by_name: dict[str, list[dict]] = defaultdict(list)
    for s in scores:
        scores_by_name[s.get("name", "?")].append(s)

    score_cursor: Counter[str] = Counter()
    ranked_remaining: Counter[str] = Counter(r.get("name", "?") for r in rankings)
    swiss_remaining: Counter[str] = Counter(swiss_records.keys())

    rows: list[dict[str, str]] = []
    for i, person in enumerate(people):
        name = person.get("name", f"unnamed_{i}")

        # Consume ranked candidates first so duplicate names do not collapse.
        if ranked_remaining[name] > 0:
            ranked_remaining[name] -= 1
            if swiss_remaining[name] > 0:
                swiss_remaining[name] -= 1
            if score_cursor[name] < len(scores_by_name[name]):
                score_cursor[name] += 1
            continue

        score_list = scores_by_name[name]
        idx = score_cursor[name]
        score = score_list[idx] if idx < len(score_list) else {}
        if idx < len(score_list):
            score_cursor[name] += 1

        if score.get("error"):
            reason = "pointwise_error"
            detail = str(score.get("error", ""))[:200]
        elif swiss_remaining[name] <= 0:
            reason = "missing_swiss_record"
            detail = "No Swiss record found for candidate"
        else:
            reason = "combine_exclusion"
            detail = "Candidate was removed during signal intersection"
            swiss_remaining[name] -= 1

        is_ambiguous_name = name_counts[name] > 1
        if is_ambiguous_name:
            detail = f"{detail} [ambiguous duplicate name]"

        rows.append(
            {
                "candidate_id": "" if is_ambiguous_name else person.get("candidate_id", ""),
                "name": name,
                "email": "" if is_ambiguous_name else person.get("email", ""),
                "reason": reason,
                "detail": detail,
            }
        )

    return _write_csv(
        run_dir / "FAILED_TO_RANK.csv",
        ["candidate_id", "name", "email", "reason", "detail"],
        rows,
        logger,
        force_write=force_write,
    )


def _write_needs_review_report(
    run_dir: Path,
    people: list[dict],
    rankings: list[dict],
    scores: list[dict],
    swiss_records: dict[str, dict],
    bt_strengths: dict[str, float],
    accept_count: int,
    config: dict,
    logger,
) -> Path | None:
    """Write NEEDS_REVIEW.csv for boundary and high-disagreement candidates."""
    needs_cfg = config.get("guardrails", {}).get("needs_review", {})
    if not bool(needs_cfg.get("enabled", True)):
        return None

    people_by_name: dict[str, list[dict]] = defaultdict(list)
    for i, person in enumerate(people):
        people_by_name[person.get("name", f"unnamed_{i}")].append(person)

    scores_by_name: dict[str, list[dict]] = defaultdict(list)
    for s in scores:
        scores_by_name[s.get("name", "?")].append(s)

    seen_name_occurrence: Counter[str] = Counter()
    rank_rows: list[dict[str, object]] = []
    rank_rows_by_name: dict[str, list[dict[str, object]]] = defaultdict(list)
    for entry in rankings:
        name = entry.get("name", "?")
        occ = seen_name_occurrence[name]
        seen_name_occurrence[name] += 1

        person_list = people_by_name.get(name, [])
        is_ambiguous_name = len(person_list) > 1
        person = person_list[occ] if occ < len(person_list) and not is_ambiguous_name else {}
        score_list = scores_by_name.get(name, [])
        score = score_list[occ] if occ < len(score_list) and not is_ambiguous_name else {}
        candidate_id = str(person.get("candidate_id") or f"{name}::{occ}")
        if is_ambiguous_name:
            candidate_id = f"ambiguous::{name}::{occ}"

        row = {
            "key": candidate_id,
            "name": name,
            "person": person,
            "score": score,
            "ranking": entry,
            "ambiguous_name": is_ambiguous_name,
        }
        rank_rows.append(row)
        rank_rows_by_name[name].append(row)

    n = len(rankings)
    boundary_width = max(
        int(needs_cfg.get("boundary_width_min", 20)),
        int(n * float(needs_cfg.get("boundary_width_pct", 0.10))),
    )
    low = max(1, accept_count - boundary_width)
    high = min(n, accept_count + boundary_width)

    review_reasons: dict[str, set[str]] = {}
    for row in rank_rows:
        entry = row["ranking"]
        rank = int(entry.get("rank", 0))
        if low <= rank <= high:
            review_reasons.setdefault(str(row["key"]), set()).add("boundary_band")

    from cv_rank.metrics import signal_disagreement

    disagreement_threshold = float(needs_cfg.get("disagreement_threshold", 0.25))
    disagreements = signal_disagreement(
        scores,
        swiss_records,
        bt_strengths,
        threshold=disagreement_threshold,
    )
    for item in disagreements:
        for row in rank_rows_by_name.get(item["name"], []):
            review_reasons.setdefault(str(row["key"]), set()).add(
                f"signal_disagreement:{item['direction']}"
            )

    rows: list[dict[str, str]] = []
    for row in sorted(rank_rows, key=lambda x: int(x["ranking"].get("rank", 10**9))):
        key = str(row["key"])
        if key not in review_reasons:
            continue
        person = row["person"]
        ranking = row["ranking"]
        score = row["score"]
        name = str(row["name"])
        rows.append(
            {
                "candidate_id": "" if row["ambiguous_name"] else str(person.get("candidate_id", "")),
                "name": name,
                "email": "" if row["ambiguous_name"] else str(person.get("email", "")),
                "rank": str(ranking.get("rank", "")),
                "final_score": f"{ranking.get('final_score', '')}",
                "pointwise_score": "" if row["ambiguous_name"] else f"{score.get('score', '')}",
                "swiss_record": f"{ranking.get('swiss_wins', 0)}W-{ranking.get('swiss_losses', 0)}L",
                "reasons": ";".join(sorted(review_reasons[key])),
            }
        )

    return _write_csv(
        run_dir / "NEEDS_REVIEW.csv",
        ["candidate_id", "name", "email", "rank", "final_score", "pointwise_score", "swiss_record", "reasons"],
        rows,
        logger,
    )


def _check_exclusion_guardrail(people, rankings, config, logger) -> bool:
    """Return False when exclusion rate exceeds configured guardrail."""
    max_exclusion_rate = float(config.get("guardrails", {}).get("max_exclusion_rate", 0.02))
    total = len(people)
    ranked = len(rankings)
    excluded = max(0, total - ranked)
    exclusion_rate = excluded / total if total else 0.0
    logger.info(
        "Exclusion guardrail: excluded=%d/%d (%.2f%%), max=%.2f%%",
        excluded,
        total,
        100 * exclusion_rate,
        100 * max_exclusion_rate,
    )
    return exclusion_rate <= max_exclusion_rate



# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="cv-rank",
        description="Rank event applicants using LLM scoring + Swiss tournaments.",
        epilog=(
            "Quick start:\n"
            "  cv-rank run --csv applicants.csv --accept 50\n"
            "  cv-rank run --csv applicants.csv --accept 50 --model gpt-5-mini --dry-run\n"
            "  cv-rank validate --csv applicants.csv\n"
            "  cv-rank init\n"
            "\n"
            "Set OPENAI_API_KEY in .env or environment. Run 'cv-rank init' to generate config templates."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--version", action="version", version=f"cv-rank {__version__}",
    )

    subs = parser.add_subparsers(dest="command")

    # ── run ───────────────────────────────────────────────────────────
    run_p = subs.add_parser(
        "run", help="Run the full ranking pipeline",
        epilog=(
            "Examples:\n"
            "  cv-rank run --csv applicants.csv --accept 50\n"
            "  cv-rank run --csv applicants.csv --accept 50 --model gpt-5-mini\n"
            "  cv-rank run --csv applicants.csv --accept 50 --scoring-mode rubric\n"
            "  cv-rank run --csv applicants.csv --accept 50 --dry-run\n"
            "  cv-rank run --csv applicants.csv --accept 50 --resume\n"
            "\n"
            "Pipeline: Enrichment → Pointwise → Debias → Swiss → Combine → Borderline → Quality → Export\n"
            "Output: results/run_YYYYMMDD_HHMMSS/RANKED.csv (45 columns)"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    run_p.add_argument("--csv", help="Path to applicant CSV")
    run_p.add_argument("--event", help="Load applicants from Supabase by event name (requires SUPABASE_URL/KEY)")
    run_p.add_argument("--criteria", help="Evaluation criteria (e.g. 'AI builders' or 'technical_depth:0.3,creativity:0.2')")
    run_p.add_argument("--accept", type=int, help="How many people to accept (REQUIRED unless set in config)")
    run_p.add_argument("--model", help="LLM model for scoring, swiss, quality (borderline stays on gpt-5.2 for accuracy)")
    run_p.add_argument("--rounds", type=int, help="Swiss tournament rounds")
    run_p.add_argument("--event-name", help="Event name for output columns")
    run_p.add_argument("--event-type", choices=["hackathon", "community_meetup"],
                       help="Event type for criteria presets (auto-detected from event name if omitted)")
    run_p.add_argument("--swiss-weight", type=float, help="Swiss weight (0.0-1.0)")
    run_p.add_argument(
        "--scoring-mode",
        choices=["single", "rubric"],
        help="Pointwise scoring mode: 'single' (1-100) or 'rubric' (5-dim, 1-5 each)",
    )
    run_p.add_argument("--config", help="Path to config.yaml")
    run_p.add_argument("--output-dir", default="results", help="Output directory (default: results)")
    run_p.add_argument("--resume", action="store_true", help="Resume from latest run")
    run_p.add_argument("--enrich-only", action="store_true", help="Stop after enrichment (no scoring)")
    run_p.add_argument("--no-supabase", action="store_true", help="Disable Supabase enrichment (on by default)")
    run_p.add_argument("--no-github", action="store_true", help="Disable GitHub API enrichment (on by default)")
    run_p.add_argument("--no-platform-db", action="store_true", help="Disable platform DB enrichment (on by default)")
    run_p.add_argument("-v", "--verbose", action="store_true", help="DEBUG-level logging to console")
    run_p.add_argument("-q", "--quiet", action="store_true", help="Suppress console output except errors")
    run_p.add_argument("--dry-run", action="store_true", help="Show estimated cost and exit")
    run_p.add_argument("--event-date", help="Event date YYYY-MM-DD (for P_Show timing; auto-detected from DB with --event)")
    run_p.add_argument("--city", help="Event city (for P_Show timezone distance; auto-detected from DB with --event)")

    # ── validate ──────────────────────────────────────────────────────
    val_p = subs.add_parser("validate", help="Validate CSV and config")
    val_p.add_argument("--csv", required=True, help="Path to applicant CSV")
    val_p.add_argument("--config", help="Path to config.yaml")

    # ── init ──────────────────────────────────────────────────────────
    init_p = subs.add_parser("init", help="Create config.yaml and .env from templates")
    init_p.add_argument("--dir", help="Target directory (default: current)")

    # ── incremental ──────────────────────────────────────────────
    inc_p = subs.add_parser(
        "incremental",
        help="Rank new candidates incrementally against a previous run",
        epilog=(
            "Examples:\n"
            "  cv-rank incremental --prev-run results/run_20260226_094441 \\\n"
            "    --csv new_applicants.csv --accept 200\n"
            "\n"
            "Reuses enrichment, pointwise scores, and Swiss results from the\n"
            "previous run.  Only new candidates are scored and matched.\n"
            "\n"
            "Algorithm (Research-Informed, v3):\n"
            "  Phase A: Mini Swiss among new candidates only (4 rounds)\n"
            "  Phase B: BT-informed borderline detection + adaptive match budget\n"
            "  Phase C: BT refit with pointwise prior on all comparisons\n"
            "\n"
            "v3 improvements: BT-based borderline detection, adaptive match\n"
            "budget (2-7 per candidate), info-maximizing opponent selection,\n"
            "pointwise-prior virtual matches in BT."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    inc_p.add_argument("--prev-run", required=True,
                       help="Path to previous run directory (e.g. results/run_20260226_094441)")
    inc_p.add_argument("--csv", required=True, help="Path to updated applicant CSV (includes old + new)")
    inc_p.add_argument("--accept", type=int, help="Target accepts (REQUIRED)")
    inc_p.add_argument("--model", help="LLM model override")
    inc_p.add_argument("--criteria", help="Evaluation criteria")
    inc_p.add_argument("--event-type", choices=["hackathon", "community_meetup"])
    inc_p.add_argument("--config", help="Path to config.yaml")
    inc_p.add_argument("--output-dir", default="results", help="Output directory")
    inc_p.add_argument("--no-supabase", action="store_true", help="Disable Supabase enrichment (on by default)")
    inc_p.add_argument("--no-github", action="store_true", help="Disable GitHub API enrichment (on by default)")
    inc_p.add_argument("--no-platform-db", action="store_true", help="Disable platform DB enrichment (on by default)")
    inc_p.add_argument("-v", "--verbose", action="store_true")
    inc_p.add_argument("-q", "--quiet", action="store_true")

    # ── clean ─────────────────────────────────────────────────────
    clean_p = subs.add_parser(
        "clean",
        help="Generate a clean CSV from a full RANKED.csv (fewer columns, normalized GitHub URLs)",
        epilog=(
            "Examples:\n"
            "  cv-rank clean results/run_20260226_094441/RANKED.csv\n"
            "  cv-rank clean RANKED.csv -o clean_output.csv\n"
            "\n"
            "Strips the 45-column RANKED.csv down to the 15 most useful columns\n"
            "and normalizes all GitHub URLs to https://github.com/username format."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    clean_p.add_argument("csv", help="Path to RANKED.csv")
    clean_p.add_argument("-o", "--output", help="Output path (default: RANKED_CLEAN.csv next to input)")

    # ── apply ─────────────────────────────────────────────────────
    apply_p = subs.add_parser(
        "apply",
        help="Stamp cv-rank decisions back onto the original applicant CSV",
        epilog=(
            "Examples:\n"
            "  cv-rank apply results/run_*/RANKED.csv --original applicants.csv\n"
            "  cv-rank apply RANKED.csv --original sheet.csv --into 'CV Recommended'\n"
            "  cv-rank apply RANKED.csv --original sheet.csv --columns Rank,Verdict,Combined_Score\n"
            "\n"
            "Joins on Email (case-insensitive), falls back to Name.\n"
            "Auto-detects the *_Status column from RANKED.csv."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    apply_p.add_argument("ranked_csv", help="Path to RANKED.csv or RANKED_CLEAN.csv")
    apply_p.add_argument("--original", required=True, help="Path to original applicant CSV")
    apply_p.add_argument("-o", "--output", help="Output path (default: {original}_applied.csv)")
    apply_p.add_argument("--columns", help="Comma-separated RANKED.csv columns to copy (default: Rank,Verdict,Status)")
    apply_p.add_argument("--into", help="Write Status into this existing column instead of appending")

    # ── events ────────────────────────────────────────────────────
    events_p = subs.add_parser(
        "events",
        help="List recent events from the platform DB",
        epilog=(
            "Examples:\n"
            "  cv-rank events\n"
            "  cv-rank events --upcoming\n"
            "  cv-rank events --limit 30\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    events_p.add_argument("--upcoming", action="store_true", help="Only show future events")
    events_p.add_argument("--limit", type=int, default=20, help="Max events to show (default: 20)")

    sponsor_analyze_p = subs.add_parser(
        "sponsor-analyze",
        help="Build an analyst-facing sponsor analysis bundle for one event",
        epilog=(
            "Examples:\n"
            "  cv-rank sponsor-analyze --event \"Gemini 3 NYC Hackathon\" --run-id run_20260306_185829\n"
            "  cv-rank sponsor-analyze --run-id run_20260307_023434\n"
            "\n"
            "Outputs:\n"
            "  - analysis_claim_review.csv\n"
            "  - analysis_segment_funnel.csv\n"
            "  - analysis_tool_summary.csv\n"
            "  - analysis_top_projects.csv\n"
            "  - analysis_big_numbers.csv\n"
            "  - ANALYSIS_SUMMARY.md\n"
            "  - ANALYSIS_TASKS.md\n"
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    sponsor_analyze_p.add_argument("--event", help="Exact platform event title")
    sponsor_analyze_p.add_argument("--run-id", help="Run folder to use as the enriched-profile source")
    sponsor_analyze_p.add_argument("--enriched-json", help="Explicit enriched JSON path (overrides run resolution)")
    sponsor_analyze_p.add_argument(
        "--reviewer-mode",
        choices=["strict", "off"],
        default="strict",
        help="Claim reviewer mode for include/exclude triage",
    )
    sponsor_analyze_p.add_argument(
        "--output-root",
        default="outputs",
        help="Output root for analysis helper bundle (default: outputs)",
    )

    waves_train_p = subs.add_parser(
        "waves-train",
        help="Train and evaluate the Waves v2 attendance predictor",
    )
    waves_train_p.add_argument("--output-dir", default="results/waves_v2/latest")
    waves_train_p.add_argument("--calibration-events", type=int, default=12)
    waves_train_p.add_argument("--test-events", type=int, default=10)
    waves_train_p.add_argument("--horizons", default="14,7,3,1")
    waves_train_p.add_argument(
        "--stage0-mode",
        choices=["direct", "hybrid"],
        default="direct",
        help="Signup forecasting strategy (default: direct).",
    )

    waves_predict_p = subs.add_parser(
        "waves-predict",
        help="Predict upcoming attendance with a trained Waves v2 artifact",
    )
    waves_predict_p.add_argument("--artifact-dir", default="results/waves_v2/latest")
    waves_predict_p.add_argument("--days-ahead", type=int, default=14)
    waves_predict_p.add_argument("--output-csv", default="results/upcoming_predictions_v2.csv")
    waves_predict_p.add_argument("--include-non-hackathons", action="store_true")

    dossiers_p = subs.add_parser(
        "dossiers",
        help="Enqueue attendees and launch per-attendee full 3-pass MiniMax dossier workers on Daytona.",
        epilog=(
            "Examples:\n"
            "  cv-rank dossiers --queue-name sf-minimax-exa --limit 25\n"
            "  cv-rank dossiers --queue-name sf-minimax-exa --people-file people.json --sandbox-count 5 --workers-per-sandbox 5\n"
            "  cv-rank dossiers --queue-name sf-minimax-exa --enqueue-only --people-file top_2000_deduped.json\n"
            "\n"
            "This command uses the queue-worker path, so each worker claims one attendee,\n"
            "runs pass1 -> pass2 -> braindump, writes final outputs, then claims the next attendee."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    dossiers_p.add_argument("--queue-name", default="sf-minimax-exa")
    dossiers_p.add_argument("--people-file", help="Optional JSON people file. If omitted, pulls ranked attendees from the platform DB.")
    dossiers_p.add_argument("--limit", type=int, default=0, help="How many attendees to enqueue. 0 means use the queue default or all rows with --all.")
    dossiers_p.add_argument("--offset", type=int, default=0)
    dossiers_p.add_argument("--all", action="store_true", help="Enqueue all rows from the ranked source.")
    dossiers_p.add_argument("--batch-size", type=int, default=250)
    dossiers_p.add_argument("--enqueue-only", action="store_true")
    dossiers_p.add_argument("--launch-only", action="store_true")
    dossiers_p.add_argument("--model", default="MiniMax-M2.7")
    dossiers_p.add_argument("--minimax-env-file", default=str(Path.home() / ".claude-wafer" / "minimax.env"))
    dossiers_p.add_argument("--daytona-env-file", default=str(DEFAULT_DAYTONA_ENV))
    dossiers_p.add_argument("--sandbox-prefix", default="cv-rank-minimax")
    dossiers_p.add_argument("--sandbox-start-index", type=int, default=1)
    dossiers_p.add_argument("--sandbox-count", type=int, default=5)
    dossiers_p.add_argument("--workers-per-sandbox", type=int, default=5)
    dossiers_p.add_argument("--web-mode", choices=("exa", "mixed", "claude"), default="exa")
    dossiers_p.add_argument("--timeout-seconds", type=int, default=1800)
    dossiers_p.add_argument("--cpu", type=int, default=4)
    dossiers_p.add_argument("--memory", type=int, default=8)
    dossiers_p.add_argument("--disk", type=int, default=10)
    dossiers_p.add_argument("--log-dir", default="/tmp/daytona-minimax-queue-workers")
    dossiers_p.add_argument("--dry-run", action="store_true")

    # ── reaccept ─────────────────────────────────────────────────
    reaccept_p = subs.add_parser(
        "reaccept",
        help="Move the accept cutline on an existing run (re-scores borderline)",
        epilog=(
            "Examples:\n"
            "  cv-rank reaccept 250 --prev-run results/run_20260302_164838\n"
            "  cv-rank reaccept 250 --prev-run results/run_20260302_164838 --quick\n"
            "  cv-rank reaccept 250 --prev-run results/run_20260302_164838 --no-rescore\n"
            "  cv-rank reaccept 250 --prev-run results/run_20260302_164838 --dry-run\n"
            "\n"
            "3-stage algorithm:\n"
            "  Stage 1: Triage — identify borderline zone around old/new cutline\n"
            "  Stage 2: Re-score QC on borderline with updated framing (--quick skips stage 3)\n"
            "  Stage 3: Pairwise Swiss resolution for ~30 candidates nearest new cutline\n"
            "  Stage 4: Re-export RANKED.csv with new cutline\n"
            "\n"
            "Use --no-rescore for instant re-slice (free, no API calls).\n"
            "Use --quick for QC-only refresh (~$1-2).\n"
            "Default runs all stages (~$3)."
        ),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    reaccept_p.add_argument("new_accept", type=int, help="New target accepts (e.g. 250)")
    reaccept_p.add_argument("--prev-run", required=True,
                            help="Path to previous run directory (e.g. results/run_20260302_164838)")
    reaccept_p.add_argument("--quick", action="store_true",
                            help="Skip pairwise resolution (stage 3), only re-score QC")
    reaccept_p.add_argument("--no-rescore", action="store_true",
                            help="Skip all re-scoring, just re-slice at new cutline (free)")
    reaccept_p.add_argument("--dry-run", action="store_true",
                            help="Show who would be promoted and estimated cost, then exit")
    reaccept_p.add_argument("--email-list", action="store_true",
                            help="Print emails of newly promoted candidates after run")
    reaccept_p.add_argument("--model", help="LLM model override for QC/Swiss")
    reaccept_p.add_argument("--config", help="Path to config.yaml override")
    reaccept_p.add_argument("--output-dir", default="results", help="Output directory (default: results)")
    reaccept_p.add_argument("-v", "--verbose", action="store_true", help="DEBUG-level console logging")

    return parser


# ---------------------------------------------------------------------------
# Subcommand: incremental
# ---------------------------------------------------------------------------

def _cmd_incremental(args: argparse.Namespace) -> int:
    """Run incremental ranking: score only new candidates, targeted Swiss."""
    from cv_rank.checkpoint import (
        create_run_dir,
        load_checkpoint,
        save_checkpoint,
        save_meta,
    )
    from cv_rank.config import load_config, parse_criteria, setup_logging, validate_config
    from cv_rank.csv_io import export_clean_csv, export_ranked_csv, load_csv
    from cv_rank.display import print_banner, print_summary_table
    from cv_rank.profile import format_profile

    # ── Validate args ────────────────────────────────────────────────
    prev_run = Path(args.prev_run)
    if not prev_run.exists():
        print(f"Error: previous run not found: {prev_run}", file=sys.stderr)
        return 1

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"Error: CSV not found: {csv_path}", file=sys.stderr)
        return 1

    # ── Load config ──────────────────────────────────────────────────
    config = load_config(args.config)

    if args.accept is not None:
        config["event"]["target_accepts"] = args.accept
    if args.model:
        config["models"]["scoring"] = args.model
        config["models"]["swiss"] = args.model
        config["models"]["quality_check"] = args.model

    # Event type auto-detect
    if getattr(args, "event_type", None):
        config["event"]["type"] = args.event_type
    else:
        name_lower = (csv_path.stem or "").lower()
        if "hackathon" in name_lower or "hack" in name_lower:
            config["event"]["type"] = "hackathon"

    if not config["event"]["name"]:
        config["event"]["name"] = csv_path.stem

    # Enrichment: all sources ON by default, --no-X to disable.
    import os
    supa = config["enrichment"]["supabase"]
    if not supa.get("url"):
        supa["url"] = os.environ.get("SUPABASE_URL", "")
    if not supa.get("key"):
        supa["key"] = os.environ.get("SUPABASE_KEY", "")

    gh = config["enrichment"]["github"]
    if not gh.get("token"):
        gh["token"] = os.environ.get("GITHUB_TOKEN", "")

    pdb = config["enrichment"].get("platform_db", {})
    if not pdb.get("dsn"):
        pdb["dsn"] = os.environ.get("PLATFORM_DATABASE_URL", "")

    if getattr(args, "no_supabase", False):
        print(
            "  Error: Supabase enrichment is required for this command.\n"
            "         Remove --no-supabase and provide SUPABASE_URL/SUPABASE_KEY in .env.",
            file=sys.stderr,
        )
        return 1
    if getattr(args, "no_github", False):
        gh["enabled"] = False
    if getattr(args, "no_platform_db", False):
        pdb["enabled"] = False

    url = (supa.get("url") or "").strip()
    key = (supa.get("key") or "").strip()
    if not url or not key or url.startswith("${") or key.startswith("${"):
        print(
            "  Error: Supabase enrichment is required for this command.\n"
            "         Provide SUPABASE_URL/SUPABASE_KEY in .env.",
            file=sys.stderr,
        )
        return 1

    token = (gh.get("token") or "").strip()
    if gh.get("enabled") and (not token or token.startswith("${")):
        gh["enabled"] = False

    dsn = (pdb.get("dsn") or "").strip()
    if pdb.get("enabled") and (not dsn or dsn.startswith("${")):
        pdb["enabled"] = False

    if config["event"]["target_accepts"] <= 0:
        print("Error: --accept is required.", file=sys.stderr)
        return 1

    errors = validate_config(config)
    if errors:
        for e in errors:
            print(f"  Config error: {e}", file=sys.stderr)
        return 1

    # ── Create new run dir ───────────────────────────────────────────
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    run_dir = create_run_dir(output_dir)

    logger = setup_logging("cv_rank", run_dir)

    if getattr(args, "verbose", False):
        for h in logger.handlers:
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                h.setLevel(logging.DEBUG)

    # Save config
    import copy
    safe_config = copy.deepcopy(config)
    for source in safe_config.get("enrichment", {}).values():
        if isinstance(source, dict):
            for k in ("key", "token", "dsn"):
                if k in source and source[k]:
                    source[k] = "***REDACTED***"
    with open(run_dir / "config.json", "w") as f:
        json.dump(safe_config, f, indent=2, default=str)

    # ── Load previous run data ───────────────────────────────────────
    print_banner("Loading previous run")
    logger.info("Previous run: %s", prev_run)

    prev_enrichment = load_checkpoint(prev_run, "enrichment") or []
    prev_pointwise = load_checkpoint(prev_run, "pointwise") or []
    prev_swiss_data = load_checkpoint(prev_run, "swiss") or {}
    prev_combine = load_checkpoint(prev_run, "combine") or []
    prev_quality = load_checkpoint(prev_run, "quality") or []

    prev_swiss_records = prev_swiss_data.get("records", {})
    # Load Swiss match history
    swiss_checkpoint_path = prev_run / "swiss_checkpoint.json"
    prev_matches: list[dict] = []
    if swiss_checkpoint_path.exists():
        with open(swiss_checkpoint_path) as f:
            sc = json.load(f)
        prev_matches = sc.get("matches", [])

    logger.info(
        "Previous run: %d enriched, %d scored, %d Swiss records, "
        "%d matches, %d rankings",
        len(prev_enrichment), len(prev_pointwise),
        len(prev_swiss_records), len(prev_matches), len(prev_combine),
    )

    # ── Load full CSV and diff ───────────────────────────────────────
    print_banner("Diffing applicants")

    all_people = load_csv(csv_path)
    if not all_people:
        logger.error("No valid applicants in %s", csv_path)
        return 1

    # Build email→person map for prev
    prev_emails: dict[str, dict] = {}
    for p in prev_enrichment:
        email = (p.get("email") or "").strip().lower()
        if email:
            prev_emails[email] = p

    # Split into existing vs new
    existing_people = []
    new_people = []
    for p in all_people:
        email = (p.get("email") or "").strip().lower()
        if email in prev_emails:
            # Use enriched version from previous run
            existing_people.append(prev_emails[email])
        else:
            new_people.append(p)

    logger.info(
        "Applicant diff: %d total, %d existing (reuse), %d new",
        len(all_people), len(existing_people), len(new_people),
    )
    save_meta(run_dir, csv_path=str(csv_path), people_count=len(all_people),
              existing_count=len(existing_people), new_count=len(new_people),
              prev_run=str(prev_run), mode="incremental")

    if not new_people:
        logger.info("No new candidates — nothing to do!")
        print("No new candidates found. Exiting.", file=sys.stderr)
        return 0

    pipeline_t0 = time.time()
    criteria = parse_criteria(args.criteria, config)
    accept_count = config["event"]["target_accepts"]
    logger.info("Criteria: %s", criteria)
    logger.info("Target accepts: %d / %d", accept_count, len(all_people))

    # ── Phase 1: Enrich new people only ──────────────────────────────
    print_banner("Phase 1: Enrich new candidates")
    t0 = time.time()

    if config["enrichment"]["supabase"]["enabled"]:
        from cv_rank.enrichment.supabase import enrich_from_supabase
        new_people = enrich_from_supabase(new_people, config)

    if config["enrichment"].get("exa", {}).get("enabled"):
        from cv_rank.enrichment.exa_enricher import enrich_from_exa
        new_people = enrich_from_exa(new_people, config)

    if config["enrichment"]["github"]["enabled"]:
        from cv_rank.enrichment.github_api import enrich_from_github_api
        new_people = enrich_from_github_api(new_people, config)

    if config["enrichment"].get("platform_db", {}).get("enabled"):
        from cv_rank.enrichment.platform_db import enrich_from_platform_db
        new_people = enrich_from_platform_db(new_people, config)

    logger.info("Enrichment (new only) done in %.0fs", time.time() - t0)

    # Combine all enriched people
    all_enriched = existing_people + new_people
    save_checkpoint(run_dir, "enrichment", all_enriched)

    # ── Anonymization ────────────────────────────────────────────────
    def format_profile_with_raw(person, _orig=format_profile):
        return _orig(person, include_raw_csv=True)

    anon_format = format_profile_with_raw
    if config.get("anonymize", {}).get("enabled", True):
        from cv_rank.anonymize import Anonymizer
        anonymizer = Anonymizer(all_enriched)
        anonymizer.save(run_dir / "anonymizer_map.json")
        anon_format = anonymizer.wrap_format_fn(format_profile_with_raw)
        logger.info("Anonymization enabled")

    # ── Phase 2: Pointwise score new people only ─────────────────────
    print_banner("Phase 2: Score new candidates")
    t_pw = time.time()

    from cv_rank.scoring.pointwise import score_all
    new_scores = asyncio.run(score_all(
        new_people, criteria, config["models"]["scoring"],
        accept_count, config, run_dir, anon_format,
    ))

    logger.info(
        "Pointwise (new only): %d scored in %.0fs",
        len(new_scores), time.time() - t_pw,
    )

    # Combine all scores
    all_scores = list(prev_pointwise) + new_scores
    save_checkpoint(run_dir, "pointwise", all_scores)

    # ── Phase 2.5: Debias new scores ─────────────────────────────────
    if config.get("debiasing", {}).get("enabled", True):
        from cv_rank.debiasing import compute_profile_lengths, debias_scores

        strength = config.get("debiasing", {}).get("strength", 0.5)
        # Only debias new scores (existing ones are already debiased)
        profile_lengths = compute_profile_lengths(new_people, anon_format)
        new_scores = debias_scores(new_scores, profile_lengths, strength=strength)
        logger.info("Debiased %d new scores", len(new_scores))

        # Rebuild combined scores with debiased new scores
        all_scores = list(prev_pointwise) + new_scores
        save_checkpoint(run_dir, "pointwise", all_scores)

    # ── Phase 3: Targeted Swiss integration ──────────────────────────
    print_banner("Phase 3: Hybrid Swiss-Merge (v3)")

    t_sw = time.time()
    from cv_rank.scoring.incremental import run_incremental_swiss

    swiss_records, bt_strengths, all_matches = asyncio.run(
        run_incremental_swiss(
            new_people=new_people,
            existing_people=existing_people,
            existing_ranking=prev_combine,
            existing_matches=prev_matches,
            criteria=criteria,
            model=config["models"]["swiss"],
            config=config,
            run_dir=run_dir,
            format_profile_fn=anon_format,
            new_pointwise_scores=new_scores,
            existing_pointwise_scores=prev_pointwise,
        )
    )

    save_checkpoint(run_dir, "swiss", {
        "records": swiss_records,
        "bt_strengths": bt_strengths,
        "match_count": len(all_matches),
    })

    # Save full match history so future incremental runs can chain off this one
    with open(run_dir / "swiss_checkpoint.json", "w") as f:
        json.dump({
            "records": swiss_records,
            "matches": all_matches,
            "completed_rounds": 0,
            "total_tokens": sum(m.get("tokens", 0) for m in all_matches),
        }, f, default=str)

    logger.info(
        "Incremental Swiss done in %.0fs: %d total matches",
        time.time() - t_sw, len(all_matches),
    )

    # ── Phase 4: Combine rankings ────────────────────────────────────
    print_banner("Phase 4: Combine rankings")
    from cv_rank.scoring.combine import combine_rankings

    rankings = combine_rankings(
        all_scores, swiss_records, bt_strengths,
        swiss_weight=config["weights"]["swiss"],
        pointwise_weight=config["weights"]["pointwise"],
        auto_weight=config.get("weights", {}).get("auto", False),
    )
    save_checkpoint(run_dir, "combine", rankings)

    # ── Phase 5: Quality check (new + borderline only) ───────────────
    print_banner("Phase 5: Quality check")
    from cv_rank.quality import run_quality_check

    # Reuse existing QC for unchanged candidates, run only for new + shifted
    prev_qc_map = {r["name"]: r for r in prev_quality}
    new_ranked_names = {p.get("name", "?") for p in new_people}

    # Find people near the cutline who might have shifted
    borderline_names = set()
    for r in rankings:
        rank = r.get("rank", 999)
        if abs(rank - accept_count) <= max(10, len(new_people) // 3):
            borderline_names.add(r["name"])

    needs_qc_names = new_ranked_names | borderline_names
    needs_qc_people = [p for p in all_enriched if p.get("name") in needs_qc_names]
    needs_qc_rankings = [r for r in rankings if r["name"] in needs_qc_names]

    logger.info(
        "Quality check: %d candidates (%d new + %d borderline)",
        len(needs_qc_people), len(new_ranked_names), len(borderline_names),
    )

    if needs_qc_people:
        new_qc = asyncio.run(run_quality_check(
            needs_qc_people, needs_qc_rankings, config, run_dir, format_profile,
        ))
    else:
        new_qc = []

    # Merge: new QC results + reused old ones
    new_qc_map = {r["name"]: r for r in new_qc}
    all_qc = []
    for r in rankings:
        name = r["name"]
        if name in new_qc_map:
            all_qc.append(new_qc_map[name])
        elif name in prev_qc_map:
            all_qc.append(prev_qc_map[name])
    save_checkpoint(run_dir, "quality", all_qc)

    # ── Phase 6: Export ──────────────────────────────────────────────
    print_banner("Phase 6: Export")

    score_map = {r["name"]: r.get("final_score", 0) for r in rankings}
    qc_map = {r["name"]: r for r in all_qc}
    pw_map = {s["name"]: s.get("score", 0) for s in all_scores}

    output_csv = run_dir / "RANKED.csv"
    export_ranked_csv(
        people=all_enriched,
        scores=score_map,
        swiss_records=swiss_records,
        pointwise_scores=pw_map,
        quality_results=qc_map,
        config=config,
        output_path=output_csv,
    )
    save_checkpoint(run_dir, "export", {"csv": str(output_csv)})
    clean_csv = export_clean_csv(output_csv)

    # ── Summary ──────────────────────────────────────────────────────
    print_banner("Incremental Run Complete!")

    top_n = min(20, len(rankings))
    rows = []
    for r in rankings[:top_n]:
        name = r["name"]
        qc = qc_map.get(name, {})
        is_new = "NEW" if name in new_ranked_names else ""
        rows.append([
            r.get("rank", "?"),
            name[:22],
            f"{r.get('final_score', 0):.3f}",
            f"{r.get('pointwise_score', 0):.1f}",
            f"{r.get('swiss_wins', 0)}W-{r.get('swiss_losses', 0)}L",
            qc.get("verdict", "?"),
            is_new,
        ])

    print_summary_table(rows, ["Rank", "Name", "Score", "PW", "Swiss", "Verdict", "New?"])

    pipeline_elapsed = time.time() - pipeline_t0
    logger.info("Results: %s", output_csv)
    logger.info("Clean: %s", clean_csv)
    logger.info("Run dir: %s", run_dir)
    logger.info(
        "Incremental pipeline: %.1fs (%.1f min) | %d new + %d existing = %d total",
        pipeline_elapsed, pipeline_elapsed / 60,
        len(new_people), len(existing_people), len(all_enriched),
    )

    from cv_rank.cost import print_cost_summary
    print_cost_summary(run_dir, config, logger)

    return 0


# ---------------------------------------------------------------------------
# Subcommand: reaccept
# ---------------------------------------------------------------------------

def _install_readonly_db_guard():
    """Monkey-patch psycopg2.connect so every connection is read-only.

    Sets the PostgreSQL session to READ ONLY via ``SET SESSION CHARACTERISTICS
    AS TRANSACTION READ ONLY``.  The database itself will reject any INSERT,
    UPDATE, DELETE, DROP, etc. — there is no Python-side bypass.
    """
    try:
        import psycopg2
    except ImportError:
        return  # no psycopg2 → no DB → nothing to guard

    if getattr(psycopg2, "_cv_rank_readonly", False):
        return  # already patched — avoid double-wrapping

    _orig_connect = psycopg2.connect

    def _readonly_connect(*a, **kw):
        conn = _orig_connect(*a, **kw)
        conn.set_session(readonly=True)
        return conn

    psycopg2.connect = _readonly_connect
    psycopg2._cv_rank_readonly = True


def _cmd_reaccept(args: argparse.Namespace) -> int:
    """Move the accept cutline on an existing run, re-scoring borderline candidates."""
    import copy

    from cv_rank.checkpoint import (
        create_run_dir,
        load_checkpoint,
        save_checkpoint,
        save_meta,
    )
    from cv_rank.config import load_config, setup_logging
    from cv_rank.csv_io import export_clean_csv, export_ranked_csv
    from cv_rank.display import print_banner, print_summary_table
    from cv_rank.profile import format_profile

    # ── Load previous run ─────────────────────────────────────────
    prev_run = Path(args.prev_run)
    if not prev_run.exists():
        print(f"Error: previous run not found: {prev_run}", file=sys.stderr)
        return 1

    config_path = prev_run / "config.json"
    if not config_path.exists():
        print(f"Error: no config.json in {prev_run}", file=sys.stderr)
        return 1

    with open(config_path) as f:
        prev_config = json.load(f)

    prev_enriched = load_checkpoint(prev_run, "enrichment") or []
    prev_pointwise = load_checkpoint(prev_run, "pointwise") or []
    prev_swiss = load_checkpoint(prev_run, "swiss") or {}
    prev_combine = load_checkpoint(prev_run, "combine") or []
    prev_quality = load_checkpoint(prev_run, "quality") or []

    if not prev_combine:
        print("Error: no combine.json in previous run (incomplete run?)", file=sys.stderr)
        return 1

    old_n = prev_config.get("event", {}).get("target_accepts", 0)
    new_n = args.new_accept

    if new_n <= 0:
        print("Error: new_accept must be positive", file=sys.stderr)
        return 1
    if new_n > len(prev_combine):
        print(f"Error: new_accept ({new_n}) exceeds total candidates ({len(prev_combine)})",
              file=sys.stderr)
        return 1
    if new_n == old_n:
        print(f"New target ({new_n}) is the same as old target ({old_n}). Nothing to do.")
        return 0

    # ── Compute borderline zone ───────────────────────────────────
    # Symmetric around both cutlines so it works for expanding AND shrinking
    band = max(10, int(0.15 * max(old_n, new_n)))
    border_lo = max(1, min(old_n, new_n) - band)
    border_hi = min(len(prev_combine), max(old_n, new_n) + band)
    borderline_names = {r["name"] for r in prev_combine if border_lo <= r["rank"] <= border_hi}

    promoted = [r for r in prev_combine if old_n < r["rank"] <= new_n] if new_n > old_n else []
    demoted = [r for r in prev_combine if new_n < r["rank"] <= old_n] if new_n < old_n else []

    direction = "expanding" if new_n > old_n else "shrinking"
    moved = promoted or demoted
    move_label = f"{'promote' if promoted else 'demote'} {len(moved)}"

    # ── Dry run ───────────────────────────────────────────────────
    if args.dry_run:
        print_banner(f"Reaccept Dry Run: {old_n} → {new_n} ({direction})")
        print(f"  Would {move_label} candidates (ranks {min(old_n, new_n)+1}-{max(old_n, new_n)})")
        print(f"  Borderline zone: {len(borderline_names)} candidates (ranks {border_lo}-{border_hi})")
        print()

        if not args.no_rescore:
            qc_cost_est = len(borderline_names) * 0.012  # ~$0.012 per QC call
            print(f"  Stage 2 (QC re-score): ~{len(borderline_names)} calls, ~${qc_cost_est:.2f}")
            if not args.quick:
                cutline_count = min(30, len(prev_combine))
                swiss_cost = cutline_count // 2 * 3 * 0.015
                print(f"  Stage 3 (pairwise):    ~{cutline_count // 2 * 3} matches, ~${swiss_cost:.2f}")
                print(f"  Estimated total: ~${qc_cost_est + swiss_cost:.2f}")
            else:
                print(f"  Estimated total: ~${qc_cost_est:.2f} (--quick: no pairwise)")
        else:
            print("  --no-rescore: $0 (instant re-slice)")

        print()
        print("  Candidates that would move:")
        for r in moved[:20]:
            name = r["name"]
            qc = next((q for q in prev_quality if q.get("name") == name), {})
            print(f"    #{r['rank']:>3}  {name[:30]:<30}  score={r.get('final_score', 0):.3f}  verdict={qc.get('verdict', '?')}")
        if len(moved) > 20:
            print(f"    ... and {len(moved) - 20} more")
        return 0

    # ── Create new run dir ────────────────────────────────────────
    output_dir = Path(args.output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    run_dir = create_run_dir(output_dir)

    if args.config:
        if not Path(args.config).exists():
            print(f"Error: config not found: {args.config}", file=sys.stderr)
            return 1
        config = load_config(args.config)
    else:
        config = copy.deepcopy(prev_config)
    config["event"]["target_accepts"] = new_n
    if args.model:
        config["models"]["quality_check"] = args.model
        config["models"]["swiss"] = args.model

    # SAFETY: reaccept must NEVER write to the platform DB.
    # Wrap the connection to reject any INSERT/UPDATE/DELETE/DROP/ALTER.
    _install_readonly_db_guard()

    logger = setup_logging("cv_rank", run_dir)
    if getattr(args, "verbose", False):
        for h in logger.handlers:
            if isinstance(h, logging.StreamHandler) and not isinstance(h, logging.FileHandler):
                h.setLevel(logging.DEBUG)

    save_meta(run_dir, mode="reaccept", prev_run=str(prev_run), old_n=old_n, new_n=new_n)

    # Save config (redact secrets)
    safe_config = copy.deepcopy(config)
    for source in safe_config.get("enrichment", {}).values():
        if isinstance(source, dict):
            for k in ("key", "token", "dsn"):
                if k in source and source[k]:
                    source[k] = "***REDACTED***"
    with open(run_dir / "config.json", "w") as f:
        json.dump(safe_config, f, indent=2, default=str)

    pipeline_t0 = time.time()

    prev_qc_map = {r["name"]: r for r in prev_quality}
    swiss_records = prev_swiss.get("records", {})
    bt_strengths = prev_swiss.get("bt_strengths", {})
    rankings = prev_combine

    if args.no_rescore:
        # ── INSTANT MODE: just re-slice ───────────────────────────
        print_banner(f"Reaccept: {old_n} → {new_n} (instant re-slice)")
        all_qc = prev_quality
        logger.info("No-rescore mode: re-exporting with new cutline")
    else:
        # ── STAGE 2: Re-score QC on borderline zone ──────────────
        print_banner(f"Stage 2: Re-score QC ({len(borderline_names)} borderline candidates)")
        from cv_rank.quality import run_quality_check

        borderline_people = [p for p in prev_enriched if p.get("name") in borderline_names]
        borderline_rankings = [r for r in prev_combine if r["name"] in borderline_names]

        logger.info(
            "QC re-score: %d candidates in borderline zone (ranks %d-%d)",
            len(borderline_people), border_lo, border_hi,
        )

        if borderline_people:
            new_qc = asyncio.run(run_quality_check(
                borderline_people, borderline_rankings, config, run_dir, format_profile,
            ))
        else:
            new_qc = []

        # Merge: new QC replaces old for borderline, keep old for rest
        new_qc_map = {r["name"]: r for r in new_qc}
        all_qc = []
        for r in prev_combine:
            name = r["name"]
            if name in new_qc_map:
                all_qc.append(new_qc_map[name])
            elif name in prev_qc_map:
                all_qc.append(prev_qc_map[name])

        # Count verdict changes
        changed = 0
        for r in new_qc:
            old_verdict = prev_qc_map.get(r["name"], {}).get("verdict")
            if old_verdict and old_verdict != r.get("verdict"):
                changed += 1
                logger.info("  Verdict changed: %s — %s → %s", r["name"], old_verdict, r.get("verdict"))
        logger.info("QC re-score: %d / %d verdicts changed", changed, len(new_qc))

        if not args.quick:
            # ── STAGE 3: Pairwise resolution near new cutline ─────
            cutline_band = 15
            cutline_lo = max(1, new_n - cutline_band)
            cutline_hi = min(len(prev_combine), new_n + cutline_band)
            cutline_names = {r["name"] for r in prev_combine if cutline_lo <= r["rank"] <= cutline_hi}
            cutline_people = [p for p in prev_enriched if p.get("name") in cutline_names]

            print_banner(f"Stage 3: Pairwise resolution ({len(cutline_people)} near cutline)")

            if len(cutline_people) >= 2:
                from cv_rank.config import parse_criteria
                from cv_rank.scoring.swiss import run_swiss

                criteria = parse_criteria(None, config)

                # Anonymize for swiss (same pattern as incremental)
                def format_profile_with_raw(person, _orig=format_profile):
                    return _orig(person, include_raw_csv=True)

                anon_format = format_profile_with_raw
                if config.get("anonymize", {}).get("enabled", True):
                    from cv_rank.anonymize import Anonymizer
                    anonymizer = Anonymizer(cutline_people)
                    anonymizer.save(run_dir / "anonymizer_map.json")
                    anon_format = anonymizer.wrap_format_fn(format_profile_with_raw)

                cutline_pw = [s for s in prev_pointwise if s.get("name") in cutline_names]

                new_records, new_bt, new_matches = asyncio.run(run_swiss(
                    cutline_people, criteria, config["models"]["swiss"],
                    3,  # 3 rounds for focused resolution
                    new_n, config, run_dir, anon_format,
                    pointwise_scores=cutline_pw,
                ))

                logger.info("Pairwise: %d matches among %d candidates",
                            len(new_matches), len(cutline_people))

                # Merge swiss records additively
                swiss_records = copy.deepcopy(prev_swiss.get("records", {}))
                for name, rec in new_records.items():
                    if name in swiss_records:
                        swiss_records[name]["wins"] += rec["wins"]
                        swiss_records[name]["losses"] += rec["losses"]
                    else:
                        swiss_records[name] = rec

                # Refit BT on merged data
                from cv_rank.scoring.bradley_terry import compute_bt_strengths

                all_names = list(swiss_records.keys())
                name_to_idx = {n: i for i, n in enumerate(all_names)}

                # Build comparisons from new matches
                new_comparisons = []
                for m in new_matches:
                    w = m.get("winner")
                    a, b = m.get("name_a"), m.get("name_b")
                    loser = b if w == a else a
                    if w in name_to_idx and loser in name_to_idx:
                        new_comparisons.append((name_to_idx[w], name_to_idx[loser]))

                # Build comparisons from old win/loss records (approximate)
                # We don't have raw old matches, so use new matches only for BT refit
                # but keep the merged win/loss records for combine_rankings
                pw_prior = {s["name"]: s.get("score", 50) for s in prev_pointwise}
                bt_strengths = compute_bt_strengths(
                    all_names, new_comparisons, pointwise_prior=pw_prior, prior_strength=3,
                )

                # Recompute combined rankings
                from cv_rank.scoring.combine import combine_rankings

                rankings = combine_rankings(
                    prev_pointwise, swiss_records, bt_strengths,
                    swiss_weight=config.get("weights", {}).get("swiss", 0.5),
                    pointwise_weight=config.get("weights", {}).get("pointwise", 0.5),
                    auto_weight=config.get("weights", {}).get("auto", False),
                )

                save_checkpoint(run_dir, "swiss", {
                    "records": swiss_records,
                    "bt_strengths": bt_strengths,
                    "match_count": prev_swiss.get("match_count", 0) + len(new_matches),
                })
            else:
                logger.info("Skipping pairwise: fewer than 2 candidates in cutline zone")

    # Always save combine checkpoint so this run can be chained into
    # a subsequent reaccept or incremental run.
    save_checkpoint(run_dir, "combine", rankings)

    # ── STAGE 4: Re-export ────────────────────────────────────────
    print_banner("Stage 4: Export")

    score_map = {r["name"]: r.get("final_score", 0) for r in rankings}
    qc_map = {r["name"]: r for r in all_qc}
    pw_map = {s["name"]: s.get("score", 0) for s in prev_pointwise}

    save_checkpoint(run_dir, "quality", all_qc)

    output_csv = run_dir / "RANKED.csv"
    export_ranked_csv(
        people=prev_enriched,
        scores=score_map,
        swiss_records=swiss_records,
        pointwise_scores=pw_map,
        quality_results=qc_map,
        config=config,
        output_path=output_csv,
    )
    save_checkpoint(run_dir, "export", {"csv": str(output_csv)})
    clean_csv = export_clean_csv(output_csv)

    # ── Summary ───────────────────────────────────────────────────
    print_banner(f"Reaccept Complete: {old_n} → {new_n}")

    # Recompute moved from final rankings (stage 3 may have reshuffled)
    old_accepted = {r["name"] for r in prev_combine if r.get("rank", 0) <= old_n}
    new_accepted = {r["name"] for r in rankings if r.get("rank", 0) <= new_n}
    promoted_names = new_accepted - old_accepted
    demoted_names = old_accepted - new_accepted
    moved_names = promoted_names | demoted_names

    print(f"  Direction: {direction}")
    print(f"  Promoted: {len(promoted_names)}, Demoted: {len(demoted_names)}")
    print(f"  Output: {output_csv}")
    print(f"  Clean:  {clean_csv}")
    print(f"  Run dir: {run_dir}")
    print()

    # Show moved candidates
    rows = []
    for r in rankings:
        if r["name"] in moved_names:
            name = r["name"]
            qc = qc_map.get(name, {})
            old_v = prev_qc_map.get(name, {}).get("verdict", "?")
            new_v = qc.get("verdict", "?")
            v_change = f"{old_v}→{new_v}" if old_v != new_v else new_v
            rows.append([
                r.get("rank", "?"),
                name[:28],
                f"{r.get('final_score', 0):.3f}",
                v_change,
                qc.get("specific_why", "")[:50],
            ])

    if rows:
        print_summary_table(rows, ["Rank", "Name", "Score", "Verdict", "Why"])

    pipeline_elapsed = time.time() - pipeline_t0
    logger.info("Reaccept: %.1fs (%.1f min)", pipeline_elapsed, pipeline_elapsed / 60)

    # Email list (from final rankings, not the pre-rerank snapshot)
    if args.email_list and promoted_names:
        print()
        print_banner("Newly Accepted Emails")
        people_by_name = {p["name"]: p for p in prev_enriched}
        for name in sorted(promoted_names):
            email = people_by_name.get(name, {}).get("email", "")
            if email:
                print(email)

    from cv_rank.cost import print_cost_summary
    print_cost_summary(run_dir, config, logger)

    return 0


# ---------------------------------------------------------------------------
# Subcommand: clean
# ---------------------------------------------------------------------------


def _cmd_clean(args: argparse.Namespace) -> int:
    """Generate a clean CSV from a full RANKED.csv."""
    from cv_rank.csv_io import export_clean_csv

    csv_path = Path(args.csv)
    if not csv_path.exists():
        print(f"Error: file not found: {csv_path}", file=sys.stderr)
        return 1

    output = export_clean_csv(csv_path, args.output)
    print(f"  Clean CSV written: {output}")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: apply
# ---------------------------------------------------------------------------


def _cmd_apply(args: argparse.Namespace) -> int:
    """Stamp cv-rank decisions back onto the original applicant CSV."""
    from cv_rank.csv_io import apply_rankings_to_original

    ranked_path = Path(args.ranked_csv)
    original_path = Path(args.original)

    if not ranked_path.exists():
        print(f"Error: file not found: {ranked_path}", file=sys.stderr)
        return 1
    if not original_path.exists():
        print(f"Error: file not found: {original_path}", file=sys.stderr)
        return 1

    columns = None
    if args.columns:
        columns = [c.strip() for c in args.columns.split(",")]

    output_path, matched, unmatched = apply_rankings_to_original(
        ranked_path,
        original_path,
        args.output,
        columns=columns,
        into_col=args.into,
    )

    print(f"  Applied: {matched} matched, {unmatched} unmatched")
    print(f"  Output:  {output_path}")
    return 0


# ---------------------------------------------------------------------------
# Subcommand: events
# ---------------------------------------------------------------------------


def _cmd_events(args: argparse.Namespace) -> int:
    """List recent events from the platform DB."""
    import os
    from cv_rank.enrichment.platform_db import list_platform_events

    dsn = os.environ.get("PLATFORM_DATABASE_URL", "")
    if not dsn:
        print("Error: PLATFORM_DATABASE_URL not set in environment or .env", file=sys.stderr)
        return 1

    events = list_platform_events(dsn, upcoming_only=args.upcoming, limit=args.limit)
    if not events:
        print("No events found.")
        return 0

    print(f"\n{'Title':<50} {'Date':>12} {'Applicants':>10}")
    print(f"{'─'*50} {'─'*12} {'─'*10}")
    for e in events:
        title = e["title"][:50]
        date = str(e.get("date", "?"))
        count = e.get("applicants", 0)
        print(f"{title:<50} {date:>12} {count:>10}")
    print("\nUse: cv-rank run --event \"<title>\" --accept N")
    return 0


def _run_waves_script(script_name: str, extra_args: list[str]) -> int:
    script_path = Path(__file__).resolve().parents[2] / "scripts" / script_name
    if not script_path.exists():
        print(f"Error: missing script {script_path}", file=sys.stderr)
        return 1
    completed = subprocess.run(
        [sys.executable, str(script_path), *extra_args],
        check=False,
    )
    return int(completed.returncode)


def _cmd_sponsor_analyze(args: argparse.Namespace) -> int:
    if not args.event and not args.run_id:
        print("Error: sponsor-analyze requires --event or --run-id", file=sys.stderr)
        return 1

    from cv_rank.sponsor_automation.analysis_helper import AnalysisConfig, run_analysis_helper

    repo_root = Path(__file__).resolve().parents[2]
    result = run_analysis_helper(
        AnalysisConfig(
            repo_root=repo_root,
            output_root=Path(args.output_root),
            event_name=args.event,
            run_id=args.run_id,
            enriched_json=args.enriched_json,
            reviewer_mode=args.reviewer_mode,
        )
    )

    print(f"event_name={result.event_name}")
    print(f"event_slug={result.event_slug}")
    print(f"output_dir={result.output_dir}")
    print(f"summary={result.summary_path}")
    print(f"tasks={result.tasks_path}")
    print(f"snapshot={result.snapshot_path}")
    print(f"claim_review={result.claim_review_path}")
    print(f"segment_funnel={result.segment_funnel_path}")
    print(f"tool_summary={result.tool_summary_path}")
    print(f"theme_summary={result.theme_summary_path}")
    print(f"audience_summary={result.audience_summary_path}")
    print(f"tool_outcomes={result.tool_outcomes_path}")
    print(f"tool_reconciliation={result.tool_reconciliation_path}")
    print(f"top_projects={result.top_projects_path}")
    print(f"big_numbers={result.big_numbers_path}")
    print("raw_bundle=")
    for path in result.raw_bundle_paths:
        print(f"- {path}")
    return 0


def _cmd_waves_train(args: argparse.Namespace) -> int:
    extra_args = [
        "--output-dir",
        args.output_dir,
        "--calibration-events",
        str(args.calibration_events),
        "--test-events",
        str(args.test_events),
        "--horizons",
        args.horizons,
        "--stage0-mode",
        args.stage0_mode,
    ]
    return _run_waves_script("train_waves_v2.py", extra_args)


def _cmd_waves_predict(args: argparse.Namespace) -> int:
    extra_args = [
        "--artifact-dir",
        args.artifact_dir,
        "--days-ahead",
        str(args.days_ahead),
        "--output-csv",
        args.output_csv,
    ]
    if args.include_non_hackathons:
        extra_args.append("--include-non-hackathons")
    return _run_waves_script("predict_upcoming_v2.py", extra_args)


def _render_cmd(cmd: list[str]) -> str:
    return " ".join(shlex.quote(part) for part in cmd)


def _build_dossier_enqueue_cmd(args: argparse.Namespace) -> list[str]:
    cmd = [
        sys.executable,
        str(ATTENDEE_QUEUE_SCRIPT),
        "enqueue",
        "--queue-name",
        args.queue_name,
        "--offset",
        str(args.offset),
        "--batch-size",
        str(args.batch_size),
    ]
    people_file = (args.people_file or "").strip()
    if people_file:
        cmd.extend(["--people-file", str(Path(people_file).expanduser().resolve())])
    if args.all:
        cmd.append("--all")
    elif int(args.limit or 0) > 0:
        cmd.extend(["--limit", str(args.limit)])
    return cmd


def _build_dossier_launch_cmd(args: argparse.Namespace) -> list[str]:
    return [
        sys.executable,
        str(MINIMAX_DOSSIER_WORKERS),
        "--env-file",
        str(Path(args.daytona_env_file).expanduser().resolve()),
        "--queue-name",
        args.queue_name,
        "--model",
        args.model,
        "--sandbox-prefix",
        args.sandbox_prefix,
        "--sandbox-start-index",
        str(args.sandbox_start_index),
        "--sandbox-count",
        str(args.sandbox_count),
        "--workers-per-sandbox",
        str(args.workers_per_sandbox),
        "--log-dir",
        args.log_dir,
        "--web-mode",
        args.web_mode,
        "--timeout-seconds",
        str(args.timeout_seconds),
        "--minimax-env-file",
        str(Path(args.minimax_env_file).expanduser().resolve()),
        "--cpu",
        str(args.cpu),
        "--memory",
        str(args.memory),
        "--disk",
        str(args.disk),
    ]


def _cmd_dossiers(args: argparse.Namespace) -> int:
    if args.enqueue_only and args.launch_only:
        raise SystemExit("Use at most one of --enqueue-only or --launch-only.")
    if not ATTENDEE_QUEUE_SCRIPT.exists():
        raise SystemExit(f"Missing queue script: {ATTENDEE_QUEUE_SCRIPT}")
    if not MINIMAX_DOSSIER_WORKERS.exists():
        raise SystemExit(f"Missing MiniMax worker launcher: {MINIMAX_DOSSIER_WORKERS}")

    enqueue_cmd = _build_dossier_enqueue_cmd(args)
    launch_cmd = _build_dossier_launch_cmd(args)
    if args.dry_run:
        if args.people_file:
            print(f"people_file={Path(args.people_file).expanduser().resolve()}")
        else:
            print("people_file=<platform-ranked source>")
        print(f"enqueue_command={_render_cmd(enqueue_cmd)}")
        print(f"launch_command={_render_cmd(launch_cmd)}")
        return 0

    if not args.launch_only:
        enqueue_result = subprocess.run(enqueue_cmd, cwd=str(REPO_ROOT), check=False)
        if enqueue_result.returncode != 0:
            return int(enqueue_result.returncode)
    if not args.enqueue_only:
        launch_result = subprocess.run(launch_cmd, cwd=str(REPO_ROOT), check=False)
        return int(launch_result.returncode)
    return 0


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main() -> None:
    import os
    os.environ.setdefault("PYTHONUNBUFFERED", "1")

    load_dotenv()

    parser = _build_parser()
    args = parser.parse_args()

    if not args.command:
        parser.print_help()
        sys.exit(0)

    dispatch = {
        "run": _cmd_run,
        "validate": _cmd_validate,
        "init": _cmd_init,
        "clean": _cmd_clean,
        "apply": _cmd_apply,
        "incremental": _cmd_incremental,
        "reaccept": _cmd_reaccept,
        "events": _cmd_events,
        "sponsor-analyze": _cmd_sponsor_analyze,
        "waves-train": _cmd_waves_train,
        "waves-predict": _cmd_waves_predict,
        "dossiers": _cmd_dossiers,
    }

    handler = dispatch.get(args.command)
    if handler is None:
        parser.print_help()
        sys.exit(1)

    try:
        rc = handler(args)
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        rc = 130
    except Exception as exc:
        print(f"\nError: {exc}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)
        rc = 1

    sys.exit(rc)


if __name__ == "__main__":
    main()
