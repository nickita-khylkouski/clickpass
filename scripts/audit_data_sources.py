from __future__ import annotations

import json
import os
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.request import Request, urlopen

import psycopg2
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent
LOCAL_DATA_DIR = BASE_DIR / "local_data"
MANIFESTS_DIR = LOCAL_DATA_DIR / "manifests"


def load_env() -> None:
    load_dotenv(BASE_DIR / ".env")


@dataclass
class SourceSummary:
    available: bool
    notes: str


def platform_db_summary() -> dict:
    dsn = os.environ.get("PLATFORM_DATABASE_URL", "").strip()
    if not dsn:
        return {"source": asdict(SourceSummary(False, "PLATFORM_DATABASE_URL not configured"))}

    conn = psycopg2.connect(dsn)
    try:
        cur = conn.cursor()
        cur.execute(
            """
            select current_database(), pg_database_size(current_database())
            """
        )
        db_name, db_bytes = cur.fetchone()
        cur.execute(
            """
            select
              schemaname,
              relname,
              pg_total_relation_size(format('%I.%I', schemaname, relname)) as bytes
            from pg_stat_user_tables
            order by bytes desc
            limit 25
            """
        )
        top_tables = [
            {"schema": schema, "table": table, "bytes": int(size_bytes)}
            for schema, table, size_bytes in cur.fetchall()
        ]
    finally:
        conn.close()

    estimated_custom_dump_bytes = int(db_bytes * 0.45)
    return {
        "source": asdict(SourceSummary(True, "Platform Postgres available via PLATFORM_DATABASE_URL")),
        "database_name": db_name,
        "database_bytes": int(db_bytes),
        "estimated_custom_dump_bytes": estimated_custom_dump_bytes,
        "top_tables": top_tables,
    }


def supabase_summary() -> dict:
    url = os.environ.get("SUPABASE_URL", "").strip()
    key = os.environ.get("SUPABASE_KEY", "").strip()
    available = bool(url and key)
    return {
        "source": asdict(
            SourceSummary(
                available,
                "Supabase REST/API credentials available" if available else "SUPABASE_URL/SUPABASE_KEY not fully configured",
            )
        ),
        "same_project_as_platform_db": True if available else None,
    }


def run_posthog_hogql(query: str, *, name: str) -> dict:
    api_host = os.environ.get("POSTHOG_API_HOST", "").rstrip("/")
    ingest_host = os.environ.get("POSTHOG_HOST", "").rstrip("/")
    if not api_host and ingest_host == "https://us.i.posthog.com":
        api_host = "https://us.posthog.com"
    project_id = os.environ.get("POSTHOG_PROJECT_ID", "").strip()
    api_key = os.environ.get("POSTHOG_API_KEY", "").strip()
    if not api_host or not project_id or not api_key:
        raise ValueError("Missing PostHog API configuration")

    payload = json.dumps(
        {
            "query": {"kind": "HogQLQuery", "query": query},
            "name": name,
        }
    ).encode()
    req = Request(
        f"{api_host}/api/projects/{project_id}/query/",
        data=payload,
        method="POST",
    )
    req.add_header("Authorization", f"Bearer {api_key}")
    req.add_header("Content-Type", "application/json")
    with urlopen(req, timeout=120) as resp:
        return json.loads(resp.read().decode())


def posthog_summary() -> dict:
    try:
        total = run_posthog_hogql(
            "select count() from events where timestamp >= now() - interval 365 day",
            name="cv-rank-posthog-total-year",
        )
    except Exception as exc:
        return {"source": asdict(SourceSummary(False, f"PostHog unavailable: {exc}"))}

    total_events = int(total["results"][0][0])
    # Rough planning estimate only. Raw JSON event exports vary a lot.
    estimated_raw_bytes = total_events * 900
    estimated_gzip_bytes = int(estimated_raw_bytes * 0.35)
    return {
        "source": asdict(SourceSummary(True, "PostHog API available")),
        "events_last_365d": total_events,
        "estimated_raw_export_bytes": estimated_raw_bytes,
        "estimated_gzip_export_bytes": estimated_gzip_bytes,
    }


def local_artifact_summary() -> dict:
    results_dir = BASE_DIR / "results"
    data_dir = BASE_DIR / "data"
    interesting = [
        results_dir / "signup_velocity.csv",
        results_dir / "engagement_profiles.csv",
        results_dir / "application_velocity.csv",
        results_dir / "posthog_event_velocity.csv",
    ]
    files = []
    for path in interesting:
        if path.exists():
            files.append({"path": str(path.relative_to(BASE_DIR)), "bytes": path.stat().st_size})
    results_bytes = sum(path.stat().st_size for path in results_dir.rglob("*") if path.is_file()) if results_dir.exists() else 0
    data_bytes = sum(path.stat().st_size for path in data_dir.rglob("*") if path.is_file()) if data_dir.exists() else 0
    return {
        "results_bytes": results_bytes,
        "data_bytes": data_bytes,
        "tracked_files": files,
    }


def build_manifest() -> dict:
    return {
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "sources": {
            "platform_db": platform_db_summary(),
            "supabase": supabase_summary(),
            "posthog": posthog_summary(),
        },
        "local_artifacts": local_artifact_summary(),
        "notes": {
            "normal_db": "No separate DATABASE_URL is configured in this repo. Supabase and PLATFORM_DATABASE_URL point to the same underlying platform data source.",
            "raw_posthog": "The PostHog size estimate is approximate. Full raw export is much larger than the current derived artifacts.",
        },
    }


def main() -> None:
    load_env()
    MANIFESTS_DIR.mkdir(parents=True, exist_ok=True)
    manifest = build_manifest()
    output_path = MANIFESTS_DIR / "source_inventory.json"
    output_path.write_text(json.dumps(manifest, indent=2) + "\n")

    print(f"Wrote {output_path}")
    platform = manifest["sources"]["platform_db"]
    posthog = manifest["sources"]["posthog"]
    if platform["source"]["available"]:
        print(f"Platform DB: {platform['database_bytes']} bytes")
        print(f"Estimated pg_dump --format=custom size: {platform['estimated_custom_dump_bytes']} bytes")
    if posthog["source"]["available"]:
        print(f"PostHog events (365d): {posthog['events_last_365d']}")
        print(f"Estimated raw export size: {posthog['estimated_raw_export_bytes']} bytes")
        print(f"Estimated gzipped export size: {posthog['estimated_gzip_export_bytes']} bytes")


if __name__ == "__main__":
    main()
