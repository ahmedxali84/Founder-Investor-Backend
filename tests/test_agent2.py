"""
Regression coverage for agent2.py's deterministic logic — GitHub API result
aggregation, repo-ownership verification, and profile resolution branching.
This is where agent2's real value is: the LLM-facing prompt formatting isn't
retested here beyond one focused case (analyze_founder_profile's
linkedin_verified override), since the rest of that path is just "format a
string and call ask_llm_json," already exercised by every other agent's
tests.
"""
import agents.agent2 as agent2


class _FakeResponse:
    def __init__(self, status_code, json_data=None):
        self.status_code = status_code
        self._json = json_data or {}

    def json(self):
        return self._json


class _FakeHttpxClient:
    """Maps a URL (query string ignored) to a canned _FakeResponse."""
    def __init__(self, responses):
        self._responses = responses

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def get(self, url, headers=None, params=None):
        base = url.split("?")[0]
        return self._responses.get(base, _FakeResponse(404, {}))


def _patch_client(monkeypatch, responses):
    monkeypatch.setattr(agent2.httpx, "Client", lambda *a, **kw: _FakeHttpxClient(responses))


# ---------------------------------------------------------------------------
# fetch_github_live_insights
# ---------------------------------------------------------------------------

def test_fetch_github_live_insights_extracts_username_from_various_formats(monkeypatch):
    seen_urls = []

    class RecordingClient(_FakeHttpxClient):
        def get(self, url, headers=None, params=None):
            seen_urls.append(url)
            return _FakeResponse(404, {})

    monkeypatch.setattr(agent2.httpx, "Client", lambda *a, **kw: RecordingClient({}))

    for raw in ["https://github.com/ahmedxali84", "https://github.com/ahmedxali84/", "@ahmedxali84", "ahmedxali84"]:
        seen_urls.clear()
        agent2.fetch_github_live_insights(raw)
        assert any("ahmedxali84" in u for u in seen_urls), f"failed for input {raw!r}: {seen_urls}"


def test_fetch_github_live_insights_returns_empty_dict_for_blank_input():
    assert agent2.fetch_github_live_insights("") == {}
    assert agent2.fetch_github_live_insights("   ") == {}


def test_fetch_github_live_insights_aggregates_repos_correctly(monkeypatch):
    user_data = {
        "name": "Ahmed Ali", "bio": "Builder", "avatar_url": "https://x/a.png",
        "location": "Karachi", "company": "Wajedo", "blog": "",
        "public_repos": 3, "followers": 17, "following": 5,
    }
    repos_data = [
        {"name": "repo-a", "description": "First", "language": "Python", "stargazers_count": 10, "forks_count": 2, "html_url": "u1", "fork": False},
        {"name": "repo-b", "description": "Second", "language": "Python", "stargazers_count": 5, "forks_count": 1, "html_url": "u2", "fork": False},
        {"name": "repo-c", "description": "Third", "language": "JavaScript", "stargazers_count": 0, "forks_count": 0, "html_url": "u3", "fork": False},
        {"name": "forked-repo", "description": "Not theirs", "language": "Go", "stargazers_count": 999, "forks_count": 999, "html_url": "u4", "fork": True},
    ]
    responses = {
        "https://api.github.com/users/testuser": _FakeResponse(200, user_data),
        "https://api.github.com/users/testuser/repos": _FakeResponse(200, repos_data),
    }
    _patch_client(monkeypatch, responses)

    insights = agent2.fetch_github_live_insights("testuser")

    assert insights["verified"] is True
    assert insights["name"] == "Ahmed Ali"
    assert insights["public_repos"] == 3
    # The forked repo must be excluded from stars/forks totals and recent_repos.
    assert insights["stars_count"] == 15
    assert insights["forks_count"] == 3
    assert "forked-repo" not in insights["recent_repos"]
    assert set(insights["recent_repos"]) == {"repo-a", "repo-b", "repo-c"}
    # Python appears in 2 of the 3 language-tagged (non-fork) repos -> 67%.
    assert insights["top_languages"]["Python"] == 67


def test_fetch_github_live_insights_handles_user_not_found(monkeypatch):
    _patch_client(monkeypatch, {})  # every URL 404s
    insights = agent2.fetch_github_live_insights("ghost-user")
    assert insights["verified"] is False
    assert insights["stars_count"] == 0
    assert insights["public_repos"] == 0


# ---------------------------------------------------------------------------
# verify_repo_ownership
# ---------------------------------------------------------------------------

