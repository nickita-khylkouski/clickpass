"""Six deterministic runtime agents for TrialPilot."""

from __future__ import annotations

import hashlib
import json
import re
from datetime import datetime, timezone
from typing import Any, Mapping, MutableMapping

from .models import IdentityProfile, PipelineAgent, TargetService, TrialPolicy

Context = MutableMapping[str, Any]


def _digest(payload: Any) -> str:
    encoded = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()[:12]


def _ensure_collections(context: Context) -> None:
    if not isinstance(context.get("events"), list):
        context["events"] = []
    if not isinstance(context.get("artifacts"), list):
        context["artifacts"] = []
    if not isinstance(context.get("output"), dict):
        context["output"] = {}


def _add_record(
    context: Context,
    *,
    stage: PipelineAgent,
    event: str,
    detail: str,
    data: Mapping[str, Any] | None = None,
) -> None:
    _ensure_collections(context)
    created_at = datetime.now(timezone.utc).isoformat()
    payload = {
        "stage": stage.value,
        "event": event,
        "detail": detail,
        "created_at": created_at,
    }
    if data:
        payload["data"] = dict(data)
    context["events"].append(payload)
    context["artifacts"].append(payload)


def _approval(approvals: Mapping[str, Any], key: str, *, default: bool = False) -> bool:
    return bool(approvals.get(key, default))


def _result_text(run_result: Mapping[str, Any]) -> str:
    chunks: list[str] = []
    message = run_result.get("message")
    if message:
        chunks.append(str(message))
    raw = run_result.get("raw")
    if isinstance(raw, Mapping):
        output = raw.get("output")
        if output:
            chunks.append(str(output))
        judgement = raw.get("judgement")
        if judgement:
            chunks.append(str(judgement))
    return "\n".join(chunks)


def _has_signup_blocker(text: str) -> bool:
    lowered = text.lower()
    blocker_terms = (
        "blocked",
        "otp",
        "verification code",
        "captcha",
        "login required",
        "payment wall",
        "requires manual",
    )
    return any(term in lowered for term in blocker_terms)


