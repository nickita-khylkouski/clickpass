# All Signup Engines — Comparison Package

7 different implementations of automated SaaS signup + API key extraction, built by different agents (Claude, Codex, Windsurf). All use browser-use cloud browsers + agentmail disposable emails.

## Implementations

### 1_sigma_original/ — Original signup.py (816 lines)
- Single-file, uses browser-use Python SDK directly
- 3 phases: Signup → Email Verification → Login + API Key
- Uses `bu-2-0` LLM model, faker for identity
- Handles: OTP, magic links, WebAuthn (virtual authenticator via CDP)
- **Built by**: Claude (sigma project)

### 2_sigma_v2/ — sigma_combined.py (~1500 lines)
- Evolution of original, used by the server.py SSE endpoint
- Prints structured `[time] [stage] message` logs for streaming
- Same core approach but with more robust parsing
- **Built by**: Claude (sigma project, later iteration)

### 3_codex_super/ — signup_super.py + TASKS.md
- Codex agent's version with engineering rigor focus
- TASKS.md has detailed reliability roadmap (P0-P4 priorities)
- Focus: strict JSON contracts, lifecycle cleanup, auth-state decisioning
- **Built by**: Codex agent

### 4_codex_conductor/ — batch_signup.py + enrich_success_rows.py
- Concurrent batch orchestrator — runs many sites in parallel
- Hard per-site timeout cutoffs
- CSV output with enrichment post-processing
- **Built by**: Codex agent (batch/ops focused)

### 5_windsurf_demo/ — signup_super.py + windsurf_demo.py
- Windsurf agent's variant of the signup engine
- Has its own demo runner
- **Built by**: Windsurf agent

### 6_trialpilot/ — Full 6-agent pipeline (most sophisticated)
- 6 sequential agents: Identity → Policy → Signup → Verification → Billing → Credential
- Uses Browser Use HTTP API v2 (not Python SDK)
- HAR capture for traffic analysis
- Anti-block recovery: proxy rotation, CAPTCHA handling, manual checkpoint mode
- `--super-fast` mode for demo speed
- CLI: `python -m trialpilot.live_cli onboard --site X`
- **Built by**: Claude (box/trialpilot project)

### 7_ghostapi/ — Security-focused signup + API fuzzing
- Full security testing suite: signup, HAR capture, OpenAPI codegen, pentest
- `signup_agent.py` handles credit card fields, auth adapters
- Also has: bypass analyzer, policy engine, recipe system, vulnerability scanner
- Much broader scope than pure signup
- **Built by**: Claude (box/ghostapi project)

## Key Differences

| Feature | sigma | v2 | codex_super | conductor | windsurf | trialpilot | ghostapi |
|---------|-------|----|-------------|-----------|----------|------------|----------|
| Browser Use SDK | Python SDK | Python SDK | Python SDK | subprocess | Python SDK | HTTP API v2 | HTTP API |
| Email | agentmail | agentmail | agentmail | agentmail | agentmail | agentmail | agentmail |
| Concurrency | single | single | single | parallel batch | single | single | single |
| Anti-block | basic | basic | planned | timeout cutoff | basic | proxy+CAPTCHA | passive |
| HAR capture | no | no | no | no | no | yes | yes |
| API key extract | yes | yes | yes | yes | yes | yes | yes |
| WebAuthn | CDP virtual | CDP virtual | ? | no | ? | no | no |
| Output format | print | structured log | JSON contract | CSV | print | JSON envelope | JSON |

## Question for Analysis

Which approach/combination gives the best:
1. **Reliability** — highest success rate across diverse SaaS sites
2. **Speed** — fastest signup-to-API-key time
3. **Robustness** — best handling of edge cases (CAPTCHA, magic links, WebAuthn, anti-bot)
4. **Maintainability** — cleanest code, easiest to extend
5. **What's the ideal merged version?** — cherry-pick the best from each
