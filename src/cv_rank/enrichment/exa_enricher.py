"""
Exa enrichment: enrich profiles using Exa API web scraping.

Adds Exa-scraped data to people dicts, similar to supabase.py pattern.
Can optionally write back to Supabase for persistence.
"""

import logging

logger = logging.getLogger("cv_rank")

try:
    from exa_py import Exa
    EXA_AVAILABLE = True
except ImportError:
    EXA_AVAILABLE = False

genai = None
try:
    import google.generativeai as genai
    GEMINI_AVAILABLE = True
except ImportError:
    GEMINI_AVAILABLE = False


def enrich_from_exa(people: list[dict], config: dict) -> list[dict]:
    """Enrich people from Exa API web scraping.

    Scrapes LinkedIn, GitHub profile + activity, DevPost, publications, and tech news for:
    - Work experience (internships, companies, roles)
    - GitHub deep dive (profile stats, contributions, commits, PRs, issues, stars, followers)
    - Publications (venues, counts)
    - Hackathons (wins, prizes)
    - Tech news (conference talks, media coverage, launches)
    - Special programs (Google Summer of Code, fellowships)

    Parameters
    ----------
    people:
        List of person dicts (mutated in place and returned).
    config:
        Full configuration dict. Exa config is in config["enrichment"]["exa"]

    Returns
    -------
    list[dict]
        The same *people* list, enriched with exa_* fields.
    """
    if not EXA_AVAILABLE:
        logger.warning("Exa library not installed -- skipping Exa enrichment")
        return people

    exa_cfg = config.get("enrichment", {}).get("exa", {})
    if not exa_cfg.get("enabled", False):
        logger.info("Exa enrichment disabled in config")
        return people

    exa_api_key = exa_cfg.get("api_key")
    if not exa_api_key:
        logger.warning("EXA_API_KEY not configured -- skipping Exa enrichment")
        return people

    logger.info("ENRICHING FROM EXA API")

    exa = Exa(api_key=exa_api_key)

    # Configure Gemini if available
    gemini_key = exa_cfg.get("gemini_api_key")
    if GEMINI_AVAILABLE and gemini_key:
        genai.configure(api_key=gemini_key)
    else:
        logger.warning("Gemini not available -- will store raw data only")

    total_cost = 0.0
    enriched_count = 0

    for i, person in enumerate(people, 1):
        name = person.get("name", "")
        email = person.get("email", "")

        if not name or not email:
            logger.debug(f"Skipping person {i}: missing name or email")
            continue

        logger.info(f"  [{i}/{len(people)}] Exa enriching: {name}")

        try:
            # Collect data from Exa using Supabase context
            data = _collect_exa_data(exa, name, email, exa_cfg, person)
            total_cost += data["cost"]

            # Store raw data
            person["exa_linkedin_chars"] = data["sources"].get("linkedin", {}).get("chars", 0)
            person["exa_github_chars"] = data["sources"].get("github", {}).get("chars", 0)
            person["exa_devpost_chars"] = data["sources"].get("devpost", {}).get("chars", 0)
            person["exa_publications_count"] = data["sources"].get("publications", {}).get("urls", 0)
            person["exa_news_chars"] = data["sources"].get("news", {}).get("chars", 0)

            # Extract resume using Gemini if available with validation
            if GEMINI_AVAILABLE and gemini_key:
                resume_data = _extract_exa_resume(data, exa_cfg, person)
                total_cost += resume_data["cost"]

                person["exa_resume"] = resume_data["resume"]
                person["exa_enrichment_cost"] = data["cost"] + resume_data["cost"]

                # Parse resume into structured fields (best effort)
                _parse_resume_to_fields(person, resume_data["resume"])
            else:
                # Store raw highlights
                person["exa_linkedin_text"] = data["sources"].get("linkedin", {}).get("full_text", "")[:5000]
                person["exa_github_highlights"] = data["sources"].get("github", {}).get("highlights", [])[:10]
                person["exa_devpost_highlights"] = data["sources"].get("devpost", {}).get("highlights", [])[:10]

            enriched_count += 1

        except Exception as e:
            logger.error(f"  Error enriching {name}: {e}")
            person["exa_error"] = str(e)

    avg_cost = total_cost / enriched_count if enriched_count > 0 else 0
    logger.info(f"  Exa enrichment: {enriched_count}/{len(people)} people, ${total_cost:.2f} total (${avg_cost:.4f}/person)")

    return people


