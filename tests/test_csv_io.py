"""Tests for cv_rank.csv_io — CSV loading, validation, and export."""

from __future__ import annotations

import csv
import json
from pathlib import Path


from cv_rank.csv_io import export_ranked_csv, load_csv, validate_csv


# ---------------------------------------------------------------------------
# load_csv
# ---------------------------------------------------------------------------


class TestLoadCsv:
    """Tests for load_csv()."""

    def test_load_well_formed_csv(self, tmp_path: Path) -> None:
        """A standard CSV with canonical column names is loaded correctly."""
        csv_path = tmp_path / "applicants.csv"
        csv_path.write_text(
            "Name,Email,Company,Role,LinkedIn URL,GitHub URL\n"
            "Alice Chen,alice@example.com,Anthropic,ML Engineer,https://li.com/alice,https://gh.com/alice\n"
            "Bob Smith,bob@test.com,Google,SWE,https://li.com/bob,\n"
        )

        people = load_csv(csv_path)

        assert len(people) == 2
        assert people[0]["name"] == "Alice Chen"
        assert people[0]["email"] == "alice@example.com"
        assert people[0]["company"] == "Anthropic"
        assert people[0]["role"] == "ML Engineer"
        assert people[0]["linkedin_url"] == "https://li.com/alice"
        assert people[0]["github_url"] == "https://gh.com/alice"

    def test_load_csv_first_last_name_columns(self, tmp_path: Path) -> None:
        """When First Name and Last Name columns are present, they are parsed."""
        csv_path = tmp_path / "people.csv"
        csv_path.write_text(
            "First Name,Last Name,Email\n"
            "Jane,Doe,jane@test.com\n"
            "John,Smith,john@test.com\n"
        )

        people = load_csv(csv_path)

        assert len(people) == 2
        # first_name and last_name are extracted from their own columns
        assert people[0]["first_name"] == "Jane"
        assert people[0]["last_name"] == "Doe"
        # name field is present (via substring heuristic mapping "name" to "First Name")
        assert people[0]["name"] != ""

    def test_load_csv_full_name_synthesis(self, tmp_path: Path) -> None:
        """When first_name + last_name are present but name is empty, name is synthesised."""
        csv_path = tmp_path / "people.csv"
        csv_path.write_text(
            "Full Name,First_Name,Last_Name,Email\n"
            ",Jane,Doe,jane@test.com\n"
        )

        people = load_csv(csv_path)

        assert len(people) == 1
        assert people[0]["name"] == "Jane Doe"
        assert people[0]["first_name"] == "Jane"
        assert people[0]["last_name"] == "Doe"

    def test_load_csv_dedup_by_email(self, tmp_path: Path) -> None:
        """Duplicate email addresses keep only the first occurrence."""
        csv_path = tmp_path / "dupes.csv"
        csv_path.write_text(
            "Name,Email\n"
            "Alice Original,alice@dup.com\n"
            "Alice Duplicate,alice@dup.com\n"
            "Bob Unique,bob@unique.com\n"
        )

        people = load_csv(csv_path)

        assert len(people) == 2
        names = [p["name"] for p in people]
        assert "Alice Original" in names
        assert "Alice Duplicate" not in names
        assert "Bob Unique" in names

    def test_load_csv_empty_file(self, tmp_path: Path) -> None:
        """An empty CSV (headers only or no rows) returns an empty list."""
        csv_path = tmp_path / "empty.csv"
        csv_path.write_text("Name,Email\n")

        people = load_csv(csv_path)
        assert people == []

    def test_load_csv_strips_whitespace(self, tmp_path: Path) -> None:
        """Leading/trailing whitespace is stripped from values."""
        csv_path = tmp_path / "spaces.csv"
        csv_path.write_text(
            "Name,Email,Company\n"
            "  Alice Chen  , alice@test.com ,  Anthropic  \n"
        )

        people = load_csv(csv_path)
        assert people[0]["name"] == "Alice Chen"
        assert people[0]["email"] == "alice@test.com"
        assert people[0]["company"] == "Anthropic"

    def test_load_csv_preserves_raw_csv(self, tmp_path: Path) -> None:
        """The _raw_csv field contains the original column values."""
        csv_path = tmp_path / "raw.csv"
        csv_path.write_text(
            "Name,Email,Custom Field\n"
            "Alice,alice@test.com,custom_value\n"
        )

        people = load_csv(csv_path)
        assert "_raw_csv" in people[0]
        assert "Custom Field" in people[0]["_raw_csv"]
        assert people[0]["_raw_csv"]["Custom Field"] == "custom_value"

    def test_load_csv_skips_rows_without_name(self, tmp_path: Path) -> None:
        """Rows with no usable name are silently skipped."""
        csv_path = tmp_path / "noname.csv"
        csv_path.write_text(
            "Name,Email\n"
            ",anonymous@test.com\n"
            "Alice Chen,alice@test.com\n"
        )

        people = load_csv(csv_path)
        assert len(people) == 1
        assert people[0]["name"] == "Alice Chen"

    def test_load_csv_case_insensitive_headers(self, tmp_path: Path) -> None:
        """Column detection works case-insensitively."""
        csv_path = tmp_path / "caps.csv"
        csv_path.write_text(
            "NAME,EMAIL,COMPANY\n"
            "Alice,alice@test.com,Anthropic\n"
        )

        people = load_csv(csv_path)
        assert len(people) == 1
        assert people[0]["name"] == "Alice"
        assert people[0]["email"] == "alice@test.com"

    def test_load_csv_email_lowercased(self, tmp_path: Path) -> None:
        """Emails are normalised to lowercase."""
        csv_path = tmp_path / "email_case.csv"
        csv_path.write_text(
            "Name,Email\n"
            "Alice,Alice@EXAMPLE.COM\n"
        )

        people = load_csv(csv_path)
        assert people[0]["email"] == "alice@example.com"

    def test_load_csv_derives_first_last_from_full_name(self, tmp_path: Path) -> None:
        """If only full name is present, first_name and last_name are derived."""
        csv_path = tmp_path / "fullname.csv"
        csv_path.write_text(
            "Name,Email\n"
            "Alice Chen,alice@test.com\n"
        )

        people = load_csv(csv_path)
        assert people[0]["first_name"] == "Alice"
        assert people[0]["last_name"] == "Chen"


