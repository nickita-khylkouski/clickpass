#!/usr/bin/env python3
"""Single-person show-up prediction + leakage-safe event holdout backtest.

This script uses:
- ML model: logistic regression (numpy) with per-fold retraining
- Empirical bucket model (interpretable fallback)
- Existing heuristic model from cv_rank.waves.show_rate_model

Backtest protocol:
- Leave-one-event-out (LOEO)
- For each held-out event: train on all other events only
- Features are history-safe (prior counts only from earlier events)
"""

from __future__ import annotations

import argparse
from collections import defaultdict
from dataclasses import dataclass
from datetime import date, datetime
import hashlib
import json
import math
import os
from pathlib import Path
import random
import re
from typing import Any

import numpy as np

try:
    import psycopg2
except ImportError as exc:  # pragma: no cover
    raise SystemExit("psycopg2 is required. Install with: uv pip install psycopg2-binary") from exc

from cv_rank.waves.show_rate_model import predict_show_rate


# -----------------------------
# Core helpers
# -----------------------------

def _load_env(repo_root: Path) -> None:
    env_path = repo_root / ".env"
    if not env_path.exists():
        return
    for line in env_path.read_text().splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#") or "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def _clamp(x: float, lo: float, hi: float) -> float:
    return max(lo, min(hi, x))


def _logit(p: float) -> float:
    p = _clamp(p, 1e-6, 1.0 - 1e-6)
    return math.log(p / (1.0 - p))


def _inv_logit(x: float) -> float:
    return 1.0 / (1.0 + math.exp(-x))


def _parse_date(raw: str) -> date:
    return datetime.strptime(raw.strip(), "%Y-%m-%d").date()


def _city_base(raw: str | None) -> str:
    if raw is None:
        return "Unknown"
    s = raw.strip()
    if not s:
        return "Unknown"
    low = s.lower()
    if low in {"virtual", "online", "tbd", "unknown"}:
        return "Unknown"
    base = s.split(",", 1)[0].strip()
    aliases = {
        "san francisco": "San Francisco",
        "new york": "New York",
        "london": "London",
        "paris": "Paris",
        "bengaluru": "Bengaluru",
        "menlo park": "Menlo Park",
        "mountain view": "Mountain View",
        "chennai": "Chennai",
    }
    return aliases.get(base.lower(), base)


def _theme_bucket(title: str | None) -> str:
    if not title:
        return "other"
    t = title.lower()
    rules = [
        ("voice-audio", [r"voice", r"audio", r"speech"]),
        ("agents", [r"\bagent\b", r"\bagents\b", r"assistant", r"autonomous"]),
        ("model-branded", [r"gemini", r"claude", r"gpt", r"llama", r"mistral"]),
        ("infra-devtools", [r"infra", r"deployment", r"on-device", r"edge", r"tooling", r"api"]),
        ("workshop", [r"workshop", r"co-?working", r"bootcamp"]),
        ("fintech", [r"fintech", r"finance", r"trading", r"payments"]),
        ("general-hackathon", [r"hackathon", r"hack"]),
    ]
    for name, patterns in rules:
        if any(re.search(p, t) for p in patterns):
            return name
    return "other"


def _timing_bucket(days_before: float) -> str:
    d = float(days_before)
    if d < 1:
        return "0-1d"
    if d < 3:
        return "1-3d"
    if d < 7:
        return "3-7d"
    if d < 14:
        return "7-14d"
    if d < 21:
        return "14-21d"
    return "21+d"


def _prior_rate_bucket(prior_approved: int, prior_checked: int) -> str:
    if prior_approved <= 0:
        return "no-history"
    rate = prior_checked / prior_approved
    if rate == 0:
        return "0%"
    if rate < 0.25:
        return "1-24%"
    if rate < 0.50:
        return "25-49%"
    if rate < 0.75:
        return "50-74%"
    return "75-100%"


def _prior_count_bucket(prior_approved: int) -> str:
    n = int(prior_approved)
    if n <= 0:
        return "0"
    if n == 1:
        return "1"
    if n <= 3:
        return "2-3"
    if n <= 7:
        return "4-7"
    return "8+"


def _capacity_bucket(capacity: int | None) -> str:
    if capacity is None or int(capacity) <= 0:
        return "unknown"
    c = int(capacity)
    if c < 100:
        return "<100"
    if c < 250:
        return "100-249"
    if c < 500:
        return "250-499"
    return "500+"


# -----------------------------
# Data structures
# -----------------------------

@dataclass
class CandidateFeatures:
    days_before_event: float
    prior_approved: int
    prior_checked: int
    city: str
    event_title: str
    same_day_same_city_events: int
    event_day_of_week: str
    event_capacity: int | None
    profile_description: str
    score: float | None


@dataclass
class CandidateRow:
    event_id: str
    event_date: date
    checked_in: int
    features: CandidateFeatures


@dataclass
class Stat:
    n: int
    rate: float


@dataclass
class EmpiricalCalibration:
    base_rate: float
    tables: dict[str, dict[str, Stat]]


