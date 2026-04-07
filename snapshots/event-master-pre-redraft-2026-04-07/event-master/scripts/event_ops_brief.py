#!/usr/bin/env python3

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path


WORKSPACE_ROOT = Path("/Users/nickita/.superset/worktrees/start/second-handstand")
EVENTS_DB = WORKSPACE_ROOT / ".research/events-index/events.db"
NODE_CATALOG = WORKSPACE_ROOT / "web/src/data/node-catalog.json"
NODE_EVIDENCE = WORKSPACE_ROOT / "web/src/data/node-evidence.json"
FINAL_PACKETS = WORKSPACE_ROOT / ".research/agent-evidence/final"
GOOGLE_EXPORT = WORKSPACE_ROOT / ".research/events-google-export"


TASK_FAMILIES = {
    "slide_deck": {
        "keywords": ["slides", "slide deck", "opening slides", "closing slides", "winner slides", "finalist slides", "deck"],
        "artifact_queries": ["slides", "opening slides", "closing slides", "run of show", "poster"],
        "template_query": "slides template",
        "example_query": "slides",
        "node_query": "slides",
        "db_checks": ["PlatformEvent"],
        "next_actions": [
            "Find the strongest prior opening or closing deck precedent instead of designing from scratch.",
            "Pull the event poster, OG image, or live event media before looking for generic logos.",
            "Use the run of show and slide-specific checklist rows to determine what belongs on each slide.",
            "Build the deck locally, render previews, and inspect the cover before importing to Google Slides.",
        ],
    },
    "participant_comms": {
        "keywords": ["details page", "participant resources", "resource blast", "blast", "participant guide", "hacker resources", "reminder"],
        "artifact_queries": ["details page", "participant resources", "digital checklist", "blast", "discord", "run of show"],
        "template_query": "hackathon template participant resources",
        "example_query": "participant resources",
        "node_query": "hacker resources",
        "db_checks": ["PlatformEvent", "EventReminder", "EventNotificationBlast"],
        "next_actions": [
            "Find the participant resources block in the planning doc and the matching execution-sheet row for the exact send or page.",
            "Inspect the planning doc plus the relevant execution sheets before relying on one source alone.",
            "Compare against one finished prior participant-facing artifact of the same family.",
            "Verify details page, reminders, or notification blasts in the platform DB if the task touches live state.",
        ],
    },
    "partner_package": {
        "keywords": ["partner package", "post event", "recap", "winner email", "send to partner"],
        "artifact_queries": ["partner package", "winner email", "partner handbook", "attendee list", "project submissions"],
        "template_query": "template partner package",
        "example_query": "partner package",
        "node_query": "partner package",
        "db_checks": ["PlatformEvent", "HackathonSubmission", "EventApplicant"],
        "next_actions": [
            "Find the partner package template and the nearest completed example.",
            "Pull winners, submissions, attendee counts, gallery/media links, and any advanced attendee exports.",
            "Check whether recap/demo videos exist yet or need to be added later.",
            "Route the package through the approval node before send.",
        ],
    },
    "proposal_quote": {
        "keywords": ["proposal", "quote", "pricing", "sow", "perfect venue", "venue quote"],
        "artifact_queries": ["proposal", "pricing proposal", "quote", "perfect venue", "contract"],
        "template_query": "template proposal pricing",
        "example_query": "proposal",
        "node_query": "pricing proposal",
        "db_checks": [],
        "next_actions": [
            "Find the canonical proposal template or nearest prior proposal example.",
            "Pull venue quote, scope, pricing lines, discounts, and review context.",
            "Resolve whether this is drafting, review, client send, or update.",
            "Use raw quote/proposal bodies, not summaries, when updating the artifact.",
        ],
    },
    "event_page": {
        "keywords": ["event page", "description", "publish", "live page", "application questions"],
        "artifact_queries": ["master planning", "event page", "application questions", "poster", "social copy"],
        "template_query": "template master planning",
        "example_query": "master planning",
        "node_query": "create event page",
        "db_checks": ["PlatformEvent", "EventApplicant", "EventReminder", "EventNotificationBlast"],
        "next_actions": [
            "Find the master planning doc and the nearest completed event page example.",
            "Verify the live PlatformEvent row before claiming the page is ready or published.",
            "Check whether application questions, waiver, poster, and location visibility are set correctly.",
            "Distinguish create/update/publish from a generic copy-edit request.",
        ],
    },
    "contract_invoice": {
        "keywords": ["contract", "docusign", "invoice", "deposit", "signer", "ap contact"],
        "artifact_queries": ["contract", "docusign", "invoice", "deposit", "proposal"],
        "template_query": "template contract",
        "example_query": "contract",
        "node_query": "contract",
        "db_checks": [],
        "next_actions": [
            "Find the latest approved proposal and the nearest prior contract/invoice handoff example.",
            "Determine signer, AP contact, deposit timing, and any attached quote/SOW.",
            "Distinguish draft-contract work from post-signature invoice issuance.",
            "Use Tammy/finance handoff packets for the exact operating pattern.",
        ],
    },
    "social_copy": {
        "keywords": ["social", "twitter", "x post", "typefully", "launch post", "announcement"],
        "artifact_queries": ["social copy", "typefully", "poster", "event page", "master planning"],
        "template_query": "template social copy",
        "example_query": "social copy",
        "node_query": "social copy",
        "db_checks": ["PlatformEvent"],
        "next_actions": [
            "Find the social template, poster, and event page copy.",
            "Check the go-live timing and whether the event page is actually ready.",
            "Use one or two prior event social examples instead of writing from scratch.",
            "Separate copy drafting from publish timing and partner/client coordination.",
        ],
    },
    "judge_hacker_comms": {
        "keywords": ["judge", "judging", "hacker resources", "discord blast", "reminder", "judge logistics"],
        "artifact_queries": ["judge", "hacker resources", "discord", "reminder", "run of show"],
        "template_query": "template judge",
        "example_query": "judge",
        "node_query": "judge",
        "db_checks": ["EventReminder", "EventNotificationBlast", "HackathonSubmission"],
        "next_actions": [
            "Find the judge sheet, hacker resources, and run of show docs.",
            "Determine whether the task is prep, send, reminder, or follow-up.",
            "Verify reminders/blasts in the platform DB when the task claims comms were sent.",
            "Use the exact operational examples from prior events for timing and contents.",
        ],
    },
    "general": {
        "keywords": [],
        "artifact_queries": ["master planning", "proposal", "partner package"],
        "template_query": "template",
        "example_query": "",
        "node_query": "",
        "db_checks": [],
        "next_actions": [
            "Resolve the event folder first.",
            "Find the canonical docs and the nearest completed example.",
            "Find the owning node plus the immediate upstream and downstream steps.",
            "Verify live DB/product state if the task touches platform state.",
        ],
    },
}


