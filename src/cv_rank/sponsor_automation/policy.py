from __future__ import annotations

import re
from typing import Any, Mapping, Sequence

STRICT_EVIDENCE_CLAIM_LABELS: frozenset[str] = frozenset(
    {
        "observed_in_code",
        "observed_in_db",
    }
)

STRICT_EVIDENCE_SOURCE_MARKERS: tuple[str, ...] = (
    "project_tech_evidence",
    "endpoint_evidence",
    "model_id",
    "sdk_evidence",
    "submission",
    "repo",
    "code",
)

_ENDPOINT_USAGE_RE = re.compile(
    r"\b("
    r"endpoint|api|sdk|model\s*-?\s*id|used\s+gemini|used\s+temporal|used\s+llamaindex|"
    r"used\s+agno|used\s+antigravity|openai|anthropic|tool\s+usage"
    r")\b",
    flags=re.IGNORECASE,
)

_RATIO_REFERENCE_RE = re.compile(
    r"(?P<n1>\d+(?:\.\d+)?)\s*(?:/|out\s+of|of)\s*(?P<n2>\d+(?:\.\d+)?)",
    flags=re.IGNORECASE,
)


def _coerce_mapping(item: Any) -> Mapping[str, Any]:
    if isinstance(item, Mapping):
        return item
    if hasattr(item, "__dict__"):
        return vars(item)
    return {}


def _claim_id(claim: Mapping[str, Any], index: int) -> str:
    for key in ("id", "claim_id", "metric_id", "metric_name"):
        value = claim.get(key)
        if value is not None and str(value).strip():
            return str(value)
    return f"claim_{index}"


def _claim_text(claim: Mapping[str, Any]) -> str:
    for key in ("claim", "claim_text", "headline", "title", "text", "message"):
        value = claim.get(key)
        if value is not None and str(value).strip():
            return str(value).strip()
    return ""


def _safe_float(value: Any) -> float | None:
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    s = str(value).strip().replace(",", "")
    if not s:
        return None
    if s.endswith("%"):
        s = s[:-1].strip()
    try:
        return float(s)
    except ValueError:
        return None


def _has_strict_evidence(claim: Mapping[str, Any]) -> bool:
    strict_flag = claim.get("strict_evidence")
    if isinstance(strict_flag, bool) and strict_flag:
        return True

    evidence_tier = str(claim.get("evidence_tier") or "").strip().lower()
    if evidence_tier in {"strict", "tier_a", "high"}:
        return True

    claim_label = str(claim.get("claim_label") or "").strip().lower()
    if claim_label in STRICT_EVIDENCE_CLAIM_LABELS:
        return True

    source = str(claim.get("source") or claim.get("source_table_or_file") or "").lower()
    if any(marker in source for marker in STRICT_EVIDENCE_SOURCE_MARKERS):
        return True

    notes = str(claim.get("notes") or "").lower()
    if "strict evidence" in notes or "endpoint evidence" in notes:
        return True

    return False


def _is_endpoint_usage_claim(text: str) -> bool:
    return bool(_ENDPOINT_USAGE_RE.search(text))


def _extract_ratio_reference(text: str) -> tuple[float, float] | None:
    match = _RATIO_REFERENCE_RE.search(text)
    if not match:
        return None
    left = _safe_float(match.group("n1"))
    right = _safe_float(match.group("n2"))
    if left is None or right is None:
        return None
    return left, right


