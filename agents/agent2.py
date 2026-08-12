import re
import httpx
from urllib.parse import urlparse
from agents.llm import ask_llm_json

FOUNDER_ANALYSIS_PROMPT = """You are Agent 2 (Eshaal), a talent and profile analysis agent.

You will be given up to THREE sources of real information about a founder. Some of them may
be empty — that is expected, not an error. Your job is to extract facts ONLY from what is
actually present below. Do NOT invent employers, schools, years of experience, or achievements
that are not directly supported by the text. If a field cannot be supported by the sources,
leave it as an empty string, empty list, or a generic honest placeholder — never fabricate a
specific-sounding fact (e.g. do not invent a company name or degree).

1. LinkedIn — OAuth-verified identity (real, confirmed by LinkedIn login): {linkedin_verified}
2. LinkedIn — real bio text pasted directly by the founder (their own words, not scraped): {linkedin_bio_text}
3. GitHub — live metrics pulled from the real GitHub REST API: {github_api_data}

Additional free-text background the founder typed in: {additional_text}

Provide your analysis in STRICT JSON format matching the schema below:
{{
  "name": "Full Name (prefer the OAuth-verified name, then GitHub, then bio text)",
  "role": "Designation / Software Engineer / Founder (only if stated or clearly implied)",
  "specialization": "Specialization field (e.g. Fullstack, AI, Backend, Mobile) inferred from GitHub languages/repos or bio text",
  "skills": ["list", "of", "skills", "actually", "evidenced", "by", "github_api_data", "or", "bio_text"],
  "experience": "Junior, Intermediate, or Expert — only estimate this from real signals (repo count/age, bio text), state your best honest guess if unsure rather than inventing a reason",
  "years_of_experience": "Number of years if stated in the bio text, otherwise empty string",
  "education": "Only if explicitly mentioned in the bio text — otherwise empty string",
  "resume_summary": "Short 2-3 sentence summary built ONLY from the real sources above — no invented job history",
  "custom_details": {{
     "location": "from OAuth/GitHub only if present, otherwise empty string",
     "company": "from GitHub 'company' field or bio text only, otherwise empty string",
     "any_other_key": "any other real fact actually present in the sources"
  }}
}}
"""

INVESTOR_ANALYSIS_PROMPT = """You are Agent 2 (Eshaal), a talent and profile analysis agent.
Your task is to analyze the investor's real background information and extract EVERYTHING
that is ACTUALLY present below. Do not invent a firm name, ticket size, or track record
that isn't stated — leave a field empty or use a clearly generic placeholder instead of fabricating.
A bare LinkedIn profile URL carries no information by itself — LinkedIn cannot be scraped or
browsed by you, so only the OAuth-verified identity and pasted bio text below are real sources.

1. LinkedIn — OAuth-verified identity (real, confirmed by LinkedIn login): {linkedin_verified}
2. LinkedIn — real bio text pasted directly by the investor (their own words, not scraped): {linkedin_bio_text}

Additional background text (investment thesis, focus, etc.): {additional_text}

Provide your analysis in STRICT JSON format matching the schema below:
{{
  "name": "Full Name",
  "designation": "Current Title/Designation",
  "firm": "Name of Venture Capital / Investment Firm",
  "experience": "Junior, Intermediate, or Expert",
  "focus_sectors": ["list", "of", "sectors"],
  "min_ticket": 100000,
  "max_ticket": 1000000,
  "bio": "Short professional biography built only from real text provided",
  "custom_details": {{
     "location": "extracted if present",
     "any_other_key": "any other extracted details"
  }}
}}
"""


