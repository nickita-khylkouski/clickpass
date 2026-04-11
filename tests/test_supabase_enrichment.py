"""Tests for the sub-functions extracted from enrich_from_supabase().

Each test verifies that the extracted helper produces the same mutations/results
as the corresponding section in the original monolithic function.
"""

from unittest.mock import patch

from cv_rank.enrichment.supabase import (
    _enrich_linkedin,
    _resolve_org_names,
    _apply_org_names,
    _enrich_github_db,
    _enrich_events,
    enrich_from_supabase,
)


URL = "https://test.supabase.co"
KEY = "test-key"
BATCH_SIZE = 50


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _make_supa_get_side_effect(table_responses: dict):
    """Return a side_effect function that returns rows based on table name."""
    def _side_effect(url, key, table, params):
        return table_responses.get(table, [])
    return _side_effect


# ---------------------------------------------------------------------------
# _enrich_linkedin
# ---------------------------------------------------------------------------

class TestEnrichLinkedin:
    """Tests for _enrich_linkedin sub-function."""

    def test_returns_org_ids_from_positions(self):
        people = [{"linkedin_url": "https://linkedin.com/in/alice"}]
        li_map = {"alice": [0]}

        responses = {
            "linkedin": [{"username": "alice", "headline": "Engineer"}],
            "linkedin_analytics": [],
            "linkedin_position": [
                {"linkedin_username": "alice", "title": "Dev",
                 "organization_id": "org-1", "from_date": "2020",
                 "to_date": None, "current_position": True,
                 "description": "", "duration_years": 3},
            ],
            "linkedin_education": [],
            "linkedin_publication": [],
            "linkedin_certification": [],
            "linkedin_project": [],
            "linkedin_volunteer_experience": [],
        }

        with patch("cv_rank.enrichment.supabase.supa_get",
                    side_effect=_make_supa_get_side_effect(responses)):
            org_ids = _enrich_linkedin(people, URL, KEY, BATCH_SIZE, li_map)

        assert "org-1" in org_ids
        assert people[0]["linkedin_headline"] == "Engineer"
        assert people[0]["_raw_positions"][0]["title"] == "Dev"

    def test_returns_org_ids_from_education(self):
        people = [{"linkedin_url": "https://linkedin.com/in/bob"}]
        li_map = {"bob": [0]}

        responses = {
            "linkedin": [],
            "linkedin_analytics": [],
            "linkedin_position": [],
            "linkedin_education": [
                {"linkedin_username": "bob", "degree": "BS CS",
                 "organization_id": "org-school-1", "from_date": "2015",
                 "to_date": "2019", "education_level": "bachelors"},
            ],
            "linkedin_publication": [],
            "linkedin_certification": [],
            "linkedin_project": [],
            "linkedin_volunteer_experience": [],
        }

        with patch("cv_rank.enrichment.supabase.supa_get",
                    side_effect=_make_supa_get_side_effect(responses)):
            org_ids = _enrich_linkedin(people, URL, KEY, BATCH_SIZE, li_map)

        assert "org-school-1" in org_ids
        assert people[0]["_raw_education"][0]["degree"] == "BS CS"

    def test_returns_org_ids_from_volunteer(self):
        people = [{"linkedin_url": "https://linkedin.com/in/carol"}]
        li_map = {"carol": [0]}

        responses = {
            "linkedin": [],
            "linkedin_analytics": [],
            "linkedin_position": [],
            "linkedin_education": [],
            "linkedin_publication": [],
            "linkedin_certification": [],
            "linkedin_project": [],
            "linkedin_volunteer_experience": [
                {"linkedin_username": "carol", "role": "Mentor",
                 "organization_id": "org-vol-1", "from_date": "2020",
                 "to_date": "2021"},
            ],
        }

        with patch("cv_rank.enrichment.supabase.supa_get",
                    side_effect=_make_supa_get_side_effect(responses)):
            org_ids = _enrich_linkedin(people, URL, KEY, BATCH_SIZE, li_map)

        assert "org-vol-1" in org_ids
        assert people[0]["_raw_volunteer"][0]["role"] == "Mentor"

    def test_empty_li_map_returns_empty_set(self):
        people = [{"name": "nobody"}]
        li_map = {}

        with patch("cv_rank.enrichment.supabase.supa_get") as mock_supa:
            org_ids = _enrich_linkedin(people, URL, KEY, BATCH_SIZE, li_map)

        assert org_ids == set()
        mock_supa.assert_not_called()

    def test_enriches_profile_fields(self):
        people = [{"linkedin_url": "https://linkedin.com/in/dave"}]
        li_map = {"dave": [0]}

        responses = {
            "linkedin": [{
                "username": "dave",
                "headline": "CTO",
                "summary": "Leader",
                "skills": ["python", "go"],
                "follower_count": 500,
                "connection_count": 300,
                "country": "US",
                "is_creator": True,
                "is_premium": False,
                "awards": ["Top Voice"],
                "patents": None,
            }],
            "linkedin_analytics": [{
                "linkedin_username": "dave",
                "is_founder": True,
                "is_decision_maker": True,
                "is_in_big_tech": False,
                "is_student": False,
                "years_experience": 12,
                "education_level": "masters",
                "top_school": True,
                "technical_school": False,
                "employment_category": "executive",
                "notable_skills": ["leadership"],
                "notable_achievements": ["built team"],
            }],
            "linkedin_position": [],
            "linkedin_education": [],
            "linkedin_publication": [],
            "linkedin_certification": [],
            "linkedin_project": [],
            "linkedin_volunteer_experience": [],
        }

        with patch("cv_rank.enrichment.supabase.supa_get",
                    side_effect=_make_supa_get_side_effect(responses)):
            _enrich_linkedin(people, URL, KEY, BATCH_SIZE, li_map)

        p = people[0]
        assert p["linkedin_headline"] == "CTO"
        assert p["linkedin_bio"] == "Leader"
        assert p["linkedin_skills"] == ["python", "go"]
        assert p["li_follower_count"] == 500
        assert p["li_country"] == "US"
        assert p["is_founder"] is True
        assert p["years_experience"] == 12
        assert p["notable_skills_linkedin"] == ["leadership"]

    def test_publications_certifications_projects(self):
        people = [{"linkedin_url": "https://linkedin.com/in/eve"}]
        li_map = {"eve": [0]}

        responses = {
            "linkedin": [],
            "linkedin_analytics": [],
            "linkedin_position": [],
            "linkedin_education": [],
            "linkedin_publication": [
                {"linkedin_username": "eve", "name": "Paper1",
                 "description": "desc", "publisher": "ACM",
                 "published_date": "2022"},
            ],
            "linkedin_certification": [
                {"linkedin_username": "eve", "name": "AWS SA",
                 "authority": "Amazon", "issued_date": "2023"},
            ],
            "linkedin_project": [
                {"linkedin_username": "eve", "title": "Project X",
                 "description": "cool thing", "from_date": "2021",
                 "to_date": "2022"},
            ],
            "linkedin_volunteer_experience": [],
        }

        with patch("cv_rank.enrichment.supabase.supa_get",
                    side_effect=_make_supa_get_side_effect(responses)):
            _enrich_linkedin(people, URL, KEY, BATCH_SIZE, li_map)

        p = people[0]
        assert len(p["publications"]) == 1
        assert p["publications"][0]["name"] == "Paper1"
        assert len(p["certifications"]) == 1
        assert p["certifications"][0]["name"] == "AWS SA"
        assert len(p["linkedin_projects"]) == 1
        assert p["linkedin_projects"][0]["title"] == "Project X"

    def test_multiple_people_same_username(self):
        """Two people entries sharing the same LinkedIn username."""
        people = [
            {"linkedin_url": "https://linkedin.com/in/frank"},
            {"linkedin_url": "https://linkedin.com/in/frank"},
        ]
        li_map = {"frank": [0, 1]}

        responses = {
            "linkedin": [{"username": "frank", "headline": "Dev"}],
            "linkedin_analytics": [],
            "linkedin_position": [],
            "linkedin_education": [],
            "linkedin_publication": [],
            "linkedin_certification": [],
            "linkedin_project": [],
            "linkedin_volunteer_experience": [],
        }

        with patch("cv_rank.enrichment.supabase.supa_get",
                    side_effect=_make_supa_get_side_effect(responses)):
            _enrich_linkedin(people, URL, KEY, BATCH_SIZE, li_map)

        assert people[0]["linkedin_headline"] == "Dev"
        assert people[1]["linkedin_headline"] == "Dev"