# -----------------------------
# Data extraction (no leakage features)
# -----------------------------

def _fetch_dataset(conn: Any, *, min_event_checkins: int) -> list[CandidateRow]:
    cur = conn.cursor()
    cur.execute(
        """
        WITH clean AS (
          SELECT
            ea."userId" AS user_id,
            ea."checkedIn" AS checked_in,
            ea."createdAt" AS created_at,
            pe."startDateTime" AS start_at,
            pe."startDateTime"::date AS event_date,
            pe.id AS event_id,
            pe.city AS city_raw,
            pe.title AS title,
            pe.capacity AS capacity,
            up.description AS profile_description,
            COALESCE(NULLIF(split_part(pe.city, ',', 1), ''), 'Unknown') AS city_base,
            pe."startDateTime"::date AS event_date_base
          FROM "EventApplicant" ea
          JOIN "PlatformEvent" pe ON pe.id = ea."eventId"
          LEFT JOIN "UserProfile" up ON up."userId" = ea."userId"
          WHERE pe."isPlatformHackathon" = true
            AND ea.status = 'approved'
            AND ea."createdAt" < pe."startDateTime"
        ),
        event_context AS (
          SELECT
            event_id,
            event_date,
            city_base,
            SUM(CASE WHEN checked_in THEN 1 ELSE 0 END) AS event_checked
          FROM clean
          GROUP BY event_id, event_date, city_base
        ),
        collisions AS (
          SELECT
            event_id,
            event_checked,
            COUNT(*) OVER (PARTITION BY event_date, city_base) AS same_day_same_city_events
          FROM event_context
        ),
        with_event_quality AS (
          SELECT
            c.*,
            x.event_checked,
            x.same_day_same_city_events
          FROM clean c
          JOIN collisions x ON x.event_id = c.event_id
        ),
        ranked AS (
          SELECT
            event_id,
            checked_in,
            event_date,
            EXTRACT(EPOCH FROM (start_at - created_at))/86400.0 AS days_before,
            ROW_NUMBER() OVER (PARTITION BY user_id ORDER BY start_at, event_id) - 1 AS prior_approved,
            COALESCE(
              SUM(CASE WHEN checked_in THEN 1 ELSE 0 END) OVER (
                PARTITION BY user_id ORDER BY start_at, event_id
                ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING
              ), 0
            ) AS prior_checked,
            city_raw,
            title,
            capacity,
            profile_description,
            TRIM(TO_CHAR(start_at, 'Dy')) AS dow,
            same_day_same_city_events,
            event_checked
          FROM with_event_quality
        )
        SELECT
          event_id::text,
          checked_in,
          event_date,
          days_before,
          prior_approved,
          prior_checked,
          city_raw,
          title,
          capacity,
          profile_description,
          dow,
          same_day_same_city_events
        FROM ranked
        WHERE event_checked >= %s
        """,
        (min_event_checkins,),
    )
    rows: list[CandidateRow] = []
    for (
        event_id,
        checked,
        event_date,
        days_before,
        prior_approved,
        prior_checked,
        city_raw,
        title,
        capacity,
        profile_description,
        dow,
        overlap,
    ) in cur.fetchall():
        rows.append(
            CandidateRow(
                event_id=event_id,
                event_date=event_date,
                checked_in=1 if checked else 0,
                features=CandidateFeatures(
                    days_before_event=float(days_before),
                    prior_approved=int(prior_approved),
                    prior_checked=int(prior_checked),
                    city=_city_base(city_raw),
                    event_title=title or "",
                    same_day_same_city_events=max(1, int(overlap or 1)),
                    event_day_of_week=dow or "Unknown",
                    event_capacity=int(capacity) if capacity is not None else None,
                    profile_description=(profile_description or "").strip(),
                    score=None,
                ),
            )
        )
    cur.close()
    return rows


# -----------------------------
# Empirical bucket model
# -----------------------------

def _build_empirical_calibration(rows: list[CandidateRow], strength: float = 25.0) -> EmpiricalCalibration:
    base_rate = sum(r.checked_in for r in rows) / max(1, len(rows))

    def smooth(success: int, n: int) -> float:
        return (success + base_rate * strength) / (n + strength)

    def key_extractors(r: CandidateRow) -> dict[str, str]:
        f = r.features
        return {
            "timing": _timing_bucket(f.days_before_event),
            "prior_rate": _prior_rate_bucket(f.prior_approved, f.prior_checked),
            "prior_count": _prior_count_bucket(f.prior_approved),
            "city": f.city,
            "theme": _theme_bucket(f.event_title),
            "overlap": str(max(1, f.same_day_same_city_events)),
            "dow": f.event_day_of_week,
            "capacity": _capacity_bucket(f.event_capacity),
        }

    counters: dict[str, dict[str, list[int]]] = defaultdict(lambda: defaultdict(lambda: [0, 0]))
    for row in rows:
        y = row.checked_in
        for feature_name, key in key_extractors(row).items():
            counters[feature_name][key][0] += 1
            counters[feature_name][key][1] += y

    tables: dict[str, dict[str, Stat]] = {}
    for feature_name, by_key in counters.items():
        tables[feature_name] = {}
        for key, (n, success) in by_key.items():
            tables[feature_name][key] = Stat(n=n, rate=smooth(success, n))

    return EmpiricalCalibration(base_rate=base_rate, tables=tables)


