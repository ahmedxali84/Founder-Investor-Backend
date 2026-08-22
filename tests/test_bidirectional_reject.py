"""
Regression coverage for a live user report: "a rejected person shows up
again as a match." Investigation found two independent real bugs:

1. Rejection was one-directional. Rejecting a match only updated the
   rejecter's own future shortlist (state.rejected_ids) — the rejected
   party's own state was never touched, so they could keep being shown (and
   keep pursuing) someone who had already declined them, from their own
   side. Fixed by mirroring the rejection onto the other party's own
   rejected_ids too.

2. rejected_ids was never persisted to Postgres, only to sessions_db.json on
   Render's ephemeral disk — the same durability class as the resume-file
   bug fixed earlier. Any redeploy/restart silently reset everyone's
   rejection history. Fixed with db_store.save_rejected_ids/load_rejected_ids
   plus write-through calls and startup rehydration.
"""
import asyncio

from fastapi.testclient import TestClient

import main_app as m
from agents import db_store


def _client_as(user_id):
    client = TestClient(m.app)
    m.app.dependency_overrides[m.get_current_user_id] = lambda: user_id
    return client


def test_founder_reject_mirrors_onto_the_investors_own_state(monkeypatch):
    founder_id = "test-founder-bidir"
    investor_owner_id = "test-investor-bidir-owner"

    founder_state = m.get_user_state(founder_id)
    founder_state.user_type = "founder"
    founder_state.rejected_ids = set()
    idea = {"id": "idea_bidir_1", "owner_user_id": founder_id}
    investor = {"id": "inv_bidir_1", "owner_user_id": investor_owner_id, "name": "Test Investor"}
    founder_state.current_match = {"idea": idea, "investor": investor}
    founder_state.shortlisted_investors = [investor]

    investor_state = m.get_user_state(investor_owner_id)
    investor_state.user_type = "investor"
    investor_state.rejected_ids = set()

    monkeypatch.setattr(db_store, "save_rejected_ids", lambda uid, ids: True)
    monkeypatch.setattr(m.store, "run_and_record", lambda *a, **kw: (None, "no more matches"))

    client = _client_as(founder_id)
    try:
        resp = client.post("/api/reject")
        assert resp.status_code == 200
    finally:
        m.app.dependency_overrides.pop(m.get_current_user_id, None)

    assert "inv_bidir_1" in founder_state.rejected_ids
    # The investor's OWN state must also exclude this idea now — this is
    # the actual fix. Before it, investor_state.rejected_ids stayed empty.
    assert "idea_bidir_1" in investor_state.rejected_ids


def test_investor_reject_mirrors_onto_the_founders_own_state(monkeypatch):
    investor_id = "test-investor-bidir-2"
    founder_owner_id = "test-founder-bidir-owner-2"

    investor_state = m.get_user_state(investor_id)
    investor_state.user_type = "investor"
    investor_state.rejected_ids = set()
    idea = {"id": "idea_bidir_2", "owner_user_id": founder_owner_id}
    investor = {"id": "inv_bidir_2", "owner_user_id": investor_id, "name": "Test Investor 2"}
    investor_state.current_match = {"idea": idea, "investor": investor}
    investor_state.shortlisted_ideas = [idea]

    founder_state = m.get_user_state(founder_owner_id)
    founder_state.user_type = "founder"
    founder_state.rejected_ids = set()

    monkeypatch.setattr(db_store, "save_rejected_ids", lambda uid, ids: True)
    monkeypatch.setattr(m.store, "run_and_record", lambda *a, **kw: (None, "no more matches"))

    client = _client_as(investor_id)
    try:
        resp = client.post("/api/reject")
        assert resp.status_code == 200
    finally:
        m.app.dependency_overrides.pop(m.get_current_user_id, None)

    assert "idea_bidir_2" in investor_state.rejected_ids
    assert "inv_bidir_2" in founder_state.rejected_ids


def test_reject_writes_through_to_postgres_for_both_parties(monkeypatch):
    founder_id = "test-founder-writethrough"
    investor_owner_id = "test-investor-writethrough-owner"

    founder_state = m.get_user_state(founder_id)
    founder_state.user_type = "founder"
    founder_state.rejected_ids = set()
    idea = {"id": "idea_wt_1", "owner_user_id": founder_id}
    investor = {"id": "inv_wt_1", "owner_user_id": investor_owner_id}
    founder_state.current_match = {"idea": idea, "investor": investor}
    founder_state.shortlisted_investors = [investor]

    investor_state = m.get_user_state(investor_owner_id)
    investor_state.user_type = "investor"
    investor_state.rejected_ids = set()

    saved = {}
    def fake_save(uid, ids):
        saved[uid] = set(ids)
        return True
    monkeypatch.setattr(db_store, "save_rejected_ids", fake_save)
    monkeypatch.setattr(m.store, "run_and_record", lambda *a, **kw: (None, "no more matches"))

    client = _client_as(founder_id)
    try:
        resp = client.post("/api/reject")
        assert resp.status_code == 200
    finally:
        m.app.dependency_overrides.pop(m.get_current_user_id, None)

    assert saved.get(founder_id) == {"inv_wt_1"}
    assert saved.get(investor_owner_id) == {"idea_wt_1"}


def test_hydrate_rejected_ids_loads_from_postgres_when_state_is_cold(monkeypatch):
    monkeypatch.setattr(db_store, "load_rejected_ids", lambda uid: ["a", "b", "c"])

    class FakeState:
        rejected_ids = set()

    state = FakeState()
    asyncio.run(m._hydrate_rejected_ids(state, "some-user"))

    assert state.rejected_ids == {"a", "b", "c"}


def test_hydrate_rejected_ids_does_not_overwrite_existing_in_memory_state(monkeypatch):
    monkeypatch.setattr(db_store, "load_rejected_ids", lambda uid: ["stale", "postgres", "data"])

    class FakeState:
        rejected_ids = {"fresh-in-memory"}

    state = FakeState()
    asyncio.run(m._hydrate_rejected_ids(state, "some-user"))

    assert state.rejected_ids == {"fresh-in-memory"}