# ---------------------------------------------------------------------------
# _resolve_org_names
# ---------------------------------------------------------------------------

class TestResolveOrgNames:
    """Tests for _resolve_org_names sub-function."""

    def test_resolves_org_ids_to_names(self):
        responses = {
            "linkedin_organization": [
                {"id": "org-1", "name": "Acme Corp", "industry": "Tech"},
                {"id": "org-2", "name": "MIT", "industry": "Education"},
            ],
        }

        with patch("cv_rank.enrichment.supabase.supa_get",
                    side_effect=_make_supa_get_side_effect(responses)):
            result = _resolve_org_names({"org-1", "org-2"}, URL, KEY, BATCH_SIZE)

        assert result == {"org-1": "Acme Corp", "org-2": "MIT"}

    def test_empty_org_ids_returns_empty_dict(self):
        with patch("cv_rank.enrichment.supabase.supa_get") as mock_supa:
            result = _resolve_org_names(set(), URL, KEY, BATCH_SIZE)

        assert result == {}
        mock_supa.assert_not_called()

    def test_missing_name_defaults_to_question_mark(self):
        responses = {
            "linkedin_organization": [
                {"id": "org-x", "name": None, "industry": "Unknown"},
            ],
        }

        with patch("cv_rank.enrichment.supabase.supa_get",
                    side_effect=_make_supa_get_side_effect(responses)):
            result = _resolve_org_names({"org-x"}, URL, KEY, BATCH_SIZE)

        assert result["org-x"] == "?"


