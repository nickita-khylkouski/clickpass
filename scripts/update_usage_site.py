from __future__ import annotations

import argparse
import glob
import importlib.util
import json
import socket
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Any
from zoneinfo import ZoneInfo


ROOT = Path("/Users/nickita/.superset/worktrees/start/second-handstand")
DATA_DIR = ROOT / "web" / "src" / "data"
SITE_DIR = ROOT / "local-nickitakhy-site"
DEFAULT_PORT = 5181
TIMEZONE = ZoneInfo("America/Los_Angeles")
CLAUDE_AUDIT_PATH = Path("/Users/nickita/.local/bin/aiusage_audit.py")


def parse_codex_date(value: str) -> date:
    return datetime.strptime(value, "%b %d, %Y").date()


def format_codex_date(value: date) -> str:
    return value.strftime("%b %d, %Y")


def parse_claude_date(value: str) -> date:
    return datetime.strptime(value, "%Y-%m-%d").date()


def format_claude_date(value: date) -> str:
    return value.isoformat()


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text()) if path.exists() else {"daily": [], "totals": {}}


def write_json(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2) + "\n")


def latest_daily_date(report: dict[str, Any], parser) -> date | None:
    daily = report.get("daily") or []
    if not daily:
        return None
    return parser(daily[-1]["date"])


def find_latest_npx_entry(package_suffix: str) -> Path:
    matches = sorted(
        (Path.home() / ".npm" / "_npx").glob(f"*/node_modules/{package_suffix}"),
        key=lambda path: path.stat().st_mtime,
        reverse=True,
    )
    if not matches:
        raise FileNotFoundError(f"Missing cached npm package: {package_suffix}")
    return matches[0]


def load_js_prefetched_constant(bundle_path: Path, const_name: str) -> dict[str, Any]:
    script = f"""
import fs from 'fs';
const text = fs.readFileSync({json.dumps(str(bundle_path))}, 'utf8');
const match = text.match(new RegExp(`const {const_name} = (\\\\{{[\\\\s\\\\S]*?\\\\n\\\\}});`));
if (!match) {{
  console.error('missing constant');
  process.exit(1);
}}
const obj = Function(`return (${{match[1]}})`)();
process.stdout.write(JSON.stringify(obj));
"""
    result = subprocess.run(
        ["node", "--input-type=module", "-e", script],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )
    return json.loads(result.stdout)


def load_aiusage_audit_module():
    spec = importlib.util.spec_from_file_location("aiusage_audit", CLAUDE_AUDIT_PATH)
    if spec is None or spec.loader is None:
        raise RuntimeError("Could not load aiusage_audit.py")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def codex_extract_model(value: Any) -> str | None:
    if not isinstance(value, dict):
        return None
    info = value.get("info")
    if isinstance(info, dict):
        for key in ("model", "model_name"):
            candidate = info.get(key)
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
        metadata = info.get("metadata")
        if isinstance(metadata, dict):
            candidate = metadata.get("model")
            if isinstance(candidate, str) and candidate.strip():
                return candidate.strip()
    candidate = value.get("model")
    if isinstance(candidate, str) and candidate.strip():
        return candidate.strip()
    metadata = value.get("metadata")
    if isinstance(metadata, dict):
        candidate = metadata.get("model")
        if isinstance(candidate, str) and candidate.strip():
            return candidate.strip()
    return None


def codex_normalize_raw_usage(value: Any) -> dict[str, int] | None:
    if not isinstance(value, dict):
        return None
    input_tokens = int(value.get("input_tokens") or 0)
    cached = int(value.get("cached_input_tokens") or value.get("cache_read_input_tokens") or 0)
    output = int(value.get("output_tokens") or 0)
    reasoning = int(value.get("reasoning_output_tokens") or 0)
    total = int(value.get("total_tokens") or 0)
    if total <= 0:
        total = input_tokens + output
    return {
        "input_tokens": input_tokens,
        "cached_input_tokens": cached,
        "output_tokens": output,
        "reasoning_output_tokens": reasoning,
        "total_tokens": total,
    }


