from __future__ import annotations

from cv_rank_v2.ingest.event_loader import load_event_applicants


def test_load_event_applicants_prefers_platform_and_samples(monkeypatch) -> None:
    platform_people = [
        {
            "name": "Alice Chen",
            "first_name": "Alice",
            "last_name": "Chen",
            "email": "alice@example.com",
            "linkedin_url": "linkedin.com/in/alice",
            "github_url": "github.com/alice",
            "self_description": "Builder",
            "_raw_csv": {"What are you building?": "An agent"},
        },
        {
            "name": "Bob Park",
            "first_name": "Bob",
            "last_name": "Park",
            "email": "bob@example.com",
            "linkedin_url": "linkedin.com/in/bob",
            "github_url": "github.com/bob",
            "self_description": "Researcher",
            "_raw_csv": {"What are you building?": "Infra"},
        },
        {
            "name": "Cara Singh",
            "first_name": "Cara",
            "last_name": "Singh",
            "email": "cara@example.com",
            "linkedin_url": "linkedin.com/in/cara",
            "github_url": "github.com/cara",
            "self_description": "Founder",
            "_raw_csv": {"What are you building?": "Evaluation tooling"},
        },
    ]

    monkeypatch.setattr(
        "cv_rank_v2.ingest.event_loader.load_from_platform_db",
        lambda event_name, dsn: platform_people,
    )
    monkeypatch.setattr(
        "cv_rank_v2.ingest.event_loader.load_from_supabase",
        lambda event_name, url, key: [],
    )

    result = load_event_applicants(
        "Nebius.Build SF",
        platform_dsn="postgres://example",
        sample_size=2,
        sample_seed=7,
    )

    assert result.validation.valid
    assert result.validation.source == "platform_db:Nebius.Build SF"
    assert len(result.applicants) == 2
    assert any(message.code == "sampled_event_applicants" for message in result.validation.messages)
    assert all(applicant.application_answers for applicant in result.applicants)


def test_load_event_applicants_falls_back_to_supabase(monkeypatch) -> None:
    supabase_people = [
        {
            "name": "Dina Rao",
            "first_name": "Dina",
            "last_name": "Rao",
            "email": "dina@example.com",
            "linkedin_url": "linkedin.com/in/dina",
            "github_url": "github.com/dina",
            "self_description": "ML engineer",
        }
    ]

    monkeypatch.setattr(
        "cv_rank_v2.ingest.event_loader.load_from_platform_db",
        lambda event_name, dsn: [],
    )
    monkeypatch.setattr(
        "cv_rank_v2.ingest.event_loader.load_from_supabase",
        lambda event_name, url, key: supabase_people,
    )

    result = load_event_applicants(
        "Gemini 3 NYC Hackathon",
        platform_dsn="postgres://example",
        supabase_url="https://example.supabase.co",
        supabase_key="secret",
    )

    assert result.validation.valid
    assert result.validation.source == "supabase:Gemini 3 NYC Hackathon"
    assert [applicant.name for applicant in result.applicants] == ["Dina Rao"]
