"""Tests for cv_rank.profile — profile formatting for LLM prompts."""

from __future__ import annotations

from cv_rank.profile import format_profile


class TestFormatProfile:
    """Tests for format_profile()."""

    def test_full_data_all_sections_present(self) -> None:
        """A person with rich data includes all expected sections."""
        person = {
            "name": "Alice Chen",
            "title": "Senior ML Engineer",
            "company": "Anthropic",
            "current_job": "Building safe AI",
            "looking_for_job": "no",
            "self_description": "ML engineer focused on alignment",
            "ai_project": "LLM safety toolkit",
            "education": "MS Computer Science, Stanford",
            "linkedin_url": "https://linkedin.com/in/alicechen",
            "linkedin_headline": "ML @ Anthropic",
            "linkedin_bio": "Building safe AI systems for everyone",
            "linkedin_skills": ["Python", "TensorFlow", "NLP"],
            "li_follower_count": 5200,
            "li_connection_count": 1800,
            "li_is_creator": True,
            "li_is_premium": True,
            "li_country": "US",
            "is_founder": False,
            "is_decision_maker": True,
            "is_in_big_tech": True,
            "is_student": False,
            "years_experience": 8.0,
            "education_level": "Masters",
            "top_school": True,
            "employment_category": "employed",
            "notable_achievements": ["NeurIPS best paper", "Google AI award"],
            "github_url": "https://github.com/alicechen",
            "github_bio": "Building safe AI tools",
            "gh_api_stars": 340,
            "gh_api_followers": 120,
            "gh_api_repos": 45,
            "gh_api_commits_year": 890,
            "github_total_score": 4,
            "github_best_languages": ["Python", "Rust"],
            "positions_with_companies": [
                {
                    "title": "Senior ML Engineer",
                    "company_name": "Anthropic",
                    "from_date": "2022-01-01T00:00:00",
                    "to_date": "",
                    "is_current": True,
                    "description": "Working on safety",
                },
                {
                    "title": "ML Engineer",
                    "company_name": "Google",
                    "from_date": "2018-06",
                    "to_date": "2021-12",
                    "description": "NLP systems",
                },
            ],
            "education_with_schools": [
                {
                    "school_name": "Stanford University",
                    "degree": "MS",
                    "field_of_study": "Computer Science",
                    "from_date": "2016-09",
                    "to_date": "2018-06",
                    "education_level": "Masters",
                },
            ],
            "certifications": [
                {"name": "AWS ML Specialty", "authority": "Amazon"},
            ],
            "publications_detail": [
                {"name": "Safe LLM Alignment Paper", "publisher": "NeurIPS"},
            ],
            "total_cv_events": 4,
        }

        result = format_profile(person)

        # Check sections are present
        assert "BASIC INFO" in result
        assert "LINKEDIN" in result
        assert "LINKEDIN ANALYTICS" in result
        assert "GITHUB" in result
        assert "WORK HISTORY" in result
        assert "EDUCATION" in result
        assert "CERTIFICATIONS" in result
        assert "PUBLICATIONS" in result

        # Verify specific content
        assert "Alice Chen" in result
        assert "Senior ML Engineer" in result
        assert "Anthropic" in result
        assert "linkedin.com/in/alicechen" in result
        assert "github.com/alicechen" in result
        assert "Python, TensorFlow, NLP" in result
        assert "Stanford University" in result
        assert "NeurIPS" in result
        assert "AWS ML Specialty" in result
        assert "Founder: No" in result
        assert "Big Tech: Yes" in result

    def test_minimal_data_just_name(self) -> None:
        """A person with only a name still produces valid output."""
        person = {"name": "Jane Unknown"}

        result = format_profile(person)

        assert "BASIC INFO" in result
        assert "Jane Unknown" in result
        # LinkedIn section shows NOT PROVIDED
        assert "LinkedIn: NOT PROVIDED" in result
        assert "GitHub: NOT PROVIDED" in result

    def test_format_profile_with_nested_positions(self) -> None:
        """Work history from positions_with_companies is rendered correctly."""
        person = {
            "name": "Bob Martinez",
            "positions_with_companies": [
                {
                    "title": "CTO",
                    "company_name": "Startup Inc",
                    "from_date": "2020-01",
                    "to_date": "",
                    "is_current": True,
                },
                {
                    "title": "Senior Engineer",
                    "company_name": "BigCo",
                    "from_date": "2015-06",
                    "to_date": "2019-12",
                },
            ],
        }

        result = format_profile(person)

        assert "WORK HISTORY" in result
        assert "2 positions" in result
        assert "CTO @ Startup Inc" in result
        assert "Senior Engineer @ BigCo" in result
        assert "[current]" in result

    def test_format_profile_with_nested_education(self) -> None:
        """Education from education_with_schools is rendered correctly."""
        person = {
            "name": "Carol Student",
            "education_with_schools": [
                {
                    "school_name": "MIT",
                    "degree": "PhD",
                    "field_of_study": "Computer Science",
                    "from_date": "2020-09",
                    "to_date": "2025-06",
                    "education_level": "PhD",
                },
                {
                    "school_name": "UC Berkeley",
                    "degree": "BS",
                    "field_of_study": "Mathematics",
                    "from_date": "2016-08",
                    "to_date": "2020-05",
                },
            ],
        }

        result = format_profile(person)

        assert "EDUCATION" in result
        assert "2 entries" in result
        assert "MIT" in result
        assert "UC Berkeley" in result
        assert "PhD" in result
        assert "[PhD]" in result  # education_level annotation

    def test_format_profile_github_not_provided(self) -> None:
        """When github_url is absent, 'GitHub: NOT PROVIDED' is shown."""
        person = {"name": "No Github", "linkedin_url": "https://linkedin.com/in/test"}

        result = format_profile(person)

        assert "GitHub: NOT PROVIDED" in result
        assert "linkedin.com/in/test" in result

    def test_format_profile_linkedin_not_provided(self) -> None:
        """When linkedin_url is absent, 'LinkedIn: NOT PROVIDED' is shown."""
        person = {"name": "No LinkedIn", "github_url": "https://github.com/test"}

        result = format_profile(person)

        assert "LinkedIn: NOT PROVIDED" in result
        assert "github.com/test" in result

    def test_format_profile_x_url_is_not_prefixed_with_at(self) -> None:
        person = {"name": "X User", "x_handle": "https://x.com/testuser"}

        result = format_profile(person)

        assert "X/Twitter: https://x.com/testuser" in result
        assert "@https://x.com/testuser" not in result

    def test_format_profile_include_raw_csv(self) -> None:
        """Raw CSV passthrough adds extra fields to BASIC INFO."""
        person = {
            "name": "With Raw",
            "_raw_csv": {
                "name": "With Raw",
                "Custom Skill": "Expert Python developer",
                "Short": "ab",  # too short, should be skipped
            },
        }

        result = format_profile(person, include_raw_csv=True)

        assert "Custom Skill: Expert Python developer" in result
        # Short field (len <= 2) should not appear
        assert "Short:" not in result

    def test_format_profile_include_raw_csv_skips_duplicate_canonical_fields(self) -> None:
        person = {
            "name": "With Raw",
            "self_description": "Builder",
            "ai_project": "Robotics agent",
            "_raw_csv": {
                "linkedin_url": "https://linkedin.com/in/withraw",
                "github_url": "https://github.com/withraw",
                "self_description": "Builder",
                "ai_project": "Robotics agent",
                "custom_field": "Ships real code",
            },
        }

        result = format_profile(person, include_raw_csv=True)

        assert "custom_field: Ships real code" in result
        assert "linkedin_url:" not in result
        assert "github_url:" not in result
        assert result.count("Self-Description: Builder") == 1
        assert result.count("AI Project: Robotics agent") == 1

    def test_format_profile_github_stats(self) -> None:
        """GitHub numeric stats are rendered when present."""
        person = {
            "name": "Dev Person",
            "github_url": "https://github.com/devperson",
            "gh_api_stars": 500,
            "gh_api_followers": 200,
            "gh_api_repos": 30,
            "gh_api_commits_year": 1200,
        }

        result = format_profile(person)

        assert "Total Stars: 500" in result
        assert "Followers: 200" in result
        assert "Public Repos: 30" in result
        assert "Commits (last year): 1,200" in result

    def test_format_profile_repositories_section(self) -> None:
        """Top repositories are rendered when present."""
        person = {
            "name": "Repo Owner",
            "github_url": "https://github.com/repoowner",
            "top_repos_evaluated": [
                {
                    "name": "awesome-project",
                    "stars": 150,
                    "description": "A really cool project",
                    "implementation_rating": 4,
                    "difficulty_rating": 3,
                },
            ],
        }

        result = format_profile(person)

        assert "REPOSITORIES" in result
        assert "awesome-project" in result
        assert "150 stars" in result
        assert "impl=4" in result

    def test_format_profile_event_history(self) -> None:
        """Event history and QA answers are rendered when present."""
        person = {
            "name": "Event Goer",
            "total_events_applied": 5,
            "checkin_count": 3,
            "checkin_rate": 0.6,
            "event_history": [
                {"event_name": "AI Summit 2025"},
                {"event_name": "Hack the Planet"},
            ],
            "event_qa_answers": {
                "Why do you want to attend?": "I love AI and want to learn more.",
            },
        }

        result = format_profile(person)

        assert "EVENT HISTORY" in result
        assert "Events Applied: 5 | Checked In: 3 | Rate: 60%" in result
        assert "AI Summit 2025" in result
        assert "Why do you want to attend?" in result

    def test_format_profile_community_hackathons(self) -> None:
        """Community/CV events section is rendered when data exists."""
        person = {
            "name": "Community Builder",
            "total_cv_events": 7,
        }

        result = format_profile(person)

        assert "COMMUNITY & HACKATHONS" in result
        assert "CV Events Attended: 7" in result

    def test_format_profile_hackathon_submissions(self) -> None:
        """Hackathon submissions are rendered with proper formatting."""
        person = {
            "name": "Hackathon Hero",
            "hackathon_submissions": [
                {
                    "event": "AI Hackathon 2025",
                    "team": "Team Alpha",
                    "event_date": "2025-03-15",
                    "placement": "1st Place",
                },
            ],
        }

        result = format_profile(person)

        assert "HACKATHON SUBMISSIONS" in result
        assert "Team Alpha @ AI Hackathon 2025" in result
        assert "1st Place" in result

    def test_format_profile_date_trimming(self) -> None:
        """ISO timestamps are trimmed to YYYY-MM format."""
        person = {
            "name": "Date Test",
            "positions_with_companies": [
                {
                    "title": "Engineer",
                    "company_name": "Co",
                    "from_date": "2020-01-15T00:00:00Z",
                    "to_date": "2023-06-30T23:59:59Z",
                },
            ],
        }

        result = format_profile(person)

        # Dates should be trimmed to YYYY-MM
        assert "2020-01" in result
        assert "2023-06" in result
        # Full timestamps should not appear
        assert "T00:00:00" not in result

    def test_format_profile_volunteer_section(self) -> None:
        """Volunteer experience section is rendered when present."""
        person = {
            "name": "Volunteer",
            "volunteer_experience": [
                {"role": "Mentor", "organization": "Code for Good"},
            ],
        }

        result = format_profile(person)

        assert "VOLUNTEER" in result
        assert "Mentor @ Code for Good" in result
