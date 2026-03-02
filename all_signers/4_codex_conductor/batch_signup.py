#!/usr/bin/env python3
"""Run many signup_super jobs concurrently with hard per-site cutoffs."""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import csv
import json
import os
import re
import signal
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import httpx


@dataclass
class JobResult:
    url: str
    domain: str
    status: str
    runtime_s: float
    log_path: str
    email: str | None = None
    password: str | None = None
    username: str | None = None
    signup: str | None = None
    verified: str | None = None
    verify_ch: str | None = None
    login: str | None = None
    api_key: str | None = None
    api_key_status: str | None = None
    api_key_url: str | None = None
    money: str | None = None
    login_url: str | None = None
    notes: str | None = None
    error: str | None = None


def domain_of(url: str) -> str:
    host = urlparse(url).netloc.lower().split(":")[0]
    return host[4:] if host.startswith("www.") else host


def parse_result_block(text: str) -> dict[str, str]:
    out: dict[str, str] = {}
    marker = "RESULTS ("
    idx = text.rfind(marker)
    if idx == -1:
        return out
    text = text[idx:]
    keys = (
        "URL",
        "EMAIL",
        "PASSWORD",
        "USERNAME",
        "SIGNUP",
        "VERIFIED",
        "VERIFY_CH",
        "LOGIN",
        "API_KEY",
        "API_KEY_STATUS",
        "API_KEY_URL",
        "MONEY",
        "LOGIN_URL",
        "NOTES",
    )
    for key in keys:
        matches = re.findall(rf"(?m)^{key}:\s*(.+)$", text)
        if matches:
            out[key] = matches[-1].strip()
    return out


def run_one_sync(cmd: list[str], log_path: Path, timeout_s: int) -> tuple[int | None, str | None]:
    proc: subprocess.Popen[str] | None = None
    try:
        with log_path.open("w", encoding="utf-8") as f:
            # Use a dedicated process group so timeout cleanup can terminate uv + spawned python.
            proc = subprocess.Popen(
                cmd,
                stdout=f,
                stderr=subprocess.STDOUT,
                text=True,
                start_new_session=True,
            )
            try:
                code = proc.wait(timeout=timeout_s)
                return code, None
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(proc.pid, signal.SIGKILL)
                except Exception:
                    with contextlib.suppress(Exception):
                        proc.kill()
                with contextlib.suppress(Exception):
                    proc.wait(timeout=5)
                f.write(f"\n[BATCH] TIMEOUT after {timeout_s}s\n")
                return None, f"timeout_after_{timeout_s}s"
    except subprocess.TimeoutExpired:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"\n[BATCH] TIMEOUT after {timeout_s}s\n")
        return None, f"timeout_after_{timeout_s}s"
    except Exception as exc:
        with log_path.open("a", encoding="utf-8") as f:
            f.write(f"\n[BATCH] EXCEPTION: {exc}\n")
        return None, str(exc)


async def stop_active_browsers(api_key: str, max_to_stop: int = 100) -> int:
    headers = {"X-Browser-Use-API-Key": api_key, "Content-Type": "application/json"}
    stopped = 0
    async with httpx.AsyncClient(timeout=20) as client:
        resp = await client.get("https://api.browser-use.com/api/v2/browsers?limit=100", headers=headers)
        if resp.status_code >= 400:
            return 0
        items = resp.json().get("items", [])
        active = [it for it in items if str(it.get("status", "")).lower() == "active" and it.get("id")]
        for item in active[:max_to_stop]:
            sid = str(item["id"])
            patch = await client.patch(
                f"https://api.browser-use.com/api/v2/browsers/{sid}",
                headers=headers,
                json={"action": "stop"},
            )
            if patch.status_code < 400:
                stopped += 1
    return stopped


