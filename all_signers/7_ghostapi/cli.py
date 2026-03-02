from __future__ import annotations

import argparse
import asyncio
import csv
import json
import os
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from .capture import capture_har_with_playwright
from .enrichment import (
    EnrichmentStore,
    enrich_domain,
    normalize_domain,
    result_to_csv_header,
    result_to_csv_row,
)
from .analysis import analyze_har_file
from .models import Endpoint
from .pentest import build_authorized_audit_prompt, create_audit_report, run_nuclei_scan
from .pipeline import (
    analyze_artifacts,
    compare_with_recipe,
    enforce_scope,
    map_target,
    refresh_recipe_from_analysis,
    run_capture_map_sync,
    run_learning_pipeline,
    scan_target,
    try_fast_path_from_recipe_store,
    write_analysis_artifacts,
)
from .recipe_store import RecipeStore
from .report import build_output_dir, write_json, write_report_markdown
from .providers import get_provider_status


def _load_endpoints(path: str) -> list[Endpoint]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    endpoints = []
    for item in payload:
        endpoints.append(
            Endpoint(
                method=item.get("method", "GET"),
                url=item.get("url", ""),
                path=item.get("path", "/"),
                source=item.get("source", "unknown"),
                status=item.get("status"),
                content_type=item.get("content_type"),
                notes=item.get("notes", []) or [],
            )
        )
    return endpoints


def _serialize_endpoints(endpoints: list[Endpoint]) -> list[dict[str, Any]]:
    return [ep.to_dict() for ep in endpoints]


def _add_policy_args(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--confirm-authorized",
        action="store_true",
        help="Required safety flag: confirm explicit authorization.",
    )
    parser.add_argument(
        "--policy-file",
        default=None,
        help='Optional policy JSON, e.g. {"allowed_domains": ["example.com", "*.internal.example.com"]}.',
    )


def _resolve_include_hosts(hosts: list[str], host: str, no_host_filter: bool) -> set[str] | None:
    if no_host_filter:
        return set(hosts) if hosts else None
    return set(hosts) if hosts else {host}


def _host_for_url(url: str) -> str:
    parsed = urlsplit(url)
    if not parsed.netloc:
        raise SystemExit(f"Invalid URL: {url}")
    return parsed.netloc.lower()


def cmd_capture(args: argparse.Namespace) -> int:
    host = enforce_scope(args.target, args.confirm_authorized, args.policy_file)
    out_dir = build_output_dir(args.out_dir, host)
    out_har = str(out_dir / "capture.har")

    asyncio.run(
        capture_har_with_playwright(
            target_url=args.target,
            out_har=out_har,
            capture_seconds=args.capture_seconds,
            headed=args.headed,
            email=args.email,
            password=args.password,
            phone=args.phone,
            totp_secret=args.totp_secret,
        )
    )
    print(f"HAR captured: {out_har}")
    return 0


def cmd_map(args: argparse.Namespace) -> int:
    host = enforce_scope(args.target, args.confirm_authorized, args.policy_file)
    out_dir = build_output_dir(args.out_dir, host)

    result = map_target(
        target_url=args.target,
        target_host=host,
        har_path=args.har,
        run_passive=args.run_passive,
        js_files=args.js_file or [],
    )

    endpoints_json = out_dir / "endpoints.json"
    summary_json = out_dir / "map_summary.json"
    report_md = out_dir / "report.md"

    write_json(endpoints_json, _serialize_endpoints(result.endpoints))
    write_json(
        summary_json,
        {
            "target": args.target,
            "host": host,
            "used_har": result.used_har,
            "passive_count": result.passive_count,
            "har_count": result.har_count,
            "merged_count": len(result.endpoints),
        },
    )
    write_report_markdown(report_md, args.target, result.endpoints, findings=[])

    print(f"Mapped {len(result.endpoints)} endpoints")
    print(f"- {endpoints_json}")
    print(f"- {summary_json}")
    print(f"- {report_md}")
    return 0


def cmd_scan(args: argparse.Namespace) -> int:
    host = enforce_scope(args.target, args.confirm_authorized, args.policy_file)
    out_dir = build_output_dir(args.out_dir, host)
    endpoints = _load_endpoints(args.endpoints_file)
    findings = scan_target(args.target, endpoints, run_nuclei_checks=args.run_nuclei)

    findings_json = out_dir / "findings.json"
    report_md = out_dir / "report.md"
    write_json(findings_json, [f.to_dict() for f in findings])
    write_report_markdown(report_md, args.target, endpoints, findings)

    print(f"Generated {len(findings)} findings")
    print(f"- {findings_json}")
    print(f"- {report_md}")
    return 0


def cmd_analyze(args: argparse.Namespace) -> int:
    host = enforce_scope(args.target, args.confirm_authorized, args.policy_file)
    out_dir = build_output_dir(args.out_dir, host)

    artifacts = analyze_artifacts(
        target_url=args.target,
        har_path=args.har,
        server_name=args.server_name,
        include_hosts={host},
    )

    analysis_path = out_dir / "analysis.json"
    openapi_path = out_dir / "openapi.json"
    mcp_path = out_dir / "generated_mcp_server.py"

    write_json(analysis_path, artifacts["analysis"])
    write_json(openapi_path, artifacts["openapi"])
    mcp_path.write_text(artifacts["mcp_server"], encoding="utf-8")
    recipe_path = refresh_recipe_from_analysis(
        target_url=args.target,
        analysis=artifacts["analysis"],
        recipe_store_dir=args.recipe_store_dir,
    )

    print("Analysis artifacts generated")
    print(f"- {analysis_path}")
    print(f"- {openapi_path}")
    print(f"- {mcp_path}")
    print(f"- {recipe_path}")
    return 0


