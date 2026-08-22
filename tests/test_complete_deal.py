"""
Regression coverage for the "no Done button" gap found via a live user
report: a confirmed deal (both_opted_in=True) had no further lifecycle
state — nothing ever released it, so it permanently occupied that investor's
or founder's one-deal slot even after the collaboration actually concluded.

/api/complete-deal adds a "completed" flag either party can set
unilaterally (matching /api/reject's existing one-sided pattern), which
frees both parties up for a new deal via _investor_has_other_confirmed_deal
/ _idea_has_other_confirmed_deal.
"""
from fastapi.testclient import TestClient

import main_app as m
from agents import db_store


def _client_as(user_id):
    client = TestClient(m.app)
    m.app.dependency_overrides[m.get_current_user_id] = lambda: user_id
    return client


def _confirmed_slot(idea_id, investor_id, founder_owner_id, investor_owner_id):
    return {
        "idea_id": idea_id,
        "investor_id": investor_id,
        "founder_requested": True,
        "investor_raised": True,
        "both_opted_in": True,
        "idea": {"id": idea_id, "owner_user_id": founder_owner_id, "title": "Test Idea"},
        "investor": {"id": investor_id, "owner_user_id": investor_owner_id, "name": "Test Investor"},
        "created_at": "2026-01-01T00:00:00+00:00",
        "updated_at": "2026-01-01T00:00:00+00:00",
    }


def test_founder_can_mark_a_confirmed_deal_complete(monkeypatch):
    founder_id = "test-founder-complete"
    idea_id, investor_id = "idea_complete_1", "inv_complete_1"

    founder_state = m.get_user_state(founder_id)
    founder_state.user_type = "founder"
    founder_state.active_idea_id = idea_id

    key = m._meeting_key(idea_id, investor_id)
    m.GLOBAL_MEETING_REQUESTS[key] = _confirmed_slot(idea_id, investor_id, founder_id, "investor-owner-1")
    monkeypatch.setattr(db_store, "save_meeting_request", lambda slot: True)

    client = _client_as(founder_id)
    try:
        resp = client.post("/api/complete-deal", json={"id": investor_id})
    finally:
        m.app.dependency_overrides.pop(m.get_current_user_id, None)

    assert resp.status_code == 200
    assert resp.json()["success"] is True
    assert m.GLOBAL_MEETING_REQUESTS[key]["completed"] is True
    assert m.GLOBAL_MEETING_REQUESTS[key]["completed_at"]


def test_investor_can_mark_a_confirmed_deal_complete(monkeypatch):
    investor_owner_id = "test-investor-complete"
    idea_id, investor_id = "idea_complete_2", "inv_complete_2"

    investor_state = m.get_user_state(investor_owner_id)
    investor_state.user_type = "investor"
    investor_state.investor_profile = {"id": investor_id}

    key = m._meeting_key(idea_id, investor_id)
    m.GLOBAL_MEETING_REQUESTS[key] = _confirmed_slot(idea_id, investor_id, "founder-owner-2", investor_owner_id)
    monkeypatch.setattr(db_store, "save_meeting_request", lambda slot: True)

    client = _client_as(investor_owner_id)
    try:
        resp = client.post("/api/complete-deal", json={"id": idea_id})
    finally:
        m.app.dependency_overrides.pop(m.get_current_user_id, None)

    assert resp.status_code == 200
    assert m.GLOBAL_MEETING_REQUESTS[key]["completed"] is True


def test_complete_deal_404s_when_no_confirmed_deal_exists(monkeypatch):
    founder_id = "test-founder-complete-404"
    founder_state = m.get_user_state(founder_id)
    founder_state.user_type = "founder"
    founder_state.active_idea_id = "idea_nonexistent"

    client = _client_as(founder_id)
    try:
        resp = client.post("/api/complete-deal", json={"id": "inv_nonexistent"})
    finally:
        m.app.dependency_overrides.pop(m.get_current_user_id, None)

    assert resp.status_code == 404


def test_completed_deal_frees_the_investor_for_a_new_confirmed_deal():
    idea_a, idea_b, investor_id = "idea_free_a", "idea_free_b", "inv_free_1"

    key_a = m._meeting_key(idea_a, investor_id)
    m.GLOBAL_MEETING_REQUESTS[key_a] = _confirmed_slot(idea_a, investor_id, "founder-a", "investor-owner-free")

    # Still has an active (uncompleted) deal with idea_a -> blocked from idea_b.
    assert m._investor_has_other_confirmed_deal(investor_id, excluding_idea_id=idea_b) is True

    m.GLOBAL_MEETING_REQUESTS[key_a]["completed"] = True

    # Once completed, that slot no longer counts as blocking.
    assert m._investor_has_other_confirmed_deal(investor_id, excluding_idea_id=idea_b) is False


def test_completed_deal_still_reported_in_investor_deals_track_record():
    idea_id, investor_id = "idea_track_1", "inv_track_1"
    key = m._meeting_key(idea_id, investor_id)
    m.GLOBAL_MEETING_REQUESTS[key] = _confirmed_slot(idea_id, investor_id, "founder-track", "investor-owner-track")
    m.GLOBAL_MEETING_REQUESTS[key]["completed"] = True

    deals = m._investor_deals(investor_id)
    assert any(d["idea_id"] == idea_id for d in deals)
