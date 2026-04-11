from __future__ import annotations

import argparse
import gzip
import json
import os
import shutil
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import psycopg2
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
LOCAL_DATA_DIR = BASE_DIR / "local_data"
DERIVED_DIR = LOCAL_DATA_DIR / "derived"
PLATFORM_DIR = LOCAL_DATA_DIR / "platform"
PLATFORM_TABLES_DIR = PLATFORM_DIR / "tables"
PLATFORM_DUMPS_DIR = PLATFORM_DIR / "dumps"
POSTHOG_DIR = LOCAL_DATA_DIR / "posthog"
AGENT_ANALYSIS_DIR = LOCAL_DATA_DIR / "agent_analysis"
AGENT_PORTAL_DIR = LOCAL_DATA_DIR / "agent_portal"
MANIFESTS_DIR = LOCAL_DATA_DIR / "manifests"

CORE_PLATFORM_TABLES = [
    "PlatformEvent",
    "EventApplicant",
    "Insight",
    "PlatformNotification",
]

ANALYSIS_PLATFORM_TABLES = CORE_PLATFORM_TABLES + [
    "UserProfile",
    "UTMTracking",
    "EventInvitation",
    "EventQuestion",
    "EventQuestionAnswer",
    "EventReminder",
    "EventNotificationBlast",
    "UserFollow",
    "NaturalLanguageSearchQuery",
]

HACKATHON_PLATFORM_TABLES = [
    "HackathonSubmission",
    "HackathonSubmissionValue",
    "HackathonTeamMember",
    "HackathonJudgingScore",
    "HackathonSubmissionJudging",
    "HackathonSubmissionField",
    "HackathonSubmissionPublicVote",
]

MESSAGING_PLATFORM_TABLES = [
    "EventApplicationChannel",
    "EventApplicationChannelMessage",
    "ConversationChannel",
    "ConversationChannelMessage",
    "ConversationChannelMember",
]

TABLE_PROFILES = {
    "core": CORE_PLATFORM_TABLES,
    "analysis": ANALYSIS_PLATFORM_TABLES,
    "hackathon": HACKATHON_PLATFORM_TABLES,
    "messaging": MESSAGING_PLATFORM_TABLES,
    "all_useful": ANALYSIS_PLATFORM_TABLES + HACKATHON_PLATFORM_TABLES + MESSAGING_PLATFORM_TABLES,
}

DERIVED_FILES = [
    "signup_velocity.csv",
    "engagement_profiles.csv",
    "application_velocity.csv",
    "posthog_event_velocity.csv",
]

POSTHOG_ANALYSIS_FILES = [
    "README.md",
    "top_events.json",
    "event_funnel_events.json",
    "linkability.json",
    "top_apply_events.json",
    "top_events.tsv",
    "event_funnel_events.tsv",
    "linkability.tsv",
    "top_apply_events.tsv",
]
POSTHOG_PORTAL_FILES = [
    "README.md",
    "index.json",
    "top_events_30d.json",
]


def load_env() -> None:
    load_dotenv(BASE_DIR / ".env")


def run_python_script(script_name: str) -> None:
    subprocess.run(
        ["uv", "run", "python", f"scripts/{script_name}"],
        cwd=BASE_DIR,
        check=True,
    )


def copy_if_exists(src: Path, dst: Path) -> None:
    if not src.exists():
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dst)