def _unsupported_numerator_denominator(claim: Mapping[str, Any], text: str) -> tuple[bool, dict[str, Any]]:
    numerator = _safe_float(claim.get("numerator"))
    denominator = _safe_float(claim.get("denominator"))

    ratio_ref = _extract_ratio_reference(text)
    mentions_ratio = ratio_ref is not None or "%" in text or "numerator" in text.lower() or "denominator" in text.lower()

    if not mentions_ratio:
        return False, {}

    if numerator is None or denominator is None or denominator <= 0:
        return True, {
            "numerator": numerator,
            "denominator": denominator,
            "reason": "missing_or_non_positive_denominator",
        }

    if numerator < 0 or numerator > denominator:
        return True, {
            "numerator": numerator,
            "denominator": denominator,
            "reason": "numerator_out_of_bounds",
        }

    if ratio_ref is not None:
        ref_num, ref_den = ratio_ref
        if abs(ref_num - numerator) > 1e-6 or abs(ref_den - denominator) > 1e-6:
            return True, {
                "numerator": numerator,
                "denominator": denominator,
                "referenced_numerator": ref_num,
                "referenced_denominator": ref_den,
                "reason": "text_reference_mismatch",
            }

    return False, {}


def run_policy_checks(
    claims: Sequence[Mapping[str, Any] | Any],
) -> dict[str, Any]:
    """
    Enforce sponsor policy gates.

    Policy failures are blocking and should prevent claim publication.
    """

    claim_rows: list[Mapping[str, Any]] = [_coerce_mapping(c) for c in claims]
    blocked: list[dict[str, Any]] = []
    allowed: list[dict[str, Any]] = []

    for index, claim in enumerate(claim_rows):
        claim_id = _claim_id(claim, index)
        text = _claim_text(claim)

        violations: list[dict[str, Any]] = []

        if _is_endpoint_usage_claim(text) and not _has_strict_evidence(claim):
            violations.append(
                {
                    "code": "endpoint_usage_requires_strict_evidence",
                    "reason": "Endpoint/tool usage claim lacks strict evidence marker.",
                    "details": {
                        "claim_label": claim.get("claim_label"),
                        "source": claim.get("source") or claim.get("source_table_or_file"),
                    },
                }
            )

        unsupported_ratio, ratio_details = _unsupported_numerator_denominator(claim, text)
        if unsupported_ratio:
            violations.append(
                {
                    "code": "unsupported_numerator_denominator_reference",
                    "reason": "Claim contains unsupported numerator/denominator reference.",
                    "details": ratio_details,
                }
            )

        if violations:
            blocked.append(
                {
                    "claim_id": claim_id,
                    "status": "blocked",
                    "claim_text": text,
                    "violations": violations,
                }
            )
        else:
            allowed.append(
                {
                    "claim_id": claim_id,
                    "status": "allowed",
                }
            )

    blocked_ids = [entry["claim_id"] for entry in blocked]
    return {
        "ok": len(blocked) == 0,
        "status": "pass" if len(blocked) == 0 else "fail",
        "summary": {
            "claims_audited": len(claim_rows),
            "allowed_claims": len(allowed),
            "blocked_claims": len(blocked),
            "blocking_rules": 2,
        },
        "blocked_claim_ids": blocked_ids,
        "blocked": blocked,
        "allowed": allowed,
    }


def evaluate_policy_claims(claims: Sequence[Mapping[str, Any] | Any]) -> dict[str, Any]:
    """Alias for orchestrators using evaluate_* naming."""
    return run_policy_checks(claims)


def filter_allowed_claims(claims: Sequence[Mapping[str, Any] | Any]) -> dict[str, Any]:
    """
    Run policy and return surviving claim payloads.

    Result shape is still machine-readable for direct orchestrator usage.
    """

    claim_rows: list[Mapping[str, Any]] = [_coerce_mapping(c) for c in claims]
    report = run_policy_checks(claim_rows)
    blocked_ids = set(report["blocked_claim_ids"])
    allowed_claim_payloads = []
    for index, claim in enumerate(claim_rows):
        claim_id = _claim_id(claim, index)
        if claim_id not in blocked_ids:
            allowed_claim_payloads.append(dict(claim))

    return {
        **report,
        "allowed_claim_payloads": allowed_claim_payloads,
    }


__all__ = [
    "STRICT_EVIDENCE_CLAIM_LABELS",
    "STRICT_EVIDENCE_SOURCE_MARKERS",
    "run_policy_checks",
    "evaluate_policy_claims",
    "filter_allowed_claims",
]