ARTIFACT_FAMILIES = {
    "slide_deck": {
        "keywords": ["slides", "slide deck", "opening slides", "closing slides", "winner slides", "finalist slides", "deck"],
        "source_labels": [
            "event-specific slide row",
            "run of show / timing sheet",
            "prior slide deck precedent",
            "event media / poster / og image",
            "node packet",
        ],
        "path_keywords": ["slides", "deck", "presentation", "run of show", "poster"],
    },
    "participant_comms": {
        "keywords": [
            "details page",
            "participant resources",
            "resource blast",
            "blast",
            "reminder",
            "discord message",
            "hacker resources",
            "participant guide",
        ],
        "source_labels": [
            "execution checklist / blast calendar",
            "details page / participant resources draft",
            "prior participant-facing example",
            "discord or event-page link source",
            "node packet",
        ],
        "path_keywords": ["digital checklist", "details", "participant", "resource", "blast", "discord", "guide"],
    },
    "judge_ops": {
        "keywords": [
            "judge logistics",
            "judges",
            "judge onboarding",
            "judge email",
            "judging process",
            "judging rubric",
            "judging sheet",
        ],
        "source_labels": [
            "judge tracking / judge comms sheet",
            "judging sheet or rubric doc",
            "prior judge logistics example",
            "run of show judge timing",
            "node packet",
        ],
        "path_keywords": ["judge", "judging", "rubric", "sheet", "run of show"],
    },
    "vendor_ops": {
        "keywords": [
            "wifi",
            "venue",
            "catering",
            "cleaners",
            "av",
            "load-in",
            "load out",
            "hospitality",
            "vendor",
        ],
        "source_labels": [
            "vendor / venue checklist rows",
            "run of show operations rows",
            "planning doc logistics block",
            "prior vendor coordination example",
            "node packet",
        ],
        "path_keywords": ["vendor", "venue", "catering", "wifi", "run of show", "hospitality", "planning"],
    },
    "run_of_show_ops": {
        "keywords": [
            "run of show",
            "doors open",
            "onsite",
            "registration",
            "team huddle",
            "show ready",
            "day of",
        ],
        "source_labels": [
            "run of show sheet",
            "digital checklist timing rows",
            "staffing / onsite ops notes",
            "prior event-day ops example",
            "node packet",
        ],
        "path_keywords": ["run of show", "digital checklist", "onsite", "staff", "registration"],
    },
    "general": {
        "keywords": [],
        "source_labels": [
            "canonical planning doc",
            "execution sheet",
            "best prior example",
            "live state check",
            "node packet",
        ],
        "path_keywords": [],
    },
}