def export_platform_schema(dsn: str) -> Path:
    PLATFORM_DIR.mkdir(parents=True, exist_ok=True)
    schema_path = PLATFORM_DIR / "schema.sql"
    subprocess.run(
        [
            "pg_dump",
            dsn,
            "--schema-only",
            "--no-owner",
            "--no-privileges",
            "--file",
            str(schema_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return schema_path


def export_platform_tables(dsn: str, tables: list[str]) -> tuple[list[dict], list[dict]]:
    PLATFORM_TABLES_DIR.mkdir(parents=True, exist_ok=True)
    conn = psycopg2.connect(dsn)
    manifests: list[dict] = []
    warnings: list[dict] = []
    try:
        with conn.cursor() as cur:
            cur.execute("SET statement_timeout TO 0")
            for table in tables:
                output_path = PLATFORM_TABLES_DIR / f"{table}.csv.gz"
                try:
                    with gzip.open(output_path, "wt", compresslevel=6) as gz:
                        copy_sql = f'COPY (SELECT * FROM public."{table}") TO STDOUT WITH CSV HEADER'
                        cur.copy_expert(copy_sql, gz)
                    cur.execute(f'SELECT count(*) FROM public."{table}"')
                    row_count = int(cur.fetchone()[0])
                    manifests.append(
                        {
                            "table": table,
                            "rows": row_count,
                            "path": str(output_path.relative_to(BASE_DIR)),
                            "bytes": output_path.stat().st_size,
                        }
                    )
                except Exception as exc:
                    warnings.append({"table": table, "error": str(exc).strip()})
                    if output_path.exists():
                        output_path.unlink()
    finally:
        conn.close()
    return manifests, warnings


def export_platform_dump(dsn: str) -> Path:
    PLATFORM_DUMPS_DIR.mkdir(parents=True, exist_ok=True)
    dump_path = PLATFORM_DUMPS_DIR / "platform_latest.dump"
    subprocess.run(
        [
            "pg_dump",
            dsn,
            "--format=custom",
            "--compress=9",
            "--no-owner",
            "--no-privileges",
            "--file",
            str(dump_path),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    return dump_path


def sync_derived_artifacts() -> list[dict]:
    DERIVED_DIR.mkdir(parents=True, exist_ok=True)
    synced: list[dict] = []
    for filename in DERIVED_FILES:
        src = BASE_DIR / "results" / filename
        dst = DERIVED_DIR / filename
        copy_if_exists(src, dst)
        if dst.exists():
            synced.append(
                {
                    "path": str(dst.relative_to(BASE_DIR)),
                    "bytes": dst.stat().st_size,
                }
            )
    return synced


def sync_posthog_analysis() -> list[dict]:
    target_dir = POSTHOG_DIR / "analysis"
    target_dir.mkdir(parents=True, exist_ok=True)
    synced: list[dict] = []
    for filename in POSTHOG_ANALYSIS_FILES:
        src = BASE_DIR / "results" / "posthog_analysis" / filename
        dst = target_dir / filename
        copy_if_exists(src, dst)
        if dst.exists():
            synced.append(
                {
                    "path": str(dst.relative_to(BASE_DIR)),
                    "bytes": dst.stat().st_size,
                }
            )
    return synced


def sync_posthog_portal() -> list[dict]:
    target_dir = POSTHOG_DIR / "portal"
    if not target_dir.exists():
        return []
    synced: list[dict] = []
    for filename in POSTHOG_PORTAL_FILES:
        path = target_dir / filename
        if path.exists():
            synced.append({"path": str(path.relative_to(BASE_DIR)), "bytes": path.stat().st_size})
    sample_dir = target_dir / "samples"
    if sample_dir.exists():
        for path in sorted(sample_dir.glob("*.json")):
            synced.append({"path": str(path.relative_to(BASE_DIR)), "bytes": path.stat().st_size})
    return synced


def existing_platform_table_artifacts() -> list[dict]:
    if not PLATFORM_TABLES_DIR.exists():
        return []
    artifacts: list[dict] = []
    for path in sorted(PLATFORM_TABLES_DIR.glob("*.csv.gz")):
        artifacts.append(
            {
                "table": path.stem.replace(".csv", ""),
                "path": str(path.relative_to(BASE_DIR)),
                "bytes": path.stat().st_size,
            }
        )
    return artifacts


def existing_platform_schema_artifact() -> dict | None:
    path = PLATFORM_DIR / "schema.sql"
    if not path.exists():
        return None
    return {"path": str(path.relative_to(BASE_DIR)), "bytes": path.stat().st_size}


def existing_platform_dump_artifact() -> dict | None:
    path = PLATFORM_DUMPS_DIR / "platform_latest.dump"
    if not path.exists():
        return None
    return {"path": str(path.relative_to(BASE_DIR)), "bytes": path.stat().st_size}


def existing_agent_analysis_manifest() -> dict | None:
    path = AGENT_ANALYSIS_DIR / "manifest.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {"path": str(path.relative_to(BASE_DIR))}
    return {
        "path": str(path.relative_to(BASE_DIR)),
        "generated_at": data.get("generated_at"),
        "outputs": data.get("outputs", {}),
        "derived_exports": data.get("derived_exports", {}),
    }


def existing_agent_portal_index() -> dict | None:
    path = AGENT_PORTAL_DIR / "index.json"
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text())
    except Exception:
        return {"path": str(path.relative_to(BASE_DIR))}
    return {
        "path": str(path.relative_to(BASE_DIR)),
        "generated_at": data.get("generated_at"),
        "profiles": data.get("profiles", {}),
        "groups": data.get("groups", {}),
    }


def refresh_source_inventory() -> None:
    subprocess.run(
        ["uv", "run", "python", "scripts/audit_data_sources.py"],
        cwd=BASE_DIR,
        check=True,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Sync local data store for cv-rank.")
    parser.add_argument("--refresh-derived", action="store_true", help="Rebuild derived CSV artifacts before copying them.")
    parser.add_argument("--build-agent-pack", action="store_true", help="Build compact agent-analysis CSV packs.")
    parser.add_argument("--build-agent-portal", action="store_true", help="Build per-file data profiles, sample rows, and catalogs for agents.")
    parser.add_argument("--build-posthog-reference", action="store_true", help="Build saved PostHog sample rows and event reference files.")
    parser.add_argument("--agent-pack-users", action="store_true", help="Include non-PII user profile rows in the agent pack.")
    parser.add_argument("--agent-pack-hackathon-history", action="store_true", help="Include user hackathon history in the agent pack.")
    parser.add_argument("--agent-pack-messaging", action="store_true", help="Include applicant messaging rollups in the agent pack.")
    parser.add_argument("--export-platform-schema", action="store_true", help="Export platform DB schema to local_data/platform/schema.sql.")
    parser.add_argument("--export-platform-tables", action="store_true", help="Export selected platform tables to gzipped CSV.")
    parser.add_argument("--dump-platform", action="store_true", help="Write a full compressed pg_dump to local_data/platform/dumps.")
    parser.add_argument(
        "--platform-profile",
        choices=sorted(TABLE_PROFILES.keys()),
        default="core",
        help="Named platform table profile to export when --export-platform-tables is set.",
    )
    parser.add_argument(
        "--platform-tables",
        nargs="*",
        default=None,
        help="Explicit platform tables to export when --export-platform-tables is set. Overrides --platform-profile.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_env()

    LOCAL_DATA_DIR.mkdir(parents=True, exist_ok=True)
    MANIFESTS_DIR.mkdir(parents=True, exist_ok=True)
    preflight_warnings: list[dict] = []

    if args.refresh_derived:
        run_python_script("extract_signup_velocity.py")
        run_python_script("extract_engagement_profiles.py")
        run_python_script("extract_application_velocity.py")
        run_python_script("extract_posthog_event_velocity.py")
        run_python_script("analyze_posthog_taxonomy.py")
    if args.build_agent_pack:
        cmd = ["uv", "run", "python", "scripts/build_agent_analysis_pack.py"]
        if args.agent_pack_users:
            cmd.append("--include-users")
        if args.agent_pack_hackathon_history:
            cmd.append("--include-hackathon-history")
        if args.agent_pack_messaging:
            cmd.append("--include-messaging")
        subprocess.run(cmd, cwd=BASE_DIR, check=True)
    if args.build_agent_portal or args.build_agent_pack:
        try:
            subprocess.run(["uv", "run", "python", "scripts/build_agent_data_portal.py"], cwd=BASE_DIR, check=True)
        except subprocess.CalledProcessError as exc:
            preflight_warnings.append(
                {
                    "step": "build-agent-portal",
                    "error": (exc.stderr or exc.stdout or str(exc)).strip(),
                }
            )
    if args.build_posthog_reference:
        try:
            subprocess.run(["uv", "run", "python", "scripts/build_posthog_reference.py"], cwd=BASE_DIR, check=True)
        except subprocess.CalledProcessError as exc:
            preflight_warnings.append(
                {
                    "step": "build-posthog-reference",
                    "error": (exc.stderr or exc.stdout or str(exc)).strip(),
                }
            )

    synced = {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "derived": sync_derived_artifacts(),
        "posthog_analysis": sync_posthog_analysis(),
        "posthog_portal": sync_posthog_portal(),
        "agent_analysis": existing_agent_analysis_manifest(),
        "agent_portal": existing_agent_portal_index(),
        "platform_schema": existing_platform_schema_artifact(),
        "platform_tables": existing_platform_table_artifacts(),
        "platform_dump": existing_platform_dump_artifact(),
        "platform_profile": args.platform_profile,
        "warnings": preflight_warnings,
    }

    dsn = os.environ.get("PLATFORM_DATABASE_URL", "").strip()
    if args.export_platform_schema and dsn:
        try:
            schema_path = export_platform_schema(dsn)
            synced["platform_schema"] = {
                "path": str(schema_path.relative_to(BASE_DIR)),
                "bytes": schema_path.stat().st_size,
            }
        except subprocess.CalledProcessError as exc:
            synced["warnings"].append(
                {
                    "step": "export-platform-schema",
                    "error": (exc.stderr or exc.stdout or str(exc)).strip(),
                }
            )

    if args.export_platform_tables and dsn:
        selected_tables = args.platform_tables or TABLE_PROFILES[args.platform_profile]
        tables, table_warnings = export_platform_tables(dsn, selected_tables)
        synced["platform_tables"] = tables
        synced["warnings"].extend(
            {"step": "export-platform-tables", **warning} for warning in table_warnings
        )

    if args.dump_platform and dsn:
        try:
            dump_path = export_platform_dump(dsn)
            synced["platform_dump"] = {
                "path": str(dump_path.relative_to(BASE_DIR)),
                "bytes": dump_path.stat().st_size,
            }
        except subprocess.CalledProcessError as exc:
            synced["warnings"].append(
                {
                    "step": "dump-platform",
                    "error": (exc.stderr or exc.stdout or str(exc)).strip(),
                }
            )

    refresh_source_inventory()
    output_path = MANIFESTS_DIR / "sync_manifest.json"
    output_path.write_text(json.dumps(synced, indent=2) + "\n")
    print(f"Wrote {output_path}")


if __name__ == "__main__":
    main()