def fetch_github_live_insights(github_url_or_user: str) -> dict:
    """Query GitHub REST API to extract real live data from user's authentic account."""
    if not github_url_or_user:
        return {}

    # Extract clean GitHub username
    clean_user = github_url_or_user.rstrip('/')
    if '/' in clean_user:
        clean_user = clean_user.split('/')[-1]

    clean_user = clean_user.replace('@', '').strip()
    if not clean_user:
        return {}

    # Real insights object initialized with 0 defaults (NO fake mock numbers)
    insights = {
        "username": clean_user,
        "name": clean_user,
        "bio": "",
        "avatar_url": f"https://github.com/{clean_user}.png",
        "location": "",
        "company": "",
        "blog": "",
        "public_repos": 0,
        "stars_count": 0,
        "forks_count": 0,
        "followers": 0,
        "following": 0,
        "top_languages": {},
        "recent_repos": [],
        "repo_details": [],
        "verified": False,
    }

    try:
        headers = {"User-Agent": "TechFlixx-Realtime-Agent"}
        with httpx.Client(timeout=6.0, follow_redirects=True) as client:
            # 1. Fetch GitHub User Profile
            user_res = client.get(f"https://api.github.com/users/{clean_user}", headers=headers)
            if user_res.status_code == 200:
                udata = user_res.json()
                insights["verified"] = True
                insights["name"] = udata.get("name") or clean_user
                insights["bio"] = udata.get("bio") or ""
                insights["avatar_url"] = udata.get("avatar_url") or insights["avatar_url"]
                insights["location"] = udata.get("location") or ""
                insights["company"] = udata.get("company") or ""
                insights["blog"] = udata.get("blog") or ""
                insights["public_repos"] = udata.get("public_repos", 0)
                insights["followers"] = udata.get("followers", 0)
                insights["following"] = udata.get("following", 0)

            # 2. Fetch Public Repositories (Up to 30)
            repos_res = client.get(
                f"https://api.github.com/users/{clean_user}/repos?sort=updated&per_page=30",
                headers=headers
            )
            if repos_res.status_code == 200:
                repos = repos_res.json()
                total_stars = 0
                total_forks = 0
                lang_counts = {}
                repo_list = []
                recent_names = []

                for r in repos:
                    if isinstance(r, dict) and not r.get("fork", False):
                        r_name = r.get("name", "")
                        r_desc = r.get("description") or "Repository project"
                        r_lang = r.get("language")
                        r_stars = r.get("stargazers_count", 0)
                        r_forks = r.get("forks_count", 0)
                        r_url = r.get("html_url", "")

                        total_stars += r_stars
                        total_forks += r_forks

                        if r_lang:
                            lang_counts[r_lang] = lang_counts.get(r_lang, 0) + 1

                        if r_name:
                            recent_names.append(r_name)
                            repo_list.append({
                                "name": r_name,
                                "description": r_desc,
                                "language": r_lang or "Code",
                                "stars": r_stars,
                                "forks": r_forks,
                                "url": r_url,
                            })

                insights["stars_count"] = total_stars
                insights["forks_count"] = total_forks
                insights["recent_repos"] = recent_names[:8]
                insights["repo_details"] = repo_list[:8]

                # Calculate real language breakdown percentages
                if lang_counts:
                    total_lang_repos = sum(lang_counts.values())
                    insights["top_languages"] = {
                        l: round((c / total_lang_repos) * 100)
                        for l, c in sorted(lang_counts.items(), key=lambda x: x[1], reverse=True)[:5]
                    }

    except Exception as e:
        print(f"GitHub Live API Fetch Error: {e}")

    return insights


def verify_repo_ownership(username: str, repo_url: str) -> bool:
    """
    Asks GitHub directly whether `repo_url` is actually owned by `username`,
    instead of trusting whatever the founder typed. Parses owner/repo out of
    the URL and checks the real repo's owner.login — this covers forks and
    repos beyond the first 30 that fetch_github_live_insights doesn't scan.
    """
    if not username or not repo_url:
        return False

    path = urlparse(repo_url.strip()).path.strip("/")
    parts = [p for p in path.split("/") if p]
    if len(parts) < 2:
        return False
    owner, repo = parts[0], parts[1]

    try:
        headers = {"User-Agent": "TechFlixx-Realtime-Agent"}
        with httpx.Client(timeout=6.0, follow_redirects=True) as client:
            res = client.get(f"https://api.github.com/repos/{owner}/{repo}", headers=headers)
            if res.status_code != 200:
                return False
            actual_owner = res.json().get("owner", {}).get("login", "")
            return actual_owner.lower() == username.strip().lstrip("@").lower()
    except Exception as e:
        print(f"GitHub Repo Ownership Check Error: {e}")
        return False