# ---------------------------------------------------------------------------
# _apply_org_names
# ---------------------------------------------------------------------------

class TestApplyOrgNames:
    """Tests for _apply_org_names sub-function."""

    def test_maps_positions_to_companies(self):
        people = [{
            "_raw_positions": [
                {"title": "Engineer", "organization_id": "org-1",
                 "description": "wrote code", "from_date": "2020",
                 "to_date": "2023", "current_position": False},
            ],
        }]
        org_names = {"org-1": "Acme Corp"}

        _apply_org_names(people, org_names)

        assert "_raw_positions" not in people[0]
        pos = people[0]["positions_with_companies"]
        assert len(pos) == 1
        assert pos[0]["company_name"] == "Acme Corp"
        assert pos[0]["title"] == "Engineer"

    def test_maps_education_to_schools(self):
        people = [{
            "_raw_education": [
                {"degree": "BS CS", "organization_id": "org-school",
                 "education_level": "bachelors", "from_date": "2015",
                 "to_date": "2019"},
            ],
        }]
        org_names = {"org-school": "MIT"}

        _apply_org_names(people, org_names)

        assert "_raw_education" not in people[0]
        edu = people[0]["education_with_schools"]
        assert len(edu) == 1
        assert edu[0]["school_name"] == "MIT"
        assert edu[0]["degree"] == "BS CS"

    def test_maps_volunteer_to_orgs(self):
        people = [{
            "_raw_volunteer": [
                {"role": "Mentor", "organization_id": "org-vol"},
            ],
        }]
        org_names = {"org-vol": "Code.org"}

        _apply_org_names(people, org_names)

        assert "_raw_volunteer" not in people[0]
        vol = people[0]["volunteer_experience"]
        assert len(vol) == 1
        assert vol[0]["organization"] == "Code.org"
        assert vol[0]["role"] == "Mentor"

    def test_missing_org_id_gives_empty_string(self):
        people = [{
            "_raw_positions": [
                {"title": "Intern", "organization_id": "unknown-org",
                 "description": "", "from_date": "", "to_date": "",
                 "current_position": False},
            ],
        }]
        org_names = {}  # org not resolved

        _apply_org_names(people, org_names)

        assert people[0]["positions_with_companies"][0]["company_name"] == ""

    def test_no_raw_fields_is_noop(self):
        people = [{"name": "Alice"}]
        _apply_org_names(people, {})
        assert "positions_with_companies" not in people[0]
        assert "education_with_schools" not in people[0]
        assert "volunteer_experience" not in people[0]