def _empirical_predict(features: CandidateFeatures, calib: EmpiricalCalibration) -> tuple[float, list[str]]:
    base_log_odds = _logit(calib.base_rate)
    log_odds = base_log_odds
    reasons: list[str] = []

    specs = [
        ("timing", _timing_bucket(features.days_before_event), 1.00, 700),
        ("prior_rate", _prior_rate_bucket(features.prior_approved, features.prior_checked), 1.00, 700),
        ("prior_count", _prior_count_bucket(features.prior_approved), 0.35, 500),
        ("city", features.city, 0.45, 1500),
        ("theme", _theme_bucket(features.event_title), 0.45, 1000),
        ("overlap", str(max(1, features.same_day_same_city_events)), 0.30, 1000),
        ("dow", features.event_day_of_week, 0.20, 900),
        ("capacity", _capacity_bucket(features.event_capacity), 0.15, 900),
    ]

    for feature_name, key, strength, cap in specs:
        table = calib.tables.get(feature_name, {})
        stat = table.get(key, Stat(0, calib.base_rate))
        delta = _logit(stat.rate) - base_log_odds
        rel_w = _clamp(stat.n / float(cap), 0.0, 1.0)
        w = rel_w * strength
        log_odds += delta * w
        reasons.append(f"{feature_name}={key} (n={stat.n}, rate={stat.rate*100:.1f}%, weight={w:.2f})")

    p = _clamp(_inv_logit(log_odds), 0.03, 0.97)
    return p, reasons


# -----------------------------
# ML model
# -----------------------------

class FeatureBuilder:
    def __init__(self, min_cat_count: int = 20):
        self.min_cat_count = min_cat_count
        self.num_keys = [
            "days_before_log",
            "days_before_inv",
            "prior_approved_log",
            "prior_rate",
            "has_history",
            "same_day_overlap_log",
        ]
        self.cat_keys = [
            "timing",
            "prior_rate_bucket",
            "prior_count_bucket",
            "city",
            "theme",
            "dow",
            "capacity_bucket",
            "overlap_bucket",
        ]
        self.num_mean: dict[str, float] = {}
        self.num_std: dict[str, float] = {}
        self.cat_index: dict[str, dict[str, int]] = {}
        self.feature_names: list[str] = []
        self.dim = 0

    def _raw(self, f: CandidateFeatures) -> tuple[dict[str, float], dict[str, str]]:
        prior_rate = (f.prior_checked / f.prior_approved) if f.prior_approved > 0 else 0.0
        overlap = max(1, f.same_day_same_city_events)
        num = {
            "days_before_log": math.log1p(max(0.0, f.days_before_event)),
            "days_before_inv": 1.0 / (1.0 + max(0.0, f.days_before_event)),
            "prior_approved_log": math.log1p(max(0, f.prior_approved)),
            "prior_rate": prior_rate,
            "has_history": 1.0 if f.prior_approved > 0 else 0.0,
            "same_day_overlap_log": math.log1p(overlap),
        }
        cat = {
            "timing": _timing_bucket(f.days_before_event),
            "prior_rate_bucket": _prior_rate_bucket(f.prior_approved, f.prior_checked),
            "prior_count_bucket": _prior_count_bucket(f.prior_approved),
            "city": f.city,
            "theme": _theme_bucket(f.event_title),
            "dow": f.event_day_of_week,
            "capacity_bucket": _capacity_bucket(f.event_capacity),
            "overlap_bucket": str(min(overlap, 3)),
        }
        return num, cat

    def fit(self, rows: list[CandidateRow]) -> None:
        num_vals: dict[str, list[float]] = {k: [] for k in self.num_keys}
        cat_counts: dict[str, dict[str, int]] = {k: defaultdict(int) for k in self.cat_keys}

        for row in rows:
            num, cat = self._raw(row.features)
            for k in self.num_keys:
                num_vals[k].append(num[k])
            for k in self.cat_keys:
                cat_counts[k][cat[k]] += 1

        for k in self.num_keys:
            arr = np.array(num_vals[k], dtype=float)
            self.num_mean[k] = float(arr.mean())
            std = float(arr.std())
            self.num_std[k] = std if std > 1e-6 else 1.0

        self.feature_names = []
        idx = 0
        for k in self.num_keys:
            self.feature_names.append(f"num:{k}")
            idx += 1

        self.cat_index = {}
        for k in self.cat_keys:
            self.cat_index[k] = {}
            kept = [v for v, c in cat_counts[k].items() if c >= self.min_cat_count]
            kept = sorted(kept)
            for v in kept + ["__OTHER__"]:
                self.cat_index[k][v] = idx
                self.feature_names.append(f"cat:{k}={v}")
                idx += 1

        self.dim = idx

    def transform_rows(self, rows: list[CandidateRow]) -> np.ndarray:
        X = np.zeros((len(rows), self.dim), dtype=float)
        for i, row in enumerate(rows):
            X[i, :] = self.transform_one(row.features)
        return X

    def transform_one(self, f: CandidateFeatures) -> np.ndarray:
        x = np.zeros((self.dim,), dtype=float)
        num, cat = self._raw(f)

        col = 0
        for k in self.num_keys:
            x[col] = (num[k] - self.num_mean[k]) / self.num_std[k]
            col += 1

        for k in self.cat_keys:
            mapping = self.cat_index[k]
            val = cat[k]
            idx = mapping.get(val, mapping["__OTHER__"])
            x[idx] = 1.0

        return x


