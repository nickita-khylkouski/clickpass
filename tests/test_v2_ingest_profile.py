from __future__ import annotations

from pathlib import Path

from cv_rank_v2.ingest.models import Applicant, CertificationEntry, EducationEntry, EventEntry, ProjectEntry, PublicationEntry, WorkEntry
from cv_rank_v2.ingest.csv_loader import load_csv
from cv_rank_v2.ingest.normalize import build_header_map, normalize_row
from cv_rank_v2.profile.render import format_profile


def test_header_aliases_and_stable_applicant_id(tmp_path: Path) -> None:
    csv_path = tmp_path / "applicants.csv"
    csv_path.write_text(
        "Full Name,Email Address,Job Title,Company,LinkedIn,GitHub,Years Experience,Notable Achievements\n"
        "Alice Chen,alice@example.com,Senior ML Engineer,Anthropic,linkedin.com/in/alice,github.com/alice,8,NeurIPS best paper; Google award\n"
    )

    result1 = load_csv(csv_path)
    result2 = load_csv(csv_path)

    assert result1.validation.valid is True
    assert len(result1.applicants) == 1
    applicant = result1.applicants[0]

    assert applicant.name == "Alice Chen"
    assert applicant.email == "alice@example.com"
    assert applicant.role == "Senior ML Engineer"
    assert applicant.linkedin_url == "https://linkedin.com/in/alice"
    assert applicant.github_url == "https://github.com/alice"
    assert applicant.years_experience == 8.0
    assert applicant.notable_achievements == ("NeurIPS best paper", "Google award")
    assert applicant.applicant_id == result2.applicants[0].applicant_id


def test_duplicate_rows_are_deduplicated_by_stable_id(tmp_path: Path) -> None:
    csv_path = tmp_path / "dupes.csv"
    csv_path.write_text(
        "Name,Email,Company\n"
        "Alice Chen,alice@example.com,Anthropic\n"
        "Alice Chen,alice@example.com,Anthropic\n"
    )

    result = load_csv(csv_path)
    assert len(result.applicants) == 1
    assert any(message.code == "duplicate_applicant" for message in result.validation.messages)


def test_missing_name_can_fall_back_to_email(tmp_path: Path) -> None:
    csv_path = tmp_path / "email_only.csv"
    csv_path.write_text(
        "Email,Company\n"
        "bob@example.com,OpenAI\n"
    )

    result = load_csv(csv_path)
    assert len(result.applicants) == 1
    assert result.applicants[0].name == "bob@example.com"
    assert any(message.code == "synthesized_name_from_email" for message in result.validation.messages)


def test_normalize_row_parses_structured_evidence_fields() -> None:
    headers = ["Name", "Work History", "Education History", "Projects", "Publications Detail", "Certifications Detail", "Event History", "Application Answers"]
    header_map = build_header_map(headers)
    row = {
        "Name": "Alice Chen",
        "Work History": '[{"title":"Senior ML Engineer","company_name":"Anthropic","from_date":"2022-01","is_current":true,"description":"Building safe AI"}]',
        "Education History": '[{"school_name":"Stanford University","degree":"MS","field_of_study":"Computer Science","from_date":"2016-09","to_date":"2018-06","education_level":"Masters"}]',
        "Projects": '[{"title":"LLM safety toolkit","description":"Internal eval harness"}]',
        "Publications Detail": '[{"name":"Safe Alignment Paper","publisher":"NeurIPS"}]',
        "Certifications Detail": '[{"name":"AWS ML Specialty","authority":"Amazon"}]',
        "Event History": '[{"event_name":"CV Hackathon","event_date":"2024-01","status":"attended"}]',
        "Application Answers": '[{"question":"Why join?","answer":"Build safer systems"}]',
    }

    applicant, messages = normalize_row(row, header_map, source_name="test", row_number=2)

    assert applicant is not None
    assert applicant.work_history[0].company == "Anthropic"
    assert applicant.education_history[0].school == "Stanford University"
    assert applicant.projects[0].title == "LLM safety toolkit"
    assert applicant.publications[0].publisher == "NeurIPS"
    assert applicant.certifications[0].authority == "Amazon"
    assert applicant.event_history[0].status == "attended"
    assert applicant.application_answers[0].question == "Why join?"
    assert messages == ()


def test_normalize_row_and_header_map_are_order_insensitive() -> None:
    headers = ["GitHub", "Job Title", "Full Name", "Email"]
    row = {"GitHub": "github.com/test", "Job Title": "Founder", "Full Name": "Test User", "Email": "test@example.com"}
    header_map = build_header_map(headers)

    applicant_a, messages_a = normalize_row(row, header_map, source_name="test", row_number=2)
    applicant_b, messages_b = normalize_row(
        {"Email": "test@example.com", "Full Name": "Test User", "Job Title": "Founder", "GitHub": "github.com/test"},
        header_map,
        source_name="test",
        row_number=2,
    )

    assert applicant_a is not None and applicant_b is not None
    assert applicant_a.applicant_id == applicant_b.applicant_id
    assert messages_a == messages_b


def test_profile_render_is_structured_and_deterministic(tmp_path: Path) -> None:
    applicant = Applicant(
        applicant_id="app_123456789abc",
        name="Alice Chen",
        email="alice@example.com",
        company="Anthropic",
        role="Senior ML Engineer",
        location="San Francisco",
        linkedin_url="https://linkedin.com/in/alice",
        github_url="https://github.com/alice",
        self_description="Builds safe AI",
        ai_project="LLM safety toolkit",
        looking_for_job="no",
        total_cv_events=4,
        hackathon_submissions=2,
        work_history=(
            WorkEntry(title="Senior ML Engineer", company="Anthropic", start="2022-01", end="", current=True, description="Building safe AI"),
        ),
        education_history=(
            EducationEntry(school="Stanford University", degree="MS", field_of_study="Computer Science", start="2016-09", end="2018-06", level="Masters"),
        ),
        projects=(
            ProjectEntry(title="LLM safety toolkit", description="Internal eval harness"),
        ),
        publications=(
            PublicationEntry(title="Safe Alignment Paper", publisher="NeurIPS"),
        ),
        certifications=(
            CertificationEntry(name="AWS ML Specialty", authority="Amazon"),
        ),
        event_history=(
            EventEntry(name="CV Hackathon", date="2024-01", status="attended"),
        ),
        extra_fields=(("custom_note", "high confidence"),),
    )
    profile = format_profile(applicant)

    assert "BASIC INFO" in profile
    assert "CONTACT" in profile
    assert "APPLICATION" in profile
    assert "EVIDENCE" in profile
    assert "HISTORY" in profile
    assert "Applicant ID:" in profile
    assert "Alice Chen" in profile
    assert "LinkedIn: https://linkedin.com/in/alice" in profile
    assert "GitHub: https://github.com/alice" in profile
    assert "Total CV Events: 4" in profile
    assert "Hackathon Submissions: 2" in profile
