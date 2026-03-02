# TrialPilot Pipeline Deep Dive

This document explains the current onboarding pipeline end-to-end, including architecture, control flow, recovery logic, and known failure modes.

## 1) What The Pipeline Is

The `trialpilot` flow is a Browser Use + AgentMail automation pipeline that attempts to:

1. Create or reuse an inbox for signup email verification.
2. Start a Browser Use cloud session.
3. Perform signup/login steps on a target site.
4. Handle verification (OTP or magic link) in the same session.
5. Create/capture API keys when possible.
6. Optionally capture read-only plan/trial/billing facts.

Primary modules:

- `trialpilot/live_cli.py`: user-facing CLI, orchestration, retries, fallbacks, summaries.
- `trialpilot/browseruse_agentmail_demo.py`: low-level Browser Use + AgentMail flow (`run_demo`).

---

## 2) Main Commands And Entry Points

### `onboard`

Primary high-level command:

```bash
python3 -m trialpilot.live_cli onboard --target-url <url> [...flags]
```

This invokes `live_cli._run_onboard(...)`, which:

1. Resolves the target/site preset.
2. Applies runtime options (`profile`, proxy, vision, secrets).
3. Builds `DemoConfig` via `_build_common_cfg(...)`.
4. Calls `run_demo(...)` (from `browseruse_agentmail_demo.py`).
5. Applies layered recovery for blockers.
6. Runs follow-up tasks for API key and billing snapshot.
7. Produces a structured JSON result + summary block.

### `task`

Runs one custom Browser Use task in a session:

```bash
python3 -m trialpilot.live_cli task --target-url <url> --instruction "..."
```

Useful for post-login or post-blocker targeted actions.

### `session-info`

Gets current session metadata and live browser URL:

```bash
python3 -m trialpilot.live_cli session-info --session-id <id>
```

---

## 3) Internal Data Model

`DemoConfig` (in `browseruse_agentmail_demo.py`) drives run behavior:

- Target and identity fields: `target_url`, names, password, phone.
- Verification fields: `otp_wait_seconds`, `otp_regex`, manual OTP.
- Session/runtime fields: `profile_id`, `proxy_country_code`, viewport, flash/thinking/vision.
- Secrets + vault fields.
- New strictness field: `strict_fresh_inbox`.

`run_demo(...)` returns normalized JSON containing:

- `ok`/`blocked_reason`
- `signup_*` task output
- verification metadata
- continuation task output
- session ids and recovery flags

---

## 4) End-To-End Flow (`run_demo`)

### Phase A: Inbox selection/creation

`_create_agentmail_inbox(...)` behavior:

- If `--inbox` given: use that inbox.
- If `--inbox-username` or strict fresh mode: try `POST /inboxes` first.
- If not strict and creation fails due quota/exists: fallback to existing inbox.
- If strict and creation fails: fail fast with explicit runtime error.

### Phase B: Session creation

`_create_browser_session(...)` starts a Browser Use cloud session (`/sessions`) with:

- optional profile id
- optional proxy country code
- optional keepAlive/persist memory

### Phase C: Signup task

`_build_signup_task(...)` creates a constrained instruction.

Task is executed with `_run_task_with_wait_and_recovery(...)`, which combines:

1. Task create recovery (`_create_task_with_session_recovery`) if session is already stopped.
2. Runtime result recovery when completed task output indicates stopped-session signals.

### Phase D: Verification artifact

If signup reaches verification:

- Poll AgentMail for fresh OTP or magic link (`_wait_for_verification(...)`).
- Ignore stale inbox messages captured before signup start.
- Provider-relevant link filtering applies in `live_cli._latest_verification_link(...)`.

### Phase E: Continuation task

`_build_continue_task(...)` then:

- submits OTP or opens magic link,
- completes login/onboarding,
- optionally creates API key.

Continuation also uses session/runtime recovery wrapper.

---

## 5) Recovery And Retry System

`live_cli._run_onboard(...)` adds multi-layer recovery on top of `run_demo(...)`.