def codex_subtract_raw_usage(current: dict[str, int], previous: dict[str, int] | None) -> dict[str, int]:
    return {
        "input_tokens": max(current["input_tokens"] - (previous or {}).get("input_tokens", 0), 0),
        "cached_input_tokens": max(current["cached_input_tokens"] - (previous or {}).get("cached_input_tokens", 0), 0),
        "output_tokens": max(current["output_tokens"] - (previous or {}).get("output_tokens", 0), 0),
        "reasoning_output_tokens": max(current["reasoning_output_tokens"] - (previous or {}).get("reasoning_output_tokens", 0), 0),
        "total_tokens": max(current["total_tokens"] - (previous or {}).get("total_tokens", 0), 0),
    }


def codex_convert_delta(raw: dict[str, int]) -> dict[str, int]:
    total = raw["total_tokens"] if raw["total_tokens"] > 0 else raw["input_tokens"] + raw["output_tokens"]
    cached = min(raw["cached_input_tokens"], raw["input_tokens"])
    return {
        "inputTokens": raw["input_tokens"],
        "cachedInputTokens": cached,
        "outputTokens": raw["output_tokens"],
        "reasoningOutputTokens": raw["reasoning_output_tokens"],
        "totalTokens": total,
    }


def codex_price_for_model(model: str, pricing: dict[str, Any]) -> dict[str, float]:
    aliases = {
        "gpt-5-codex": "gpt-5",
        "gpt-5.3-codex": "gpt-5.2-codex",
    }
    key = model if model in pricing else aliases.get(model, model)
    data = pricing.get(key) or {}
    return {
        "input": float(data.get("input_cost_per_token") or 0),
        "cached": float(data.get("cache_read_input_token_cost") or data.get("input_cost_per_token") or 0),
        "output": float(data.get("output_cost_per_token") or 0),
    }


def codex_cost_usd(usage: dict[str, int], model: str, pricing: dict[str, Any]) -> float:
    model_pricing = codex_price_for_model(model, pricing)
    non_cached_input = max(usage["inputTokens"] - usage["cachedInputTokens"], 0)
    cached_input = min(usage["cachedInputTokens"], usage["inputTokens"])
    return (
        non_cached_input * model_pricing["input"]
        + cached_input * model_pricing["cached"]
        + usage["outputTokens"] * model_pricing["output"]
    )


def iter_codex_files_since(start_date: date) -> list[Path]:
    base = Path.home() / ".codex" / "sessions"
    files: list[Path] = []
    cursor = start_date
    today = datetime.now(TIMEZONE).date()
    while cursor <= today:
        day_dir = base / f"{cursor.year:04d}" / f"{cursor.month:02d}" / f"{cursor.day:02d}"
        if day_dir.is_dir():
            files.extend(sorted(day_dir.glob("*.jsonl")))
        cursor += timedelta(days=1)
    return files