def cmd_replay(args: argparse.Namespace) -> int:
    enforce_scope(args.target, args.confirm_authorized, args.policy_file)
    fast = try_fast_path_from_recipe_store(
        target_url=args.target,
        recipe_store_dir=args.recipe_store_dir,
        min_confidence=args.min_confidence,
        auth_header=args.auth_header,
        method=args.method,
        path=args.path,
        allow_stateful=args.allow_stateful,
    )
    payload = {
        "attempted": fast.attempted,
        "used_fast_path": fast.used_fast_path,
        "success": fast.success,
        "reason": fast.reason,
        "recipe_path": fast.recipe_path,
        "result": fast.result,
    }
    print(json.dumps(payload, indent=2))
    return 0


def cmd_learn(args: argparse.Namespace) -> int:
    host = enforce_scope(args.target, args.confirm_authorized, args.policy_file)
    host_out = build_output_dir(args.out_dir, host)

    har_path = args.har
    if not har_path:
        har_path = str(host_out / "capture.har")
        asyncio.run(
            capture_har_with_playwright(
                target_url=args.target,
                out_har=har_path,
                capture_seconds=args.capture_seconds,
                headed=args.headed,
                email=args.email,
                password=args.password,
                phone=args.phone,
                totp_secret=args.totp_secret,
            )
        )

    include_hosts = _resolve_include_hosts(args.hosts or [], host, args.no_host_filter)
    store = RecipeStore(args.recipe_db)
    existing = None if args.fresh else (store.get(host) or {}).get("analysis")

    analysis = run_learning_pipeline(
        har_path=har_path,
        include_hosts=include_hosts,
        out_dir=host_out,
        title=args.title,
        server_name=args.server_name,
        mode=args.mode,
        profile=args.profile,
        existing_analysis=existing,
    )
    store.upsert(host=host, title=args.title, analysis=analysis)

    recipe_path = refresh_recipe_from_analysis(
        target_url=args.target,
        analysis=analysis,
        recipe_store_dir=args.recipe_store_dir,
    )
    print(f"Updated recipe memory for {host} in {args.recipe_db}")
    print(f"Recipe fast-path file: {recipe_path}")
    return 0


def cmd_list_recipes(args: argparse.Namespace) -> int:
    rows = RecipeStore(args.recipe_db).list_all()
    if not rows:
        print("No recipes yet.")
        return 0
    for row in rows:
        print(f"{row['host']}\tendpoints={row['endpoints_count']}\ttitle={row['title']}")
    return 0


def cmd_export_recipe(args: argparse.Namespace) -> int:
    item = RecipeStore(args.recipe_db).get(args.host)
    if item is None:
        raise SystemExit(f"Recipe not found for host: {args.host}")
    out_dir = Path(args.out_dir) / args.host
    write_analysis_artifacts(
        item["analysis"],
        out_dir=out_dir,
        title=args.title,
        server_name=args.server_name,
        profile=args.profile or "full",
    )
    print(f"Exported recipe for {args.host} -> {out_dir}")
    return 0


def cmd_compare(args: argparse.Namespace) -> int:
    host = args.host.lower()
    include_hosts = _resolve_include_hosts(args.hosts or [], host, args.no_host_filter)
    result = compare_with_recipe(
        host=host,
        har_path=args.har,
        recipe_db=args.recipe_db,
        include_hosts=include_hosts,
    )

    if args.out_file:
        out_path = Path(args.out_file)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
        print(f"Wrote {args.out_file}")

    print(json.dumps(result, indent=2, sort_keys=True))
    if args.fail_on_breaking and int((result.get("summary") or {}).get("breaking_count", 0)) > 0:
        return 1
    return 0


def cmd_enrich(args: argparse.Namespace) -> int:
    domain = normalize_domain(args.domain)
    domain_input = f"https://{domain}"
    domain = enforce_scope(domain_input, args.confirm_authorized, args.policy_file)
    store = EnrichmentStore(args.enrichment_db)
    result, cached = enrich_domain(
        domain,
        store=store,
        refresh=args.refresh,
        timeout_seconds=args.timeout_seconds,
    )
    payload = result.to_dict()
    payload["cached"] = cached
    print(json.dumps(payload, indent=2))
    return 0


def cmd_get_enrichment(args: argparse.Namespace) -> int:
    domain = normalize_domain(args.domain)
    store = EnrichmentStore(args.enrichment_db)
    result = store.get(domain)
    if not result:
        print(json.dumps({"error": "not_found", "domain": domain}, indent=2))
        return 1
    print(json.dumps(result.to_dict(), indent=2))
    return 0


def cmd_enrichment_stats(args: argparse.Namespace) -> int:
    store = EnrichmentStore(args.enrichment_db)
    print(json.dumps(store.stats(), indent=2))
    return 0


