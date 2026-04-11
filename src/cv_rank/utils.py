"""
Utility functions: Supabase queries, username parsing, JSON extraction, OpenAI client.

Extracted and improved from hackathon-elo/pipeline_v2/{rank.py, shared.py}.
"""

import asyncio
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

logger = logging.getLogger("cv_rank")

_LLM_TRACE_LOCK = threading.Lock()
_LLM_TRACE_PATH: Path | None = None

# GitHub reserved paths that look like usernames but are not.
_GITHUB_RESERVED = frozenset({
    "login", "signup", "settings", "orgs", "topics", "explore",
    "features", "marketplace", "pricing", "security", "enterprise",
    "sponsors", "collections", "events", "about", "team", "pulls",
    "issues", "notifications", "new", "organizations", "stars",
})


# ---------------------------------------------------------------------------
# Supabase
# ---------------------------------------------------------------------------

def supa_get(url: str, key: str, table: str, params: dict) -> list[dict]:
    """Query Supabase PostgREST API with proper error handling.

    Parameters
    ----------
    url:
        Supabase project URL (e.g. ``https://xxxx.supabase.co``).
    key:
        Supabase ``anon`` or ``service_role`` key.
    table:
        Table name to query.
    params:
        Query-string parameters (PostgREST filters).

    Returns
    -------
    list[dict]
        Rows matching the query, or ``[]`` on any error.

    Raises no exceptions -- errors are logged.  Auth failures (401/403) are
    logged at ERROR level; everything else at WARNING.
    """
    qs = urllib.parse.urlencode(params)
    full_url = f"{url}/rest/v1/{table}?{qs}"
    req = urllib.request.Request(
        full_url,
        headers={
            "apikey": key,
            "Authorization": f"Bearer {key}",
            "Accept": "application/json",
        },
    )

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = json.loads(resp.read())
            if isinstance(data, dict) and "code" in data:
                logger.warning("Supabase %s: error %s - %s", table, data.get("code"), data.get("message", ""))
                return []
            return data if isinstance(data, list) else []
    except urllib.error.HTTPError as exc:
        if exc.code in (401, 403):
            logger.error("Supabase auth failed for %s (HTTP %s). Check SUPABASE_URL/SUPABASE_KEY.", table, exc.code)
        else:
            logger.warning("Supabase %s: HTTP %s", table, exc.code)
        return []
    except urllib.error.URLError as exc:
        logger.warning("Supabase %s: connection error - %s", table, exc.reason)
        return []
    except Exception as exc:
        logger.warning("Supabase %s: unexpected error - %s", table, exc)
        return []


# ---------------------------------------------------------------------------
# Username parsing
# ---------------------------------------------------------------------------

def parse_linkedin_username(url: Any) -> str | None:
    """Extract the LinkedIn vanity username from a profile URL.

    >>> parse_linkedin_username("https://www.linkedin.com/in/john-doe/")
    'john-doe'
    """
    if not url:
        return None
    m = re.search(r"linkedin\.com/in/([A-Za-z0-9_.-]+)", str(url))
    return m.group(1).rstrip("/").lower() if m else None


def parse_github_username(url: Any) -> str | None:
    """Extract a GitHub username from a profile URL.

    Filters out reserved GitHub paths (``login``, ``settings``, ...).

    >>> parse_github_username("https://github.com/torvalds")
    'torvalds'
    >>> parse_github_username("https://github.com/login") is None
    True
    """
    if not url:
        return None
    m = re.search(r"github\.com/([A-Za-z0-9_.-]+)", str(url))
    if m:
        username = m.group(1).rstrip("/").lower()
        if username and username not in _GITHUB_RESERVED:
            return username
    return None


# ---------------------------------------------------------------------------
# JSON extraction
# ---------------------------------------------------------------------------

def extract_json(text: str) -> dict:
    """Extract a JSON object from an LLM response.

    Handles markdown code fences (````json ... `````) and falls back to
    finding the outermost ``{...}`` block.

    Raises ``ValueError`` if *text* is ``None``.
    Raises ``json.JSONDecodeError`` if no valid JSON can be found.
    """
    if text is None:
        raise ValueError("Model returned null content")

    # Strip markdown code fences.
    cleaned = re.sub(r"^```(?:json)?\s*\n?", "", text.strip())
    cleaned = re.sub(r"\n?```\s*$", "", cleaned)

    # Fast path: the whole thing is valid JSON.
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        pass

    # Fallback: find a nested JSON object.
    match = re.search(r"\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\}", cleaned, re.DOTALL)
    if match:
        return json.loads(match.group())

    raise json.JSONDecodeError("No JSON object found in text", cleaned, 0)


# ---------------------------------------------------------------------------
# OpenAI client
# ---------------------------------------------------------------------------

def get_openai_client():
    """Return an ``AsyncOpenAI`` client using the ``OPENAI_API_KEY`` env var.

    Raises ``ValueError`` if the key is not set.

    Prefer using :func:`openai_client` context manager to ensure cleanup.
    """
    from openai import AsyncOpenAI

    key = os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ValueError("Set OPENAI_API_KEY environment variable")
    return AsyncOpenAI(api_key=key, timeout=60.0)