async def run_batch(args: argparse.Namespace) -> int:
    ts = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    out_dir = Path(args.output_dir or f"single_runs/batch-{ts}").resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    summary_jsonl = out_dir / "summary.jsonl"
    summary_csv = out_dir / "summary.csv"

    urls = list(args.urls)
    if args.urls_file:
        for line in Path(args.urls_file).read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                urls.append(line)
    seen: set[str] = set()
    deduped: list[str] = []
    for u in urls:
        if u not in seen:
            deduped.append(u)
            seen.add(u)
    urls = deduped
    if not urls:
        print("No URLs provided.")
        return 1

    if args.stop_active_browsers_first:
        key = os.getenv("BROWSER_USE_API_KEY", "")
        if key:
            stopped = await stop_active_browsers(key)
            print(f"[batch] stopped_active_browsers={stopped}")

    use_oauth = args.allow_oauth
    if args.allow_oauth and args.profile_id and args.concurrency > 1 and not args.allow_shared_profile:
        use_oauth = False
        print(
            "[batch] OAuth disabled for this run: shared profile with concurrency>1 is unsafe. "
            "Use --allow-shared-profile to override."
        )

    sem = asyncio.Semaphore(args.concurrency)
    results: list[JobResult] = []

    async def worker(url: str) -> None:
        domain = domain_of(url)
        log_path = out_dir / f"{domain.replace('.', '_')}.log"
        cmd = [
            "uv",
            "run",
            "python",
            "codex_super/signup_super.py",
            url,
            "--llm",
            args.llm,
            "--max-steps",
            str(args.max_steps),
            "--signup-timeout",
            str(args.signup_timeout),
            "--verify-timeout",
            str(args.verify_timeout),
            "--login-timeout",
            str(args.login_timeout),
            "--proxy-country",
            args.proxy_country,
            "--results-db",
            str((out_dir / "signup_runs.db").resolve()),
            "--no-sticky-inbox",
        ]
        if not args.capture_summary:
            cmd.append("--no-capture-summary")
        if use_oauth:
            cmd.append("--allow-oauth")
            if args.profile_id:
                cmd.extend(["--profile-id", args.profile_id])
        if args.reuse_inbox:
            cmd.append("--reuse-inbox")

        async with sem:
            t0 = time.time()
            print(f"[start] {domain}")
            code, err = await asyncio.to_thread(run_one_sync, cmd, log_path, args.timeout_per_site)
            runtime_s = time.time() - t0
            text = log_path.read_text(encoding="utf-8", errors="replace") if log_path.exists() else ""
            parsed = parse_result_block(text)
            status = "ok" if code == 0 else ("timeout" if err and "timeout" in err else "failed")
            result = JobResult(
                url=url,
                domain=domain,
                status=status,
                runtime_s=runtime_s,
                log_path=str(log_path),
                email=parsed.get("EMAIL"),
                password=parsed.get("PASSWORD"),
                username=parsed.get("USERNAME"),
                signup=parsed.get("SIGNUP"),
                verified=parsed.get("VERIFIED"),
                verify_ch=parsed.get("VERIFY_CH"),
                login=parsed.get("LOGIN"),
                api_key=parsed.get("API_KEY"),
                api_key_status=parsed.get("API_KEY_STATUS"),
                api_key_url=parsed.get("API_KEY_URL"),
                money=parsed.get("MONEY"),
                login_url=parsed.get("LOGIN_URL"),
                notes=parsed.get("NOTES"),
                error=err,
            )
            results.append(result)
            with summary_jsonl.open("a", encoding="utf-8") as jf:
                jf.write(json.dumps(result.__dict__, ensure_ascii=True) + "\n")
            print(
                f"[done] {domain} status={status} signup={result.signup or '-'} "
                f"login={result.login or '-'} api_key={'yes' if (result.api_key and result.api_key != 'NONE') else 'no'} "
                f"money={result.money or '-'} time={runtime_s:.1f}s"
            )

    await asyncio.gather(*(worker(url) for url in urls))

    fieldnames = list(JobResult.__dataclass_fields__.keys())
    with summary_csv.open("w", encoding="utf-8", newline="") as cf:
        writer = csv.DictWriter(cf, fieldnames=fieldnames)
        writer.writeheader()
        for r in results:
            writer.writerow(r.__dict__)

    print(f"[batch] total={len(results)} output_dir={out_dir}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="Concurrent batch runner for codex_super/signup_super.py")
    p.add_argument("urls", nargs="*", help="Target URLs")
    p.add_argument("--urls-file", default=None, help="Text file with one URL per line")
    p.add_argument("--concurrency", type=int, default=4)
    p.add_argument("--timeout-per-site", type=int, default=240, help="Hard cutoff seconds per site")
    p.add_argument("--llm", default="browseruse-fast")
    p.add_argument("--max-steps", type=int, default=8)
    p.add_argument("--signup-timeout", type=int, default=120)
    p.add_argument("--verify-timeout", type=int, default=30)
    p.add_argument("--login-timeout", type=int, default=90)
    p.add_argument("--proxy-country", default="us")
    p.add_argument(
        "--capture-summary",
        dest="capture_summary",
        action="store_true",
        default=True,
        help="Capture plan/limits/profile snapshot (default on).",
    )
    p.add_argument(
        "--no-capture-summary",
        dest="capture_summary",
        action="store_false",
        help="Disable plan/limits/profile snapshot extraction.",
    )
    p.add_argument("--allow-oauth", action="store_true", default=False)
    p.add_argument("--profile-id", default=None)
    p.add_argument("--allow-shared-profile", action="store_true", default=False)
    p.add_argument("--reuse-inbox", action="store_true", default=False)
    p.add_argument("--stop-active-browsers-first", action="store_true", default=True)
    p.add_argument("--output-dir", default=None)
    return p


def main() -> int:
    args = build_parser().parse_args()
    return asyncio.run(run_batch(args))


if __name__ == "__main__":
    raise SystemExit(main())