def cmd_enrich_batch(args: argparse.Namespace) -> int:
    rows = [line.strip() for line in Path(args.input_file).read_text(encoding="utf-8").splitlines() if line.strip()]
    if not rows:
        print(json.dumps({"error": "no_domains", "input_file": args.input_file}, indent=2))
        return 1

    store = EnrichmentStore(args.enrichment_db)
    results: list[dict[str, Any]] = []
    failures: list[dict[str, str]] = []
    for raw in rows:
        try:
            normalized = normalize_domain(raw)
            domain = enforce_scope(f"https://{normalized}", args.confirm_authorized, args.policy_file)
            result, cached = enrich_domain(
                domain,
                store=store,
                refresh=args.refresh,
                timeout_seconds=args.timeout_seconds,
            )
            payload = result.to_dict()
            payload["cached"] = cached
            results.append(payload)
        except Exception as exc:
            failures.append({"domain": raw, "error": str(exc)})
            if not args.continue_on_error:
                break

    out_path = Path(args.out_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if args.format == "json":
        out_path.write_text(json.dumps({"results": results, "failures": failures}, indent=2), encoding="utf-8")
    else:
        with out_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(result_to_csv_header() + ["cached"])
            for row in results:
                writer.writerow(
                    [
                        row.get("domain", ""),
                        row.get("company_name", "") or "",
                        ";".join(row.get("emails", []) or []),
                        ";".join(row.get("phone_numbers", []) or []),
                        row.get("contact_page_url", "") or "",
                        row.get("linkedin_url", "") or "",
                        str(row.get("confidence", "")),
                        ";".join(row.get("sources", []) or []),
                        row.get("created_at", "") or "",
                        str(bool(row.get("cached", False))).lower(),
                    ]
                )

    payload = {
        "ok": len(failures) == 0,
        "processed": len(results),
        "failed": len(failures),
        "out_file": str(out_path),
    }
    if failures:
        payload["failures"] = failures
    print(json.dumps(payload, indent=2))
    return 0 if not failures else 1


def cmd_export_enrichment(args: argparse.Namespace) -> int:
    domain = normalize_domain(args.domain)
    store = EnrichmentStore(args.enrichment_db)
    result = store.get(domain)
    if not result:
        print(json.dumps({"error": "not_found", "domain": domain}, indent=2))
        return 1

    out_path = Path(args.out_file)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    if args.format == "json":
        out_path.write_text(json.dumps(result.to_dict(), indent=2), encoding="utf-8")
    else:
        with out_path.open("w", encoding="utf-8", newline="") as f:
            writer = csv.writer(f)
            writer.writerow(result_to_csv_header())
            writer.writerow(result_to_csv_row(result))
    print(json.dumps({"ok": True, "out_file": str(out_path), "format": args.format}, indent=2))
    return 0


def cmd_trial(args: argparse.Namespace) -> int:
    """AI-driven trial signup + HAR capture + analysis + bypass detection."""
    import logging
    import time as _time

    from .wallet import load_wallet, generate_password
    from .signup_agent import run_signup_agent
    from .bypass_analyzer import analyze_for_bypasses, generate_curl_commands

    host = _host_for_url(args.target)
    out_dir = build_output_dir(args.out_dir, host)
    har_path = str(out_dir / "capture.har")

    # Setup logging to file + console
    log_path = out_dir / "trial.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(name)s] %(levelname)s: %(message)s",
        handlers=[
            logging.FileHandler(str(log_path)),
            logging.StreamHandler(),
        ],
        force=True,
    )
    log = logging.getLogger("ghostapi.trial")
    trial_start = _time.time()
    log.info("=" * 60)
    log.info("GhostAPI Trial — %s", args.target)
    log.info("=" * 60)

    # Step 1: Load or build identity from wallet + CLI overrides
    wallet = load_wallet(Path(args.wallet) if args.wallet else None)
    if args.email:
        wallet.email = args.email
    if args.password:
        wallet.password = args.password
    elif not wallet.password:
        wallet.password = generate_password()

    log.info("Wallet loaded: email=%s, name=%s %s, has_card=%s",
             wallet.email or "(none)", wallet.first_name, wallet.last_name,
             bool(wallet.card.number))

    if not wallet.email:
        # Auto-create agentmail inbox if API key is available
        agentmail_key = os.environ.get("AGENTMAIL_API_KEY", "")
        if agentmail_key:
            try:
                from agentmail import AgentMail
                am_client = AgentMail(api_key=agentmail_key)
                inbox = am_client.inboxes.create()
                wallet.email = inbox.inbox_id
                print(f"[trial] Auto-created inbox: {wallet.email}")
            except Exception as exc:
                raise SystemExit(f"No email provided and agentmail inbox creation failed: {exc}")
        else:
            raise SystemExit("Email is required. Pass --email, set AGENTMAIL_API_KEY for auto-inbox, or configure wallet.")

    headed = not args.headless

    log.info("Target: %s", args.target)
    log.info("Email: %s", wallet.email)
    log.info("Output: %s", out_dir)
    log.info("Card: %s", wallet.card.number[:4] + "****" if wallet.card.number else "(none)")

    # Step 2: Run Browser Use signup agent with HAR capture
    log.info("Step 2: Starting AI signup agent...")
    signup_result = asyncio.run(
        run_signup_agent(
            target_url=args.target,
            har_path=har_path,
            email=wallet.email,
            password=wallet.password,
            first_name=wallet.first_name,
            last_name=wallet.last_name,
            phone=wallet.phone,
            card_number=wallet.card.number,
            card_exp_month=wallet.card.exp_month,
            card_exp_year=wallet.card.exp_year,
            card_cvv=wallet.card.cvv,
            card_zip=wallet.card.zip_code,
            headed=headed,
            max_steps=args.max_steps,
        )
    )

    creds_path = out_dir / "credentials.json"
    write_json(creds_path, {
        "email": wallet.email,
        "password": wallet.password,
        "signup_success": signup_result.success,
        "error": signup_result.error,
    })
    status = "succeeded" if signup_result.success else "FAILED"
    log.info("Signup %s", status)
    if signup_result.error:
        log.error("Signup error: %s", signup_result.error)

    # Write agent history log
    if signup_result.agent_history:
        history_path = out_dir / "agent_history.json"
        write_json(history_path, signup_result.agent_history)
        log.info("Agent history: %d steps → %s", len(signup_result.agent_history), history_path)

    # Step 2b: Auto-verify email if agentmail inbox
    agentmail_key = os.environ.get("AGENTMAIL_API_KEY", "")
    if signup_result.success and agentmail_key and "@agentmail.to" in wallet.email:
        import re
        import time
        log.info("Step 2b: Checking agentmail for verification email...")
        time.sleep(5)  # Wait for email delivery
        try:
            from agentmail import AgentMail
            am = AgentMail(api_key=agentmail_key)
            msgs = am.inboxes.messages.list(inbox_id=wallet.email)
            for msg_item in (msgs.data if hasattr(msgs, 'data') else []):
                if any(kw in (msg_item.subject or "").lower() for kw in ["verify", "confirm", "activate"]):
                    full_msg = am.inboxes.messages.get(inbox_id=wallet.email, message_id=msg_item.message_id)
                    text = full_msg.text or full_msg.html or ""
                    links = re.findall(r'https?://[^\s<>\"\]]+', text)
                    verify_links = [l for l in links if any(k in l.lower() for k in ["verify", "confirm", "activate", "validation", "email-verification"])]
                    if verify_links:
                        verify_url = verify_links[0]
                        log.info("Found verification link: %s...", verify_url[:80])
                        try:
                            from browser_use import Agent as BUAgent, BrowserProfile as BUProfile
                            from browser_use.llm.openai.chat import ChatOpenAI as BUChatOpenAI
                            oai_key = os.environ.get("OPENAI_API_KEY", "")
                            bu_key = os.environ.get("BROWSER_USE_API_KEY", "")
                            if oai_key and bu_key:
                                v_llm = BUChatOpenAI(model="gpt-4o", api_key=oai_key)
                                v_profile = BUProfile(use_cloud=True)
                                v_agent = BUAgent(
                                    task=f"Navigate to {verify_url} and wait 5 seconds. If you see a code input field, enter any code from the URL parameters. Report what you see.",
                                    llm=v_llm, browser_profile=v_profile, max_steps=5,
                                )
                                asyncio.run(v_agent.run())
                                log.info("Email verified via cloud browser!")
                            else:
                                log.info("Verification link (no cloud key): %s", verify_url)
                        except Exception as ve:
                            log.warning("Verification attempt failed: %s", ve)
                            log.info("Manual verification link: %s", verify_url)
                    # Also check for OTP codes in emails
                    otp_codes = re.findall(r'\b(\d{4,8})\b', text)
                    if otp_codes and not verify_links:
                        log.info("OTP code found: %s", otp_codes[0])
                    break
        except Exception as exc:
            log.warning("Email check failed: %s", exc)

    # Step 3: Analyze HAR if captured (cloud may not produce local HAR)
    log.info("Step 3: HAR analysis...")
    analysis_path = None
    openapi_path = None
    mcp_path = None
    endpoint_count = 0
    analysis_artifacts = None

    if Path(har_path).exists():
        log.info("HAR file found at %s", har_path)
        analysis_artifacts = analyze_artifacts(
            target_url=args.target,
            har_path=har_path,
            server_name=host.replace(".", "_"),
            include_hosts={host},
        )

        analysis_path = out_dir / "analysis.json"
        openapi_path = out_dir / "openapi.json"
        mcp_path = out_dir / "generated_mcp_server.py"
        write_json(analysis_path, analysis_artifacts["analysis"])
        write_json(openapi_path, analysis_artifacts["openapi"])
        mcp_path.write_text(analysis_artifacts["mcp_server"], encoding="utf-8")

        endpoint_count = len(analysis_artifacts["analysis"].get("endpoints", []))
        log.info("Found %d API endpoints", endpoint_count)
    else:
        log.info("No HAR file (cloud mode) — skipping traffic analysis")

    # Step 4: Run bypass analysis if we have endpoints
    log.info("Step 4: Bypass analysis...")
    bypass_path = None
    curl_path = None
    if not args.skip_bypass and analysis_artifacts:
        log.info("Running Claude bypass analysis on %d endpoints...", endpoint_count)
        bypass_report = asyncio.run(
            analyze_for_bypasses(
                target_url=args.target,
                analysis=analysis_artifacts["analysis"],
                openapi_spec=analysis_artifacts["openapi"],
            )
        )

        bypass_path = out_dir / "bypass_report.json"
        write_json(bypass_path, bypass_report.to_dict())

        curl_script = generate_curl_commands(analysis_artifacts["analysis"], bypass_report)
        curl_path = out_dir / "curl_commands.sh"
        curl_path.write_text(curl_script, encoding="utf-8")

        log.info("Bypass findings: %d", len(bypass_report.findings))
        for finding in bypass_report.findings:
            log.info("  [%s] %s", finding.severity.upper(), finding.title)

    # Step 5: Summary
    elapsed_total = _time.time() - trial_start
    log.info("=" * 60)
    log.info("Trial complete for %s (%.1fs)", args.target, elapsed_total)
    log.info("=" * 60)
    log.info("  Credentials:    %s", creds_path)
    if Path(har_path).exists():
        log.info("  HAR capture:    %s", har_path)
    if analysis_path:
        log.info("  Analysis:       %s", analysis_path)
    if openapi_path:
        log.info("  OpenAPI spec:   %s", openapi_path)
    if mcp_path:
        log.info("  MCP server:     %s", mcp_path)
    if bypass_path:
        log.info("  Bypass report:  %s", bypass_path)
    if curl_path:
        log.info("  Curl commands:  %s", curl_path)
    log.info("  Full log:       %s", log_path)

    # Write summary JSON
    summary_path = out_dir / "trial_summary.json"
    write_json(summary_path, {
        "target": args.target,
        "email": wallet.email,
        "signup_success": signup_result.success,
        "signup_error": signup_result.error,
        "has_card": bool(wallet.card.number),
        "has_har": Path(har_path).exists(),
        "endpoint_count": endpoint_count,
        "bypass_findings": len(bypass_report.findings) if not args.skip_bypass and analysis_artifacts else 0,
        "elapsed_seconds": round(elapsed_total, 1),
        "agent_steps": len(signup_result.agent_history),
        "output_dir": str(out_dir),
    })
    log.info("  Summary:        %s", summary_path)
    return 0


