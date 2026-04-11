from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

import psycopg2
from psycopg2 import sql
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parent.parent


def load_env() -> None:
    load_dotenv(BASE_DIR / ".env")


def connect() -> psycopg2.extensions.connection:
    dsn = os.environ.get("PLATFORM_DATABASE_URL", "").strip()
    if not dsn:
        raise SystemExit("Missing PLATFORM_DATABASE_URL")
    conn = psycopg2.connect(dsn)
    with conn.cursor() as cur:
        cur.execute("SET statement_timeout TO 0")
    return conn


def list_tables(conn: psycopg2.extensions.connection) -> list[dict]:
    query = """
    select
      schemaname,
      relname as table_name,
      n_live_tup::bigint as estimated_rows
    from pg_stat_user_tables
    where schemaname = 'public'
    order by estimated_rows desc, table_name asc
    """
    with conn.cursor() as cur:
        cur.execute(query)
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row, strict=False)) for row in cur.fetchall()]


def describe_table(conn: psycopg2.extensions.connection, table_name: str) -> list[dict]:
    query = """
    select
      column_name,
      data_type,
      is_nullable,
      ordinal_position
    from information_schema.columns
    where table_schema = 'public' and table_name = %s
    order by ordinal_position
    """
    with conn.cursor() as cur:
        cur.execute(query, (table_name,))
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row, strict=False)) for row in cur.fetchall()]


def sample_table(
    conn: psycopg2.extensions.connection,
    table_name: str,
    *,
    limit: int,
    columns: list[str] | None,
    where: str | None,
) -> list[dict]:
    selected_columns = sql.SQL(", ").join(sql.Identifier(column) for column in columns) if columns else sql.SQL("*")
    query = sql.SQL("select {cols} from public.{table}").format(
        cols=selected_columns,
        table=sql.Identifier(table_name),
    )
    if where:
        query += sql.SQL(" where ") + sql.SQL(where)
    query += sql.SQL(" limit %s")

    with conn.cursor() as cur:
        cur.execute(query, (limit,))
        col_names = [desc[0] for desc in cur.description]
        rows = cur.fetchall()
    return [dict(zip(col_names, row, strict=False)) for row in rows]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="List, describe, or sample platform DB tables.")
    parser.add_argument("--list", action="store_true", help="List public tables with estimated row counts.")
    parser.add_argument("--table", help="Table name to describe or sample.")
    parser.add_argument("--schema", action="store_true", help="Show the schema for --table.")
    parser.add_argument("--limit", type=int, default=10, help="Sample row limit.")
    parser.add_argument("--columns", nargs="*", default=None, help="Specific columns to return for --table.")
    parser.add_argument("--where", default=None, help="Optional raw SQL WHERE clause for sampling.")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    load_env()
    conn = connect()
    try:
        payload: dict[str, object] = {}
        if args.list:
            payload["tables"] = list_tables(conn)
        elif args.table:
            payload["table"] = args.table
            if args.schema:
                payload["schema"] = describe_table(conn, args.table)
            payload["sample_rows"] = sample_table(
                conn,
                args.table,
                limit=args.limit,
                columns=args.columns,
                where=args.where,
            )
        else:
            raise SystemExit("Use --list or --table TABLE")
    finally:
        conn.close()
    print(json.dumps(payload, indent=2, default=str))


if __name__ == "__main__":
    main()