@dataclass
class LogisticModel:
    w: np.ndarray
    b: float

    @staticmethod
    def fit(
        X: np.ndarray,
        y: np.ndarray,
        *,
        lr: float = 0.08,
        reg: float = 1e-3,
        epochs: int = 1400,
    ) -> "LogisticModel":
        n, d = X.shape
        w = np.zeros((d,), dtype=float)
        y_mean = float(y.mean()) if len(y) else 0.5
        b = _logit(_clamp(y_mean, 1e-4, 1.0 - 1e-4))

        for _ in range(epochs):
            z = X @ w + b
            p = 1.0 / (1.0 + np.exp(-np.clip(z, -25.0, 25.0)))
            err = p - y
            grad_w = (X.T @ err) / n + reg * w
            grad_b = float(err.mean())
            w -= lr * grad_w
            b -= lr * grad_b

        return LogisticModel(w=w, b=b)

    def predict_proba(self, X: np.ndarray) -> np.ndarray:
        z = X @ self.w + self.b
        return 1.0 / (1.0 + np.exp(-np.clip(z, -25.0, 25.0)))


# -----------------------------
# Metrics
# -----------------------------

def _auc(scores: list[float], labels: list[int]) -> float:
    n = len(scores)
    pairs = sorted(zip(scores, labels), key=lambda t: t[0])
    ranks = [0.0] * n
    i = 0
    while i < n:
        j = i
        while j + 1 < n and pairs[j + 1][0] == pairs[i][0]:
            j += 1
        avg_rank = (i + j + 2) / 2.0
        for k in range(i, j + 1):
            ranks[k] = avg_rank
        i = j + 1

    pos = sum(labels)
    neg = n - pos
    if pos == 0 or neg == 0:
        return 0.5
    rank_sum_pos = sum(r for r, (_, y) in zip(ranks, pairs) if y == 1)
    return (rank_sum_pos - pos * (pos + 1) / 2.0) / (pos * neg)


def _brier(scores: list[float], labels: list[int]) -> float:
    return float(sum((p - y) ** 2 for p, y in zip(scores, labels)) / len(scores))


def _logloss(scores: list[float], labels: list[int]) -> float:
    eps = 1e-9
    total = 0.0
    for p, y in zip(scores, labels):
        p = _clamp(p, eps, 1.0 - eps)
        total += -(y * math.log(p) + (1 - y) * math.log(1 - p))
    return float(total / len(scores))


def _calibration_table(scores: list[float], labels: list[int], bins: int = 10) -> list[tuple[int, int, float, float]]:
    pairs = sorted(zip(scores, labels), key=lambda t: t[0])
    n = len(pairs)
    out: list[tuple[int, int, float, float]] = []
    for b in range(bins):
        lo = (n * b) // bins
        hi = (n * (b + 1)) // bins
        chunk = pairs[lo:hi]
        if not chunk:
            continue
        pred = sum(p for p, _ in chunk) / len(chunk)
        obs = sum(y for _, y in chunk) / len(chunk)
        out.append((b + 1, len(chunk), pred, obs))
    return out


# -----------------------------
# Other predictions
# -----------------------------

def _heuristic_predict(features: CandidateFeatures) -> float:
    person = {
        "is_first_time": features.prior_approved == 0,
        "historical_event_count": features.prior_approved,
        "days_before_event": features.days_before_event,
        "score": features.score,
    }
    if features.prior_approved > 0:
        person["historical_attendance_rate"] = features.prior_checked / features.prior_approved
    return float(predict_show_rate(person))


def _blend_probs(ml: float, emp: float, heu: float, w_ml: float, w_emp: float, w_heu: float) -> float:
    s = w_ml + w_emp + w_heu
    if s <= 0:
        return ml
    w_ml, w_emp, w_heu = w_ml / s, w_emp / s, w_heu / s
    return _clamp(w_ml * ml + w_emp * emp + w_heu * heu, 0.03, 0.97)


