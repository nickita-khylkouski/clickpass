"""Bridge legacy enrichers into the v2 pipeline."""

from __future__ import annotations

from typing import Any

from cv_rank.enrichment.exa_enricher import enrich_from_exa
from cv_rank.enrichment.github_api import enrich_from_github_api
from cv_rank.enrichment.local_csv import enrich_from_local_csv
from cv_rank.enrichment.platform_db import enrich_from_platform_db
from cv_rank.enrichment.supabase import enrich_from_supabase


def run_legacy_enrichment(people: list[dict[str, Any]], config: dict[str, Any]) -> list[dict[str, Any]]:
    """Run enabled legacy enrichers in v1 order."""
    enriched = people
    enrichment = config.get("enrichment", {})
    if enrichment.get("supabase", {}).get("enabled"):
        enriched = enrich_from_supabase(enriched, config)
    if enrichment.get("github", {}).get("enabled"):
        enriched = enrich_from_github_api(enriched, config)
    if enrichment.get("platform_db", {}).get("enabled"):
        enriched = enrich_from_platform_db(enriched, config)
    if enrichment.get("local_csv", {}).get("enabled"):
        enriched = enrich_from_local_csv(enriched, config)
    if enrichment.get("exa", {}).get("enabled"):
        enriched = enrich_from_exa(enriched, config)
    return enriched
