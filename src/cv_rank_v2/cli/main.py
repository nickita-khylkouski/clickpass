"""Thin CLI for the v2 ranking pipeline."""

from __future__ import annotations

import argparse
import json
import sys

from dotenv import load_dotenv

from cv_rank_v2.ingest.validate import validate_csv_file
from cv_rank_v2.pipeline import run_csv_pipeline, run_event_pipeline


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="cv-rank-v2")
    subparsers = parser.add_subparsers(dest="command")

    run_parser = subparsers.add_parser("run", help="Run the ranking-only v2 pipeline on a CSV file.")
    source_group = run_parser.add_mutually_exclusive_group(required=True)
    source_group.add_argument("--csv")
    source_group.add_argument("--event")
    run_parser.add_argument("--accept", type=int, required=True)
    run_parser.add_argument("--config")
    run_parser.add_argument("--criteria")
    run_parser.add_argument("--model")
    run_parser.add_argument("--backend", choices=["legacy", "heuristic"])
    run_parser.add_argument(
        "--ranking-policy",
        choices=["balanced", "swiss_first", "swiss_dominant", "pointwise_first"],
    )
    run_parser.add_argument("--swiss-weight", type=float)
    run_parser.add_argument("--pointwise-weight", type=float)
    run_parser.add_argument("--auto-weights", dest="auto_weights", action="store_true")
    run_parser.add_argument("--no-auto-weights", dest="auto_weights", action="store_false")
    run_parser.add_argument("--tie-breaker", choices=["swiss", "pointwise"])
    run_parser.add_argument("--shortlist-min-size", type=int)
    run_parser.add_argument("--shortlist-multiplier", type=float)
    run_parser.set_defaults(auto_weights=None)
    run_parser.add_argument("--event-name")
    run_parser.add_argument("--output-dir", default="results")
    run_parser.add_argument("--sample-size", type=int)
    run_parser.add_argument("--sample-seed", type=int, default=0)

    validate_parser = subparsers.add_parser("validate", help="Validate CSV input for the v2 pipeline.")
    validate_parser.add_argument("--csv", required=True)
    return parser


def _cmd_validate(args: argparse.Namespace) -> int:
    envelope = validate_csv_file(args.csv)
    print(
        json.dumps(
            {
                "source": envelope.source,
                "row_count": envelope.row_count,
                "accepted_count": envelope.accepted_count,
                "skipped_count": envelope.skipped_count,
                "valid": envelope.valid,
                "messages": [
                    {
                        "level": message.level,
                        "code": message.code,
                        "message": message.message,
                        "row_number": message.row_number,
                        "field": message.field,
                    }
                    for message in envelope.messages
                ],
            },
            indent=2,
        )
    )
    return 0 if envelope.valid else 1


def _cmd_run(args: argparse.Namespace) -> int:
    if args.csv:
        run = run_csv_pipeline(
            csv_path=args.csv,
            accept_count=args.accept,
            output_dir=args.output_dir,
            config_path=args.config,
            criteria_text=args.criteria,
            model=args.model,
            backend=args.backend,
            event_name=args.event_name,
            ranking_policy=args.ranking_policy,
            swiss_weight=args.swiss_weight,
            pointwise_weight=args.pointwise_weight,
            auto_weights=args.auto_weights,
            tie_breaker=args.tie_breaker,
            shortlist_min_size=args.shortlist_min_size,
            shortlist_multiplier=args.shortlist_multiplier,
        )
    else:
        run = run_event_pipeline(
            event_name=args.event,
            accept_count=args.accept,
            output_dir=args.output_dir,
            config_path=args.config,
            criteria_text=args.criteria,
            model=args.model,
            backend=args.backend,
            sample_size=args.sample_size,
            sample_seed=args.sample_seed,
            ranking_policy=args.ranking_policy,
            swiss_weight=args.swiss_weight,
            pointwise_weight=args.pointwise_weight,
            auto_weights=args.auto_weights,
            tie_breaker=args.tie_breaker,
            shortlist_min_size=args.shortlist_min_size,
            shortlist_multiplier=args.shortlist_multiplier,
        )
    print(f"Run dir: {run.run_dir}")
    print(f"Ranking policy: {run.config.ranking.policy}")
    print(
        "Weights: "
        f"swiss={run.config.ranking.swiss_weight:.2f}, "
        f"pointwise={run.config.ranking.pointwise_weight:.2f}, "
        f"auto={run.config.ranking.auto_weights}, "
        f"tie_breaker={run.config.ranking.tie_breaker}"
    )
    print(f"Ranked CSV: {run.artifact_paths['ranked_csv']}")
    print(f"Needs review CSV: {run.artifact_paths['needs_review']}")
    print(f"Failed-to-rank CSV: {run.artifact_paths['failed_pointwise']}")
    return 0


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = _build_parser()
    args = parser.parse_args(argv)
    if args.command == "validate":
        return _cmd_validate(args)
    if args.command == "run":
        return _cmd_run(args)
    parser.print_help(sys.stderr)
    return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