def configure_llm_trace(output_dir: str | Path | None, *, enabled: bool = True) -> Path | None:
    """Configure a per-run JSONL trace file for all LLM traffic."""
    global _LLM_TRACE_PATH
    if not enabled or output_dir is None:
        _LLM_TRACE_PATH = None
        return None
    out = Path(output_dir)
    out.mkdir(parents=True, exist_ok=True)
    _LLM_TRACE_PATH = out / "llm_trace.jsonl"
    return _LLM_TRACE_PATH


def _safe_jsonable(value: Any) -> Any:
    """Best-effort conversion for JSONL tracing."""
    if value is None or isinstance(value, (str, int, float, bool)):
        return value
    if isinstance(value, dict):
        return {str(k): _safe_jsonable(v) for k, v in value.items()}
    if isinstance(value, (list, tuple, set)):
        return [_safe_jsonable(v) for v in value]
    return repr(value)


def _append_llm_trace(event: dict[str, Any]) -> None:
    """Append one structured trace event to the per-run JSONL file."""
    if _LLM_TRACE_PATH is None:
        return
    payload = dict(event)
    payload.setdefault("ts", time.strftime("%Y-%m-%dT%H:%M:%S"))
    line = json.dumps(_safe_jsonable(payload), ensure_ascii=True)
    with _LLM_TRACE_LOCK:
        with _LLM_TRACE_PATH.open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")


@asynccontextmanager
async def openai_client():
    """Async context manager that creates and closes an OpenAI client.

    Ensures the httpx AsyncClient is properly closed, avoiding
    "Event loop is closed" RuntimeError during garbage collection.

    Usage::

        async with openai_client() as client:
            resp = await client.chat.completions.create(...)
    """
    client = get_openai_client()
    try:
        yield client
    finally:
        await client.close()


# ---------------------------------------------------------------------------
# LLM retry helper
# ---------------------------------------------------------------------------

class LLMRetryExhausted(Exception):
    """All retries exhausted for an LLM API call."""


