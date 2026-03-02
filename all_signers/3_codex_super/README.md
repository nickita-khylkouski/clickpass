# Sigma Super (Isolated Build)

This folder is standalone and does not modify your other scripts.

## Goal
Automate:
1. Email signup
2. Email verification (link/code)
3. Login
4. API key extraction (quick stop if none)

## File
- `signup_super.py`

## Model Presets
- `openai-best` -> `gpt-5.2` (highest quality)
- `openai-fast` -> `gpt-5-mini` (best speed/quality default)
- `openai-ultrafast` -> `gpt-5-nano` (fastest, lower reliability)
- `browseruse-fast` -> `bu-2-0` (Browser Use optimized model)

## Why This Is Faster
- Parallel setup: inbox + cloud browser in one `gather`
- `max_actions_per_step=15` to reduce LLM round trips
- Aggressive browser timing (`0.1s` waits)
- Verification inbox watcher starts early
- Direct `httpx` verification GET before browser fallback
- Short bounded API-key search (3 areas max)

## Required Env Vars
- `BROWSER_USE_API_KEY`
- `AGENTMAIL_API_KEY`
- `OPENAI_API_KEY` (for OpenAI models)

## Run
```bash
uv run python codex_super/signup_super.py https://example.com
```

### Useful flags
```bash
uv run python codex_super/signup_super.py https://example.com --llm openai-best
uv run python codex_super/signup_super.py https://example.com --llm openai-fast --reuse-inbox
uv run python codex_super/signup_super.py https://example.com --skip-verification
```
