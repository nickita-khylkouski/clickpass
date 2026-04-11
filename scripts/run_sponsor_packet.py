#!/usr/bin/env python3
from __future__ import annotations

import argparse
import sys
from pathlib import Path


def _load_orchestrator():
    repo_root = Path(__file__).resolve().parents[1]
    src_root = repo_root / "src"
    if str(src_root) not in sys.path:
        sys.path.insert(0, str(src_root))

    try:
        from cv_rank.sponsor_automation.orchestrator import PipelineConfig, run_pipeline

        return PipelineConfig, run_pipeline
    except Exception:
        import importlib.util

        module_path = src_root / "cv_rank" / "sponsor_automation" / "orchestrator.py"
        spec = importlib.util.spec_from_file_location("cv_rank.sponsor_automation.orchestrator", module_path)
        if spec is None or spec.loader is None:
            raise RuntimeError(f"Unable to load orchestrator at {module_path}")
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return module.PipelineConfig, module.run_pipeline


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate sponsor packet artifacts in one command.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--event",
        default=None,
        help="Event name (used for output slug and packet metadata).",
    )
    parser.add_argument(
        "--run-id",
        default=None,
        help="Run ID or run directory name (example: run_20260306_185829).",
    )
    parser.add_argument(
        "--enriched-json",
        default=None,
        help="Optional explicit path to enriched_complete*.json (overrides run-id resolution).",
    )
    parser.add_argument(
        "--mode",
        choices=["auto", "semi"],
        default="auto",
        help="`auto` for full unattended flow; `semi` inserts operator pause before narrative generation.",
    )
    parser.add_argument(
        "--reviewer-mode",
        choices=["strict", "off"],
        default="strict",
        help="Claim reviewer gate before narrative generation.",
    )
    parser.add_argument(
        "--pause-after-metrics",
        action="store_true",
        help="Pause after deterministic metrics extraction before continuing.",
    )
    parser.add_argument(
        "--pause-after-review",
        action="store_true",
        help="Pause after claim reviewer stage before narrative generation.",
    )
    parser.add_argument(
        "--openai-model",
        default=None,
        help="Override generation model (defaults to OPENAI_MODEL or gpt-5.2).",
    )
    parser.add_argument(
        "--openai-editor-model",
        default=None,
        help="Optional editor model for second-pass cleanup (defaults to OPENAI_EDITOR_MODEL or gpt-5.4).",
    )
    parser.add_argument(
        "--output-root",
        default=None,
        help="Output root for final packet bundle (default: <repo>/outputs).",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if not args.event and not args.run_id:
        parser.error("Provide at least one of --event or --run-id.")

    repo_root = Path(__file__).resolve().parents[1]
    output_root = Path(args.output_root) if args.output_root else (repo_root / "outputs")

    PipelineConfig, run_pipeline = _load_orchestrator()
    config = PipelineConfig(
        repo_root=repo_root,
        output_root=output_root,
        event_name=args.event,
        run_id=args.run_id,
        enriched_json=args.enriched_json,
        mode=args.mode,
        reviewer_mode=args.reviewer_mode,
        pause_after_metrics=bool(args.pause_after_metrics),
        pause_after_review=bool(args.pause_after_review),
        openai_model=args.openai_model,
        openai_editor_model=args.openai_editor_model,
    )

    try:
        result = run_pipeline(config)
    except Exception as exc:
        print(f"Sponsor packet pipeline failed: {exc}", file=sys.stderr)
        return 1

    print(f"event_slug={result.event_slug}")
    print(f"output_dir={result.output_dir}")
    print(f"packet={result.packet_path}")
    print(f"run_audit={result.run_audit_path}")
    print(f"claims={result.claims_path}")
    print("csvs=")
    for csv_path in result.copied_csv_paths:
        print(f"- {csv_path}")
    print(f"qa_passed={result.qa_report.passed}")
    if result.qa_report.warnings:
        print("qa_warnings=")
        for warning in result.qa_report.warnings:
            print(f"- {warning}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