# ---------------------------------------------------------------------------
# _enrich_github_db
# ---------------------------------------------------------------------------

class TestEnrichGithubDb:
    """Tests for _enrich_github_db sub-function."""

    def test_enriches_github_profile(self):
        people = [{"github_url": "https://github.com/alice"}]
        gh_map = {"alice": [0]}

        responses = {
            "github": [{"username": "alice", "bio": "Hacker",
                        "external_contributions": 5}],
            "github_analytics": [],
            "github_repository": [],
        }

        with patch("cv_rank.enrichment.supabase.supa_get",
                    side_effect=_make_supa_get_side_effect(responses)):
            _enrich_github_db(people, URL, KEY, BATCH_SIZE, gh_map)

        assert people[0]["github_bio"] == "Hacker"
        assert people[0]["github_external_contributions"] == 5

    def test_enriches_github_analytics(self):
        people = [{"github_url": "https://github.com/bob"}]
        gh_map = {"bob": [0]}

        responses = {
            "github": [],
            "github_analytics": [{
                "github_username": "bob",
                "total_score": 85,
                "implementation_score": 70,
                "difficulty_score": 60,
                "language_control_score": 80,
                "best_languages": ["python", "rust"],
                "notable_skills": ["ML"],
                "average_commits_per_day": 3.5,
                "num_oss_repos_contributed_to": 12,
                "total_code_by_language": {"python": 50000},
            }],
            "github_repository": [],
        }

        with patch("cv_rank.enrichment.supabase.supa_get",
                    side_effect=_make_supa_get_side_effect(responses)):
            _enrich_github_db(people, URL, KEY, BATCH_SIZE, gh_map)

        p = people[0]
        assert p["github_total_score"] == 85
        assert p["github_best_languages"] == ["python", "rust"]
        assert p["github_code_by_language"] == {"python": 50000}

    def test_enriches_github_repos(self):
        people = [{"github_url": "https://github.com/carol"}]
        gh_map = {"carol": [0]}

        responses = {
            "github": [],
            "github_analytics": [],
            "github_repository": [
                {"github_username": "carol", "name": "awesome-lib",
                 "stargazer_count": 100, "fork_count": 20,
                 "commit_count": 50, "is_fork": False,
                 "description": "An awesome lib",
                 "implementation_rating": 4, "difficulty_rating": 3,
                 "skills": ["python"], "tools": ["pytest"],
                 "topics": ["ml"], "languages": {"Python": 90}},
            ],
        }

        with patch("cv_rank.enrichment.supabase.supa_get",
                    side_effect=_make_supa_get_side_effect(responses)):
            _enrich_github_db(people, URL, KEY, BATCH_SIZE, gh_map)

        p = people[0]
        assert len(p["gh_api_top_repos"]) == 1
        assert p["gh_api_top_repos"][0]["name"] == "awesome-lib"
        assert p["gh_api_top_repos"][0]["stars"] == 100
        assert p["gh_api_stars"] == 100
        assert p["gh_api_repos"] == 1

    def test_empty_gh_map_is_noop(self):
        people = [{"name": "nobody"}]
        gh_map = {}

        with patch("cv_rank.enrichment.supabase.supa_get") as mock_supa:
            _enrich_github_db(people, URL, KEY, BATCH_SIZE, gh_map)

        mock_supa.assert_not_called()


# ---------------------------------------------------------------------------
# _enrich_events
# ---------------------------------------------------------------------------