def search_github_users(name: str, limit: int = 5) -> list:
    """
    Real GitHub Search API lookup: given a plain name (not a username/URL),
    return real candidate GitHub accounts for the user to pick from. This is
    the "give me a name and find their real GitHub" agent — it only ever
    returns accounts that actually exist on GitHub, never a guess.
    """
    query = (name or "").strip()
    if not query:
        return []

    candidates = []
    try:
        headers = {"User-Agent": "TechFlixx-Realtime-Agent"}
        with httpx.Client(timeout=6.0, follow_redirects=True) as client:
            res = client.get(
                "https://api.github.com/search/users",
                params={"q": query, "per_page": limit},
                headers=headers,
            )
            if res.status_code == 200:
                for item in res.json().get("items", [])[:limit]:
                    candidates.append({
                        "username": item.get("login", ""),
                        "profile_url": item.get("html_url", ""),
                        "avatar_url": item.get("avatar_url", ""),
                        "score": item.get("score", 0),
                    })
    except Exception as e:
        print(f"GitHub Search API Error: {e}")

    return candidates


def resolve_github_profile(name: str = "", github_hint: str = "") -> dict:
    """
    Multi-agent GitHub resolution step:
    - If a direct URL/username was given, trust it and fetch insights directly.
    - Otherwise, search GitHub by real name, and fetch insights for the best-scoring
      real match. Always returns which path was used plus the other candidates found,
      so the caller/UI can let the person confirm or pick a different match instead of
      silently attaching the wrong stranger's profile.
    """
    if github_hint and github_hint.strip():
        insights = fetch_github_live_insights(github_hint.strip())
        return {
            "resolved_by": "direct",
            "candidates": [],
            "insights": insights,
        }

    candidates = search_github_users(name)
    if not candidates:
        return {"resolved_by": "none", "candidates": [], "insights": {}}

    best = candidates[0]
    insights = fetch_github_live_insights(best["username"])
    return {
        "resolved_by": "search",
        "candidates": candidates,
        "insights": insights,
    }


def analyze_founder_profile(
    linkedin: str = "",
    github: str = "",
    additional: str = "",
    linkedin_bio_text: str = "",
    linkedin_verified: dict = None,
    github_name_hint: str = "",
) -> dict:
    """
    linkedin / github: raw URLs typed by the user (used for display + as a
      direct-fetch hint for GitHub).
    linkedin_bio_text: real text the founder pasted from their own LinkedIn
      About/Experience section — this is what Agent 2 is actually allowed to
      extract a bio from, instead of guessing from a bare URL.
    linkedin_verified: dict from the real LinkedIn OAuth (OpenID Connect)
      login — real name/email/picture confirmed by LinkedIn itself.
    github_name_hint: a plain name to search GitHub by, when no direct
      GitHub URL/username was provided.
    """
    github_resolution = resolve_github_profile(name=github_name_hint or linkedin, github_hint=github)
    github_live = github_resolution["insights"]

    prompt = FOUNDER_ANALYSIS_PROMPT.format(
        linkedin_verified=str(linkedin_verified or {}),
        linkedin_bio_text=linkedin_bio_text or "(none provided)",
        github_api_data=str(github_live),
        additional_text=additional,
    )
    result = ask_llm_json(prompt, system_prompt="You are Agent 2 (Eshaal) who parses only real supplied facts into valid JSON, never inventing details.")

    if linkedin_verified:
        result["name"] = linkedin_verified.get("name") or result.get("name")
        result["linkedin_verified"] = linkedin_verified

    result["github_insights"] = github_live
    result["github_resolution"] = {
        "resolved_by": github_resolution["resolved_by"],
        "candidates": github_resolution["candidates"],
    }
    return result


def analyze_investor_profile(
    linkedin: str,
    additional: str = "",
    linkedin_bio_text: str = "",
    linkedin_verified: dict = None,
) -> dict:
    """
    linkedin: the raw URL typed by the user — display/reference only, never a
      real data source (see module docstring above: no scraping).
    linkedin_bio_text / linkedin_verified: the two actual sources, same as
      analyze_founder_profile — pasted text or a real OAuth login.
    """
    prompt = INVESTOR_ANALYSIS_PROMPT.format(
        linkedin_verified=str(linkedin_verified or {}),
        linkedin_bio_text=linkedin_bio_text or "(none provided)",
        additional_text=additional,
    )
    result = ask_llm_json(prompt, system_prompt="You are Agent 2 (Eshaal) who parses only real supplied facts into valid JSON, never inventing details.")

    if linkedin_verified:
        result["name"] = linkedin_verified.get("name") or result.get("name")
        result["linkedin_verified"] = linkedin_verified

    return result
