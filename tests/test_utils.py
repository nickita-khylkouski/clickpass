"""Tests for cv_rank.utils — username parsing, JSON extraction, supa_get, OpenAI client."""

import json
import os
from unittest import mock

import pytest

from cv_rank.utils import (
    configure_llm_trace,
    extract_json,
    get_openai_client,
    llm_request,
    parse_github_username,
    parse_linkedin_username,
    supa_get,
)


# ---------------------------------------------------------------------------
# parse_linkedin_username
# ---------------------------------------------------------------------------

class TestParseLinkedinUsername:
    def test_standard_url(self):
        assert parse_linkedin_username("https://www.linkedin.com/in/john-doe/") == "john-doe"

    def test_no_trailing_slash(self):
        assert parse_linkedin_username("https://linkedin.com/in/alice") == "alice"

    def test_with_extra_path(self):
        assert parse_linkedin_username("https://linkedin.com/in/bob123") == "bob123"

    def test_http_prefix(self):
        assert parse_linkedin_username("http://linkedin.com/in/Charlie") == "charlie"

    def test_empty_string(self):
        assert parse_linkedin_username("") is None

    def test_none(self):
        assert parse_linkedin_username(None) is None

    def test_not_linkedin(self):
        assert parse_linkedin_username("https://github.com/in/user") is None

    def test_numeric_input(self):
        assert parse_linkedin_username(12345) is None

    def test_dots_underscores(self):
        assert parse_linkedin_username("https://linkedin.com/in/a.b_c") == "a.b_c"


# ---------------------------------------------------------------------------
# parse_github_username
# ---------------------------------------------------------------------------

class TestParseGithubUsername:
    def test_standard_url(self):
        assert parse_github_username("https://github.com/torvalds") == "torvalds"

    def test_trailing_slash(self):
        assert parse_github_username("https://github.com/octocat/") == "octocat"

    def test_reserved_login(self):
        assert parse_github_username("https://github.com/login") is None

    def test_reserved_settings(self):
        assert parse_github_username("https://github.com/settings") is None

    def test_reserved_marketplace(self):
        assert parse_github_username("https://github.com/marketplace") is None

    def test_empty_string(self):
        assert parse_github_username("") is None

    def test_none(self):
        assert parse_github_username(None) is None

    def test_www_prefix(self):
        assert parse_github_username("https://www.github.com/user123") == "user123"

    def test_mixed_case_lowered(self):
        assert parse_github_username("https://github.com/TorVaLds") == "torvalds"

    def test_dots_dashes(self):
        assert parse_github_username("https://github.com/my-user.name") == "my-user.name"


# ---------------------------------------------------------------------------
# extract_json
# ---------------------------------------------------------------------------

class TestExtractJson:
    def test_plain_json(self):
        assert extract_json('{"score": 42}') == {"score": 42}

    def test_fenced_json(self):
        result = extract_json('```json\n{"score": 42}\n```')
        assert result == {"score": 42}

    def test_fenced_no_lang(self):
        result = extract_json('```\n{"key": "val"}\n```')
        assert result == {"key": "val"}

    def test_nested_json(self):
        result = extract_json('Some text {"a": {"b": 1}} more text')
        assert result == {"a": {"b": 1}}

    def test_none_raises_value_error(self):
        with pytest.raises(ValueError, match="null content"):
            extract_json(None)

    def test_no_json_raises(self):
        with pytest.raises(json.JSONDecodeError):
            extract_json("no json here at all")

    def test_whitespace_around(self):
        result = extract_json('  \n  {"x": 1}  \n  ')
        assert result == {"x": 1}

    def test_complex_nested(self):
        text = '{"verdict": "YES", "achievements": ["a", "b"]}'
        result = extract_json(text)
        assert result["verdict"] == "YES"
        assert len(result["achievements"]) == 2


# ---------------------------------------------------------------------------
# get_openai_client
# ---------------------------------------------------------------------------

class TestGetOpenaiClient:
    def test_raises_without_key(self):
        with mock.patch.dict(os.environ, {}, clear=True):
            # Ensure OPENAI_API_KEY is not set
            os.environ.pop("OPENAI_API_KEY", None)
            with pytest.raises(ValueError, match="OPENAI_API_KEY"):
                get_openai_client()

    def test_returns_client_with_key(self):
        with mock.patch.dict(os.environ, {"OPENAI_API_KEY": "sk-test-key"}):
            client = get_openai_client()
            # Should be an AsyncOpenAI instance
            assert hasattr(client, "chat")