def normalize(text: str | None) -> str:
    return (text or "").strip()


def tokenize(text: str) -> list[str]:
    return [token for token in re.split(r"[^A-Za-z0-9]+", text.lower()) if token]


def classify_task(request: str) -> tuple[str, dict]:
    lower = request.lower()
    scored: list[tuple[int, str]] = []
    for family, spec in TASK_FAMILIES.items():
        score = 0
        for kw in spec["keywords"]:
            if kw in lower:
                score += max(2, len(kw.split()))
        if score > 0:
            scored.append((score, family))
    if not scored:
        return "general", TASK_FAMILIES["general"]
    scored.sort(reverse=True)
    family = scored[0][1]
    return family, TASK_FAMILIES[family]


def infer_artifact_family(request: str) -> tuple[str, dict]:
    lower = request.lower()
    scored: list[tuple[int, str]] = []
    for family, spec in ARTIFACT_FAMILIES.items():
        score = 0
        for kw in spec["keywords"]:
            if kw in lower:
                score += max(2, len(kw.split()))
        if score > 0:
            scored.append((score, family))
    if not scored:
        return "general", ARTIFACT_FAMILIES["general"]
    scored.sort(reverse=True)
    family = scored[0][1]
    return family, ARTIFACT_FAMILIES[family]


def open_db():
    conn = sqlite3.connect(EVENTS_DB)
    conn.row_factory = sqlite3.Row
    return conn


def fts_query(text: str) -> str:
    tokens = tokenize(text)
    if not tokens:
        return '""'
    deduped = list(dict.fromkeys(tokens))
    return " OR ".join(f'"{token}"' for token in deduped)


def search_docs(query: str, event_hint: str | None, limit: int = 8):
    conn = open_db()
    try:
        cur = conn.cursor()
        match_query = fts_query(" ".join(part for part in [query, event_hint or ""] if part))
        sql = """
        SELECT
          f.id AS file_id,
          f.rel_path,
          f.google_url,
          f.city_guess,
          f.year_guess,
          f.sponsors_guess,
          f.event_guess,
          bm25(files_fts) AS score
        FROM files_fts
        JOIN files f ON f.id = files_fts.id
        WHERE files_fts MATCH ?
        ORDER BY score
        LIMIT ?
        """
        cur.execute(sql, (match_query, limit))
        return [dict(row) for row in cur.fetchall()]
    finally:
        conn.close()


