"""Orchestrator for the TrialPilot 6-agent pipeline."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path
from typing import Any, Mapping
from uuid import uuid4

from .agents import (
    BillingAgent,
    CredentialAgent,
    IdentityAgent,
    PolicyAgent,
    SignupAgent,
    VerificationAgent,
)
from .har_intel import analyze_har
from .providers import ProviderRuntimeConfig, build_local_providers


class TrialPilotOrchestrator:
    """Executes six pipeline stages and returns structured JSON summary."""

    def __init__(
        self,
        config: Mapping[str, Any],
        *,
        approvals: Mapping[str, Any] | None = None,
        dry_run: bool = False,
        har_path: str | Path | None = None,
    ) -> None:
        self.config = dict(config)
        self.approvals = dict(approvals or {})
        self.dry_run = bool(dry_run)
        self.har_path = str(har_path) if har_path else None
        runtime = ProviderRuntimeConfig.from_env()
        self.runtime_config = replace(runtime, dry_run=self.dry_run)
        self.providers = build_local_providers(
            config=self.runtime_config
        )

        self.pipeline = [
            ("identity", IdentityAgent()),
            ("policy", PolicyAgent()),
            ("signup", SignupAgent()),
            ("verification", VerificationAgent()),
            ("billing", BillingAgent()),
            ("credential", CredentialAgent()),
        ]

    def _base_context(self) -> dict[str, Any]:
        ctx: dict[str, Any] = {
            "run_id": f"run_{uuid4().hex[:12]}",
            "input": self.config,
            "events": [],
            "artifacts": [],
            "output": {},
            "dry_run": self.dry_run,
        }
        if self.har_path:
            ctx["har_intelligence"] = analyze_har(self.har_path)
            ctx["events"].append({"stage": "har", "event": "har_loaded", "detail": self.har_path})
        return ctx

    def run(self) -> dict[str, Any]:
        context = self._base_context()
        completed_stages: list[str] = []
        stage_results: list[dict[str, Any]] = []
        failures: list[dict[str, Any]] = []

        for stage, agent in self.pipeline:
            try:
                context = agent.run(context, self.providers, self.approvals)
                completed_stages.append(stage)
                stage_results.append({"stage": stage, "ok": True})
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                failures.append({"stage": stage, "error": message})
                stage_results.append({"stage": stage, "ok": False, "error": message})
                context.setdefault("events", []).append(
                    {
                        "stage": stage,
                        "event": "failed",
                        "detail": message,
                    }
                )
                break

        if not failures:
            status = "dry_run_success" if self.dry_run else "success"
        else:
            status = "partial_failure" if completed_stages else "failed"

        failure_code = self._failure_code(failures)
        summary: dict[str, Any] = {
            "status": status,
            "failure_code": failure_code,
            "run_id": context.get("run_id"),
            "agents_total": len(self.pipeline),
            "agents_completed": len(completed_stages),
            "completed_stages": completed_stages,
            "stage_results": stage_results,
            "failures": failures,
            "provider_status": {
                "browseruse_api_key": self.runtime_config.browseruse_key_configured,
                "openai_api_key": self.runtime_config.openai_key_configured,
                "agentmail_api_key": self.runtime_config.agentmail_key_configured,
                "dry_run": self.runtime_config.dry_run,
                "ready_for_browseruse": (
                    self.runtime_config.browseruse_key_configured and not self.runtime_config.dry_run
                ),
            },
            "har_intelligence": context.get("har_intelligence"),
            "output": context.get("output", {}),
            "events": context.get("events", []),
            "artifacts": context.get("artifacts", []),
            "approvals": dict(self.approvals),
            "dry_run": self.dry_run,
        }
        return summary

    @staticmethod
    def _failure_code(failures: list[dict[str, Any]]) -> str | None:
        if not failures:
            return None
        error = failures[0].get("error", "")
        if "policy_denied" in error:
            return "policy_denied"
        if "manual_approval_required" in error:
            return "manual_approval_required"
        if "card_details_required" in error:
            return "manual_input_required"
        return "pipeline_failed"


def run_pipeline(
    config: Mapping[str, Any],
    approvals: Mapping[str, Any] | None = None,
    dry_run: bool = False,
    har_path: str | Path | None = None,
) -> dict[str, Any]:
    """Execute the full six-agent pipeline."""

    orchestrator = TrialPilotOrchestrator(
        config=config,
        approvals=approvals,
        dry_run=dry_run,
        har_path=har_path,
    )
    return orchestrator.run()


__all__ = ["TrialPilotOrchestrator", "run_pipeline"]
