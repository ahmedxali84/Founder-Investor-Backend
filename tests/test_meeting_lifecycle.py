"""
Regression coverage for the two most fragile pieces of the meeting-request
lifecycle this session found real bugs in: active_idea_id going stale
(_ensure_active_idea_id's self-heal) and the founder/investor serialization
of GLOBAL_MEETING_REQUESTS into the shape the frontend expects.
"""
import pytest
import main_app as m


@pytest.fixture(autouse=True)
def clean_shared_pools():
    """
    GLOBAL_IDEAS_POOL/GLOBAL_INVESTORS_POOL/GLOBAL_MEETING_REQUESTS are
    process-wide module state in main_app — snapshot and restore around
    every test so tests can't leak fixture data into each other.
    """
    ideas_before = list(m.GLOBAL_IDEAS_POOL)
    investors_before = list(m.GLOBAL_INVESTORS_POOL)
    meetings_before = dict(m.GLOBAL_MEETING_REQUESTS)
    yield
    m.GLOBAL_IDEAS_POOL[:] = ideas_before
    m.GLOBAL_INVESTORS_POOL[:] = investors_before
    m.GLOBAL_MEETING_REQUESTS.clear()
    m.GLOBAL_MEETING_REQUESTS.update(meetings_before)


def _make_idea(idea_id, owner_user_id):
    return {
        "id": idea_id,
        "title": "Test Idea",
        "domain": "SaaS",
        "problem": "p", "solution": "s", "target_market": "t",
        "features_must_have": [], "features_nice_to_have": [], "roadmap": [],
        "founder": {"name": "Test Founder"},
        "owner_user_id": owner_user_id,
        "base_scores": {"potential": 80, "feasibility": 80, "market_fit": 80},
    }


def _make_investor(inv_id, owner_user_id):
    return {
        "id": inv_id,
        "owner_user_id": owner_user_id,
        "name": "Test Investor",
        "designation": "Partner",
        "firm": "Test Capital",
        "focus_sectors": ["SaaS"],
        "min_ticket": 50_000,
        "max_ticket": 500_000,
    }


def test_ensure_active_idea_id_self_heals_from_stale_none():
    user_id = "test-founder-uid"
    idea = _make_idea("idea_test_01", user_id)
    m.GLOBAL_IDEAS_POOL.append(idea)

    state = m.SessionState()
    state.user_type = "founder"
    state.active_idea_id = None  # simulates a re-onboarding reset, or a failed refresh

    m._ensure_active_idea_id(state, user_id)

    assert state.active_idea_id == "idea_test_01"


def test_ensure_active_idea_id_is_a_noop_when_already_set():
    user_id = "test-founder-uid-2"
    m.GLOBAL_IDEAS_POOL.append(_make_idea("idea_test_02", user_id))

    state = m.SessionState()
    state.user_type = "founder"
    state.active_idea_id = "some_other_idea_id"

    m._ensure_active_idea_id(state, user_id)

    assert state.active_idea_id == "some_other_idea_id"


def test_serialize_meeting_requests_founder_side_confirmed():
    founder_uid = "test-founder-uid-3"
    investor_uid = "test-investor-uid-3"
    idea = _make_idea("idea_test_03", founder_uid)
    investor = _make_investor("inv_test_03", investor_uid)
    m.GLOBAL_IDEAS_POOL.append(idea)
    m.GLOBAL_INVESTORS_POOL.append(investor)

    key = m._meeting_key("idea_test_03", "inv_test_03")
    m.GLOBAL_MEETING_REQUESTS[key] = {
        "idea_id": "idea_test_03",
        "investor_id": "inv_test_03",
        "founder_requested": True,
        "investor_raised": True,
        "both_opted_in": True,
        "idea": idea,
        "investor": investor,
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }

    state = m.SessionState()
    state.user_type = "founder"
    state.active_idea_id = "idea_test_03"

    result = m._serialize_meeting_requests(state)

    assert len(result) == 1
    assert result[0]["id"] == "inv_test_03"
    assert result[0]["both_opted_in"] is True
    assert result[0]["investor"]["name"] == "Test Investor"


def test_serialize_meeting_requests_founder_side_empty_without_active_idea_id():
    """
    A founder with no active_idea_id (and no owned idea in the pool for
    _ensure_active_idea_id to heal from) correctly sees no meeting requests
    — this is the failure mode that _ensure_active_idea_id exists to prevent
    for founders who DO still have a live idea in the pool.
    """
    state = m.SessionState()
    state.user_type = "founder"
    state.active_idea_id = None

    result = m._serialize_meeting_requests(state)

    assert result == []
