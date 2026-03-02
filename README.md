# Sigma

Automated SaaS signup + API key extraction pipeline. Give it a URL, get back credentials and an API key.

## What it does

```
$ python v2/sigma_combined.py https://helicone.ai

========================================================
  RESULTS (199s, model=bu-2-0)
========================================================
  URL:      https://helicone.ai
  Email:    longconnection655@agentmail.to
  Password: YDuR5pVq0fJ!b^*tQN
  Signup:   ok
  Verified: yes
  Login:    ok
  API Key:  sk-helicone-mwnaa3a-ki3etny-ubrrfiy-s7hrr6q
  Key URL:  https://us.helicone.ai/settings/api-keys
========================================================
```

## Pipeline

Three phases run sequentially:

### Phase 1: Signup
1. **Firecrawl recon** — scrapes the site to build a `SiteProfile` (signup URL, form fields, auth provider, CAPTCHA type)
2. **Tier 1 (Deterministic)** — if profile has selectors, fills the form via Playwright JS injection (no LLM)
3. **Tier 2 (Agent-assisted)** — browser-use agent with profile-informed prompts + stripped HTML context
4. **Tier 3 (Blind)** — generic agent prompt for unknown sites
5. **OAuth fallback** — if email signup fails and an OAuth browser profile is provided

### Phase 2: Email Verification
- **AgentMail** inbox polling for verification emails
- Extracts links and OTP codes from email HTML
- HTTP verification for simple token links
- Browser navigation for Supabase/Auth0 verify URLs
- Agent-assisted code entry for OTP flows

### Phase 3: Login + API Key Extraction
- Password login with structured output (`LoginApiKeyOutput` Pydantic model)
- Magic link login flow (trigger → poll inbox → navigate programmatically)
- OTP login flow (trigger → poll inbox → agent enters code)
- API key discovery: direct URL paths, Settings/Developer navigation
- DOM-first API key scan via clipboard interception
- Focused API-only retry pass if login succeeds but key wasn't captured

## Architecture

```
sigma_combined.py (~3700 lines)
├── Firecrawl recon → SiteProfile (cached per domain)
├── Deterministic form fill (Tier 1, no LLM)
├── browser-use Agent with Controller (Tier 2/3)
│   ├── SignupOutput (Pydantic structured output)
│   ├── LoginApiKeyOutput (Pydantic structured output)
│   ├── Stealth JS injection (anti-bot)
│   └── initial_actions (pre-navigation, no LLM cost)
├── AgentMail (disposable email with real deliverability)
├── Verification (HTTP + browser + OTP)
└── Login + API key extraction
```

### Key features
- **Structured output** — Pydantic models via `Controller.use_structured_output_action()` replace regex parsing
- **Stealth** — CDP init scripts remove `navigator.webdriver`, Playwright bindings; fake `chrome.runtime`
- **Error recovery** — browser rebuild on crash, error history feedback, model fallback
- **Profile caching** — `~/.sigma/profiles/<domain>.json` stores form selectors, URLs, auth info
- **HAR capture** — records full network traffic for post-run analysis
- **Screen classification** — categorizes page type before acting (form, oauth_only, captcha_blocked)

## Setup

```bash
# Clone and install
git clone https://github.com/nickita-khylkouski/clickpass.git
cd clickpass
uv sync

# Required env vars (.env)
BROWSER_USE_API_KEY=...   # browser-use cloud (or use --local)
AGENTMAIL_API_KEY=...     # disposable email
OPENAI_API_KEY=...        # fallback LLM (optional)
FIRECRAWL_API_KEY=...     # site recon (optional, improves Tier 1)
```

## Usage

```bash
# Cloud browser (default)
python v2/sigma_combined.py https://example.com

# Local browser (more reliable, your IP)
python v2/sigma_combined.py https://example.com --local

# Local browser with visible window
python v2/sigma_combined.py https://example.com --local --headed

# Skip Firecrawl recon (faster, uses cached profile)
python v2/sigma_combined.py https://example.com --no-recon

# Refresh cached profile
python v2/sigma_combined.py https://example.com --refresh-profile

# With OAuth fallback (Google/GitHub browser profile)
python v2/sigma_combined.py https://example.com --oauth-profile <profile-id>

# Custom timeouts
python v2/sigma_combined.py https://example.com --signup-timeout 300 --login-timeout 180
```

## Options

| Flag | Default | Description |
|------|---------|-------------|
| `--local` | cloud | Use local Playwright browser |
| `--headed` | headless | Show browser window (local only) |
| `--no-recon` | recon on | Skip Firecrawl site analysis |
| `--refresh-profile` | use cache | Force re-scrape of site profile |
| `--skip-verification` | verify on | Skip email verification phase |
| `--llm` | `bu` | LLM model (`bu`, `gpt4o`, `gpt4o-mini`) |
| `--max-steps` | 25 | Max agent steps per phase |
| `--signup-timeout` | 250 | Signup phase timeout (seconds) |
| `--login-timeout` | 140 | Login phase timeout (seconds) |
| `--profile-dir` | `~/.sigma/profiles` | Custom profile cache directory |
| `--oauth-profile` | none | browser-use profile ID for OAuth |
| `--no-oauth-fallback` | fallback on | Disable OAuth retry on email failure |

## Dependencies

- [browser-use](https://github.com/browser-use/browser-use) v0.12+ — AI browser automation
- [AgentMail](https://agentmail.to) — disposable email API
- [Firecrawl](https://firecrawl.dev) — web scraping for site recon
- [Faker](https://faker.readthedocs.io) — identity generation

## Test results

### Latest (2026-03-02, v3 with structured output + stealth)

Tested on 12 unique sites — **6/12 full success (50%)**:

| Site | Signup | Verify | Login | API Key | Notes |
|------|--------|--------|-------|---------|-------|
| helicone.ai | ok | yes | ok | `sk-helicone-...` | |
| langfuse.com | ok | yes | ok | `sk-lf-...` | |
| greptile.com | ok | - | ok | `b7TE5YqO...` | No verification needed |
| reducto.ai | ok | yes | ok | `05c7ceed...` | |
| parea.ai | ok | yes | ok | `pai-9982...` | |
| hyperbrowser.ai | ok | yes | ok | `hb_3ce28b...` | |
| vellum.ai | ok | yes | ok | NONE | Auth0 returns HTTP 500 |
| humanloop.com | fail | - | - | - | Clerk redirect, no email signup |
| mem0.ai | fail | - | - | - | Wrong signup URL from recon |
| firecrawl.dev | fail | - | - | - | Cloudflare CAPTCHA |
| cerebrium.ai | fail | - | - | - | Turnstile CAPTCHA |
| theneo.io | fail | - | - | - | Hidden CAPTCHA |

**Failure categories:**
- CAPTCHA blocking (3 sites, 25%) — biggest fixable gap
- Auth provider redirect (1) — Clerk OAuth-only
- Bad recon URL (1) — Firecrawl returned wrong signup page
- Server-side error (1) — Auth0 callback HTTP 500

### Earlier (2026-03-01, v2)

Tested on 19 sites:
- **7/19 full success**: helicone, langfuse, parea, confident-ai, cerebrium, reducto, theneo
- **3/19 correct refusal**: traceloop, getzep, laminar (OAuth-only)
- Common failure modes: Auth0 stalls, Shadow DOM OTP, disposable email blocking