def test_verify_repo_ownership_rejects_malformed_url_without_any_request(monkeypatch):
    def explode(*a, **kw):
        raise AssertionError("should never construct an HTTP client for a malformed URL")
    monkeypatch.setattr(agent2.httpx, "Client", explode)

    assert agent2.verify_repo_ownership("ahmedxali84", "https://github.com/ahmedxali84") is False  # only 1 path segment


def test_verify_repo_ownership_matches_case_insensitively(monkeypatch):
    responses = {
        "https://api.github.com/repos/ahmedxali84/techflix": _FakeResponse(200, {"owner": {"login": "ahmedxali84"}}),
    }
    _patch_client(monkeypatch, responses)
    assert agent2.verify_repo_ownership("AhmedXali84", "https://github.com/ahmedxali84/techflix") is True


def test_verify_repo_ownership_rejects_mismatched_owner(monkeypatch):
    responses = {
        "https://api.github.com/repos/someone-else/techflix": _FakeResponse(200, {"owner": {"login": "someone-else"}}),
    }
    _patch_client(monkeypatch, responses)
    assert agent2.verify_repo_ownership("ahmedxali84", "https://github.com/someone-else/techflix") is False


# ---------------------------------------------------------------------------
# search_github_users
# ---------------------------------------------------------------------------

def test_search_github_users_returns_empty_for_blank_name():
    assert agent2.search_github_users("") == []
    assert agent2.search_github_users("   ") == []


def test_search_github_users_maps_real_response(monkeypatch):
    responses = {
        "https://api.github.com/search/users": _FakeResponse(200, {
            "items": [
                {"login": "ahmedxali84", "html_url": "https://github.com/ahmedxali84", "avatar_url": "a.png", "score": 12.5},
            ]
        }),
    }
    _patch_client(monkeypatch, responses)
    candidates = agent2.search_github_users("Ahmed Ali")
    assert candidates == [
        {"username": "ahmedxali84", "profile_url": "https://github.com/ahmedxali84", "avatar_url": "a.png", "score": 12.5},
    ]


# ---------------------------------------------------------------------------
# resolve_github_profile
# ---------------------------------------------------------------------------

def test_resolve_github_profile_direct_path_skips_search(monkeypatch):
    search_called = []
    monkeypatch.setattr(agent2, "search_github_users", lambda name, limit=5: search_called.append(name) or [])
    monkeypatch.setattr(agent2, "fetch_github_live_insights", lambda hint: {"username": hint})

    result = agent2.resolve_github_profile(name="Ahmed Ali", github_hint="ahmedxali84")

    assert result["resolved_by"] == "direct"
    assert result["insights"] == {"username": "ahmedxali84"}
    assert search_called == []  # never fell through to a name search


def test_resolve_github_profile_search_path_uses_best_match(monkeypatch):
    monkeypatch.setattr(agent2, "search_github_users", lambda name, limit=5: [{"username": "best-match"}, {"username": "other"}])
    monkeypatch.setattr(agent2, "fetch_github_live_insights", lambda user: {"username": user})

    result = agent2.resolve_github_profile(name="Ahmed Ali", github_hint="")

    assert result["resolved_by"] == "search"
    assert result["insights"] == {"username": "best-match"}
    assert len(result["candidates"]) == 2


def test_resolve_github_profile_returns_none_when_nothing_found(monkeypatch):
    monkeypatch.setattr(agent2, "search_github_users", lambda name, limit=5: [])
    result = agent2.resolve_github_profile(name="Nobody Findable", github_hint="")
    assert result == {"resolved_by": "none", "candidates": [], "insights": {}}


# ---------------------------------------------------------------------------
# analyze_founder_profile
# ---------------------------------------------------------------------------

def test_analyze_founder_profile_prefers_oauth_verified_name(monkeypatch):
    monkeypatch.setattr(
        agent2, "resolve_github_profile",
        lambda name, github_hint: {"resolved_by": "direct", "candidates": [], "insights": {"verified": True}},
    )
    monkeypatch.setattr(agent2, "ask_llm_json", lambda prompt, system_prompt="": {"name": "Guessed Name From Bio"})

    result = agent2.analyze_founder_profile(
        github="ahmedxali84",
        linkedin_verified={"name": "Real OAuth Name", "email": "real@example.com"},
    )

    # The real, OAuth-confirmed name must win over whatever the LLM guessed
    # from unverified text.
    assert result["name"] == "Real OAuth Name"
    assert result["linkedin_verified"]["email"] == "real@example.com"
    assert result["github_insights"] == {"verified": True}