def _collect_exa_data(exa: "Exa", name: str, email: str, config: dict, person: dict) -> dict:
    """Collect data from Exa for one person using Supabase context."""
    data = {"name": name, "email": email, "cost": 0.0, "sources": {}}
    username = email.split('@')[0]

    linkedin_urls = config.get("linkedin_urls", 20)
    devpost_urls = config.get("devpost_urls", 30)
    pub_urls = config.get("publication_urls", 30)
    web_urls = config.get("web_urls", 15)

    # Get Supabase context
    linkedin_url = person.get("linkedin_url")
    github_url = person.get("github_url")
    company = person.get("company", "")
    school = ""
    if person.get("education_with_schools"):
        school = person["education_with_schools"][0].get("school_name", "")

    # 1. LINKEDIN - Use exact URL if available, else search with context
    try:
        if linkedin_url:
            # Direct scraping (no ambiguity!)
            logger.debug(f"Using exact LinkedIn URL: {linkedin_url}")
            content = exa.get_contents([linkedin_url], text=True)
            data["cost"] += 0.003

            if content.results and hasattr(content.results[0], 'text') and content.results[0].text:
                texts = [content.results[0].text]
                data["sources"]["linkedin"] = {
                    "urls": 1,
                    "chars": len(texts[0]),
                    "full_text": texts[0]
                }
            else:
                data["sources"]["linkedin"] = {"urls": 0, "chars": 0, "full_text": ""}
        else:
            # Fallback to search with context
            search_query = f'"{name}"'
            if company:
                search_query += f' {company}'
            if school:
                search_query += f' {school}'
            search_query += ' site:linkedin.com'

            logger.debug(f"Searching LinkedIn: {search_query}")
            search = exa.search(search_query, type="instant", num_results=min(5, linkedin_urls))
            data["cost"] += 0.003

            urls = [r.url for r in search.results]
            if urls:
                content = exa.get_contents(urls, text=True)
                data["cost"] += 0.003 * len(urls)

                texts = [r.text for r in content.results if hasattr(r, 'text') and r.text]
                data["sources"]["linkedin"] = {
                    "urls": len(urls),
                    "chars": sum(len(t) for t in texts),
                    "full_text": " ".join(texts)
                }
            else:
                data["sources"]["linkedin"] = {"urls": 0, "chars": 0, "full_text": ""}
    except Exception as e:
        logger.debug(f"LinkedIn scrape failed: {e}")
        data["sources"]["linkedin"] = {"urls": 0, "chars": 0, "full_text": ""}

    # 2. GITHUB - Deep dive: profile + activity + external mentions
    try:
        github_username = None
        if github_url:
            # Extract username from URL
            github_username = github_url.rstrip('/').split('/')[-1]
            logger.debug(f"Using exact GitHub username: {github_username}")
        else:
            github_username = username

        all_highlights = []

        # 2a. Scrape actual GitHub profile page
        if github_username:
            try:
                profile_url = f"https://github.com/{github_username}"
                profile_content = exa.get_contents(
                    [profile_url],
                    contents={
                        "highlights": {
                            "query": "repositories OR contributions OR followers OR stars OR commits OR pull requests OR issues OR discussions",
                            "num_sentences": 20,
                            "highlights_per_url": 15
                        }
                    }
                )
                data["cost"] += 0.003

                if profile_content.results and hasattr(profile_content.results[0], 'highlights'):
                    all_highlights.extend(profile_content.results[0].highlights or [])
            except Exception as e:
                logger.debug(f"GitHub profile scrape failed: {e}")

        # 2b. Search for GitHub activity mentions (blog posts, articles about their projects)
        search = exa.search(
            f'"{github_username}" github (repositories OR repos OR projects OR contributions)',
            type="instant",
            num_results=8,
            exclude_domains=["github.com"],  # External mentions only
            contents={
                "highlights": {
                    "query": "github OR repositories OR open source OR contributions OR commits OR project",
                    "num_sentences": 20,
                    "highlights_per_url": 8
                }
            }
        )
        data["cost"] += 0.012

        for r in search.results:
            if hasattr(r, 'highlights') and r.highlights:
                all_highlights.extend(r.highlights)

        data["sources"]["github"] = {
            "urls": len(search.results) + 1,  # +1 for profile
            "highlights": all_highlights,
            "chars": sum(len(h) for h in all_highlights)
        }
    except Exception as e:
        logger.debug(f"GitHub scrape failed: {e}")
        data["sources"]["github"] = {"urls": 0, "highlights": [], "chars": 0}

    # 3. DEVPOST - Hackathon highlights
    try:
        search = exa.search(
            f'"{email}" OR "{username}" site:devpost.com',
            type="instant",
            num_results=devpost_urls,
            contents={
                "highlights": {
                    "query": "winner OR prize OR award OR hackathon OR $",
                    "num_sentences": 50,
                    "highlights_per_url": 20
                }
            }
        )
        data["cost"] += 0.008

        highlights = []
        for r in search.results:
            if hasattr(r, 'highlights') and r.highlights:
                highlights.extend(r.highlights)

        data["sources"]["devpost"] = {
            "urls": len(search.results),
            "highlights": highlights,
            "chars": sum(len(h) for h in highlights)
        }
    except Exception as e:
        logger.debug(f"DevPost scrape failed: {e}")
        data["sources"]["devpost"] = {"urls": 0, "highlights": [], "chars": 0}

    # 4. WEB - Awards, fellowships, special programs (generic search)
    try:
        search = exa.search(
            f'"{name}" (winner OR award OR fellowship OR scholar OR program)',
            type="instant",
            num_results=web_urls,
            contents={
                "highlights": {
                    "query": "winner OR prize OR fellowship OR award OR scholar OR program",
                    "num_sentences": 15,
                    "highlights_per_url": 5
                }
            }
        )
        data["cost"] += 0.008

        highlights = []
        for r in search.results:
            if hasattr(r, 'highlights') and r.highlights:
                highlights.extend(r.highlights)

        data["sources"]["web"] = {
            "urls": len(search.results),
            "highlights": highlights,
            "chars": sum(len(h) for h in highlights)
        }
    except Exception as e:
        logger.debug(f"Web scrape failed: {e}")
        data["sources"]["web"] = {"urls": 0, "highlights": [], "chars": 0}

    # 5. TECH NEWS - Media mentions, conference talks
    try:
        news_search = exa.search(
            f'"{name}" (featured OR speaker OR launched OR techcrunch OR hackernews)',
            type="instant",
            num_results=5,
            exclude_domains=["linkedin.com", "github.com", "twitter.com"],
            contents={
                "highlights": {
                    "query": "TechCrunch OR conference OR speaker OR featured OR launched OR YCombinator OR HackerNews",
                    "num_sentences": 8,
                    "highlights_per_url": 3
                }
            }
        )
        data["cost"] += 0.008

        news_highlights = []
        for r in news_search.results:
            if hasattr(r, 'highlights') and r.highlights:
                news_highlights.extend(r.highlights)

        data["sources"]["news"] = {
            "urls": len(news_search.results),
            "highlights": news_highlights,
            "chars": sum(len(h) for h in news_highlights)
        }
    except Exception as e:
        logger.debug(f"News scrape failed: {e}")
        data["sources"]["news"] = {"urls": 0, "highlights": [], "chars": 0}

    # 6. PUBLICATIONS - Summaries + venue highlights, use school for better disambiguation
    try:
        search_query = f'"{name}" author'
        if school:
            search_query += f' {school}'

        search = exa.search(
            search_query,
            type="auto",
            category="research paper",
            num_results=pub_urls,
            contents={
                "summary": True,
                "highlights": {
                    "query": "JAMA OR NeurIPS OR ICML OR Nature OR conference",
                    "num_sentences": 5,
                    "highlights_per_url": 3
                }
            }
        )
        data["cost"] += 0.015

        summaries = []
        highlights = []
        for r in search.results:
            if hasattr(r, 'summary') and r.summary:
                summaries.append(r.summary)
            if hasattr(r, 'highlights') and r.highlights:
                highlights.extend(r.highlights)

        data["sources"]["publications"] = {
            "urls": len(search.results),
            "summaries": summaries,
            "highlights": highlights,
            "chars": sum(len(s) for s in summaries) + sum(len(h) for h in highlights)
        }
    except Exception as e:
        logger.debug(f"Publications scrape failed: {e}")
        data["sources"]["publications"] = {"urls": 0, "summaries": [], "highlights": [], "chars": 0}

    return data