def rank_hit(hit: dict, *, event_hint: str | None, key_phrase: str | None = None, require_template: bool = False):
    rel = (hit.get("rel_path") or "").lower()
    score = 0
    if key_phrase and key_phrase.lower() in rel:
        score += 35
    if "[template]" in rel:
        score += 30
    if require_template and "[template]" in rel:
        score += 50
    if event_hint and event_hint.lower() in rel:
        score += 20
    for phrase, weight in [("partner package", 10), ("proposal", 10), ("master planning", 8), ("winner email", 8), ("social copy", 8), ("judge", 6), ("reminder", 6)]:
        if phrase in rel:
            score += weight
    return score


def path_suffix(rel: str) -> str:
    suffix = Path(rel).suffix.lower()
    return suffix


def format_preference(rel: str, artifact_family: str, *, for_template: bool) -> int:
    rel = rel.lower()
    suffix = path_suffix(rel)
    if artifact_family == "participant_comms":
        if suffix == ".gdoc":
            return 18
        if suffix == ".gsheet":
            return 12
        if suffix in {".md", ".txt"}:
            return 10
        if suffix == ".pdf":
            return 4
        if suffix in {".png", ".jpg", ".jpeg", ".gif", ".svg"}:
            return -20
    if artifact_family == "slide_deck":
        if suffix in {".gslides", ".pptx", ".ppt", ".key"}:
            return 18
        if suffix == ".pdf":
            return 10
        if suffix in {".png", ".jpg", ".jpeg", ".gif", ".svg"}:
            return -16
    if artifact_family == "judge_ops":
        if suffix in {".gdoc", ".gsheet"}:
            return 14
        if suffix in {".md", ".txt"}:
            return 10
        if suffix == ".pdf":
            return 6
        if suffix in {".png", ".jpg", ".jpeg", ".gif", ".svg"}:
            return -18
    if artifact_family == "vendor_ops":
        if suffix in {".gdoc", ".gsheet"}:
            return 12
        if suffix in {".md", ".txt"}:
            return 10
        if suffix == ".pdf":
            return 8
        if suffix in {".png", ".jpg", ".jpeg", ".gif", ".svg"}:
            return -12
    if for_template and suffix in {".png", ".jpg", ".jpeg", ".gif", ".svg"}:
        return -8
    return 0


def artifact_rank_adjustment(rel: str, artifact_family: str) -> int:
    rel = rel.lower()
    score = 0
    if artifact_family == "participant_comms":
        if any(bad in rel for bad in ["attendee list", "partner package", "catering", "quote & menu", "menu", "utm-analytics", "github-analytics", "linkedin-analytics"]):
            score -= 25
        if any(good in rel for good in ["participant", "resource", "details page", "hacker resources", "announcement messages", "blast", "digital checklist"]):
            score += 20
    elif artifact_family == "slide_deck":
        if any(bad in rel for bad in ["social copy", "partner package", "attendee list", "catering", "quote & menu"]):
            score -= 25
        if any(good in rel for good in ["slides", "deck", "presentation", "opening", "closing", "poster"]):
            score += 20
    elif artifact_family == "judge_ops":
        if any(bad in rel for bad in ["partner package", "social copy", "attendee list"]):
            score -= 25
        if any(good in rel for good in ["judge", "judging", "rubric", "sheet", "logistics"]):
            score += 20
    elif artifact_family == "vendor_ops":
        if any(bad in rel for bad in ["partner package", "social copy", "attendee list"]):
            score -= 20
        if any(good in rel for good in ["venue", "wifi", "catering", "load-in", "cleaners", "vendor"]):
            score += 18
    return score