def compute_codex_rows(start_date: date) -> list[dict[str, Any]]:
    codex_bundle = find_latest_npx_entry("@ccusage/codex/dist/index.js")
    pricing = load_js_prefetched_constant(codex_bundle, "PREFETCHED_CODEX_PRICING")
    by_day: dict[date, dict[str, Any]] = {}

    for file_path in iter_codex_files_since(start_date):
        previous_totals: dict[str, int] | None = None
        current_model: str | None = None
        current_model_is_fallback = False

        with file_path.open(encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except json.JSONDecodeError:
                    continue

                entry_type = entry.get("type")
                payload = entry.get("payload")
                if entry_type == "turn_context" and isinstance(payload, dict):
                    context_model = codex_extract_model(payload)
                    if context_model:
                        current_model = context_model
                        current_model_is_fallback = False
                    continue

                if entry_type != "event_msg" or not isinstance(payload, dict) or payload.get("type") != "token_count":
                    continue

                timestamp = entry.get("timestamp")
                if not isinstance(timestamp, str):
                    continue

                info = payload.get("info")
                last_usage = codex_normalize_raw_usage(info.get("last_token_usage") if isinstance(info, dict) else None)
                total_usage = codex_normalize_raw_usage(info.get("total_token_usage") if isinstance(info, dict) else None)
                raw = last_usage if last_usage is not None else (codex_subtract_raw_usage(total_usage, previous_totals) if total_usage is not None else None)
                if total_usage is not None:
                    previous_totals = total_usage
                if raw is None:
                    continue

                delta = codex_convert_delta(raw)
                if not any(delta[key] for key in ("inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens")):
                    continue

                extracted_model = codex_extract_model({**payload, "info": info} if isinstance(info, dict) else payload)
                is_fallback = False
                if extracted_model:
                    current_model = extracted_model
                    current_model_is_fallback = False

                model = extracted_model or current_model
                if model is None:
                    model = "gpt-5"
                    is_fallback = True
                    current_model = model
                    current_model_is_fallback = True
                elif extracted_model is None and current_model_is_fallback:
                    is_fallback = True

                local_day = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(TIMEZONE).date()
                if local_day < start_date:
                    continue

                row = by_day.setdefault(
                    local_day,
                    {
                        "date": format_codex_date(local_day),
                        "inputTokens": 0,
                        "cachedInputTokens": 0,
                        "outputTokens": 0,
                        "reasoningOutputTokens": 0,
                        "totalTokens": 0,
                        "costUSD": 0.0,
                        "models": {},
                    },
                )
                for key in ("inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens", "totalTokens"):
                    row[key] += delta[key]
                row["costUSD"] += codex_cost_usd(delta, model, pricing)

                model_bucket = row["models"].setdefault(
                    model,
                    {
                        "inputTokens": 0,
                        "cachedInputTokens": 0,
                        "outputTokens": 0,
                        "reasoningOutputTokens": 0,
                        "totalTokens": 0,
                        "isFallback": False,
                    },
                )
                for key in ("inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens", "totalTokens"):
                    model_bucket[key] += delta[key]
                model_bucket["isFallback"] = model_bucket["isFallback"] or is_fallback

    return [by_day[day] for day in sorted(by_day)]


def codex_totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    totals = {
        "inputTokens": 0,
        "cachedInputTokens": 0,
        "outputTokens": 0,
        "reasoningOutputTokens": 0,
        "totalTokens": 0,
        "costUSD": 0.0,
    }
    for row in rows:
        for key in ("inputTokens", "cachedInputTokens", "outputTokens", "reasoningOutputTokens", "totalTokens"):
            totals[key] += int(row.get(key, 0))
        totals["costUSD"] += float(row.get("costUSD", 0))
    return totals


def update_codex_usage() -> Path:
    out_path = DATA_DIR / "codex-usage.json"
    report = load_json(out_path)
    start_date = latest_daily_date(report, parse_codex_date)
    if start_date is None:
        first_file = sorted((Path.home() / ".codex" / "sessions").rglob("*.jsonl"))[0]
        start_date = date(
            int(first_file.parts[-4]),
            int(first_file.parts[-3]),
            int(first_file.parts[-2]),
        )

    fresh_rows = compute_codex_rows(start_date)
    kept_rows = [row for row in report.get("daily", []) if parse_codex_date(row["date"]) < start_date]
    merged_rows = kept_rows + fresh_rows
    payload = {"daily": merged_rows, "totals": codex_totals(merged_rows)}
    write_json(out_path, payload)
    return out_path


def load_claude_pricing() -> dict[str, Any]:
    bundle = find_latest_npx_entry("ccusage/dist/data-loader-B58Zt4YE.js")
    return load_js_prefetched_constant(bundle, "PREFETCHED_CLAUDE_PRICING")


def claude_tiered_cost(tokens: int, base_price: float | None, tiered_price: float | None, threshold: int = 200_000) -> float:
    if tokens <= 0:
        return 0.0
    if tokens > threshold and tiered_price is not None:
        below = min(tokens, threshold)
        cost = max(0, tokens - threshold) * tiered_price
        if base_price is not None:
            cost += below * base_price
        return cost
    if base_price is not None:
        return tokens * base_price
    return 0.0


def claude_cost_usd(usage: dict[str, int], model: str, speed: str | None, pricing: dict[str, Any]) -> float:
    model_pricing = pricing.get(model)
    if not isinstance(model_pricing, dict):
        return 0.0
    cost = (
        claude_tiered_cost(usage["inputTokens"], model_pricing.get("input_cost_per_token"), model_pricing.get("input_cost_per_token_above_200k_tokens"))
        + claude_tiered_cost(usage["outputTokens"], model_pricing.get("output_cost_per_token"), model_pricing.get("output_cost_per_token_above_200k_tokens"))
        + claude_tiered_cost(usage["cacheCreationTokens"], model_pricing.get("cache_creation_input_token_cost"), model_pricing.get("cache_creation_input_token_cost_above_200k_tokens"))
        + claude_tiered_cost(usage["cacheReadTokens"], model_pricing.get("cache_read_input_token_cost"), model_pricing.get("cache_read_input_token_cost_above_200k_tokens"))
    )
    if speed == "fast":
        cost *= float((model_pricing.get("provider_specific_entry") or {}).get("fast") or 1)
    return cost


def compute_claude_rows(start_date: date) -> list[dict[str, Any]]:
    pricing = load_claude_pricing()
    by_day: dict[date, dict[str, Any]] = {}
    seen_messages: set[str] = set()

    min_mtime = datetime.combine(start_date, datetime.min.time(), tzinfo=TIMEZONE).timestamp()
    claude_files = [
        Path(path)
        for path in glob.glob(str(Path.home() / ".claude" / "projects" / "**" / "*.jsonl"), recursive=True)
        if Path(path).stat().st_mtime >= min_mtime
    ]

    for file_path in sorted(claude_files):
        with file_path.open(encoding="utf-8", errors="ignore") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    obj = json.loads(line)
                except json.JSONDecodeError:
                    continue

                message = obj.get("message")
                if not isinstance(message, dict):
                    continue
                usage = message.get("usage")
                if not isinstance(usage, dict):
                    continue
                message_id = message.get("id")
                request_id = obj.get("requestId")
                dedupe_key = f"{message_id}:{request_id}" if message_id and request_id else None
                if dedupe_key and dedupe_key in seen_messages:
                    continue
                if dedupe_key:
                    seen_messages.add(dedupe_key)

                timestamp = obj.get("timestamp")
                if not isinstance(timestamp, str):
                    continue
                local_day = datetime.fromisoformat(timestamp.replace("Z", "+00:00")).astimezone(TIMEZONE).date()
                if local_day < start_date:
                    continue

                model = str(message.get("model") or "unknown")
                usage_row = {
                    "inputTokens": int(usage.get("input_tokens") or 0),
                    "outputTokens": int(usage.get("output_tokens") or 0),
                    "cacheCreationTokens": int(usage.get("cache_creation_input_tokens") or 0),
                    "cacheReadTokens": int(usage.get("cache_read_input_tokens") or 0),
                }
                usage_row["totalTokens"] = (
                    usage_row["inputTokens"]
                    + usage_row["outputTokens"]
                    + usage_row["cacheCreationTokens"]
                    + usage_row["cacheReadTokens"]
                )

                row = by_day.setdefault(
                    local_day,
                    {
                        "date": format_claude_date(local_day),
                        "inputTokens": 0,
                        "outputTokens": 0,
                        "cacheCreationTokens": 0,
                        "cacheReadTokens": 0,
                        "totalTokens": 0,
                        "totalCost": 0.0,
                        "modelBreakdownsByName": {},
                    },
                )
                for key in ("inputTokens", "outputTokens", "cacheCreationTokens", "cacheReadTokens", "totalTokens"):
                    row[key] += usage_row[key]

                model_bucket = row["modelBreakdownsByName"].setdefault(
                    model,
                    {
                        "modelName": model,
                        "inputTokens": 0,
                        "outputTokens": 0,
                        "cacheCreationTokens": 0,
                        "cacheReadTokens": 0,
                        "cost": 0.0,
                    },
                )
                for key in ("inputTokens", "outputTokens", "cacheCreationTokens", "cacheReadTokens"):
                    model_bucket[key] += usage_row[key]

                cost = claude_cost_usd(usage_row, model, usage.get("speed"), pricing)
                row["totalCost"] += cost
                model_bucket["cost"] += cost

    rows: list[dict[str, Any]] = []
    for day in sorted(by_day):
        row = by_day[day]
        model_breakdowns = sorted(row.pop("modelBreakdownsByName").values(), key=lambda item: item["modelName"])
        row["modelsUsed"] = [item["modelName"] for item in model_breakdowns]
        row["modelBreakdowns"] = model_breakdowns
        rows.append(row)
    return rows


def claude_totals(rows: list[dict[str, Any]]) -> dict[str, Any]:
    totals = {
        "inputTokens": 0,
        "outputTokens": 0,
        "cacheCreationTokens": 0,
        "cacheReadTokens": 0,
        "totalCost": 0.0,
        "totalTokens": 0,
    }
    for row in rows:
        for key in ("inputTokens", "outputTokens", "cacheCreationTokens", "cacheReadTokens", "totalTokens"):
            totals[key] += int(row.get(key, 0))
        totals["totalCost"] += float(row.get("totalCost", 0))
    return totals


def update_claude_usage() -> Path:
    out_path = DATA_DIR / "claude-usage.json"
    report = load_json(out_path)
    start_date = latest_daily_date(report, parse_claude_date)
    if start_date is None:
        start_date = date(2026, 1, 1)

    fresh_rows = compute_claude_rows(start_date)
    kept_rows = [row for row in report.get("daily", []) if parse_claude_date(row["date"]) < start_date]
    merged_rows = kept_rows + fresh_rows
    payload = {"daily": merged_rows, "totals": claude_totals(merged_rows)}
    write_json(out_path, payload)
    return out_path


def build_site() -> None:
    subprocess.run(
        ["python3", str(ROOT / "scripts" / "build_local_nickitakhy_site.py")],
        cwd=ROOT,
        check=True,
    )


def port_is_open(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.settimeout(0.2)
        return sock.connect_ex(("127.0.0.1", port)) == 0


def serve_site(port: int) -> subprocess.Popen[str]:
    if port_is_open(port):
        return None
    return subprocess.Popen(
        ["python3", "-m", "http.server", str(port), "--bind", "127.0.0.1"],
        cwd=SITE_DIR,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        start_new_session=True,
        text=True,
    )


def open_browser(url: str) -> None:
    subprocess.run(["open", url], cwd=ROOT, check=False)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Incrementally refresh local Codex/Claude usage JSON and rebuild the local nickitakhy.me mirror."
    )
    parser.add_argument("--serve", action="store_true", help="Start a local static server after rebuilding.")
    parser.add_argument("--port", type=int, default=DEFAULT_PORT, help=f"Port for --serve. Default: {DEFAULT_PORT}.")
    parser.add_argument("--open", dest="open_browser", action="store_true", help="Open the usage page in the browser.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()

    codex_path = update_codex_usage()
    claude_path = update_claude_usage()
    build_site()

    print("Updated usage snapshots:")
    print(f"  Codex:  {codex_path}")
    print(f"  Claude: {claude_path}")
    print(f"  Site:   {SITE_DIR / 'usage' / 'index.html'}")

    url = f"http://127.0.0.1:{args.port}/usage/"
    if args.serve:
        process = serve_site(args.port)
        if process is None:
            print(f"Port {args.port} already has a server. Reusing http://127.0.0.1:{args.port}/")
            print(f"Usage page: {url}")
        else:
            print(f"Serving local site at http://127.0.0.1:{args.port}/")
            print(f"Usage page: {url}")
            print(f"Server PID: {process.pid}")
    else:
        print(f"Open the local site by serving {SITE_DIR} or use --serve.")
        print(f"Usage page URL if served on {args.port}: {url}")

    if args.open_browser:
        if args.serve:
            open_browser(url)
        elif port_is_open(args.port):
            open_browser(url)
        else:
            print(f"Skipped opening browser because nothing is serving on port {args.port}.", file=sys.stderr)

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
