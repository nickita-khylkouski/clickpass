# TrialPilot Live Onboarding Pipeline (Deep Overview)

This document explains the current `trialpilot.live_cli onboard` pipeline end-to-end, including control flow, retries, anti-block handling, and known failure modes.

## 1) Purpose

The pipeline is designed to:

- Create a new account on an arbitrary target site using Browser Use + AgentMail.
- Complete email verification (OTP or magic link).
- Capture account credentials (`email`, `password`, `login_url`).
- Attempt API key creation/retrieval where possible.
- Optionally capture read-only billing/balance signals (plan, credits, cash balance).
- Return machine-parseable JSON output for automation.

Primary entrypoint:

- `python3 -m trialpilot.live_cli onboard ...`

Core code:

- `trialpilot/live_cli.py`
- `trialpilot/browseruse_agentmail_demo.py`

## 2) High-Level Execution Path

`main()` in `live_cli.py`:

1. Loads `.env`.
2. Parses CLI args.
3. For `onboard` command:
- Applies `--super-fast` tuning (`_apply_super_fast_onboard_args`).
- Executes `_run_onboard(args)`.
- Emits JSON envelope:
  - `{"ok": <bool>, "command": "onboard", "result": {...}}`

Inside `_run_onboard(args)`:

1. Resolves site URL (`--site` preset or `--target-url`).
2. Applies defaults/overrides for API-key step.
3. Applies hard-block target guardrails for Apollo/Daytona class targets.
4. Builds runtime config (profile/proxy/vision/secrets).
5. Runs signup + verification flow via `run_demo(cfg)` with retry wrapper.
6. Performs conditional recovery loops (validation errors, timeouts, access blocks).
7. Performs API-key recovery and capture tasks.
8. Optionally runs read-only balance snapshot.
9. Builds final summary and returns result JSON.

## 3) Components and Responsibilities

### 3.1 Browser + Task Runtime

Browser tasks are executed through Browser Use API:

- session creation: `/api/v2/sessions`
- task creation: `/api/v2/tasks`
- task polling: `/api/v2/tasks/{id}`

The pipeline configures:

- profile (`profile_id`)
- proxy country (`proxy_country_code` / `cloud_proxy_country_code`)
- visual/thinking mode (`vision`, `flash_mode`, `thinking`)
- allowed domain scope for tasks

### 3.2 Inbox and Verification

Email verification uses AgentMail:

- create/reuse inbox
- poll inbox messages
- extract OTP or magic/verification link

Verification artifacts are used to resume the browser flow in the same session.

### 3.3 API Key Capture

After verification/login:

- Runs an explicit API-key task instruction to open developer settings.
- Tries to detect visible key directly.
- If key is masked, creates one (`automation-export`) and captures the one-time value.
- Parses known key formats via regex (`pmx_`, `ctx_live_`, `dsk-live-`, etc.).

## 4) Recovery and Reliability Strategy

### 4.1 Transient Infra Retries

Transient classes include:

- network/transport timeouts
- Browser Use 408/409/429/5xx
- task lookup failures (including `Task not found`)
- stopped sessions

Retry wrappers:

- `_run_demo_with_retries(...)`
- `_run_custom_task(...)` now retries transient failures and can force new session.
- `main()` onboard path retries transient exceptions (`TRIALPILOT_ONBOARD_RETRIES`).

### 4.2 Signup Validation Recovery

If signup fails due form validation:

- retries with phone variants (digits-only, dropped country code, omitted phone)
- retries with alternate inboxes (limited)

### 4.3 Verification Timeout Recovery

If no verification arrives in time:

- retries same inbox with longer wait
- rotates through alternate inboxes

### 4.4 Access-Blocked Recovery (Cloudflare/CAPTCHA/blank render)

On `signup_access_blocked`:

- applies proxy-country fallback retries
- fallback list defaults are target-aware (Apollo has expanded list)
- optional manual checkpoint mode:
  - opens live browser URL
  - waits configurable checkpoint window
  - retries in same session

### 4.5 Verification Link Recovery

If output indicates expired OTP / unconfirmed email / invalid credentials:

- fetches latest provider-relevant verification link from inbox
- opens exact link in session
- retries key capture afterward

## 5) Super-Fast Mode (`--super-fast`)

Super-fast mode aggressively trims latency:

- reduces steps/timeouts/retries
- keeps API-key step on
- disables non-essential exploration

For hard-block targets (Apollo/Daytona class), super-fast now preserves critical anti-block behavior:

- keeps proxy fallback retries enabled (>=4)
- enables manual checkpoint mode
- can force fresh profile when no explicit profile is supplied

For non-hard targets, super-fast remains strict/lean.

## 6) Current Output Shape

Top-level:

- `ok`
- `command`
- `result`

`result` includes (when successful):

- `login.email`
- `login.password`
- `login.login_url`
- `api_key`
- `session_id`
- `created_profile`
- `summary`:
  - `api_key_status`
  - `billing_facts`
  - `timing_seconds`
  - `hiccups`
  - `profile_id`

## 7) What Works Reliably Now

Observed successful target:

- `parse.bot`: full flow returns email/password/api_key + balance facts.

Example success artifact from this workspace:

- `.claude/cache/parsebot/live_validate_1772332438.json`

## 8) Apollo Status (Current)

Apollo is still not reliable in this environment.

Observed Apollo blockers:

- Signup render/access blocked (`signup_access_blocked` / blank-like behavior).
- Browser Use task-state race conditions (`session already has a running task`).
- occasional task lookup inconsistency (`Task not found`) despite retries.

Latest Apollo artifacts in this workspace:

- `.claude/cache/parsebot/apollo_validate_1772335218.json`
- `.claude/cache/parsebot/apollo_validate_retry_1772335321.json`
- `.claude/cache/parsebot/apollo_validate_final_1772336112.json`

## 9) Why Competing Flows Can Seem Better on Apollo

A comparison flow (e.g., `unapi`) may appear more robust because it:

- hard-stops at verification boundary and immediately handles link/code.
- explicitly instructs captcha checkbox interaction.
- can declare success from captured auth artifacts even without API-key UI completion.

Our pipeline is stricter for account+API-key completion and therefore surfaces Apollo anti-bot failures explicitly.

## 10) Recommended Next Reliability Upgrades

1. Session task scheduler lock in `_run_custom_task` to avoid overlapping tasks in same session.
2. Automatic wait-and-retry for Browser Use `running task` 400 with session polling.
3. Apollo-specific parser/instruction profile with minimal navigation branch set.
4. Optional dual-mode success criteria:
- mode A: strict (`must return API key`)
- mode B: auth-artifact fallback (`session/auth success without API key`).

