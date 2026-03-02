"""CLI for TrialPilot 6-agent onboarding devtool."""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Iterable, TextIO

from .orchestrator import run_pipeline

APPROVAL_TYPES = ("terms", "sms", "kyc", "billing")

DEFAULT_CONFIG: dict[str, Any] = {
    "identity": {
        "full_name": "",
        "email": "",
        "phone": "",
        "country": "US",
    },
    "target": {
        "service_name": "",
        "signup_url": "",
        "api_key_path_hint": "/settings/api",
        "trial_plan": "free-trial",
        "expected_challenges": ["sms", "kyc"],
    },
    "policy": {
        "allowed_domains": [],
        "max_spend_usd": 10.0,
        "require_sms_approval": True,
        "require_kyc_approval": True,
        "require_terms_approval": True,
        "auto_cancel_hours_before_billing": 24,
        "cancel_immediately": True,
        "require_trial_started": False,
    },
    "payment": {
        "card_number": "",
        "exp_month": "",
        "exp_year": "",
        "cvc": "",
        "cardholder_name": "",
    },
    "verification": {
        "email_otp": "000000",
    },
}


class JsonArgumentParser(argparse.ArgumentParser):
    """Parser that raises typed errors so CLI can keep JSON-only output."""

    def error(self, message: str) -> None:  # pragma: no cover - argparse hook
        raise ValueError(message)


def _emit(payload: dict[str, Any], out: TextIO) -> None:
    json.dump(payload, out, sort_keys=True)
    out.write("\n")


def _load_config(value: str) -> dict[str, Any]:
    path = Path(value)
    raw = path.read_text(encoding="utf-8") if path.exists() else value
    data = json.loads(raw)
    if not isinstance(data, dict):
        raise ValueError("config must be a JSON object")
    return data


def _normalize_approvals(values: Iterable[str] | None) -> dict[str, bool]:
    approvals: dict[str, bool] = {}
    for value in values or []:
        normalized = str(value).strip().lower()
        if normalized in APPROVAL_TYPES:
            approvals[normalized] = True
    return approvals


def _exit_code_for_result(result: dict[str, Any]) -> int:
    status = str(result.get("status", ""))
    if status in {"success", "dry_run_success"}:
        return 0

    failure_code = str(result.get("failure_code") or "")
    if failure_code == "policy_denied":
        return 2
    if failure_code == "manual_approval_required":
        return 3
    if failure_code == "manual_input_required":
        return 4
    return 1


def _build_parser() -> JsonArgumentParser:
    parser = JsonArgumentParser(prog="trialpilot", add_help=False)
    sub = parser.add_subparsers(dest="command", required=True)

    bootstrap = sub.add_parser("bootstrap-config", add_help=False)
    bootstrap.add_argument("--out", required=True)

    run = sub.add_parser("run", add_help=False)
    run.add_argument("--config", required=True)
    run.add_argument("--approve", choices=APPROVAL_TYPES, action="append", default=[])
    run.add_argument("--dry-run", action="store_true")
    run.add_argument("--har")

    return parser


def main(argv: list[str] | None = None, out: TextIO | None = None) -> int:
    output = out if out is not None else sys.stdout
    parser = _build_parser()

    try:
        args = parser.parse_args(argv)
    except ValueError as exc:
        _emit({"ok": False, "error": "argparse_error", "detail": str(exc)}, output)
        return 2

    try:
        if args.command == "bootstrap-config":
            out_path = Path(args.out)
            out_path.parent.mkdir(parents=True, exist_ok=True)
            out_path.write_text(json.dumps(DEFAULT_CONFIG, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            _emit({"ok": True, "command": "bootstrap-config", "out": str(out_path)}, output)
            return 0

        if args.command == "run":
            config = _load_config(args.config)
            approvals = _normalize_approvals(args.approve)
            result = run_pipeline(
                config=config,
                approvals=approvals,
                dry_run=bool(args.dry_run),
                har_path=args.har,
            )
            code = _exit_code_for_result(result)
            _emit({"ok": code == 0, "command": "run", "result": result}, output)
            return code

        _emit({"ok": False, "error": "unknown_command"}, output)
        return 2
    except Exception as exc:
        _emit({"ok": False, "error": "runtime_error", "detail": f"{type(exc).__name__}: {exc}"}, output)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())