def _extract_exa_resume(data: dict, config: dict, person: dict) -> dict:
    """Extract structured resume from Exa data using Gemini with Supabase validation."""
    name = data['name']
    email = data['email']

    # Get known facts from Supabase for validation
    known_company = person.get("company", "")
    known_headline = person.get("linkedin_headline", "")
    known_school = ""
    if person.get("education_with_schools"):
        known_school = person["education_with_schools"][0].get("school_name", "")

    # Build context
    ctx = f"# {name} ({email})\n\n"

    # Add KNOWN FACTS section if we have any
    if known_company or known_headline or known_school:
        ctx += "## KNOWN FACTS (from our database - use for validation):\n"
        if known_company:
            ctx += f"- Current Company: {known_company}\n"
        if known_headline:
            ctx += f"- LinkedIn Headline: {known_headline}\n"
        if known_school:
            ctx += f"- School: {known_school}\n"
        ctx += "\n"

    if "linkedin" in data["sources"]:
        li = data["sources"]["linkedin"]
        ctx += f"## LINKEDIN ({li['chars']:,} chars)\n{li.get('full_text', '')}\n\n"

    if "github" in data["sources"]:
        gh = data["sources"]["github"]
        ctx += "## GITHUB\n" + "\n".join(gh.get('highlights', [])) + "\n\n"

    if "devpost" in data["sources"]:
        dp = data["sources"]["devpost"]
        ctx += "## HACKATHONS\n" + "\n".join(dp.get('highlights', [])) + "\n\n"

    if "web" in data["sources"]:
        web = data["sources"]["web"]
        ctx += "## WEB\n" + "\n".join(web.get('highlights', [])) + "\n\n"

    if "publications" in data["sources"]:
        pub = data["sources"]["publications"]
        ctx += "## PUBLICATIONS\n" + "\n".join(pub.get('summaries', [])) + "\n\n"
        if pub.get('highlights'):
            ctx += "\n".join(pub.get('highlights', [])) + "\n\n"

    if "news" in data["sources"]:
        news = data["sources"]["news"]
        if news.get('highlights'):
            ctx += "## TECH NEWS (Media Coverage)\n" + "\n".join(news.get('highlights', [])) + "\n\n"

    prompt = f"""Extract a focused resume for {name} ({email}).

CRITICAL VALIDATION:
- If scraped data mentions the known company/headline/school, this is the CORRECT person
- If scraped data conflicts with known facts, FLAG IT with "⚠️ MISMATCH"
- If you find MULTIPLE people, identify which matches our known facts
- If data is ambiguous, say "⚠️ AMBIGUOUS" not "No data found"

EXTRACT:
1. ALL internships/jobs (Amazon, Google, Meta, Microsoft, etc.)
2. GitHub activity (repo count, contributions, commits, PRs, issues, stars, followers)
3. Publications (count + top venues like JAMA, NeurIPS, ICML, Nature)
4. Special programs (Google Summer of Code, fellowships, scholars)
5. Hackathons (total count, wins)
6. Tech news mentions (conference talks, media coverage, launches)

FORMAT (dense single paragraph):
[ALL internships]; [Special programs]; [University] research. X publications incl. [Venues (YEAR)]; X GitHub repos/activity; X hackathons, Y wins. [Media: coverage/talks].

EXTRACT EVERYTHING found. If uncertain, prefix with ⚠️"""

    try:
        model = genai.GenerativeModel('gemini-2.5-flash')

        response = model.generate_content(
            f"{prompt}\n\n{ctx}",
            generation_config={'temperature': 0.0, 'max_output_tokens': 4000}
        )

        resume = response.text
        input_chars = len(ctx) + len(prompt)
        output_chars = len(resume)
        cost = (input_chars / 4 / 1_000_000) * 0.075 + (output_chars / 4 / 1_000_000) * 0.30

        return {"resume": resume, "cost": cost}

    except Exception as e:
        logger.error(f"Gemini extraction failed: {e}")
        return {"resume": f"Error: {e}", "cost": 0}