def _extract_api_key(text: str) -> str:
    patterns = [
        r"\b(sk-[A-Za-z0-9_\-]{16,})\b",
        r"\b(tp_[A-Za-z0-9]{10,})\b",
        r"\b(minimax_[A-Za-z0-9_\-]{16,})\b",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            return match.group(1)
    return ""


def _contains_any(text: str, terms: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(term in lowered for term in terms)


class IdentityAgent:
    """Loads and validates the delegated user identity."""

    def run(self, context: Context, providers: Any, approvals: Mapping[str, Any]) -> Context:
        source = dict(context.get("input", {}).get("identity", {}) or {})
        identity = IdentityProfile.from_dict(source)

        missing = []
        if not identity.full_name:
            missing.append("full_name")
        if not identity.email:
            missing.append("email")
        if not identity.phone:
            missing.append("phone")
        if missing:
            raise ValueError(f"identity_missing_fields:{','.join(missing)}")

        context["identity"] = identity.to_dict()
        _add_record(
            context,
            stage=PipelineAgent.IDENTITY,
            event="identity_ready",
            detail="Identity profile loaded and validated",
            data={"email": identity.email, "country": identity.country},
        )
        return context


class PolicyAgent:
    """Enforces domain/safety policy and required user approvals."""

    def run(self, context: Context, providers: Any, approvals: Mapping[str, Any]) -> Context:
        raw_policy = dict(context.get("input", {}).get("policy", {}) or {})
        raw_target = dict(context.get("input", {}).get("target", {}) or {})

        policy = TrialPolicy.from_dict(raw_policy)
        target = TargetService.from_dict(raw_target)
        if not target.signup_url:
            raise ValueError("target_signup_url_required")

        if policy.allowed_domains:
            domain = target.signup_domain
            allowed = any(domain == item or domain.endswith(f".{item}") for item in policy.allowed_domains)
            if not allowed:
                raise PermissionError(f"policy_denied_domain:{domain}")

        if policy.require_terms_approval and not _approval(approvals, "terms", default=False):
            raise PermissionError("policy_denied_terms_unapproved")

        context["policy"] = policy.to_dict()
        context["target"] = target.to_dict()
        _add_record(
            context,
            stage=PipelineAgent.POLICY,
            event="policy_passed",
            detail="Policy checks passed",
            data={"domain": target.signup_domain, "max_spend_usd": policy.max_spend_usd},
        )
        return context


class SignupAgent:
    """Runs browser signup automation through Browser Use adapter."""

    def run(self, context: Context, providers: Any, approvals: Mapping[str, Any]) -> Context:
        target = dict(context.get("target", {}) or {})
        identity = dict(context.get("identity", {}) or {})
        auth = dict(context.get("input", {}).get("auth", {}) or {})
        account_password = str(auth.get("password", "")).strip()
        browseruse = providers.get("browseruse") if providers is not None else None

        instruction = [
            f"Create account for {target.get('service_name', 'service')} "
            f"at {target.get('signup_url', '')} using user email {identity.get('email', '')}.",
            "Do not attempt to access inbox/email provider.",
            "Stop when verification code (OTP) prompt appears and report current URL.",
        ]
        if account_password:
            instruction.append(f"Use this account password for signup if required: {account_password}.")
        instruction_text = " ".join(instruction)

        if browseruse is None:
            run_result: dict[str, Any] = {
                "ok": True,
                "mock": True,
                "message": "BrowserUse provider unavailable; simulated signup result.",
            }
        else:
            run_result = browseruse.run_task(
                instruction=instruction_text,
                url=target.get("signup_url"),
                context={
                    "identity_email": identity.get("email"),
                    "service": target.get("service_name"),
                },
            )

        if not run_result.get("ok"):
            raise RuntimeError("signup_failed")
        result_text = _result_text(run_result)
        if result_text and _has_signup_blocker(result_text):
            lowered = result_text.lower()
            if "otp" in lowered or "verification code" in lowered:
                context["signup_requires_otp"] = True
            else:
                raise PermissionError("manual_approval_required:signup_blocked")

        signup_id = f"signup_{_digest([identity.get('email'), target.get('signup_url')])}"
        context["signup"] = {
            "signup_id": signup_id,
            "task_id": run_result.get("task_id", ""),
            "session_id": run_result.get("session_id", ""),
            "provider_message": run_result.get("message", ""),
            "provider_output": result_text[:1000],
            "requires_otp": bool(context.get("signup_requires_otp")),
        }

        _add_record(
            context,
            stage=PipelineAgent.SIGNUP,
            event="signup_completed",
            detail="Signup automation completed",
            data={"signup_id": signup_id, "task_id": run_result.get("task_id", "")},
        )
        return context


class VerificationAgent:
    """Handles email OTP and manual SMS/KYC approvals."""

    def run(self, context: Context, providers: Any, approvals: Mapping[str, Any]) -> Context:
        identity = dict(context.get("identity", {}) or {})
        policy = TrialPolicy.from_dict(dict(context.get("policy", {}) or {}))
        target = TargetService.from_dict(dict(context.get("target", {}) or {}))
        signup = dict(context.get("signup", {}) or {})

        verifier = providers.get("verification") if providers is not None else None
        if verifier is None:
            raise RuntimeError("verification_provider_missing")

        sent = verifier.send_code(destination=str(identity.get("email", "")), channel="email")
        if not sent.get("ok"):
            raise RuntimeError("email_otp_send_failed")

        # If AgentMail provider is available, poll real inbox first.
        fetch_latest_otp = getattr(verifier, "fetch_latest_otp", None)
        otp_code = ""
        if callable(fetch_latest_otp):
            otp_result = fetch_latest_otp(
                destination=str(identity.get("email", "")),
                provider_hint=target.service_name or None,
            )
            if otp_result.get("ok"):
                otp_code = str(otp_result.get("otp", "")).strip()
            else:
                otp_code = ""

        if not otp_code:
            expected_code = sent.get("mock_code", "000000")
            otp_code = (
                str(context.get("input", {}).get("verification", {}).get("email_otp", "")).strip() or expected_code
            )

        verified = verifier.verify_code(destination=str(identity.get("email", "")), code=otp_code)
        if not verified.get("ok"):
            raise PermissionError("email_verification_failed")

        # If signup stage reported OTP blocker, try one continuation task with the discovered code.
        provider_output = str(signup.get("provider_output", ""))
        if provider_output and ("otp" in provider_output.lower() or "verification" in provider_output.lower()):
            browseruse = providers.get("browseruse") if providers is not None else None
            if browseruse is None:
                raise RuntimeError("browseruse_provider_missing_for_otp_continue")
            auth = dict(context.get("input", {}).get("auth", {}) or {})
            account_password = str(auth.get("password", "")).strip()
            password_hint = f" Use account password {account_password}." if account_password else ""
            continue_context: dict[str, Any] = {"service": target.service_name, "otp_code": otp_code}
            session_id = str(signup.get("session_id", "")).strip()
            if session_id:
                continue_context["_browseruse"] = {"sessionId": session_id, "maxSteps": 30, "saveBrowserData": True}
            continue_result = browseruse.run_task(
                instruction=(
                    f"Continue signup/login for {target.service_name}. "
                    f"Use OTP code {otp_code}. "
                    f"{password_hint}"
                    " If successful, proceed to developer dashboard."
                ),
                url=target.signup_url,
                context=continue_context,
            )
            if not continue_result.get("ok") and session_id:
                # Session can be closed server-side; retry once without session pinning.
                continue_result = browseruse.run_task(
                    instruction=(
                        f"Retry login/signup for {target.service_name} from scratch. "
                        f"Use email {identity.get('email', '')} and OTP code {otp_code}. "
                        f"{password_hint}"
                        "If OTP expired, report that explicitly."
                    ),
                    url=target.signup_url,
                    context={"service": target.service_name, "otp_code": otp_code, "retry": "fresh-session"},
                )
            if not continue_result.get("ok"):
                raise PermissionError("manual_approval_required:otp_resume_failed")

        needs_sms = policy.require_sms_approval or "sms" in target.expected_challenges
        needs_kyc = policy.require_kyc_approval or "kyc" in target.expected_challenges

        if needs_sms and not _approval(approvals, "sms", default=False):
            raise PermissionError("manual_approval_required:sms")
        if needs_kyc and not _approval(approvals, "kyc", default=False):
            raise PermissionError("manual_approval_required:kyc")

        context["verification"] = {
            "email_verified": True,
            "email_otp": otp_code,
            "sms_approved": not needs_sms or _approval(approvals, "sms", default=False),
            "kyc_approved": not needs_kyc or _approval(approvals, "kyc", default=False),
        }
        _add_record(
            context,
            stage=PipelineAgent.VERIFICATION,
            event="verification_completed",
            detail="Verification gates completed",
            data=context["verification"],
        )
        return context


class BillingAgent:
    """Provisions constrained payment method and spend guardrails."""

    def run(self, context: Context, providers: Any, approvals: Mapping[str, Any]) -> Context:
        if not _approval(approvals, "billing", default=True):
            raise PermissionError("billing_not_approved")

        card_provider = providers.get("card") if providers is not None else None
        if card_provider is None:
            raise RuntimeError("card_provider_missing")

        payment = dict(context.get("input", {}).get("payment", {}) or {})
        card_number = str(payment.get("card_number", "")).strip()
        exp_month = str(payment.get("exp_month", "")).strip()
        exp_year = str(payment.get("exp_year", "")).strip()
        cvc = str(payment.get("cvc", "")).strip()
        cardholder_name = str(payment.get("cardholder_name", "")).strip()

        if not card_number:
            raise PermissionError("card_details_required")

        tokenized = card_provider.tokenize_card(
            card_number=card_number,
            exp_month=exp_month,
            exp_year=exp_year,
            cvc=cvc,
            cardholder_name=cardholder_name,
        )
        if not tokenized.get("ok"):
            raise RuntimeError("card_tokenization_failed")

        policy = TrialPolicy.from_dict(dict(context.get("policy", {}) or {}))
        target = TargetService.from_dict(dict(context.get("target", {}) or {}))
        identity = dict(context.get("identity", {}) or {})
        auth = dict(context.get("input", {}).get("auth", {}) or {})
        account_password = str(auth.get("password", "")).strip()

        browseruse = providers.get("browseruse") if providers is not None else None
        trial_text = ""
        trial_started = False
        trial_canceled = False
        cancellation_verified = False
        trial_run_ok = True

        if browseruse is not None:
            trial_instruction_parts = [
                f"Open {target.service_name} billing/subscription settings starting at {target.signup_url}.",
                "Check whether a free trial, free credits, or active subscription exists.",
                "If free trial is available and not started, start it.",
            ]
            if account_password:
                trial_instruction_parts.append(
                    f"If login is required, use email {identity.get('email', '')} and password {account_password}."
                )
            if policy.cancel_immediately:
                trial_instruction_parts.append(
                    "Then cancel recurring billing/subscription immediately and verify cancellation state."
                )
            trial_instruction_parts.append(
                "Return explicit final status: trial_started yes/no, cancellation_done yes/no, reason."
            )

            trial_result = browseruse.run_task(
                instruction=" ".join(trial_instruction_parts),
                url=target.signup_url,
                context={"service": target.service_name, "mode": "trial-lifecycle"},
            )
            trial_run_ok = bool(trial_result.get("ok"))
            trial_text = _result_text(trial_result)

            if trial_run_ok:
                m_started = re.search(r"trial_started\s*:\s*(yes|no)", trial_text, flags=re.IGNORECASE)
                if m_started:
                    trial_started = m_started.group(1).lower() == "yes"
                else:
                    trial_started = _contains_any(
                        trial_text,
                        (
                            "trial started",
                            "started free trial",
                            "free trial activated",
                            "free plan active",
                            "free credits available",
                        ),
                    ) and not _contains_any(
                        trial_text,
                        (
                            "no free trial",
                            "trial not available",
                            "no free credits",
                            "no active subscriptions",
                        ),
                    )

                m_canceled = re.search(r"cancellation_done\s*:\s*(yes|no)", trial_text, flags=re.IGNORECASE)
                if m_canceled:
                    trial_canceled = m_canceled.group(1).lower() == "yes"
                else:
                    trial_canceled = _contains_any(
                        trial_text,
                        (
                            "canceled",
                            "cancelled",
                            "subscription canceled",
                            "auto-renew off",
                            "recurring billing disabled",
                        ),
                    ) and not _contains_any(
                        trial_text,
                        ("not canceled", "cancellation_done: no"),
                    )
                cancellation_verified = _contains_any(
                    trial_text,
                    (
                        "cancellation confirmed",
                        "no active subscription",
                        "not renewing",
                        "renewal disabled",
                        "billing canceled",
                    ),
                ) or trial_canceled
            else:
                trial_text = trial_text or "trial lifecycle task failed"

        is_dry_run = bool(context.get("dry_run"))
        if policy.require_trial_started and not trial_started and not is_dry_run:
            raise RuntimeError("trial_not_started")
        if policy.cancel_immediately and trial_run_ok and not cancellation_verified and not is_dry_run:
            # Keep non-blocking for providers that only offer free credits without subscriptions.
            if not _contains_any(trial_text, ("free credits", "no active subscription", "not applicable")):
                raise RuntimeError("trial_cancel_not_verified")

        billing = {
            "card_token": tokenized.get("token", ""),
            "card_last4": tokenized.get("last4", ""),
            "card_brand": tokenized.get("brand", ""),
            "max_spend_usd": policy.max_spend_usd,
            "auto_cancel_hours_before_billing": policy.auto_cancel_hours_before_billing,
            "trial_started": trial_started,
            "trial_canceled": trial_canceled,
            "cancellation_verified": cancellation_verified,
            "trial_evidence": trial_text[:1400],
        }
        context["billing"] = billing
        _add_record(
            context,
            stage=PipelineAgent.BILLING,
            event="billing_guardrails_configured",
            detail="Card tokenized and trial spend policy applied",
            data={
                "card_last4": billing["card_last4"],
                "max_spend_usd": policy.max_spend_usd,
                "trial_started": trial_started,
                "trial_canceled": trial_canceled,
            },
        )
        return context


class CredentialAgent:
    """Collects API credentials and emits integration payload."""

    def run(self, context: Context, providers: Any, approvals: Mapping[str, Any]) -> Context:
        target = TargetService.from_dict(dict(context.get("target", {}) or {}))
        identity = dict(context.get("identity", {}) or {})
        auth = dict(context.get("input", {}).get("auth", {}) or {})
        account_password = str(auth.get("password", "")).strip()
        browseruse = providers.get("browseruse") if providers is not None else None

        instruction = [
            f"Navigate to {target.api_key_path_hint} for {target.service_name} "
            f"and create an API key for {identity.get('email', '')}."
        ]
        if account_password:
            instruction.append(
                f"If login is required, sign in with email {identity.get('email', '')} and password {account_password}."
            )
        instruction_text = " ".join(instruction)
        run_result: dict[str, Any]
        if browseruse is None:
            run_result = {
                "ok": True,
                "mock": True,
                "message": "BrowserUse provider unavailable; simulated API key retrieval.",
            }
        else:
            run_result = browseruse.run_task(
                instruction=instruction_text,
                url=target.signup_url,
                context={"api_key_path_hint": target.api_key_path_hint},
            )

        if not run_result.get("ok"):
            raise RuntimeError("credential_retrieval_failed")

        result_text = _result_text(run_result)
        api_key = _extract_api_key(result_text)
        if context.get("dry_run") and not api_key:
            api_key = f"tp_{_digest([target.service_name, identity.get('email'), context.get('run_id')])}"
        raw = run_result.get("raw")
        raw_success = bool(raw.get("isSuccess")) if isinstance(raw, Mapping) else False
        created_without_secret = (
            raw_success and "created" in result_text.lower() and "api key" in result_text.lower()
        )
        if not api_key and not created_without_secret:
            raise RuntimeError("api_key_not_found")
        output = dict(context.get("output", {}) or {})
        output.update(
            {
                "service": target.service_name,
                "signup_url": target.signup_url,
                "api_key": api_key or "",
                "api_key_path_hint": target.api_key_path_hint,
                "card_last4": context.get("billing", {}).get("card_last4", ""),
                "trial_started": bool(context.get("billing", {}).get("trial_started")),
                "trial_canceled": bool(context.get("billing", {}).get("trial_canceled")),
                "cancellation_verified": bool(context.get("billing", {}).get("cancellation_verified")),
                "trial_evidence": str(context.get("billing", {}).get("trial_evidence", ""))[:1400],
                "provider_output": result_text[:1000],
                "api_key_created": bool(api_key or created_without_secret),
                "api_key_secret_visible": bool(api_key),
                "manual_copy_required": bool(created_without_secret and not api_key),
            }
        )
        context["output"] = output

        _add_record(
            context,
            stage=PipelineAgent.CREDENTIAL,
            event="credential_pack_ready",
            detail="Credential package generated",
            data={"service": target.service_name, "api_key_preview": f"{api_key[:7]}..."},
        )
        return context