def template_example_adjustment(rel: str, artifact_family: str, *, for_template: bool) -> int:
    rel = rel.lower()
    score = format_preference(rel, artifact_family, for_template=for_template)
    suffix = path_suffix(rel)

    common_bad = [
        "utm-analytics",
        "github-analytics",
        "analytics",
        "attendee list",
        "quote & menu",
        "menu",
        "invoice",
        "sponsorship information form",
        "file responses",
    ]
    if any(term in rel for term in common_bad):
        score -= 18

    if artifact_family == "participant_comms":
        if suffix in {".png", ".jpg", ".jpeg", ".gif", ".svg"}:
            score -= 24
        good = [
            "participant resources",
            "hacker resources",
            "participant guide",
            "details page",
            "announcement messages",
            "blast",
            "resource",
            "guide",
        ]
        bad = [
            "qr code",
            "/media/",
            "poster",
            "attendee blast",
        ]
        if any(term in rel for term in good):
            score += 18
        if any(term in rel for term in bad):
            score -= 34
        if for_template and "[template]" in rel:
            score += 14
    elif artifact_family == "slide_deck":
        if suffix in {".png", ".jpg", ".jpeg", ".gif", ".svg"}:
            score -= 28
        good = [
            "hackathon",
            "opening",
            "closing",
            "slides",
            "slide deck",
            "presentation",
            "winner",
            "finalist",
            "[day of] presentation",
        ]
        bad = [
            "career fair",
            "judge sheet",
            "attendee list",
            "social copy",
            "qr code",
        ]
        if any(term in rel for term in good):
            score += 16
        if any(term in rel for term in bad):
            score -= 18
    elif artifact_family == "judge_ops":
        good = [
            "judge logistics",
            "judging rubric",
            "judge onboarding",
            "judge sheet",
            "judge comms",
        ]
        bad = [
            "poster",
            "social copy",
            "attendee list",
            "qr code",
        ]
        if any(term in rel for term in good):
            score += 16
        if any(term in rel for term in bad):
            score -= 18
    elif artifact_family == "vendor_ops":
        good = [
            "wifi",
            "venue",
            "catering",
            "hospitality",
            "cleaners",
            "load-in",
            "load out",
        ]
        bad = [
            "poster",
            "social copy",
            "qr code",
            "attendee list",
        ]
        if any(term in rel for term in good):
            score += 14
        if any(term in rel for term in bad):
            score -= 16

    return score


def choose_template_and_examples(
    template_hits: list[dict],
    example_hits: list[dict],
    event_hint: str | None,
    key_phrase: str | None,
    artifact_family: str,
):
    scored_templates = [
        (
            rank_hit(hit, event_hint=event_hint, key_phrase=key_phrase, require_template=True)
            + artifact_rank_adjustment(hit.get("rel_path", ""), artifact_family)
            + template_example_adjustment(hit.get("rel_path", ""), artifact_family, for_template=True),
            hit,
        )
        for hit in template_hits
    ]
    ranked_templates = sorted(scored_templates, key=lambda item: (-item[0], item[1].get("rel_path", "")))
    template = next((hit for score, hit in ranked_templates if score > 0), None)

    scored_examples = [
        (
            rank_hit(hit, event_hint=event_hint, key_phrase=key_phrase)
            + artifact_rank_adjustment(hit.get("rel_path", ""), artifact_family)
            + template_example_adjustment(hit.get("rel_path", ""), artifact_family, for_template=False),
            hit,
        )
        for hit in example_hits
    ]
    ranked_examples = sorted(scored_examples, key=lambda item: (-item[0], item[1].get("rel_path", "")))
    examples = []
    seen = {template["file_id"]} if template else set()
    for score, hit in ranked_examples:
        if score <= 0:
            continue
        if hit["file_id"] in seen:
            continue
        seen.add(hit["file_id"])
        examples.append(hit)
    return template, examples[:4]


def score_doc_for_artifact_family(hit: dict, artifact_family: str, event_hint: str | None) -> int:
    spec = ARTIFACT_FAMILIES.get(artifact_family, ARTIFACT_FAMILIES["general"])
    rel = (hit.get("rel_path") or "").lower()
    score = 0
    if event_hint and event_hint.lower() in rel:
        score += 6
    if "master checklist" in rel:
        score += 5
    if "digital checklist" in rel:
        score += 8
    if "run of show" in rel:
        score += 6
    for keyword in spec.get("path_keywords", []):
        if keyword in rel:
            score += 5
    if "[template]" in rel:
        score += 1
    if "master planning" in rel:
        score += 2
    if artifact_family == "participant_comms":
        if any(good in rel for good in ["announcement messages", "participant announcement", "hacker resources", "participant guide"]):
            score += 7
        if any(bad in rel for bad in ["proposals/", "proposal", "file responses", "sponsorship information form", "/media/"]):
            score -= 10
    elif artifact_family == "slide_deck":
        if "hackathon" in rel:
            score += 5
        if any(good in rel for good in ["opening", "closing", "[day of] presentation"]):
            score += 6
        if "career fair" in rel:
            score -= 8
    return score