class TestEnrichEvents:
    """Tests for _enrich_events sub-function."""

    def test_enriches_event_history_and_qa(self):
        people = [{"email": "alice@example.com"}]
        email_map = {"alice@example.com": [0]}

        def _side_effect(url, key, table, params):
            if table == "event_applicants":
                return [
                    {"email": "alice@example.com", "event_name": "Hackathon",
                     "status": "approved",
                     "company": "Acme", "job_title": "Dev",
                     "event_specific_data": {
                         "Why do you want to attend?": "I love hacking!",
                         "email": "skip-this",
                     }},
                ]
            return []

        with patch("cv_rank.enrichment.supabase.supa_get",
                    side_effect=_side_effect):
            _enrich_events(people, URL, KEY, BATCH_SIZE, email_map)

        p = people[0]
        assert len(p["event_history"]) == 1
        assert p["event_history"][0]["event_name"] == "Hackathon"
        assert "Why do you want to attend?" in p["event_qa_answers"]
        assert "email" not in p["event_qa_answers"]  # internal field skipped
        assert p["total_events_applied"] == 1
        assert p["events_approved"] == 1
        assert p["company"] == "Acme"
        assert p["title"] == "Dev"

    def test_does_not_overwrite_existing_company(self):
        people = [{"email": "bob@example.com", "company": "Existing Co",
                    "title": "Existing Title"}]
        email_map = {"bob@example.com": [0]}

        def _side_effect(url, key, table, params):
            if table == "event_applicants":
                return [{"email": "bob@example.com", "event_name": "E1",
                         "status": "approved",
                         "company": "New Co", "job_title": "New Title",
                         "event_specific_data": {}}]
            return []

        with patch("cv_rank.enrichment.supabase.supa_get",
                    side_effect=_side_effect):
            _enrich_events(people, URL, KEY, BATCH_SIZE, email_map)

        assert people[0]["company"] == "Existing Co"
        assert people[0]["title"] == "Existing Title"

    def test_empty_email_map_is_noop(self):
        people = [{"name": "nobody"}]
        email_map = {}

        with patch("cv_rank.enrichment.supabase.supa_get") as mock_supa:
            _enrich_events(people, URL, KEY, BATCH_SIZE, email_map)

        mock_supa.assert_not_called()

    def test_skips_short_qa_answers(self):
        """Answers <= 5 chars should be filtered out."""
        people = [{"email": "c@example.com"}]
        email_map = {"c@example.com": [0]}

        def _side_effect(url, key, table, params):
            if table == "event_applicants":
                return [{
                    "email": "c@example.com", "event_name": "E1",
                    "status": "pending",
                    "event_specific_data": {
                        "Short?": "No",  # len 2, too short
                        "Long?": "Yes I do want to!",  # long enough
                    },
                }]
            return []

        with patch("cv_rank.enrichment.supabase.supa_get",
                    side_effect=_side_effect):
            _enrich_events(people, URL, KEY, BATCH_SIZE, email_map)

        p = people[0]
        assert "Short?" not in p.get("event_qa_answers", {})
        assert "Long?" in p["event_qa_answers"]


# ---------------------------------------------------------------------------
# enrich_from_supabase (orchestrator) integration
# ---------------------------------------------------------------------------

class TestEnrichFromSupabaseOrchestrator:
    """Verify the orchestrator still calls all phases correctly."""

    def test_returns_people_unchanged_when_no_credentials(self):
        people = [{"name": "test"}]
        config = {"enrichment": {"supabase": {"url": "", "key": ""}}}
        result = enrich_from_supabase(people, config)
        assert result is people

    def test_calls_all_phases(self):
        """Verify all sub-functions are called in sequence."""
        people = [{"email": "a@b.com", "linkedin_url": "", "github_url": ""}]
        config = {"enrichment": {"supabase": {
            "url": "https://test.supabase.co",
            "key": "key",
            "batch_size": 50,
        }}}

        with patch("cv_rank.enrichment.supabase._enrich_linkedin", return_value=set()) as m_li, \
             patch("cv_rank.enrichment.supabase._resolve_org_names", return_value={}) as m_org, \
             patch("cv_rank.enrichment.supabase._apply_org_names") as m_apply, \
             patch("cv_rank.enrichment.supabase._enrich_github_db") as m_gh, \
             patch("cv_rank.enrichment.supabase._enrich_events") as m_ev, \
             patch("cv_rank.enrichment.supabase._enrich_x_handles") as m_x, \
             patch("cv_rank.enrichment.supabase._print_enrichment_summary") as m_summary:

            result = enrich_from_supabase(people, config)

        m_li.assert_called_once()
        m_org.assert_called_once()
        m_apply.assert_called_once()
        m_gh.assert_called_once()
        m_ev.assert_called_once()
        m_x.assert_called_once()
        m_summary.assert_called_once()
        assert result is people
