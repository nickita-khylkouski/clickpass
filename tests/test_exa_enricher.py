from __future__ import annotations

from types import SimpleNamespace

from cv_rank.enrichment import exa_enricher


class _FakeSearchResult:
    def __init__(self, url: str, *, highlights: list[str] | None = None, summary: str | None = None) -> None:
        self.url = url
        self.highlights = highlights or []
        self.summary = summary


class _FakeContentResult:
    def __init__(self, text: str) -> None:
        self.text = text


class _FakeExa:
    def __init__(self) -> None:
        self.search_calls: list[tuple[str, dict]] = []
        self.contents_calls: list[tuple[list[str], bool]] = []

    def search(self, query: str, **kwargs):
        self.search_calls.append((query, kwargs))
        if "site:linkedin.com" in query:
            return SimpleNamespace(results=[_FakeSearchResult("https://linkedin.com/in/fallback-person")])
        return SimpleNamespace(results=[])

    def get_contents(self, urls: list[str], text: bool = True):
        self.contents_calls.append((list(urls), text))
        return SimpleNamespace(results=[_FakeContentResult("LinkedIn profile text") for _ in urls])


class TestCollectExaData:
    def test_uses_exact_linkedin_url_when_available(self) -> None:
        exa = _FakeExa()
        person = {
            "linkedin_url": "https://linkedin.com/in/exact-person",
            "company": "Anthropic",
            "education_with_schools": [{"school_name": "MIT"}],
        }

        data = exa_enricher._collect_exa_data(exa, "Alice Chen", "alice@example.com", {}, person)

        assert exa.contents_calls[0][0] == ["https://linkedin.com/in/exact-person"]
        assert not any("site:linkedin.com" in query for query, _ in exa.search_calls)
        assert data["sources"]["linkedin"]["urls"] == 1

    def test_linkedin_fallback_query_includes_company_and_school(self) -> None:
        exa = _FakeExa()
        person = {
            "company": "Anthropic",
            "education_with_schools": [{"school_name": "MIT"}],
        }

        exa_enricher._collect_exa_data(exa, "Alice Chen", "alice@example.com", {}, person)

        linkedin_query = next(query for query, _ in exa.search_calls if "site:linkedin.com" in query)
        assert '"Alice Chen"' in linkedin_query
        assert "Anthropic" in linkedin_query
        assert "MIT" in linkedin_query


class TestExtractExaResume:
    def test_prompt_includes_known_facts_and_validation_rules(self, monkeypatch) -> None:
        captured: dict[str, object] = {}

        class _FakeModel:
            def __init__(self, model_name: str) -> None:
                captured["model_name"] = model_name

            def generate_content(self, prompt: str, generation_config: dict):
                captured["prompt"] = prompt
                captured["generation_config"] = generation_config
                return SimpleNamespace(text="Structured resume")

        monkeypatch.setattr(
            exa_enricher,
            "genai",
            SimpleNamespace(GenerativeModel=lambda model_name: _FakeModel(model_name)),
        )

        data = {
            "name": "Alice Chen",
            "email": "alice@example.com",
            "sources": {
                "linkedin": {"chars": 20, "full_text": "Anthropic engineer"},
                "github": {"highlights": ["Built agent infra"]},
                "devpost": {"highlights": ["Won 2 hackathons"]},
                "web": {"highlights": ["Scholar award"]},
                "publications": {"summaries": ["Paper summary"], "highlights": ["NeurIPS 2025"]},
            },
        }
        person = {
            "company": "Anthropic",
            "linkedin_headline": "ML Engineer @ Anthropic",
            "education_with_schools": [{"school_name": "MIT"}],
        }

        result = exa_enricher._extract_exa_resume(data, {}, person)

        assert result["resume"] == "Structured resume"
        prompt = str(captured["prompt"])
        assert "KNOWN FACTS" in prompt
        assert "Anthropic" in prompt
        assert "ML Engineer @ Anthropic" in prompt
        assert "MIT" in prompt
        assert "⚠️ MISMATCH" in prompt
        assert "⚠️ AMBIGUOUS" in prompt