def choose_operational_artifacts(candidate_docs: list[dict], artifact_family: str, event_hint: str | None, limit: int = 5) -> list[dict]:
    ranked = sorted(
        candidate_docs,
        key=lambda hit: (
            -(
                score_doc_for_artifact_family(hit, artifact_family, event_hint)
                + template_example_adjustment(hit.get("rel_path", ""), artifact_family, for_template=False)
            ),
            hit.get("rel_path", ""),
        ),
    )
    picked: list[dict] = []
    seen: set[str] = set()
    for hit in ranked:
        rel = hit.get("rel_path", "")
        if not rel or rel in seen:
            continue
        combined_score = score_doc_for_artifact_family(hit, artifact_family, event_hint) + template_example_adjustment(
            rel,
            artifact_family,
            for_template=False,
        )
        if combined_score <= 0:
            continue
        seen.add(rel)
        picked.append(
            {
                "rel_path": rel,
                "google_url": hit.get("google_url"),
                "event_guess": hit.get("event_guess"),
                "why": artifact_reason(rel, artifact_family),
            }
        )
        if len(picked) >= limit:
            break
    return picked


def artifact_reason(rel_path: str, artifact_family: str) -> str:
    lower = rel_path.lower()
    if "run of show" in lower:
        return "run-of-show execution detail"
    if "digital checklist" in lower:
        return "step-by-step digital execution sheet"
    if "checklist" in lower:
        return "step-by-step operational checklist"
    if "judge" in lower or "judging" in lower:
        return "judge-specific operating artifact"
    if "slides" in lower or "presentation" in lower or "deck" in lower:
        return "slide precedent or slide-specific source"
    if "details" in lower or "participant" in lower or "resource" in lower:
        return "participant-facing artifact source"
    if "vendor" in lower or "venue" in lower or "catering" in lower or "wifi" in lower:
        return "vendor / venue operating source"
    if "master planning" in lower:
        return "canonical planning source"
    return f"{artifact_family.replace('_', ' ')} source"


def build_source_stack(artifact_family: str) -> list[str]:
    return ARTIFACT_FAMILIES.get(artifact_family, ARTIFACT_FAMILIES["general"])["source_labels"]


def search_nodes(query: str, limit: int = 5):
    catalog = json.loads(NODE_CATALOG.read_text())
    evidence = json.loads(NODE_EVIDENCE.read_text()).get("evidenceByNodeId", {})
    tokens = tokenize(query)
    results = []
    for node in catalog["nodes"]:
        node_id = node["nodeId"]
        ev = evidence.get(node_id, {})
        text = "\n".join(
            [
                normalize(node.get("label")),
                normalize(node.get("title")),
                normalize(node.get("summary")),
                " ".join(node.get("bullets", []) or []),
                " ".join(node.get("connectedLabels", []) or []),
                normalize(ev.get("whatThisEvidenceShows")),
                " ".join(ev.get("bullets", []) or []),
            ]
        ).lower()
        score = sum(6 if token in normalize(node.get("title")).lower() else 4 if token in normalize(node.get("label")).lower() else 1 if token in text else 0 for token in tokens)
        if score <= 0:
            continue
        results.append(
            {
                "score": score,
                "nodeId": node_id,
                "label": node.get("label"),
                "summary": node.get("summary"),
                "neighbors": (node.get("connectedLabels") or [])[:6],
                "packetSectionHeadings": (node.get("packetSectionHeadings") or [])[:6],
                "topReferences": (node.get("topReferences") or [])[:4],
            }
        )
    results.sort(key=lambda item: (-item["score"], item["nodeId"]))
    return results[:limit]


