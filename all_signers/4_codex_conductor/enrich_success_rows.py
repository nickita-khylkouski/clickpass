#!/usr/bin/env python3
"""Enrich existing successful DB rows with real account snapshot data.

This script does not create new accounts. It logs into existing successful
accounts from signup_runs.db using stored credentials, extracts snapshot
fields via Browser Use, and updates those same rows.
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import os
import sqlite3
import sys
from pathlib import Path

from browser_use import ChatBrowserUse
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from codex_super.signup_super import (
    create_cloud_browser,
    parse_account_snapshot,
    parse_login_output,
    run_agent_task,
    sanitize_api_key_candidate,
    stop_browser_safely,
)

load_dotenv()

DB_DEFAULT = Path("codex_super/state/signup_runs.db")
LOG_DIR_DEFAULT = Path("single_runs/enrich")


def pick_rows(db_path: Path, limit: int, row_ids: list[int] | None) -> list[sqlite3.Row]:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    try:
        if row_ids:
            qmarks = ",".join("?" for _ in row_ids)
            rows = conn.execute(
                f"""
                SELECT id, url, email, password, login_status, subscription, entitlements,
                       limits_text, profile_name, profile_email, connectors, api_key,
                       api_key_url, login_url, notes
                FROM signup_runs
                WHERE id IN ({qmarks})
                ORDER BY id DESC
                """,
                row_ids,
            ).fetchall()
            return rows

        rows = conn.execute(
            """
            SELECT id, url, email, password, login_status, subscription, entitlements,
                   limits_text, profile_name, profile_email, connectors, api_key,
                   api_key_url, login_url, notes
            FROM signup_runs
            WHERE login_status='SUCCESS'
              AND (
                subscription='NONE' OR entitlements='NONE' OR limits_text='NONE'
                OR profile_name='UNKNOWN_PROFILE' OR profile_email='NONE'
                OR connectors='NONE'
              )
            ORDER BY id DESC
            LIMIT ?
            """,
            (limit,),
        ).fetchall()
        return rows
    finally:
        conn.close()


def _meaningful(value: str | None) -> bool:
    if not value:
        return False
    v = value.strip()
    if not v:
        return False
    return v.upper() not in {"NONE", "UNKNOWN", "-", "N/A", "NULL"}


def _prefer(new_value: str | None, old_value: str | None) -> str:
    if _meaningful(new_value):
        return str(new_value).strip()
    if old_value is None:
        return "NONE"
    old = str(old_value).strip()
    return old if old else "NONE"


def update_row(db_path: Path, row_id: int, fields: dict[str, str]) -> None:
    conn = sqlite3.connect(db_path)
    try:
        conn.execute(
            """
            UPDATE signup_runs
            SET subscription = ?,
                entitlements = ?,
                limits_text = ?,
                profile_name = ?,
                profile_email = ?,
                connectors = ?,
                api_key = ?,
                api_key_url = ?,
                login_url = ?,
                notes = ?
            WHERE id = ?
            """,
            (
                fields["subscription"],
                fields["entitlements"],
                fields["limits_text"],
                fields["profile_name"],
                fields["profile_email"],
                fields["connectors"],
                fields["api_key"],
                fields["api_key_url"],
                fields["login_url"],
                fields["notes"],
                row_id,
            ),
        )
        conn.commit()
    finally:
        conn.close()


async def enrich_one(
    row: sqlite3.Row,
    *,
    llm: ChatBrowserUse,
    proxy_country: str,
    max_steps: int,
    login_timeout: int,
    snapshot_timeout: int,
    log_dir: Path,
    db_path: Path,
) -> None:
    row_id = int(row["id"])
    url = str(row["url"] or "").strip()
    email = str(row["email"] or "").strip()
    password = str(row["password"] or "").strip()
    log_path = log_dir / f"enrich_{row_id}.log"

    if not (url and email and password):
        return

    browser = await create_cloud_browser(proxy_country_code=proxy_country, profile_id=None)
    try:
        has_api_already = _meaningful(str(row["api_key"] or ""))
        api_section = (
            "After login, locate API key/token settings (Developer, API Keys, Account).\n"
            if not has_api_already
            else (
                "API key already exists in DB. After successful login, STOP immediately.\n"
                "Do not navigate to API pages and do not click row action buttons.\n"
            )
        )
        login_task = f"""
Log in to {url} with:
- Email: {email}
- Password: {password}

Stay on target domain only. Do not use OAuth.
{api_section}