def _parse_resume_to_fields(person: dict, resume: str) -> None:
    """Best-effort parsing of resume text into structured fields.

    Adds exa_* prefixed fields to avoid conflicts with Supabase data.
    """
    # Extract internships (look for "Intern" keyword)
    if "Intern" in resume or "intern" in resume:
        # Simple extraction: find company names before "Intern"
        import re
        internships = re.findall(r'([A-Z][a-zA-Z]+)\s+(?:Software Engineer |ML |AI )?Intern', resume)
        if internships:
            person["exa_internships"] = list(set(internships))  # Dedupe

    # Extract publication count
    import re
    pub_match = re.search(r'(\d+)\s+publications?', resume, re.IGNORECASE)
    if pub_match:
        person["exa_publication_count"] = int(pub_match.group(1))

    # Extract GitHub repo count
    repo_match = re.search(r'(\d+)\s+GitHub\s+repos?', resume, re.IGNORECASE)
    if repo_match:
        person["exa_github_repos"] = int(repo_match.group(1))

    # Extract hackathon stats
    hack_match = re.search(r'(\d+)\s+hackathons?,\s+(\d+)\s+wins?', resume, re.IGNORECASE)
    if hack_match:
        person["exa_hackathons_total"] = int(hack_match.group(1))
        person["exa_hackathons_wins"] = int(hack_match.group(2))

    # Check for fellowships/special programs (generic detection)
    fellowship_keywords = [
        "fellowship", "scholar", "Google Summer of Code", "GSoC",
        "research fellow", "presidential", "fulbright", "rhodes"
    ]
    if any(keyword.lower() in resume.lower() for keyword in fellowship_keywords):
        person["exa_has_fellowship"] = True