def cmd_provider_health(_args: argparse.Namespace) -> int:
    status = get_provider_status()
    payload = {
        "ok": True,
        "provider_keys": status.to_dict(),
        "ready_for_browseruse": status.browseruse_api_key and (status.openai_api_key or status.anthropic_api_key),
    }
    print(json.dumps(payload, indent=2))
    return 0


def cmd_run(args: argparse.Namespace) -> int:
    host = enforce_scope(args.target, args.confirm_authorized, args.policy_file)
    out_dir = build_output_dir(args.out_dir, host)

    fast_result = None
    if not args.har and not args.skip_fast_path:
        fast_result = try_fast_path_from_recipe_store(
            target_url=args.target,
            recipe_store_dir=args.recipe_store_dir,
            min_confidence=args.min_confidence,
            auth_header=args.auth_header,
            method=args.fast_method,
            path=args.fast_path,
            allow_stateful=args.allow_stateful,
        )
        if fast_result.used_fast_path and fast_result.success:
            fast_json = out_dir / "fast_path_result.json"
            summary_json = out_dir / "run_summary.json"
            write_json(
                fast_json,
                {
                    "result": fast_result.result,
                    "reason": fast_result.reason,
                    "recipe_path": fast_result.recipe_path,
                },
            )
            write_json(
                summary_json,
                {
                    "target": args.target,
                    "host": host,
                    "mode": "fast_path",
                    "fast_path": {
                        "attempted": fast_result.attempted,
                        "used_fast_path": fast_result.used_fast_path,
                        "success": fast_result.success,
                        "reason": fast_result.reason,
                        "recipe_path": fast_result.recipe_path,
                    },
                },
            )
            print(f"Fast path success for {args.target}")
            print(f"- {fast_json}")
            print(f"- {summary_json}")
            return 0

    har_path = args.har or str(out_dir / "capture.har")
    if args.har:
        map_result = map_target(
            target_url=args.target,
            target_host=host,
            har_path=har_path,
            run_passive=args.run_passive,
            js_files=args.js_file or [],
        )
    else:
        map_result = run_capture_map_sync(
            target_url=args.target,
            out_har=har_path,
            target_host=host,
            capture_seconds=args.capture_seconds,
            headed=args.headed,
            run_passive=args.run_passive,
            js_files=args.js_file or [],
            email=args.email,
            password=args.password,
            phone=args.phone,
            totp_secret=args.totp_secret,
        )

    findings = scan_target(args.target, map_result.endpoints, run_nuclei_checks=args.run_nuclei)
    analysis_artifacts = analyze_artifacts(
        target_url=args.target,
        har_path=har_path,
        server_name=args.server_name,
        include_hosts={host},
    )

    endpoints_json = out_dir / "endpoints.json"
    findings_json = out_dir / "findings.json"
    summary_json = out_dir / "run_summary.json"
    report_md = out_dir / "report.md"
    analysis_json = out_dir / "analysis.json"
    openapi_json = out_dir / "openapi.json"
    mcp_server = out_dir / "generated_mcp_server.py"

    write_json(endpoints_json, _serialize_endpoints(map_result.endpoints))
    write_json(findings_json, [f.to_dict() for f in findings])
    write_json(analysis_json, analysis_artifacts["analysis"])
    write_json(openapi_json, analysis_artifacts["openapi"])
    mcp_server.write_text(analysis_artifacts["mcp_server"], encoding="utf-8")
    recipe_path = refresh_recipe_from_analysis(
        target_url=args.target,
        analysis=analysis_artifacts["analysis"],
        recipe_store_dir=args.recipe_store_dir,
    )

    write_json(
        summary_json,
        {
            "target": args.target,
            "host": host,
            "used_har": har_path,
            "passive_count": map_result.passive_count,
            "har_count": map_result.har_count,
            "merged_count": len(map_result.endpoints),
            "findings_count": len(findings),
            "mode": "slow_path_capture" if not args.har else "har_only",
            "fast_path": {
                "attempted": bool(fast_result and fast_result.attempted),
                "used_fast_path": bool(fast_result and fast_result.used_fast_path),
                "success": bool(fast_result and fast_result.success),
                "reason": fast_result.reason if fast_result else "not_attempted",
            },
            "recipe_path": recipe_path,
        },
    )
    write_report_markdown(report_md, args.target, map_result.endpoints, findings)

    print(f"Complete run finished for {args.target}")
    print(f"- {endpoints_json}")
    print(f"- {findings_json}")
    print(f"- {analysis_json}")
    print(f"- {openapi_json}")
    print(f"- {mcp_server}")
    print(f"- {recipe_path}")
    print(f"- {summary_json}")
    print(f"- {report_md}")
    return 0