### A) Transient retry wrapper

`_run_demo_with_retries(...)` retries transient failures (network/transport/session issues).

New behavior in strict fresh mode:

- If strict fresh inbox collides on `AlreadyExists`, it auto-rotates a new username for retry.

### B) Blocker-specific recovery

Common blocker classes:

- `signup_access_blocked`
- `signup_validation_failed`
- `verification_timeout`
- `invite_only_closed_signup`

Recovery branches include:

1. Validation retries (phone variant normalization / omission).
2. Inbox rotation retries.
3. Proxy-country fallback retries.
4. Optional human-checkpoint window using live Browser Use URL.

### C) Access-style failure unification

Some targets emit `signup_validation_failed` while actually failing due blank-page/security render blockers.

New helper:

- `_looks_like_signup_render_block(...)`
- `_is_signup_access_issue_result(...)`

This routes such cases into access recovery logic (proxy/checkpoint) instead of only phone-validation retries.

---

## 6) Freshness And Anti-Staleness Controls

### Fresh inbox behavior

CLI defaults currently favor fresh inbox/profile on `onboard` runs.

New strict mode:

```bash
--strict-fresh-inbox
```

Meaning:

- Require creating a brand-new inbox.
- Do not silently reuse existing inbox if create fails.
- Useful for reducing stale-message contamination and provider dedupe issues.

### Signup email alias strategy

`_build_signup_email(...)` randomizes local-part aliases to avoid provider “already exists” collisions, with site-specific exceptions where plus-addressing may be normalized.

### Verification-link relevance

`_latest_verification_link(...)` now scores links with provider relevance and `not_before` timestamp constraints to avoid stale cross-provider link pickup.

---

## 7) API Key Capture And Billing Snapshot

After signup/verification:

1. API key capture task runs in same session (`_run_custom_task(...)` with session reuse).
2. Billing snapshot task captures read-only plan/usage/currency facts.

Summary fields include:

- `api_key_status` (found/preview/manual copy requirement)
- `trial_status`
- `billing_facts`
- `hiccups`
- `timing_seconds`

---

## 8) Why Some Targets Work And Others Fail

### Works well when

- Signup pages render normally in cloud browser.
- Email verification links are provider-consistent.
- Post-auth app pages do not enforce strong anti-automation runtime checks.

### Fails frequently when

- Target app transitions to SPA route that loads as `about:blank`.
- Security policy/challenge blocks post-redirect rendering.
- Region redirects route to protected subdomains.

In these cases, the pipeline usually fails at auth bootstrap or immediate post-verification app load, not at JSON parsing or instruction generation.

---

## 9) Current Safety/Compliance Boundaries

The pipeline intentionally does **not** bypass anti-bot/security systems.

It can:

- retry network/runtime failures,
- rotate proxy regions,
- open live session for human checkpoint,
- continue after legitimate user-auth bootstrap.

It cannot and should not attempt security circumvention.

---

## 10) Practical Run Patterns

### Fast validation run

```bash
python3 -m trialpilot.live_cli onboard \
  --target-url https://cont3xt.dev/ \
  --super-fast --no-auto-human-checkpoint --verbose
```

### Strict fresh inbox + geo fallback

```bash
python3 -m trialpilot.live_cli onboard \
  --target-url https://www.apollo.io \
  --strict-fresh-inbox \
  --proxy-country-code us \
  --proxy-fallback-country ca \
  --proxy-fallback-country nl \
  --proxy-fallback-country sg \
  --max-proxy-fallbacks 3 \
  --verbose
```

### Inspect session + open live URL

```bash
python3 -m trialpilot.live_cli session-info --session-id <id>
```

---

## 11) In Short

The current pipeline is a layered state machine:

- deterministic signup/verify steps,
- resilient session/task recovery,
- blocker-class-based retries,
- post-onboarding extraction (API key + billing facts),
- strict mode to prevent stale inbox reuse.

Its main failure domain is target-side app security/render behavior in cloud automation contexts.