class TestLlmRequest:
    @pytest.mark.asyncio
    async def test_omits_temperature_for_configured_model(self):
        response = mock.MagicMock()
        response.choices = [mock.MagicMock()]
        response.choices[0].finish_reason = "stop"
        response.choices[0].message.content = '{"score": 42}'
        response.usage = mock.MagicMock(total_tokens=123)

        client = mock.AsyncMock()
        client.chat.completions.create.return_value = response

        content, tokens = await llm_request(
            client,
            messages=[{"role": "user", "content": "score this"}],
            model="gpt-5-mini",
            max_tokens=100,
            label="test",
            temperature=0.4,
            max_retries=1,
        )

        assert content == '{"score": 42}'
        assert tokens == 123
        assert "temperature" not in client.chat.completions.create.call_args.kwargs

    @pytest.mark.asyncio
    async def test_keeps_temperature_for_other_models(self):
        response = mock.MagicMock()
        response.choices = [mock.MagicMock()]
        response.choices[0].finish_reason = "stop"
        response.choices[0].message.content = '{"score": 42}'
        response.usage = mock.MagicMock(total_tokens=123)

        client = mock.AsyncMock()
        client.chat.completions.create.return_value = response

        await llm_request(
            client,
            messages=[{"role": "user", "content": "score this"}],
            model="gpt-4o",
            max_tokens=100,
            label="test",
            temperature=0.4,
            max_retries=1,
        )

        assert client.chat.completions.create.call_args.kwargs["temperature"] == 0.4

    @pytest.mark.asyncio
    async def test_writes_full_trace_jsonl_on_success(self, tmp_path):
        trace_path = configure_llm_trace(tmp_path)
        response = mock.MagicMock()
        response.id = "resp_123"
        response.choices = [mock.MagicMock()]
        response.choices[0].finish_reason = "stop"
        response.choices[0].message.content = '{"score": 42, "why": "strong profile"}'
        response.usage = mock.MagicMock(total_tokens=123)

        client = mock.AsyncMock()
        client.chat.completions.create.return_value = response

        await llm_request(
            client,
            messages=[
                {"role": "system", "content": "system prompt"},
                {"role": "user", "content": "user prompt"},
            ],
            model="gpt-5.2",
            max_tokens=100,
            label="alice",
            max_retries=1,
            trace_context={"phase": "pointwise", "candidate_name": "alice"},
        )

        lines = trace_path.read_text().strip().splitlines()
        assert len(lines) == 2
        start_event = json.loads(lines[0])
        end_event = json.loads(lines[1])
        assert start_event["kind"] == "llm_attempt_start"
        assert start_event["request"]["messages"][0]["content"] == "system prompt"
        assert start_event["context"]["phase"] == "pointwise"
        assert end_event["kind"] == "llm_attempt_success"
        assert end_event["response_content"] == '{"score": 42, "why": "strong profile"}'
        assert end_event["tokens_used"] == 123

    @pytest.mark.asyncio
    async def test_traces_temperature_fallback(self, tmp_path):
        trace_path = configure_llm_trace(tmp_path)
        response = mock.MagicMock()
        response.id = "resp_456"
        response.choices = [mock.MagicMock()]
        response.choices[0].finish_reason = "stop"
        response.choices[0].message.content = '{"score": 42}'
        response.usage = mock.MagicMock(total_tokens=55)

        client = mock.AsyncMock()
        client.chat.completions.create.side_effect = [
            RuntimeError("This model does not support temperature"),
            response,
        ]

        await llm_request(
            client,
            messages=[{"role": "user", "content": "score this"}],
            model="gpt-4o",
            max_tokens=100,
            label="temp-fallback",
            temperature=0.4,
            max_retries=1,
        )

        events = [json.loads(line) for line in trace_path.read_text().strip().splitlines()]
        assert any(e["kind"] == "llm_attempt_retry" and e["status"] == "temperature_unsupported" for e in events)
        success = next(e for e in events if e["kind"] == "llm_attempt_success")
        assert success["attempt"] == 1


# ---------------------------------------------------------------------------
# supa_get
# ---------------------------------------------------------------------------

class TestSupaGet:
    def test_success(self):
        fake_data = [{"id": 1, "name": "test"}]
        mock_response = mock.MagicMock()
        mock_response.read.return_value = json.dumps(fake_data).encode()
        mock_response.__enter__ = mock.MagicMock(return_value=mock_response)
        mock_response.__exit__ = mock.MagicMock(return_value=False)

        with mock.patch("urllib.request.urlopen", return_value=mock_response):
            result = supa_get("https://test.supabase.co", "key123", "users", {"email": "eq.a@b.com"})
            assert result == fake_data

    def test_returns_empty_on_error_code(self):
        fake_data = {"code": "42501", "message": "permission denied"}
        mock_response = mock.MagicMock()
        mock_response.read.return_value = json.dumps(fake_data).encode()
        mock_response.__enter__ = mock.MagicMock(return_value=mock_response)
        mock_response.__exit__ = mock.MagicMock(return_value=False)

        with mock.patch("urllib.request.urlopen", return_value=mock_response):
            result = supa_get("https://test.supabase.co", "key123", "users", {})
            assert result == []

    def test_returns_empty_on_http_401(self):
        import urllib.error
        exc = urllib.error.HTTPError("url", 401, "Unauthorized", {}, None)
        with mock.patch("urllib.request.urlopen", side_effect=exc):
            result = supa_get("https://test.supabase.co", "bad_key", "users", {})
            assert result == []

    def test_returns_empty_on_connection_error(self):
        import urllib.error
        exc = urllib.error.URLError("Connection refused")
        with mock.patch("urllib.request.urlopen", side_effect=exc):
            result = supa_get("https://test.supabase.co", "key", "users", {})
            assert result == []

    def test_returns_empty_on_unexpected_error(self):
        with mock.patch("urllib.request.urlopen", side_effect=RuntimeError("boom")):
            result = supa_get("https://test.supabase.co", "key", "users", {})
            assert result == []

    def test_non_list_response_returns_empty(self):
        mock_response = mock.MagicMock()
        mock_response.read.return_value = json.dumps("string_not_list").encode()
        mock_response.__enter__ = mock.MagicMock(return_value=mock_response)
        mock_response.__exit__ = mock.MagicMock(return_value=False)

        with mock.patch("urllib.request.urlopen", return_value=mock_response):
            result = supa_get("https://test.supabase.co", "key", "users", {})
            assert result == []