def cmd_pentest(args: argparse.Namespace) -> int:
    host = enforce_scope(args.target, args.confirm_authorized, args.policy_file)
    out_dir = build_output_dir(args.out_dir, host)

    har_path = args.har or str(out_dir / "capture.har")
    if args.har:
        map_result = map_target(
            target_url=args.target,
            target_host=host,
            har_path=har_path,
            run_passive=args.run_passive,
            js_files=args.js_file or [],
        )
    else:
        map_result = run_capture_map_sync(
            target_url=args.target,
            out_har=har_path,
            target_host=host,
            capture_seconds=args.capture_seconds,
            headed=args.headed,
            run_passive=args.run_passive,
            js_files=args.js_file or [],
            email=args.email,
            password=args.password,
            phone=args.phone,
            totp_secret=args.totp_secret,
        )

    findings = scan_target(args.target, map_result.endpoints, run_nuclei_checks=args.run_nuclei)
    analysis_artifacts = analyze_artifacts(
        target_url=args.target,
        har_path=har_path,
        server_name=args.server_name,
        include_hosts={host},
    )
    site_analysis = analyze_har_file(har_path=har_path, site=args.target)

    endpoints_json = out_dir / "endpoints.json"
    findings_json = out_dir / "findings.json"
    summary_json = out_dir / "pentest_summary.json"
    report_md = out_dir / "report.md"
    analysis_json = out_dir / "analysis.json"
    openapi_json = out_dir / "openapi.json"
    mcp_server = out_dir / "generated_mcp_server.py"
    audit_prompt = out_dir / "authorized_audit_prompt.md"
    audit_report = out_dir / "audit_report.json"

    write_json(endpoints_json, _serialize_endpoints(map_result.endpoints))
    write_json(findings_json, [f.to_dict() for f in findings])
    write_json(analysis_json, analysis_artifacts["analysis"])
    write_json(openapi_json, analysis_artifacts["openapi"])
    mcp_server.write_text(analysis_artifacts["mcp_server"], encoding="utf-8")
    audit_prompt.write_text(build_authorized_audit_prompt(site_analysis), encoding="utf-8")

    nuclei_summary = {"ran": False, "reason": "disabled"}
    if args.run_nuclei_audit:
        nuclei_summary = run_nuclei_scan(
            target=args.target,
            output_json=str(out_dir / "nuclei.jsonl"),
        )
    create_audit_report(site_analysis, nuclei_summary=nuclei_summary, output_path=str(audit_report))

    write_json(
        summary_json,
        {
            "target": args.target,
            "host": host,
            "used_har": har_path,
            "passive_count": map_result.passive_count,
            "har_count": map_result.har_count,
            "merged_count": len(map_result.endpoints),
            "findings_count": len(findings),
            "mode": "pentest",
            "audit_report": str(audit_report),
            "audit_prompt": str(audit_prompt),
            "nuclei_audit": nuclei_summary,
        },
    )
    write_report_markdown(report_md, args.target, map_result.endpoints, findings)

    print(f"Pentest run finished for {args.target}")
    print(f"- {endpoints_json}")
    print(f"- {findings_json}")
    print(f"- {analysis_json}")
    print(f"- {openapi_json}")
    print(f"- {mcp_server}")
    print(f"- {audit_prompt}")
    print(f"- {audit_report}")
    print(f"- {summary_json}")
    print(f"- {report_md}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="ghostapi",
        description="Vertical-first contact enrichment platform with authorized API mapping infrastructure.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    capture = sub.add_parser("capture", help="Capture HAR traffic with Playwright.")
    capture.add_argument("--target", required=True, help="Target URL")
    capture.add_argument("--out-dir", default="ghostapi_out", help="Output base directory")
    capture.add_argument("--capture-seconds", type=int, default=20, help="How long to keep the browser open")
    capture.add_argument("--headed", action="store_true", help="Run browser headed")
    capture.add_argument("--email", default=None, help="Optional login email")
    capture.add_argument("--password", default=None, help="Optional login password")
    capture.add_argument("--phone", default=None, help="Optional phone token for OTP SMS lookup")
    capture.add_argument("--totp-secret", default=None, help="Optional TOTP seed for authenticator-based 2FA")
    _add_policy_args(capture)
    capture.set_defaults(func=cmd_capture)

    map_cmd = sub.add_parser("map", help="Map endpoints from HAR and passive recon tools.")
    map_cmd.add_argument("--target", required=True, help="Target URL")
    map_cmd.add_argument("--har", required=True, help="HAR path to parse")
    map_cmd.add_argument("--run-passive", action="store_true", help="Run katana/gau/jsluice if installed")
    map_cmd.add_argument("--js-file", action="append", help="Optional JS file path (repeatable) for jsluice")
    map_cmd.add_argument("--out-dir", default="ghostapi_out", help="Output base directory")
    _add_policy_args(map_cmd)
    map_cmd.set_defaults(func=cmd_map)

    scan = sub.add_parser("scan", help="Run baseline checks and optional nuclei scan.")
    scan.add_argument("--target", required=True, help="Target URL")
    scan.add_argument("--endpoints-file", required=True, help="Path to endpoints.json")
    scan.add_argument("--run-nuclei", action="store_true", help="Run nuclei if installed")
    scan.add_argument("--out-dir", default="ghostapi_out", help="Output base directory")
    _add_policy_args(scan)
    scan.set_defaults(func=cmd_scan)

    analyze = sub.add_parser("analyze", help="Generate analysis + OpenAPI + MCP scaffold from HAR.")
    analyze.add_argument("--target", required=True, help="Target URL")
    analyze.add_argument("--har", required=True, help="HAR file path")
    analyze.add_argument("--server-name", default="captured_site", help="Generated MCP server name")
    analyze.add_argument("--recipe-store-dir", default="ghostapi_recipes", help="Directory for persisted fast-path recipes")
    analyze.add_argument("--out-dir", default="ghostapi_out", help="Output base directory")
    _add_policy_args(analyze)
    analyze.set_defaults(func=cmd_analyze)

    replay = sub.add_parser("replay", help="Try fast-path replay from stored recipe.")
    replay.add_argument("--target", required=True, help="Target URL")
    replay.add_argument("--recipe-store-dir", default="ghostapi_recipes", help="Directory for persisted recipes")
    replay.add_argument("--min-confidence", type=float, default=0.0, help="Minimum recipe confidence to replay")
    replay.add_argument("--auth-header", default=None, help='Optional Authorization header value, e.g. "Bearer <token>"')
    replay.add_argument("--method", default=None, help="Override HTTP method for replay")
    replay.add_argument("--path", default=None, help="Override endpoint path for replay")
    replay.add_argument("--allow-stateful", action="store_true", help="Allow non-GET replay methods")
    _add_policy_args(replay)
    replay.set_defaults(func=cmd_replay)

    run = sub.add_parser("run", help="Fast-path replay, then fallback to capture+analyze+scan.")
    run.add_argument("--target", required=True, help="Target URL")
    run.add_argument("--har", default=None, help="Use existing HAR instead of capturing")
    run.add_argument("--capture-seconds", type=int, default=20, help="Capture duration if HAR not supplied")
    run.add_argument("--headed", action="store_true", help="Run browser headed")
    run.add_argument("--run-passive", action="store_true", help="Run katana/gau/jsluice if installed")
    run.add_argument("--run-nuclei", action="store_true", help="Run nuclei if installed")
    run.add_argument("--js-file", action="append", help="Optional JS file path (repeatable)")
    run.add_argument("--server-name", default="captured_site", help="Generated MCP server name")
    run.add_argument("--email", default=None, help="Optional login email")
    run.add_argument("--password", default=None, help="Optional login password")
    run.add_argument("--phone", default=None, help="Optional phone token for OTP SMS lookup")
    run.add_argument("--totp-secret", default=None, help="Optional TOTP seed for authenticator-based 2FA")
    run.add_argument("--recipe-store-dir", default="ghostapi_recipes", help="Directory for persisted recipes")
    run.add_argument("--min-confidence", type=float, default=0.65, help="Minimum confidence required for fast-path replay")
    run.add_argument("--skip-fast-path", action="store_true", help="Disable fast-path replay and force capture/analyze")
    run.add_argument("--auth-header", default=None, help="Optional Authorization header value for fast-path replay")
    run.add_argument("--fast-method", default=None, help="Override method for fast-path replay")
    run.add_argument("--fast-path", default=None, help="Override endpoint path for fast-path replay")
    run.add_argument("--allow-stateful", action="store_true", help="Allow non-GET fast-path replay")
    run.add_argument("--out-dir", default="ghostapi_out", help="Output base directory")
    _add_policy_args(run)
    run.set_defaults(func=cmd_run)

    pentest = sub.add_parser(
        "pentest",
        help="Authorized pentest pipeline: capture/map/scan/analyze + audit artifacts.",
    )
    pentest.add_argument("--target", required=True, help="Target URL")
    pentest.add_argument("--har", default=None, help="Use existing HAR instead of capturing")
    pentest.add_argument("--capture-seconds", type=int, default=20, help="Capture duration if HAR not supplied")
    pentest.add_argument("--headed", action="store_true", help="Run browser headed")
    pentest.add_argument("--run-passive", action="store_true", help="Run katana/gau/jsluice if installed")
    pentest.add_argument("--run-nuclei", action="store_true", help="Run nuclei in baseline scan stage")
    pentest.add_argument("--run-nuclei-audit", action="store_true", help="Run nuclei for audit report stage")
    pentest.add_argument("--js-file", action="append", help="Optional JS file path (repeatable)")
    pentest.add_argument("--server-name", default="captured_site", help="Generated MCP server name")
    pentest.add_argument("--email", default=None, help="Optional login email")
    pentest.add_argument("--password", default=None, help="Optional login password")
    pentest.add_argument("--phone", default=None, help="Optional phone token for OTP SMS lookup")
    pentest.add_argument("--totp-secret", default=None, help="Optional TOTP seed for authenticator-based 2FA")
    pentest.add_argument("--out-dir", default="ghostapi_out", help="Output base directory")
    _add_policy_args(pentest)
    pentest.set_defaults(func=cmd_pentest)

    learn = sub.add_parser("learn", help="Capture/analyze and update durable recipe memory (SQLite + fast recipe).")
    learn.add_argument("--target", required=True, help="Target URL")
    learn.add_argument("--har", default=None, help="Path to HAR file (if omitted, captures)")
    learn.add_argument("--out-dir", default="ghostapi_out", help="Output base directory")
    learn.add_argument("--title", default="Captured API", help="Artifact title")
    learn.add_argument("--server-name", default="captured_site", help="Generated MCP server name")
    learn.add_argument("--mode", default="generic", choices=["generic", "integration-bootstrap", "due-diligence"])
    learn.add_argument("--profile", default=None, choices=["inventory", "openapi", "mcp", "full"])
    learn.add_argument("--hosts", nargs="*", default=[], help="Optional host allowlist override")
    learn.add_argument("--no-host-filter", action="store_true", help="Analyze all hosts in HAR")
    learn.add_argument("--recipe-db", default="ghostapi/recipes.db", help="SQLite recipe memory DB path")
    learn.add_argument("--recipe-store-dir", default="ghostapi_recipes", help="Directory for fast-path recipe files")
    learn.add_argument("--capture-seconds", type=int, default=20)
    learn.add_argument("--headed", action="store_true")
    learn.add_argument("--email", default=None)
    learn.add_argument("--password", default=None)
    learn.add_argument("--phone", default=None)
    learn.add_argument("--totp-secret", default=None)
    learn.add_argument("--fresh", action="store_true", help="Do not merge with prior recipe memory")
    _add_policy_args(learn)
    learn.set_defaults(func=cmd_learn)

    list_recipes = sub.add_parser("list-recipes", help="List stored recipe memory entries.")
    list_recipes.add_argument("--recipe-db", default="ghostapi/recipes.db")
    list_recipes.set_defaults(func=cmd_list_recipes)

    export_recipe = sub.add_parser("export-recipe", help="Export artifacts from stored recipe analysis.")
    export_recipe.add_argument("--host", required=True)
    export_recipe.add_argument("--out-dir", default="ghostapi_out")
    export_recipe.add_argument("--title", default="Captured API")
    export_recipe.add_argument("--server-name", default="captured_site")
    export_recipe.add_argument("--profile", default=None, choices=["inventory", "openapi", "mcp", "full"])
    export_recipe.add_argument("--recipe-db", default="ghostapi/recipes.db")
    export_recipe.set_defaults(func=cmd_export_recipe)

    compare = sub.add_parser("compare", help="Compare baseline recipe vs new HAR and detect drift.")
    compare.add_argument("--host", required=True)
    compare.add_argument("--har", required=True)
    compare.add_argument("--recipe-db", default="ghostapi/recipes.db")
    compare.add_argument("--hosts", nargs="*", default=[])
    compare.add_argument("--no-host-filter", action="store_true")
    compare.add_argument("--fail-on-breaking", action="store_true")
    compare.add_argument("--out-file", default=None)
    compare.set_defaults(func=cmd_compare)

    enrich = sub.add_parser("enrich", help="Vertical mode: authorized contact enrichment for one domain.")
    enrich.add_argument("--domain", required=True, help="Domain or URL")
    enrich.add_argument("--enrichment-db", default="ghostapi_enrichment.db")
    enrich.add_argument("--refresh", action="store_true")
    enrich.add_argument("--timeout-seconds", type=int, default=12)
    _add_policy_args(enrich)
    enrich.set_defaults(func=cmd_enrich)

    enrich_batch = sub.add_parser("enrich-batch", help="Run enrichment for many domains from a text file.")
    enrich_batch.add_argument("--input-file", required=True, help="Path to newline-delimited domains/URLs")
    enrich_batch.add_argument("--out-file", required=True, help="Output path for batch results")
    enrich_batch.add_argument("--format", default="json", choices=["json", "csv"])
    enrich_batch.add_argument("--enrichment-db", default="ghostapi_enrichment.db")
    enrich_batch.add_argument("--refresh", action="store_true")
    enrich_batch.add_argument("--timeout-seconds", type=int, default=12)
    enrich_batch.add_argument("--continue-on-error", action="store_true")
    _add_policy_args(enrich_batch)
    enrich_batch.set_defaults(func=cmd_enrich_batch)

    get_enrichment = sub.add_parser("get-enrichment", help="Get cached enrichment result.")
    get_enrichment.add_argument("--domain", required=True)
    get_enrichment.add_argument("--enrichment-db", default="ghostapi_enrichment.db")
    get_enrichment.set_defaults(func=cmd_get_enrichment)

    export_enrichment = sub.add_parser("export-enrichment", help="Export one cached enrichment result.")
    export_enrichment.add_argument("--domain", required=True)
    export_enrichment.add_argument("--out-file", required=True)
    export_enrichment.add_argument("--format", default="json", choices=["json", "csv"])
    export_enrichment.add_argument("--enrichment-db", default="ghostapi_enrichment.db")
    export_enrichment.set_defaults(func=cmd_export_enrichment)

    stats = sub.add_parser("enrichment-stats", help="Get enrichment DB stats.")
    stats.add_argument("--enrichment-db", default="ghostapi_enrichment.db")
    stats.set_defaults(func=cmd_enrichment_stats)

    trial = sub.add_parser(
        "trial",
        help="AI-driven trial signup + HAR capture + analysis + bypass detection.",
    )
    trial.add_argument("--target", required=True, help="Service URL to sign up for")
    trial.add_argument("--email", default=None, help="Email for signup (overrides wallet)")
    trial.add_argument("--password", default=None, help="Password (auto-generated if omitted)")
    trial.add_argument("--wallet", default=None, help="Path to identity wallet JSON file")
    trial.add_argument("--headless", action="store_true", help="Run browser headless (default: headed)")
    trial.add_argument("--max-steps", type=int, default=50, help="Max Browser Use agent steps")
    trial.add_argument("--skip-bypass", action="store_true", help="Skip Claude bypass analysis")
    trial.add_argument("--out-dir", default="ghostapi_out", help="Output base directory")
    trial.set_defaults(func=cmd_trial)

    provider_health = sub.add_parser(
        "provider-health",
        help="Show whether BrowserUse/LLM provider API keys are configured.",
    )
    provider_health.set_defaults(func=cmd_provider_health)

    return parser


def _load_dotenv() -> None:
    """Load .env file from current dir or project root."""
    for p in [Path(".env"), Path(__file__).resolve().parent.parent / ".env"]:
        if p.exists():
            for line in p.read_text().splitlines():
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, _, val = line.partition("=")
                    os.environ.setdefault(key.strip(), val.strip())
            break


def main(argv: list[str] | None = None) -> int:
    _load_dotenv()
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)