async def llm_request(
    client,
    *,
    messages: list[dict],
    model: str,
    max_tokens: int,
    label: str,
    temperature: float | None = 0,
    json_mode: bool = True,
    max_retries: int = 5,
    trace_context: dict[str, Any] | None = None,
) -> tuple[str, int]:
    """Make an OpenAI chat completion with automatic retry logic.

    Handles rate limits, empty responses, temperature/json_mode probe
    failures, and quota exhaustion. Returns (content, tokens_used).

    Parameters
    ----------
    client:
        AsyncOpenAI client instance.
    messages:
        Chat messages list (system + user).
    model:
        Model name string.
    max_tokens:
        max_completion_tokens for the request.
    label:
        Human-readable label for log messages (e.g. person name).
    temperature:
        Request temperature. Set to None to omit from request.
    json_mode:
        Whether to request JSON response format.
    max_retries:
        Maximum number of real attempts before giving up.

    Returns
    -------
    tuple[str, int]
        (response_content, tokens_used)

    Raises
    ------
    LLMRetryExhausted
        When all retries are exhausted (empty content, rate limits, etc.).
        The ``args[0]`` string describes the failure reason.
    Exception
        Re-raised 402 quota exhaustion errors.
    """
    skip_temp = model.strip().lower().startswith("gpt-5-mini")
    skip_json = False
    effective_max_tokens = max_tokens
    real_attempt = 0
    context = trace_context or {}

    for _ in range(max_retries + 4):  # headroom for probe retries
        if real_attempt >= max_retries:
            break
        attempt_no = real_attempt + 1
        started = time.time()
        req: dict[str, Any] = {
            "model": model,
            "max_completion_tokens": effective_max_tokens,
            "messages": messages,
        }
        if json_mode and not skip_json:
            req["response_format"] = {"type": "json_object"}
        if temperature is not None and not skip_temp:
            req["temperature"] = temperature
        try:
            _append_llm_trace({
                "kind": "llm_attempt_start",
                "label": label,
                "attempt": attempt_no,
                "model": model,
                "max_retries": max_retries,
                "json_mode_requested": json_mode,
                "json_mode_enabled": "response_format" in req,
                "temperature_requested": temperature,
                "temperature_enabled": "temperature" in req,
                "request": req,
                "context": context,
            })

            resp = await client.chat.completions.create(**req)
            finish_reason = resp.choices[0].finish_reason

            content = resp.choices[0].message.content
            if not content or not content.strip():
                # Empty + finish_reason=length → model needs more output tokens
                if finish_reason == "length" and effective_max_tokens < max_tokens * 4:
                    new_limit = min(effective_max_tokens * 2, max_tokens * 4)
                    logger.info("%s: empty content (finish=length), bumping max_tokens %d→%d",
                                label, effective_max_tokens, new_limit)
                    _append_llm_trace({
                        "kind": "llm_attempt_retry",
                        "label": label,
                        "attempt": attempt_no,
                        "status": "empty_content_finish_length",
                        "finish_reason": finish_reason,
                        "duration_ms": round((time.time() - started) * 1000, 1),
                        "response_id": getattr(resp, "id", None),
                        "response_content": content,
                        "retry_update": {"max_completion_tokens": [effective_max_tokens, new_limit]},
                        "context": context,
                    })
                    effective_max_tokens = new_limit
                    continue  # don't count as real attempt
                # Empty + json_mode → try without json_mode (but only for non-gpt-5 models
                # since gpt-5 family REQUIRES json_mode to avoid empty responses)
                if json_mode and not skip_json and "gpt-5" not in model:
                    logger.info("%s: empty content with json_mode, retrying without it", label)
                    _append_llm_trace({
                        "kind": "llm_attempt_retry",
                        "label": label,
                        "attempt": attempt_no,
                        "status": "empty_content_disable_json_mode",
                        "finish_reason": finish_reason,
                        "duration_ms": round((time.time() - started) * 1000, 1),
                        "response_id": getattr(resp, "id", None),
                        "response_content": content,
                        "retry_update": {"json_mode_enabled": [True, False]},
                        "context": context,
                    })
                    skip_json = True
                    continue  # don't count
                real_attempt += 1
                logger.warning("%s: empty content (attempt %d/%d)", label, real_attempt, max_retries)
                _append_llm_trace({
                    "kind": "llm_attempt_error",
                    "label": label,
                    "attempt": attempt_no,
                    "status": "empty_content",
                    "finish_reason": finish_reason,
                    "duration_ms": round((time.time() - started) * 1000, 1),
                    "response_id": getattr(resp, "id", None),
                    "response_content": content,
                    "context": context,
                })
                if real_attempt < max_retries:
                    await asyncio.sleep(1)
                    continue
                raise LLMRetryExhausted("empty_content")

            tokens = resp.usage.total_tokens if resp.usage else 0
            _append_llm_trace({
                "kind": "llm_attempt_success",
                "label": label,
                "attempt": attempt_no,
                "duration_ms": round((time.time() - started) * 1000, 1),
                "finish_reason": finish_reason,
                "tokens_used": tokens,
                "response_id": getattr(resp, "id", None),
                "response_content": content,
                "context": context,
            })
            return content, tokens

        except LLMRetryExhausted:
            raise

        except Exception as e:
            err = str(e)
            if "temperature" in err.lower() and "does not support" in err.lower():
                logger.info("%s: model doesn't support temperature, retrying without it", label)
                _append_llm_trace({
                    "kind": "llm_attempt_retry",
                    "label": label,
                    "attempt": attempt_no,
                    "status": "temperature_unsupported",
                    "duration_ms": round((time.time() - started) * 1000, 1),
                    "error": err[:2000],
                    "retry_update": {"temperature_enabled": [True, False]},
                    "context": context,
                })
                skip_temp = True
                continue  # don't count
            if "max_tokens" in err.lower() or "max_completion_tokens" in err.lower():
                logger.info("%s: token param error, retrying without json_mode", label)
                _append_llm_trace({
                    "kind": "llm_attempt_retry",
                    "label": label,
                    "attempt": attempt_no,
                    "status": "token_param_error_disable_json_mode",
                    "duration_ms": round((time.time() - started) * 1000, 1),
                    "error": err[:2000],
                    "retry_update": {"json_mode_enabled": [True, False]},
                    "context": context,
                })
                skip_json = True
                continue  # don't count
            if "402" in err:
                logger.error("API quota exhausted!")
                _append_llm_trace({
                    "kind": "llm_attempt_error",
                    "label": label,
                    "attempt": attempt_no,
                    "status": "quota_exhausted",
                    "duration_ms": round((time.time() - started) * 1000, 1),
                    "error": err[:2000],
                    "context": context,
                })
                raise

            real_attempt += 1
            if "429" in err or "rate" in err.lower():
                backoff = min(60, 2 ** real_attempt)
                logger.warning("%s: rate limited (attempt %d/%d), backing off %ds",
                               label, real_attempt, max_retries, backoff)
                _append_llm_trace({
                    "kind": "llm_attempt_error",
                    "label": label,
                    "attempt": attempt_no,
                    "status": "rate_limited",
                    "duration_ms": round((time.time() - started) * 1000, 1),
                    "error": err[:2000],
                    "backoff_seconds": backoff,
                    "context": context,
                })
                await asyncio.sleep(backoff)
            else:
                logger.warning("%s: error (attempt %d/%d): %s",
                               label, real_attempt, max_retries, err[:200])
                _append_llm_trace({
                    "kind": "llm_attempt_error",
                    "label": label,
                    "attempt": attempt_no,
                    "status": "error",
                    "duration_ms": round((time.time() - started) * 1000, 1),
                    "error": err[:2000],
                    "context": context,
                })
                if real_attempt < max_retries:
                    await asyncio.sleep(2 * real_attempt)
                    continue
                raise LLMRetryExhausted(err[:200])

    _append_llm_trace({
        "kind": "llm_request_exhausted",
        "label": label,
        "attempts": real_attempt,
        "status": "max_retries",
        "context": context,
    })
    raise LLMRetryExhausted("max_retries")