# ---------------------------------------------------------------------------
# validate_csv
# ---------------------------------------------------------------------------


class TestValidateCsv:
    """Tests for validate_csv()."""

    def test_validate_valid_file(self, tmp_path: Path) -> None:
        """A well-formed CSV passes validation."""
        csv_path = tmp_path / "valid.csv"
        csv_path.write_text(
            "Name,Email,Company\n"
            "Alice,alice@test.com,Anthropic\n"
        )

        is_valid, messages = validate_csv(csv_path)
        assert is_valid is True
        assert any("OK:" in m for m in messages)

    def test_validate_empty_file(self, tmp_path: Path) -> None:
        """A CSV with headers but no data rows fails."""
        csv_path = tmp_path / "empty.csv"
        csv_path.write_text("Name,Email\n")

        is_valid, messages = validate_csv(csv_path)
        assert is_valid is False
        assert any("no data rows" in m.lower() for m in messages)

    def test_validate_missing_file(self, tmp_path: Path) -> None:
        """A non-existent file fails validation."""
        csv_path = tmp_path / "nonexistent.csv"

        is_valid, messages = validate_csv(csv_path)
        assert is_valid is False
        assert any("not found" in m.lower() for m in messages)

    def test_validate_no_name_column(self, tmp_path: Path) -> None:
        """A CSV without any name column fails validation."""
        csv_path = tmp_path / "noname.csv"
        csv_path.write_text(
            "Email,Company\n"
            "alice@test.com,Anthropic\n"
        )

        is_valid, messages = validate_csv(csv_path)
        assert is_valid is False
        assert any("name column" in m.lower() for m in messages)

    def test_validate_first_last_name_columns_ok(self, tmp_path: Path) -> None:
        """A CSV with First/Last Name columns passes validation."""
        csv_path = tmp_path / "firstlast.csv"
        csv_path.write_text(
            "First Name,Last Name,Email\n"
            "Jane,Doe,jane@test.com\n"
        )

        is_valid, messages = validate_csv(csv_path)
        assert is_valid is True
        # The substring heuristic maps 'name' to 'First Name', so has_name is True
        assert any("OK:" in m for m in messages)

    def test_validate_multiple_rows(self, tmp_path: Path) -> None:
        """A CSV with multiple data rows reports correct row count."""
        csv_path = tmp_path / "multi.csv"
        csv_path.write_text(
            "Name,Email\n"
            "Alice,alice@test.com\n"
            "Bob,bob@test.com\n"
            "Carol,carol@test.com\n"
        )

        is_valid, messages = validate_csv(csv_path)
        assert is_valid is True
        assert any("3 rows" in m for m in messages)

    def test_validate_warns_missing_useful_columns(self, tmp_path: Path) -> None:
        """Warnings are emitted for missing useful columns like email, linkedin, etc."""
        csv_path = tmp_path / "minimal.csv"
        csv_path.write_text(
            "Name\n"
            "Alice\n"
        )

        is_valid, messages = validate_csv(csv_path)
        assert is_valid is True
        warning_cols = [m for m in messages if "WARNING" in m]
        # Should warn about email, linkedin_url, github_url, company, role
        assert len(warning_cols) >= 4

    def test_validate_header_only_no_rows(self, tmp_path: Path) -> None:
        """CSV with just a header line and no content rows is invalid."""
        csv_path = tmp_path / "headeronly.csv"
        csv_path.write_text("Name,Email,Company\n")

        is_valid, messages = validate_csv(csv_path)
        assert is_valid is False


