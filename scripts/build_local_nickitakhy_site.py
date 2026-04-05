from __future__ import annotations

import json
from datetime import datetime
from pathlib import Path
from urllib.request import urlopen


ROOT = Path("/Users/nickita/.superset/worktrees/start/second-handstand")
OUT_DIR = ROOT / "local-nickitakhy-site"
DATA_DIR = ROOT / "web" / "src" / "data"


def fetch_homepage() -> str:
    with urlopen("https://nickitakhy.me/") as response:
        return response.read().decode("utf-8")


def add_usage_link_to_homepage(homepage: str) -> str:
    if 'href="/usage/"' in homepage:
        return homepage

    old = """        <a href="https://www.cerebralvalley.ai/">
          <img src="https://www.cerebralvalley.ai/icon-light-32x32.png" alt="Cerebral Valley" />
        </a>"""
    new = """        <a href="https://www.cerebralvalley.ai/">
          <img src="https://www.cerebralvalley.ai/icon-light-32x32.png" alt="Cerebral Valley" />
        </a>
        <a href="/usage/" style="margin-left: 10px; vertical-align: middle;">My AI Usage</a>"""
    if old in homepage:
        return homepage.replace(old, new, 1)

    fallback = '<div class="toplogos">'
    if fallback in homepage:
        return homepage.replace(
            fallback,
            fallback + '\n        <a href="/usage/" style="margin-right: 10px; vertical-align: middle;">My AI Usage</a>',
            1,
        )
    return homepage


def add_homepage_citations(homepage: str) -> str:
    old = "I have won 8 different hackathons, including YC and Replicate (Cloudflare)."
    new = (
        'I have won 8 different hackathons, including <a href="https://www.ycombinator.com/">YC</a> '
        'and <a href="https://replicate.com/">Replicate</a> '
        '(<a href="https://www.cloudflare.com/">Cloudflare</a>).'
    )
    return homepage.replace(old, new, 1)


def load_json(path: Path) -> dict:
    with path.open() as handle:
        return json.load(handle)


def parse_usage_date(value: str) -> datetime:
    if "-" in value:
        return datetime.strptime(value, "%Y-%m-%d")
    return datetime.strptime(value, "%b %d, %Y")


def month_spend(rows: list[dict], cost_key: str) -> list[tuple[str, float]]:
    buckets: dict[str, float] = {}
    for row in rows:
        month = parse_usage_date(row["date"]).strftime("%Y-%m")
        buckets[month] = buckets.get(month, 0.0) + float(row[cost_key])
    return sorted(buckets.items())


def normalize_codex_rows(report: dict) -> list[dict]:
    rows = []
    for row in report["daily"]:
        rows.append(
            {
                "date": row["date"],
                "models": ", ".join(row["models"].keys()) or "None",
                "input": row["inputTokens"],
                "output": row["outputTokens"],
                "aux": row["reasoningOutputTokens"],
                "aux_label": "Reasoning",
                "cache_read": row["cachedInputTokens"],
                "total": row["totalTokens"],
                "cost": row["costUSD"],
            }
        )
    return rows


def normalize_claude_rows(report: dict) -> list[dict]:
    rows = []
    for row in report["daily"]:
        rows.append(
            {
                "date": row["date"],
                "models": ", ".join(model.replace("claude-", "").replace("-20251101", "") for model in row["modelsUsed"]) or "None",
                "input": row["inputTokens"],
                "output": row["outputTokens"],
                "aux": row["cacheCreationTokens"],
                "aux_label": "Cache Create",
                "cache_read": row["cacheReadTokens"],
                "total": row["totalTokens"],
                "cost": row["totalCost"],
            }
        )
    return rows


def money(value: float) -> str:
    return f"${value:,.2f}"


def integer(value: int) -> str:
    return f"{value:,}"


def summary_rows(codex: dict, claude: dict) -> str:
    rows = [
        (
            "Codex",
            codex["totals"]["costUSD"],
            codex["totals"]["totalTokens"],
            len(codex["daily"]),
            sum(1 for row in codex["daily"] if row["costUSD"] > 0),
        ),
        (
            "Claude Code",
            claude["totals"]["totalCost"],
            claude["totals"]["totalTokens"],
            len(claude["daily"]),
            sum(1 for row in claude["daily"] if row["totalCost"] > 0),
        ),
    ]
    body = []
    for tool, spend, tokens, report_days, spend_days in rows:
        avg = spend / report_days if report_days else 0
        daily_costs = sorted(
            [row["costUSD"] for row in codex["daily"]] if tool == "Codex" else [row["totalCost"] for row in claude["daily"]]
        )
        mid = len(daily_costs) // 2
        median = (
            (daily_costs[mid - 1] + daily_costs[mid]) / 2
            if daily_costs and len(daily_costs) % 2 == 0
            else (daily_costs[mid] if daily_costs else 0)
        )
        body.append(
            f"<tr><td>{tool}</td><td>{money(spend)}</td><td>{integer(tokens)}</td><td>{report_days}</td><td>{spend_days}</td><td>{money(avg)}</td><td>{money(median)}</td></tr>"
        )
    return "\n".join(body)


def top_rows(rows: list[dict]) -> list[dict]:
    return sorted(rows, key=lambda row: row["cost"], reverse=True)[:5]


def render_month_table(rows: list[tuple[str, float]]) -> str:
    return "\n".join(
        f"<tr><td>{month}</td><td>{money(spend)}</td></tr>" for month, spend in rows
    )


def render_top_table(rows: list[dict]) -> str:
    return "\n".join(
        f"<tr><td>{row['date']}</td><td>{money(row['cost'])}</td><td>{row['models']}</td></tr>" for row in rows
    )


