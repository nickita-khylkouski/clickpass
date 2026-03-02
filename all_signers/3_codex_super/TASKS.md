# Universal Signer-Upper Tasks

Goal: make signup + verify + login + API-key extraction reliable across arbitrary SaaS sites with strong speed and low false positives.

## P0 (Do Now)

- [ ] Enforce strict JSON output contracts for all agent phases (`signup`, `verify`, `login+api`, `snapshot`).
  - Accept: parser accepts only schema-valid JSON; one format-repair retry; else hard fail.
- [ ] Add robust lifecycle cleanup with `try/finally`.
  - Accept: browser always stops, verify watcher is canceled+awaited, inbox cleanup follows flags in all exit paths.
- [ ] Remove duplicate verification wait path.
  - Accept: one bounded verification budget after signup (no double `verify_timeout` waits).
- [ ] Harden auth-state decisioning.
  - Accept: `needs_verification` only from explicit status or tight phrases; no broad `"verify"` heuristic.
- [ ] Final status must come from parsed contract, never raw `login.success`.
  - Accept: output `LOGIN` is only `SUCCESS|FAILED|SKIPPED` from parsed payload.

## P1 (High Value, Next)

- [ ] Add auth-mode classifier (`password`, `magic_link`, `otp_only`) before login task.
  - Accept: no password-loop attempts on magic-link sites; clear branch output.
- [ ] Track consumed verification artifacts (`used_codes`, `used_links`) per run.
  - Accept: same code/link never retried unless explicitly marked resend.
- [ ] Add host allowlist policy object (root domain + allowed subdomains + optional IdP hosts).
  - Accept: all phase prompts + post-checks enforce visited-host policy.
- [ ] Add retry budget caps per phase (wall-clock + attempts).
  - Accept: no runaway retries; deterministic upper bound on runtime.
- [ ] Add transient-error retry for non-exception agent failures from history errors.
  - Accept: session-manager/CDP failures trigger retry/rebuild automatically.

## P2 (Speed + Demo Quality)

- [ ] Add `--profile demo` and `--profile universal` runtime presets.
  - `demo`: fastest path, `--no-capture-summary`, tighter timeouts.
  - `universal`: higher reliability, broader retries, richer diagnostics.
- [ ] Default `capture_summary` off for speed; opt-in for demo narration.
  - Accept: no extra snapshot run unless requested.
- [ ] Reduce prompt verbosity and exploration language.
  - Accept: fewer average steps on known sites; no search/file/todo actions.
- [ ] Adaptive inbox polling/backoff.
  - Accept: lower API chatter while preserving OTP latency.

## P3 (Quality/Testing)

- [ ] Unit tests: parsers (`signup/login/snapshot`), domain normalization, verification extraction.
  - Accept: stable tests for JSON/plaintext variants and noisy email bodies.
- [ ] Decision tests: captcha fail-fast, verification gating, login skip/force logic.
  - Accept: deterministic branch coverage for core orchestration.
- [ ] Benchmark harness for 5 target sites.
  - Metrics: success rate, p50/p95 time, false-positive rate, retry counts.

## P4 (Architecture Upgrade Path)

- [ ] Introduce deterministic Playwright fallback lane for auth-critical steps.
  - Use resilient locators + checkpoint asserts for login/API pages.
- [ ] Persist auth state (`storageState`-like concept) for repeat demo runs.
  - Accept: subsequent runs can skip redundant auth.
- [ ] Optional recorded workflow mode for repeatable targets.
  - Accept: “record once, replay with variable substitution” with agent fallback.

## Hackathon Demo Definition of Done

- [ ] 3 target sites complete end-to-end live in one session.
- [ ] Each demo run prints: credentials, verification method, login result, API key/token result, and concise notes.
- [ ] Demo profile runtime target: `< 120s` median.
- [ ] No manual intervention required in happy path.

