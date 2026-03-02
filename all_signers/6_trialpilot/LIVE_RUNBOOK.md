# TrialPilot Live Runbook

Use this when repeating onboarding runs across multiple targets.

## 0) Quick UI (paste link -> run -> see live link + results)

```bash
python3 -m trialpilot.quick_ui --host 127.0.0.1 --port 8787
```

Open: `http://127.0.0.1:8787`

- Paste target URL
- Optional: inbox/profile/proxy
- `Open Live Session`: returns BrowserUse live link immediately
- `Run Onboard`: runs signup + OTP + API key capture and shows structured output

## 1) Setup once

Create `.env` in repo root:

```bash
BROWSER_USE_API_KEY=...
AGENTMAIL_API_KEY=...
TRIALPILOT_DRY_RUN=false
BROWSER_USE_PROFILE_ID=...
BROWSER_USE_PROXY_COUNTRY_CODE=us
BROWSER_USE_VISION=auto
```

Optional hardening flags:

```bash
BROWSER_USE_HIGHLIGHT_ELEMENTS=true
BROWSER_USE_FLASH_MODE=true
BROWSER_USE_THINKING=true
# Optional JSON object:
# BROWSER_USE_SECRETS_JSON='{"OPENAI_API_KEY":"sk-..."}'
```

## 2) Pick or create inbox

```bash
python3 -m trialpilot.live_cli inboxes --limit 20
```

Use an existing inbox ID if your account has inbox limits.

## 2.1) Cookie/Profile mode (recommended)

List profiles:

```bash
python3 -m trialpilot.live_cli profiles --limit 20
```

Create one if needed:

```bash
python3 -m trialpilot.live_cli create-profile --name "trialpilot-main"
```

Set `BROWSER_USE_PROFILE_ID` to that profile ID. Reusing the same profile keeps login state/cookies across runs.

## 3) Start run

Optional target scouting before live runs:

```bash
python3 -m trialpilot.live_cli scout-targets --limit 8
```

This ranks likely candidates using public page signals (signup/trial/API/bot-friction hints).

```bash
python3 -m trialpilot.live_cli start \
  --target-url https://platform.minimax.io/login \
  --inbox <INBOX_ID> \
  --profile-id <PROFILE_ID> \
  --proxy-country-code us \
  --vision auto \
  --pause-for-billing \
  --include-api-key-step \
  --verbose
```

Keep the returned `session_id`.

For one-shot site orchestration with structured summary output:

```bash
python3 -m trialpilot.live_cli onboard \
  --site windsurf \
  --inbox <INBOX_ID> \
  --profile-id <PROFILE_ID> \
  --proxy-country-code us \
  --vision auto \
  --flash-mode \
  --thinking \
  --max-steps 120 \
  --timeout-seconds 900 \
  --verbose
```

The `result.summary` block includes:
- `account_email`
- `account_password`
- `api_key_status` (`api_key_found`, `manual_copy_required`, preview)
- `trial_status` (`trial_started`, `trial_unavailable`, `trial_canceled`)

Blocked-signup recovery is enabled by default in `onboard`:
- auto-opens Browser Use `liveUrl` locally
- waits (`--human-checkpoint-seconds`, default `180`)
- retries onboarding in the same session

Disable if needed:
```bash
--no-auto-human-checkpoint --no-open-live-url
```

## 3.1) Fast demo mode (single command)

Use the lightweight demo runner when you need speed and predictable behavior:

```bash
python3 -m trialpilot.browseruse_agentmail_demo \
  --target-url https://apollo.io \
  --demo-fast \
  --include-api-key-step \
  --preload-file trialpilot_out/apollo_browseruse_full_steps.json \
  --use-preloaded-on-block \
  --verbose
```

What `--demo-fast` does:
- trims max steps/timeouts for faster feedback
- enables Browser Use keep-alive + persist-memory defaults
- auto-cleans stale active sessions to reduce `429 Too many concurrent active sessions`
- keeps API-key step enabled

If live signup is blocked by anti-bot/security checks and `--use-preloaded-on-block` is set, the command returns a transparent `mode: preloaded_fallback` payload with both live blocker info and summarized preload data.

## 4) Billing checkpoint

In pause mode, the agent stops before/around billing/verification checkpoint.
If manual form input is needed, do it in the active browser session, then continue.

## 5) Resume run

Fetch fresh OTP:

```bash
python3 -m trialpilot.live_cli latest-otp --inbox <INBOX_ID>
```

Resume:

```bash
python3 -m trialpilot.live_cli resume \
  --target-url https://platform.minimax.io/login \
  --session-id <SESSION_ID> \
  --manual-otp <OTP> \
  --profile-id <PROFILE_ID> \
  --proxy-country-code us \
  --vision auto \
  --include-api-key-step \
  --verbose
```

## 6) Success criteria

- `result.continue_status == "finished"`
- `result.continue_output` includes API key creation result or API settings URL

## Troubleshooting

- `Inbox limit exceeded`: reuse an existing inbox (`inboxes` command).
- OTP rejected: request resend, then run `latest-otp` and resume again.
- Task timeout: increase `--timeout-seconds` to 600 or 900.
- Start page loops: use direct platform login URL instead of marketing homepage.
- Anti-bot friction: use a persistent profile (`--profile-id`) plus geo-appropriate proxy (`--proxy-country-code`).

Inspect live session anytime:
```bash
python3 -m trialpilot.live_cli session-info --session-id <SESSION_ID> --open-live-url
```