def search_packets(query: str, limit: int = 4):
    tokens = tokenize(query)
    hits = []
    for path in FINAL_PACKETS.glob("*.md"):
        text = path.read_text(encoding="utf-8", errors="ignore")
        lower = text.lower()
        score = sum(1 for token in tokens if token in lower or token in path.name.lower())
        if score <= 0:
            continue
        excerpt = ""
        for token in tokens:
            idx = lower.find(token)
            if idx >= 0:
                excerpt = text[max(0, idx - 160): min(len(text), idx + 360)].strip()
                break
        hits.append({"score": score, "path": str(path), "name": path.name, "excerpt": excerpt})
    hits.sort(key=lambda item: (-item["score"], item["name"]))
    return hits[:limit]


def export_files_for_dir(doc_dir: Path) -> list[Path]:
    files: list[Path] = []
    for candidate in [doc_dir / "indexed.txt", doc_dir / "body.txt", doc_dir / "slides.txt"]:
        if candidate.exists():
            files.append(candidate)
    sheets_dir = doc_dir / "sheets"
    if sheets_dir.exists():
        files.extend(sorted(sheets_dir.glob("*.csv")))
    return files


def search_export_hits(query: str, event_hint: str | None, artifact_family: str, limit: int = 6) -> list[dict]:
    tokens = tokenize(" ".join(part for part in [query, event_hint or "", artifact_family.replace("_", " ")] if part))
    if not GOOGLE_EXPORT.exists() or not tokens:
        return []
    hits: list[dict] = []
    for doc_dir in GOOGLE_EXPORT.iterdir():
        if not doc_dir.is_dir() or doc_dir.name == "auth":
            continue
        metadata_path = doc_dir / "metadata.json"
        metadata = {}
        if metadata_path.exists():
            try:
                metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
            except Exception:
                metadata = {}
        for file_path in export_files_for_dir(doc_dir):
            try:
                text = file_path.read_text(encoding="utf-8", errors="ignore")
            except OSError:
                continue
            lower = text.lower()
            score = 0
            for token in tokens:
                if token in lower:
                    score += 2
            score += artifact_rank_adjustment(str(file_path.relative_to(GOOGLE_EXPORT)), artifact_family)
            if score <= 0:
                continue
            first_token = next((token for token in tokens if token in lower), None)
            excerpt = ""
            if first_token:
                idx = lower.find(first_token)
                excerpt = text[max(0, idx - 120): min(len(text), idx + 320)].strip().replace("\n", " ")
            hits.append(
                {
                    "score": score,
                    "doc_id": doc_dir.name,
                    "file": str(file_path.relative_to(GOOGLE_EXPORT)),
                    "kind": metadata.get("kind"),
                    "google_url": metadata.get("google_url"),
                    "excerpt": excerpt,
                }
            )
    hits.sort(key=lambda item: (-item["score"], item["file"]))
    return hits[:limit]


def build_brief(request: str, event_hint: str | None):
    family, spec = classify_task(request)
    artifact_family, _artifact_spec = infer_artifact_family(request)

    doc_hits = []
    for artifact_query in spec["artifact_queries"]:
        doc_hits.extend(search_docs(artifact_query, event_hint, limit=5))
    deduped_docs = []
    seen = set()
    for hit in doc_hits:
        if hit["file_id"] in seen:
            continue
        seen.add(hit["file_id"])
        deduped_docs.append(hit)

    template_seed = spec.get("template_query") or "template"
    if artifact_family == "participant_comms":
        template_seed = "participant resources hacker resources details page"
    elif artifact_family == "slide_deck":
        template_seed = "slide deck opening closing presentation"
    elif artifact_family == "judge_ops":
        template_seed = "judge logistics judging rubric"
    elif artifact_family == "vendor_ops":
        template_seed = "venue wifi catering vendor"

    template_hits = search_docs(template_seed, event_hint, limit=12) + search_docs(template_seed, None, limit=8)
    example_query = " ".join(part for part in [event_hint or "", spec.get("example_query") or "", artifact_family.replace("_", " ")] if part).strip() or request
    example_hits = search_docs(example_query, event_hint, limit=10) + deduped_docs

    template, examples = choose_template_and_examples(template_hits, example_hits, event_hint, spec.get("example_query"), artifact_family)
    nodes = search_nodes(spec["node_query"] or request, limit=5)
    packets = search_packets(spec["node_query"] or request, limit=4)
    export_hits = search_export_hits(spec["node_query"] or request, event_hint, artifact_family, limit=6)

    owning_node = nodes[0] if nodes else None
    neighbors = owning_node.get("neighbors", []) if owning_node else []
    operational_artifacts = choose_operational_artifacts(deduped_docs[:10], artifact_family, event_hint)

    return {
        "request": request,
        "event_hint": event_hint,
        "task_family": family,
        "artifact_family": artifact_family,
        "template": template,
        "examples": examples,
        "candidate_docs": deduped_docs[:10],
        "operational_artifacts": operational_artifacts,
        "source_stack": build_source_stack(artifact_family),
        "candidate_nodes": nodes,
        "owning_node": owning_node,
        "adjacent_steps": neighbors,
        "packet_hits": packets,
        "export_hits": export_hits,
        "recommended_db_checks": spec["db_checks"],
        "suggested_next_actions": spec["next_actions"],
    }