Output exactly:
LOGIN: SUCCESS or FAILED
API_KEY: <key> or NONE
API_KEY_URL: <url> or NONE
LOGIN_URL: <url> or NONE
PASSWORD_USED: ORIGINAL or TRAILING_DOT or UNKNOWN
NOTES: <brief>
""".strip()

        login_run = await run_agent_task(
            browser=browser,
            llm=llm,
            label=f"enrich-login#{row_id}",
            task=login_task,
            max_steps=max_steps,
            timeout_s=login_timeout,
            retries=1,
        )
        login_parsed = parse_login_output(login_run.output)

        snapshot_parsed: dict[str, str | None] = {
            "SUBSCRIPTION": None,
            "ENTITLEMENTS": None,
            "LIMITS": None,
            "PROFILE_NAME": None,
            "PROFILE_EMAIL": None,
            "CONNECTORS": None,
            "NOTES": None,
        }
        if (login_parsed.get("LOGIN") or "").strip().upper() == "SUCCESS":
            snapshot_task = """
Extract account snapshot data from the currently logged-in UI.
Look for plan, usage, limits, credits, profile/account identity, and installed connectors/integrations.
Avoid risky action buttons (delete, regenerate, revoke, row action menus).

Output exactly:
SUBSCRIPTION: <tier/plan name> or NONE
ENTITLEMENTS: <free credits/requests/etc> or NONE
LIMITS: <remaining usage/quota/reset window> or NONE
PROFILE_NAME: <display/account name> or NONE
PROFILE_EMAIL: <email on account> or NONE
CONNECTORS: <comma-separated connectors/integrations seen> or NONE
NOTES: <brief context>
""".strip()
            snapshot_run = await run_agent_task(
                browser=browser,
                llm=llm,
                label=f"enrich-snapshot#{row_id}",
                task=snapshot_task,
                max_steps=max_steps,
                timeout_s=snapshot_timeout,
                retries=1,
            )
            snapshot_parsed = parse_account_snapshot(snapshot_run.output)

        api_key_candidate = sanitize_api_key_candidate(
            login_parsed.get("API_KEY"),
            login_parsed.get("NOTES"),
        )
        merged = {
            "subscription": _prefer(snapshot_parsed.get("SUBSCRIPTION"), row["subscription"]),
            "entitlements": _prefer(snapshot_parsed.get("ENTITLEMENTS"), row["entitlements"]),
            "limits_text": _prefer(snapshot_parsed.get("LIMITS"), row["limits_text"]),
            "profile_name": _prefer(snapshot_parsed.get("PROFILE_NAME"), row["profile_name"]),
            "profile_email": _prefer(snapshot_parsed.get("PROFILE_EMAIL"), row["profile_email"]),
            "connectors": _prefer(snapshot_parsed.get("CONNECTORS"), row["connectors"]),
            "api_key": _prefer(api_key_candidate, row["api_key"]),
            "api_key_url": _prefer(login_parsed.get("API_KEY_URL"), row["api_key_url"]),
            "login_url": _prefer(login_parsed.get("LOGIN_URL"), row["login_url"]),
            "notes": _prefer(
                snapshot_parsed.get("NOTES") or login_parsed.get("NOTES"),
                row["notes"],
            ),
        }

        update_row(db_path, row_id, merged)
        with log_path.open("w", encoding="utf-8") as f:
            f.write("LOGIN_OUTPUT\n")
            f.write((login_run.output or "NONE") + "\n\n")
            f.write("SNAPSHOT_OUTPUT\n")
            f.write(str(snapshot_parsed) + "\n")
    finally:
        with contextlib.suppress(Exception):
            await stop_browser_safely(browser, label=f"enrich#{row_id}")


async def main_async(args: argparse.Namespace) -> int:
    rows = pick_rows(args.db, args.limit, args.ids)
    if not rows:
        print("No candidate rows to enrich.")
        return 0

    args.log_dir.mkdir(parents=True, exist_ok=True)
    llm = ChatBrowserUse(model=args.llm, api_key=os.environ["BROWSER_USE_API_KEY"])
    for row in rows:
        print(f"[enrich] id={row['id']} url={row['url']}")
        try:
            await enrich_one(
                row,
                llm=llm,
                proxy_country=args.proxy_country,
                max_steps=args.max_steps,
                login_timeout=args.login_timeout,
                snapshot_timeout=args.snapshot_timeout,
                log_dir=args.log_dir,
                db_path=args.db,
            )
            print(f"[ok] id={row['id']}")
        except Exception as exc:
            print(f"[fail] id={row['id']} err={exc}")
    return 0


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Enrich successful signup DB rows with real snapshot data.")
    p.add_argument("--db", type=Path, default=DB_DEFAULT)
    p.add_argument("--log-dir", type=Path, default=LOG_DIR_DEFAULT)
    p.add_argument("--limit", type=int, default=5)
    p.add_argument("--ids", type=int, nargs="*", default=None, help="Specific row IDs to enrich.")
    p.add_argument("--llm", default="bu-2-0")
    p.add_argument("--proxy-country", default="us")
    p.add_argument("--max-steps", type=int, default=10)
    p.add_argument("--login-timeout", type=int, default=140)
    p.add_argument("--snapshot-timeout", type=int, default=120)
    return p.parse_args()


def main() -> int:
    args = parse_args()
    return asyncio.run(main_async(args))


if __name__ == "__main__":
    raise SystemExit(main())