def _blend_probs4(
    ml: float,
    emp: float,
    heu: float,
    oai: float,
    w_ml: float,
    w_emp: float,
    w_heu: float,
    w_oai: float,
) -> float:
    s = w_ml + w_emp + w_heu + w_oai
    if s <= 0:
        return ml
    w_ml, w_emp, w_heu, w_oai = w_ml / s, w_emp / s, w_heu / s, w_oai / s
    return _clamp(w_ml * ml + w_emp * emp + w_heu * heu + w_oai * oai, 0.03, 0.97)


def _tier(p: float) -> str:
    if p >= 0.56:
        return "HIGH"
    if p >= 0.40:
        return "MEDIUM"
    return "LOW"


# -----------------------------
# DB lookups for single inference
# -----------------------------

def _fetch_user_history(conn: Any, user_id: str, event_date: date) -> tuple[int, int]:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT
          COUNT(*) FILTER (WHERE ea.status='approved') AS prior_approved,
          COUNT(*) FILTER (WHERE ea.status='approved' AND ea."checkedIn"=true) AS prior_checked
        FROM "EventApplicant" ea
        JOIN "PlatformEvent" pe ON pe.id = ea."eventId"
        WHERE pe."isPlatformHackathon" = true
          AND ea."userId" = %s
          AND pe."startDateTime" < %s
        """,
        (user_id, event_date),
    )
    row = cur.fetchone()
    cur.close()
    return int(row[0] or 0), int(row[1] or 0)


def _fetch_same_day_overlap(conn: Any, event_date: date, city: str) -> int:
    cur = conn.cursor()
    cur.execute(
        """
        SELECT COUNT(*)
        FROM "PlatformEvent" pe
        WHERE pe."isPlatformHackathon" = true
          AND pe."startDateTime"::date = %s
          AND COALESCE(NULLIF(split_part(pe.city, ',', 1), ''), 'Unknown') = %s
        """,
        (event_date, _city_base(city)),
    )
    count = int(cur.fetchone()[0] or 0)
    cur.close()
    return max(count, 1)


# -----------------------------
# OpenAI optional model
# -----------------------------

def _load_openai_client() -> Any | None:
    try:
        from openai import OpenAI  # type: ignore
    except Exception:
        return None
    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        return None
    return OpenAI(api_key=key)


def _extract_json(text: str) -> dict[str, Any]:
    try:
        return json.loads(text)
    except Exception:
        pass
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if not match:
        return {}
    try:
        return json.loads(match.group(0))
    except Exception:
        return {}


def _oai_payload(features: CandidateFeatures) -> dict[str, Any]:
    prior_rate = (features.prior_checked / features.prior_approved) if features.prior_approved > 0 else None
    return {
        "days_before_event": round(features.days_before_event, 3),
        "timing_bucket": _timing_bucket(features.days_before_event),
        "prior_approved": features.prior_approved,
        "prior_checked": features.prior_checked,
        "prior_rate": None if prior_rate is None else round(prior_rate, 4),
        "city": features.city,
        "theme": _theme_bucket(features.event_title),
        "same_day_same_city_events": features.same_day_same_city_events,
        "event_day_of_week": features.event_day_of_week,
        "event_capacity_bucket": _capacity_bucket(features.event_capacity),
        "profile_description": features.profile_description[:500],
    }


def _oai_predict(
    client: Any,
    model: str,
    features: CandidateFeatures,
    cache: dict[str, float],
) -> float:
    payload = _oai_payload(features)
    key = hashlib.sha1(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    if key in cache:
        return _clamp(float(cache[key]), 0.03, 0.97)

    system = (
        "You predict probability that an approved hackathon applicant will physically check in. "
        "Use only provided features and return strict JSON: {\"probability\": <0..1>}."
    )
    user = json.dumps(payload, separators=(",", ":"))
    try:
        resp = client.chat.completions.create(
            model=model,
            temperature=0,
            response_format={"type": "json_object"},
            messages=[
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        )
        content = resp.choices[0].message.content or "{}"
    except Exception:
        return 0.5

    obj = _extract_json(content)
    p = _clamp(float(obj.get("probability", 0.5)), 0.03, 0.97)
    cache[key] = p
    return p


def _load_oai_cache(path: Path) -> dict[str, float]:
    if not path.exists():
        return {}
    try:
        raw = json.loads(path.read_text())
        return {str(k): float(v) for k, v in raw.items()}
    except Exception:
        return {}


def _save_oai_cache(path: Path, cache: dict[str, float]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cache, indent=2, sort_keys=True))


# -----------------------------
# Backtest
# -----------------------------

def _run_backtest(rows: list[CandidateRow], args: argparse.Namespace) -> None:
    by_event: dict[str, list[CandidateRow]] = defaultdict(list)
    for row in rows:
        by_event[row.event_id].append(row)

    event_date: dict[str, date] = {eid: ev_rows[0].event_date for eid, ev_rows in by_event.items() if ev_rows}
    holdout_events = [eid for eid, ev_rows in by_event.items() if len(ev_rows) >= args.backtest_min_event_rows]
    holdout_events.sort(key=lambda eid: event_date[eid])
    if not holdout_events:
        raise SystemExit("No events meet --backtest-min-event-rows threshold")

    y_all: list[int] = []
    p_base: list[float] = []
    p_heur: list[float] = []
    p_emp: list[float] = []
    p_ml: list[float] = []
    p_oai: list[float] = []

    event_summaries: list[tuple[str, int, float, float]] = []

    rng = random.Random(args.backtest_random_seed)
    client = _load_openai_client() if args.use_openai else None
    oai_model = args.openai_model or os.environ.get("OPENAI_MODEL", "gpt-5-mini")
    oai_calls = 0
    cache_path = Path(args.openai_cache_path)
    oai_cache = _load_oai_cache(cache_path) if client is not None else {}

    for holdout in holdout_events:
        test_rows = list(by_event[holdout])
        if args.backtest_sample_per_event > 0 and len(test_rows) > args.backtest_sample_per_event:
            test_rows = rng.sample(test_rows, args.backtest_sample_per_event)

        if args.backtest_split == "temporal":
            cutoff = event_date[holdout]
            train_rows = [r for r in rows if r.event_date < cutoff and r.event_id != holdout]
        else:
            train_rows = [r for eid, rs in by_event.items() if eid != holdout for r in rs]
        if len(train_rows) < args.backtest_min_train_rows:
            continue

        y_train = np.array([r.checked_in for r in train_rows], dtype=float)
        fold_base = float(y_train.mean())

        fb = FeatureBuilder(min_cat_count=args.min_cat_count)
        fb.fit(train_rows)
        X_train = fb.transform_rows(train_rows)
        model = LogisticModel.fit(X_train, y_train, lr=args.lr, reg=args.reg, epochs=args.epochs)

        X_test = fb.transform_rows(test_rows)
        p_ml_fold = model.predict_proba(X_test).tolist()

        calib = _build_empirical_calibration(train_rows)

        fold_preds: list[float] = []
        fold_y: list[int] = []
        for row, ml_p in zip(test_rows, p_ml_fold):
            emp_p, _ = _empirical_predict(row.features, calib)
            heu_p = _heuristic_predict(row.features)
            if client is not None and oai_calls < args.openai_max_calls:
                oai_p = _oai_predict(client, oai_model, row.features, oai_cache)
                oai_calls += 1
            else:
                oai_p = 0.5
            y = row.checked_in

            y_all.append(y)
            p_base.append(fold_base)
            p_heur.append(heu_p)
            p_emp.append(emp_p)
            p_ml.append(ml_p)
            p_oai.append(oai_p)

            fold_preds.append(ml_p)
            fold_y.append(y)

        event_summaries.append((holdout, len(fold_y), float(np.mean(fold_preds)), float(np.mean(fold_y))))

    # Tune blend weights on out-of-fold predictions (grid).
    best_tuple = (args.w_ml, args.w_emp, args.w_heu, args.w_oai)
    best_ll = float("inf")
    for w_ml in [i / 10 for i in range(0, 11)]:
        for w_emp in [i / 10 for i in range(0, 11)]:
            for w_oai in [i / 10 for i in range(0, 11)]:
                w_heu = 1.0 - w_ml - w_emp - w_oai
                if w_heu < 0:
                    continue
                preds = [
                    _blend_probs4(m, e, h, o, w_ml, w_emp, w_heu, w_oai)
                    for m, e, h, o in zip(p_ml, p_emp, p_heur, p_oai)
                ]
                ll = _logloss(preds, y_all)
                if ll < best_ll:
                    best_ll = ll
                    best_tuple = (w_ml, w_emp, w_heu, w_oai)

    p_blend_default = [
        _blend_probs4(m, e, h, o, args.w_ml, args.w_emp, args.w_heu, args.w_oai)
        for m, e, h, o in zip(p_ml, p_emp, p_heur, p_oai)
    ]
    p_blend_best = [_blend_probs4(m, e, h, o, *best_tuple) for m, e, h, o in zip(p_ml, p_emp, p_heur, p_oai)]

    print("Backtest: Leave-One-Event-Out (Leakage-safe)")
    print("=" * 78)
    print(f"events_tested:               {len(holdout_events)}")
    print(f"rows_scored:                 {len(y_all)}")
    print(f"split_mode:                  {args.backtest_split}")
    print(f"sample_per_event:            {args.backtest_sample_per_event}")
    print(f"default_blend_weights:       ml={args.w_ml:.2f}, emp={args.w_emp:.2f}, heu={args.w_heu:.2f}, oai={args.w_oai:.2f}")
    print(
        "best_blend_weights(logloss): "
        f"ml={best_tuple[0]:.2f}, emp={best_tuple[1]:.2f}, heu={best_tuple[2]:.2f}, oai={best_tuple[3]:.2f}"
    )
    if client is not None:
        print(f"openai_calls:                {oai_calls}")
        print(f"openai_model:                {oai_model}")
    print()

    def _print_metrics(name: str, preds: list[float]) -> None:
        print(name)
        print(f"  auc:                       {_auc(preds, y_all):.3f}")
        print(f"  brier:                     {_brier(preds, y_all):.4f}")
        print(f"  logloss:                   {_logloss(preds, y_all):.4f}")

    _print_metrics("Baseline (fold base rate)", p_base)
    _print_metrics("Heuristic-only", p_heur)
    _print_metrics("Empirical-only", p_emp)
    _print_metrics("ML-only (logistic)", p_ml)
    if client is not None and any(p != 0.5 for p in p_oai):
        _print_metrics("OpenAI-only", p_oai)
    _print_metrics("Blend (default)", p_blend_default)
    if any(abs(a - b) > 1e-9 for a, b in zip(best_tuple, (args.w_ml, args.w_emp, args.w_heu, args.w_oai))):
        _print_metrics("Blend (best grid)", p_blend_best)

    print()
    print("Calibration (ML-only deciles)")
    print("bin\tn\tpred_mean\tobs_rate")
    for b, n, pred, obs in _calibration_table(p_ml, y_all, bins=10):
        print(f"{b}\t{n}\t{pred*100:.1f}%\t{obs*100:.1f}%")

    print()
    print("Per-event holdout summary (ML-only)")
    print("event_id\tn\tpred_avg\tobs_rate")
    for eid, n, pred_avg, obs in sorted(event_summaries, key=lambda t: t[3]):
        print(f"{eid}\t{n}\t{pred_avg*100:.1f}%\t{obs*100:.1f}%")

    if client is not None:
        _save_oai_cache(cache_path, oai_cache)


# -----------------------------
# Single-person inference
# -----------------------------

def _run_single(conn: Any, rows: list[CandidateRow], args: argparse.Namespace) -> None:
    if not args.event_date:
        raise SystemExit("--event-date is required for single prediction mode")

    event_date = _parse_date(args.event_date)
    if args.days_before_event is not None:
        days_before = float(args.days_before_event)
    elif args.application_date:
        app_date = _parse_date(args.application_date)
        days_before = max(0.0, float((event_date - app_date).days))
    else:
        raise SystemExit("Provide either --days-before-event or --application-date")

    if args.user_id:
        prior_approved, prior_checked = _fetch_user_history(conn, args.user_id, event_date)
    else:
        prior_approved = int(args.prior_approved or 0)
        prior_checked = int(args.prior_checked or 0)

    if args.same_day_overlap is not None:
        overlap = max(1, int(args.same_day_overlap))
    else:
        overlap = _fetch_same_day_overlap(conn, event_date, args.city)

    feat = CandidateFeatures(
        days_before_event=days_before,
        prior_approved=prior_approved,
        prior_checked=prior_checked,
        city=_city_base(args.city),
        event_title=args.event_title,
        same_day_same_city_events=overlap,
        event_day_of_week=event_date.strftime("%a"),
        event_capacity=args.event_capacity,
        profile_description=args.profile_description or "",
        score=args.score,
    )

    # Train ML on all available rows.
    fb = FeatureBuilder(min_cat_count=args.min_cat_count)
    fb.fit(rows)
    X_train = fb.transform_rows(rows)
    y_train = np.array([r.checked_in for r in rows], dtype=float)
    model = LogisticModel.fit(X_train, y_train, lr=args.lr, reg=args.reg, epochs=args.epochs)

    x = fb.transform_one(feat)
    p_ml = float(model.predict_proba(x.reshape(1, -1))[0])

    calib = _build_empirical_calibration(rows)
    p_emp, emp_reasons = _empirical_predict(feat, calib)

    p_heu = _heuristic_predict(feat)
    client = _load_openai_client() if args.use_openai else None
    if client is not None:
        cache = _load_oai_cache(Path(args.openai_cache_path))
        oai_model = args.openai_model or os.environ.get("OPENAI_MODEL", "gpt-5-mini")
        p_oai = _oai_predict(client, oai_model, feat, cache)
        _save_oai_cache(Path(args.openai_cache_path), cache)
    else:
        p_oai = 0.5

    p_final = _blend_probs4(p_ml, p_emp, p_heu, p_oai, args.w_ml, args.w_emp, args.w_heu, args.w_oai)

    # Top feature contributions for ML.
    contrib = x * model.w
    num_count = len(fb.num_keys)
    active = []
    for i, name in enumerate(fb.feature_names):
        if i < num_count or x[i] > 0:
            active.append((name, contrib[i]))
    top = sorted(active, key=lambda t: abs(t[1]), reverse=True)[:8]

    print("Single-Candidate Show Prediction")
    print("=" * 78)
    print(f"training_rows:                {len(rows)}")
    print(f"training_base_show_rate:      {y_train.mean()*100:.1f}%")
    print()
    print("Candidate features")
    print(f"  event_date:                 {event_date.isoformat()}")
    print(f"  days_before_event:          {days_before:.1f}")
    print(f"  city:                       {feat.city}")
    print(f"  theme:                      {_theme_bucket(feat.event_title)}")
    print(f"  day_of_week:                {feat.event_day_of_week}")
    print(f"  capacity_bucket:            {_capacity_bucket(feat.event_capacity)}")
    print(f"  same_day_overlap:           {feat.same_day_same_city_events}")
    print(f"  prior_approved:             {feat.prior_approved}")
    print(f"  prior_checked:              {feat.prior_checked}")
    if feat.score is not None:
        print(f"  score:                      {feat.score}")
    print()

    print("Prediction")
    print(f"  ml_model:                   {p_ml*100:.1f}%")
    print(f"  empirical_model:            {p_emp*100:.1f}%")
    print(f"  heuristic_model:            {p_heu*100:.1f}%")
    if client is not None:
        print(f"  openai_model:               {p_oai*100:.1f}%")
    print(f"  blended_final:              {p_final*100:.1f}%")
    print(f"  likelihood_tier:            {_tier(p_final)}")
    print(
        f"  blend_weights:              ml={args.w_ml:.2f}, emp={args.w_emp:.2f}, "
        f"heu={args.w_heu:.2f}, oai={args.w_oai:.2f}"
    )
    print()

    print("ML top contributions")
    for name, val in top:
        direction = "up" if val >= 0 else "down"
        print(f"  - {name}: {direction} ({val:+.4f} log-odds)")

    print()
    print("Empirical reason codes")
    for reason in emp_reasons:
        print(f"  - {reason}")


# -----------------------------
# CLI
# -----------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description="Single-person show-up predictor + leakage-safe event backtest")

    # mode flags
    parser.add_argument("--backtest", action="store_true", help="Run leave-one-event-out backtest")

    # single-prediction args
    parser.add_argument("--event-date", help="Event date YYYY-MM-DD")
    parser.add_argument("--application-date", help="Application date YYYY-MM-DD")
    parser.add_argument("--days-before-event", type=float, help="Days between application and event")
    parser.add_argument("--user-id", help="Candidate userId (uses DB history)")
    parser.add_argument("--prior-approved", type=int, help="Manual prior approved count")
    parser.add_argument("--prior-checked", type=int, help="Manual prior checked-in count")
    parser.add_argument("--city", default="Unknown", help="Event city")
    parser.add_argument("--event-title", default="Hackathon", help="Event title")
    parser.add_argument("--same-day-overlap", type=int, help="Manual same-day same-city overlap")
    parser.add_argument("--event-capacity", type=int, help="Event capacity")
    parser.add_argument("--profile-description", default="", help="Optional short candidate profile text")
    parser.add_argument("--score", type=float, help="Optional applicant score [0,1] or [0,100]")

    # data/model/backtest controls
    parser.add_argument("--min-event-checkins", type=int, default=20, help="Minimum check-ins to keep event in dataset")
    parser.add_argument("--backtest-min-event-rows", type=int, default=80, help="Minimum rows per holdout event")
    parser.add_argument("--backtest-min-train-rows", type=int, default=400, help="Minimum train rows required for a fold")
    parser.add_argument("--backtest-split", choices=["temporal", "loeo"], default="temporal", help="Backtest split mode")
    parser.add_argument("--backtest-sample-per-event", type=int, default=0, help="Sample this many random candidates per holdout event (0=all)")
    parser.add_argument("--backtest-random-seed", type=int, default=42, help="Seed for holdout sampling")
    parser.add_argument("--min-cat-count", type=int, default=20, help="Minimum category frequency to get dedicated one-hot")
    parser.add_argument("--lr", type=float, default=0.06, help="Logistic learning rate")
    parser.add_argument("--reg", type=float, default=0.003, help="L2 regularization")
    parser.add_argument("--epochs", type=int, default=1200, help="Logistic training epochs")

    # blending
    parser.add_argument("--w-ml", type=float, default=0.60, help="Blend weight for ML model")
    parser.add_argument("--w-emp", type=float, default=0.40, help="Blend weight for empirical model")
    parser.add_argument("--w-heu", type=float, default=0.00, help="Blend weight for heuristic model")
    parser.add_argument("--w-oai", type=float, default=0.00, help="Blend weight for OpenAI model")
    parser.add_argument("--use-openai", action="store_true", help="Enable OpenAI per-candidate scoring")
    parser.add_argument("--openai-model", default="", help="Override OpenAI model")
    parser.add_argument("--openai-max-calls", type=int, default=500, help="Max OpenAI calls in backtest")
    parser.add_argument(
        "--openai-cache-path",
        default="/Users/nickita/cv-rank/.cache/openai_show_predict_cache.json",
        help="Path to OpenAI prediction cache",
    )

    args = parser.parse_args()

    repo_root = Path(__file__).resolve().parents[2]
    _load_env(repo_root)

    dsn = os.environ.get("PLATFORM_DATABASE_URL")
    if not dsn:
        raise SystemExit("Missing PLATFORM_DATABASE_URL in env")

    conn = psycopg2.connect(dsn)
    try:
        rows = _fetch_dataset(conn, min_event_checkins=args.min_event_checkins)
        if args.backtest:
            _run_backtest(rows, args)
        else:
            _run_single(conn, rows, args)
    finally:
        conn.close()


if __name__ == "__main__":
    main()