def print_human(payload: dict):
    print(f'Request: {payload["request"]}')
    print(f'Task family: {payload["task_family"]}')
    print(f'Artifact family: {payload["artifact_family"]}')
    if payload.get("event_hint"):
        print(f'Event hint: {payload["event_hint"]}')
    print()

    if payload.get("source_stack"):
        print("Recommended source stack:")
        for item in payload["source_stack"]:
            print(f'- {item}')
        print()

    template = payload.get("template")
    print("Template:")
    if template:
        print(f'- {template["rel_path"]}')
        if template.get("google_url"):
            print(f'  url: {template["google_url"]}')
    else:
        print("- No clear template hit found")
    print()

    print("Best examples:")
    for item in payload.get("examples", [])[:4]:
        print(f'- {item["rel_path"]}')
        if item.get("google_url"):
            print(f'  url: {item["google_url"]}')
    print()

    if payload.get("operational_artifacts"):
        print("Operational artifacts to inspect first:")
        for item in payload["operational_artifacts"]:
            print(f'- {item["rel_path"]}')
            print(f'  why: {item["why"]}')
            if item.get("google_url"):
                print(f'  url: {item["google_url"]}')
        print()

    print("Likely owning node:")
    owning = payload.get("owning_node")
    if owning:
        print(f'- {owning["nodeId"]} — {owning["label"]}')
        if owning.get("summary"):
            print(f'  summary: {owning["summary"]}')
    else:
        print("- No strong node hit found")
    print()

    if payload.get("adjacent_steps"):
        print("Adjacent steps:")
        for step in payload["adjacent_steps"][:6]:
            print(f'- {step}')
        print()

    print("Relevant packets:")
    for pkt in payload.get("packet_hits", [])[:4]:
        print(f'- {pkt["name"]}')
        print(f'  path: {pkt["path"]}')
    print()

    if payload.get("export_hits"):
        print("Exact exported body hits:")
        for item in payload["export_hits"][:6]:
            print(f'- {item["file"]}')
            if item.get("google_url"):
                print(f'  url: {item["google_url"]}')
            if item.get("excerpt"):
                print(f'  excerpt: {item["excerpt"][:260]}')
        print()

    if payload.get("recommended_db_checks"):
        print("Recommended DB checks:")
        for check in payload["recommended_db_checks"]:
            print(f'- {check}')
        print()

    print("Suggested next actions:")
    for action in payload.get("suggested_next_actions", []):
        print(f'- {action}')


def main():
    parser = argparse.ArgumentParser(description="Turn an event-ops request into docs, nodes, packets, and next actions")
    parser.add_argument("request")
    parser.add_argument("--event")
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    payload = build_brief(args.request, args.event)
    if args.json:
        print(json.dumps(payload, indent=2))
        return
    print_human(payload)


if __name__ == "__main__":
    main()