def render_raw_table(rows: list[dict], aux_label: str) -> str:
    body = []
    for row in rows:
        body.append(
            "<tr>"
            f"<td>{row['date']}</td>"
            f"<td>{row['models']}</td>"
            f"<td>{integer(int(row['input']))}</td>"
            f"<td>{integer(int(row['output']))}</td>"
            f"<td>{integer(int(row['aux']))}</td>"
            f"<td>{integer(int(row['cache_read']))}</td>"
            f"<td>{integer(int(row['total']))}</td>"
            f"<td>{money(float(row['cost']))}</td>"
            "</tr>"
        )
    return (
        "<div style=\"overflow-x:auto;\">"
        "<table border=\"1\" cellpadding=\"6\" cellspacing=\"0\" style=\"border-collapse:collapse; width:100%; min-width:980px;\">"
        "<thead>"
        f"<tr><th>Date</th><th>Models</th><th>Input</th><th>Output</th><th>{aux_label}</th><th>Cache Read</th><th>Total Tokens</th><th>Cost</th></tr>"
        "</thead>"
        "<tbody>"
        + "\n".join(body)
        + "</tbody></table></div>"
    )


def build_usage_page(codex: dict, claude: dict) -> str:
    codex_rows = normalize_codex_rows(codex)
    claude_rows = normalize_claude_rows(claude)
    snapshot_date = max(
        max(parse_usage_date(row["date"]) for row in codex_rows),
        max(parse_usage_date(row["date"]) for row in claude_rows),
    ).strftime("%b %d, %Y")
    combined_spend = codex["totals"]["costUSD"] + claude["totals"]["totalCost"]
    combined_tokens = codex["totals"]["totalTokens"] + claude["totals"]["totalTokens"]

    return f"""<!DOCTYPE html>
<html lang="en">
  <head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width, initial-scale=1.0" />
    <title>Nickita Khylkouski Usage</title>
    <style>
      body {{
        margin: 18px 28px 32px;
        font-family: "Times New Roman", Times, serif;
        font-size: 16px;
        line-height: 1.12;
        background: white;
        color: black;
      }}
      .page {{
        max-width: 980px;
        margin: 0 auto;
      }}
      h1 {{
        margin: 18px 0 16px;
        font-size: 32px;
      }}
      h2 {{
        margin: 24px 0 14px;
        font-size: 18px;
        text-align: center;
      }}
      h3 {{
        margin: 20px 0 10px;
        font-size: 16px;
      }}
      p {{
        margin: 0 0 14px;
      }}
      table {{
        margin-top: 12px;
      }}
      @media (max-width: 760px) {{
        body {{
          margin: 16px;
        }}
      }}
    </style>
  </head>
  <body>
    <div class="page">
      <p><a href="/">home</a></p>

      <h1>Nickita Khylkouski Usage</h1>

      <p>
        Exact local Codex and Claude Code usage. Snapshot date: {snapshot_date}.
      </p>

      <p>
        <span style="font-size: 18px;"><b>Combined spend: {money(combined_spend)}.</b></span>
      </p>

      <p>
        <span style="font-size: 18px;"><b>Combined tokens: {integer(combined_tokens)}.</b></span>
      </p>

      <h2>Summary</h2>
      <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse; width:100%;">
        <thead>
          <tr>
            <th>Tool</th>
            <th>Total Spend</th>
            <th>Total Tokens</th>
            <th>Report Days</th>
            <th>Spend Days</th>
            <th>Average Per Day</th>
            <th>Median Per Day</th>
          </tr>
        </thead>
        <tbody>
          {summary_rows(codex, claude)}
        </tbody>
      </table>

      <h2>Codex</h2>
      <p>
        OpenAI Codex daily usage snapshot.
      </p>
      <h3>Monthly Spend</h3>
      <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse; width:100%;">
        <thead><tr><th>Month</th><th>Spend</th></tr></thead>
        <tbody>{render_month_table(month_spend(codex_rows, 'cost'))}</tbody>
      </table>
      <h3>Top Days</h3>
      <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse; width:100%;">
        <thead><tr><th>Date</th><th>Spend</th><th>Models</th></tr></thead>
        <tbody>{render_top_table(top_rows(codex_rows))}</tbody>
      </table>
      <h3>Raw Daily Data</h3>
      {render_raw_table(codex_rows, "Reasoning")}

      <h2>Claude Code</h2>
      <p>
        Anthropic Claude Code daily usage snapshot.
      </p>
      <h3>Monthly Spend</h3>
      <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse; width:100%;">
        <thead><tr><th>Month</th><th>Spend</th></tr></thead>
        <tbody>{render_month_table(month_spend(claude_rows, 'cost'))}</tbody>
      </table>
      <h3>Top Days</h3>
      <table border="1" cellpadding="6" cellspacing="0" style="border-collapse:collapse; width:100%;">
        <thead><tr><th>Date</th><th>Spend</th><th>Models</th></tr></thead>
        <tbody>{render_top_table(top_rows(claude_rows))}</tbody>
      </table>
      <h3>Raw Daily Data</h3>
      {render_raw_table(claude_rows, "Cache Create")}
    </div>
  </body>
</html>
"""


def main() -> None:
    OUT_DIR.mkdir(exist_ok=True)
    (OUT_DIR / "usage").mkdir(exist_ok=True)

    homepage = fetch_homepage()
    homepage = add_usage_link_to_homepage(homepage)
    homepage = add_homepage_citations(homepage)

    (OUT_DIR / "index.html").write_text(homepage)

    codex = load_json(DATA_DIR / "codex-usage.json")
    claude = load_json(DATA_DIR / "claude-usage.json")
    usage_html = build_usage_page(codex, claude)
    (OUT_DIR / "usage" / "index.html").write_text(usage_html)


if __name__ == "__main__":
    main()