# ---------------------------------------------------------------------------
# export_ranked_csv
# ---------------------------------------------------------------------------


class TestExportRankedCsv:
    """Tests for export_ranked_csv()."""

    def test_export_produces_correct_columns(self, tmp_path: Path, sample_config: dict) -> None:
        """Exported CSV has expected column structure including dynamic event columns."""
        people = [
            {
                "name": "Alice Chen",
                "first_name": "Alice",
                "last_name": "Chen",
                "email": "alice@test.com",
                "company": "Anthropic",
                "role": "Engineer",
                "location": "SF",
                "linkedin_url": "",
                "linkedin_headline": "",
                "linkedin_followers": "",
                "linkedin_connections": "",
                "github_url": "",
                "github_bio": "",
                "github_stars": "",
                "github_followers": "",
                "github_repos": "",
                "github_commits_year": "",
                "x_handle": "",
                "years_experience": "8",
                "is_founder": "false",
                "is_big_tech": "true",
                "is_student": "false",
                "top_school": "true",
                "education_level": "Masters",
                "employment_category": "employed",
                "self_description": "ML engineer",
                "ai_project": "LLM toolkit",
                "looking_for_job": "false",
                "total_cv_events": "3",
                "hackathon_submissions": "2",
                "publications": "",
                "certifications": "",
                "notable_achievements": "",
            },
            {
                "name": "Bob Smith",
                "first_name": "Bob",
                "last_name": "Smith",
                "email": "bob@test.com",
                "company": "Google",
                "role": "SWE",
                "location": "NYC",
                "linkedin_url": "",
                "linkedin_headline": "",
                "linkedin_followers": "",
                "linkedin_connections": "",
                "github_url": "",
                "github_bio": "",
                "github_stars": "",
                "github_followers": "",
                "github_repos": "",
                "github_commits_year": "",
                "x_handle": "",
                "years_experience": "5",
                "is_founder": "false",
                "is_big_tech": "true",
                "is_student": "false",
                "top_school": "false",
                "education_level": "Bachelors",
                "employment_category": "employed",
                "self_description": "SWE",
                "ai_project": "",
                "looking_for_job": "false",
                "total_cv_events": "1",
                "hackathon_submissions": "0",
                "publications": "",
                "certifications": "",
                "notable_achievements": "",
            },
        ]

        scores = {"Alice Chen": 85.5, "Bob Smith": 72.3}
        swiss_records = {
            "Alice Chen": {"wins": 15, "losses": 5},
            "Bob Smith": {"wins": 10, "losses": 10},
        }
        pointwise_scores = {"Alice Chen": 0.85, "Bob Smith": 0.55}
        quality_results = {
            "Alice Chen": {"verdict": "STRONG ACCEPT", "why": "Great ML background", "specific_why": "Deep expertise"},
            "Bob Smith": {"verdict": "ACCEPT", "why": "Solid engineer"},
        }

        output_csv = tmp_path / "ranked.csv"
        result = export_ranked_csv(
            people=people,
            scores=scores,
            swiss_records=swiss_records,
            pointwise_scores=pointwise_scores,
            quality_results=quality_results,
            config=sample_config,
            output_path=output_csv,
        )

        assert result == output_csv
        assert output_csv.exists()

        # Read and verify structure
        with open(output_csv) as f:
            reader = csv.DictReader(f)
            headers = reader.fieldnames
            rows = list(reader)

        # Check required static columns
        for col in ["Rank", "Name", "Email", "Combined_Score", "Swiss_Wins",
                     "Swiss_Losses", "Pointwise_Score",
                     "Win_Rate", "Verdict"]:
            assert col in headers, f"Missing column: {col}"

        # Check dynamic event columns
        event_name = sample_config["event"]["name"].replace(" ", "_").replace("-", "_")
        assert f"Applied_To_{event_name}" in headers
        assert f"{event_name}_Status" in headers

        # Check ranking order (Alice scored higher)
        assert rows[0]["Name"] == "Alice Chen"
        assert rows[1]["Name"] == "Bob Smith"
        assert int(rows[0]["Rank"]) == 1
        assert int(rows[1]["Rank"]) == 2

    def test_export_creates_json_alongside(self, tmp_path: Path, sample_config: dict) -> None:
        """A JSON file is written alongside the CSV."""
        people = [
            {
                "name": "Alice",
                "first_name": "Alice",
                "last_name": "",
                "email": "a@t.com",
            },
        ]
        scores = {"Alice": 90.0}
        swiss_records = {"Alice": {"wins": 10, "losses": 0}}

        output_csv = tmp_path / "ranked.csv"
        export_ranked_csv(
            people=people,
            scores=scores,
            swiss_records=swiss_records,
            pointwise_scores={},
            quality_results={},
            config=sample_config,
            output_path=output_csv,
        )

        json_path = output_csv.with_suffix(".json")
        assert json_path.exists()

        data = json.loads(json_path.read_text())
        assert isinstance(data, list)
        assert len(data) == 1
        assert data[0]["Name"] == "Alice"

    def test_export_accept_waitlist_status(self, tmp_path: Path, sample_config: dict) -> None:
        """People beyond target_accepts get WAITLIST status."""
        sample_config["event"]["target_accepts"] = 1

        people = [
            {"name": "Alice", "first_name": "Alice", "last_name": "", "email": "a@t.com"},
            {"name": "Bob", "first_name": "Bob", "last_name": "", "email": "b@t.com"},
        ]
        scores = {"Alice": 90.0, "Bob": 50.0}
        swiss_records = {
            "Alice": {"wins": 10, "losses": 0},
            "Bob": {"wins": 5, "losses": 5},
        }

        output_csv = tmp_path / "ranked.csv"
        export_ranked_csv(
            people=people,
            scores=scores,
            swiss_records=swiss_records,
            pointwise_scores={},
            quality_results={},
            config=sample_config,
            output_path=output_csv,
        )

        with open(output_csv) as f:
            reader = csv.DictReader(f)
            rows = list(reader)

        event_name = sample_config["event"]["name"].replace(" ", "_").replace("-", "_")
        status_col = f"{event_name}_Status"
        assert rows[0][status_col] == "ACCEPT"
        assert rows[1][status_col] == "WAITLIST"

    def test_export_win_rate_calculation(self, tmp_path: Path, sample_config: dict) -> None:
        """Win rate is computed correctly."""
        people = [{"name": "A", "first_name": "A", "last_name": "", "email": "a@t.com"}]
        scores = {"A": 80.0}
        swiss_records = {"A": {"wins": 3, "losses": 1}}

        output_csv = tmp_path / "ranked.csv"
        export_ranked_csv(
            people=people,
            scores=scores,
            swiss_records=swiss_records,
            pointwise_scores={},
            quality_results={},
            config=sample_config,
            output_path=output_csv,
        )

        with open(output_csv) as f:
            rows = list(csv.DictReader(f))

        assert rows[0]["Win_Rate"] == "75%"
