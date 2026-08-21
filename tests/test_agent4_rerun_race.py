"""
Regression coverage for the shortlist-resurrection race found during the
strict concurrency audit: /api/agents/agent4/rerun snapshots
candidate_ideas/candidate_investors, then makes a (potentially multi-second)
Groq call via `await asyncio.to_thread(store.run_and_record, ...)`. If a
reject lands (in another tab, or from another concurrent request) while that
await is in flight, the old code unconditionally overwrote
state.shortlisted_ideas/shortlisted_investors with the result computed from
the stale pre-await snapshot — silently resurrecting whatever was just
rejected. Fixed by re-filtering the result against state.rejected_ids as it
stands AFTER the await, not before.
"""
import asyncio

import main_app as m
from agents import store


def test_agent4_rerun_does_not_resurrect_a_reject_that_landed_during_the_await(monkeypatch):
    user_id = "test-user-agent4-race"
    state = m.get_user_state(user_id)
    state.user_type = "founder"
    state.session_id = "test-session"
    state.ideas_pool = []
    state.investors_pool = [
        {"id": "inv_a", "name": "Investor A"},
        {"id": "inv_b", "name": "Investor B"},
    ]
    state.rejected_ids = set()

    def fake_run_and_record(agent_id, uid, session_ref, fn, *args, **kwargs):
        # Simulates a reject landing (from another tab/request) while this
        # call's real Groq work would have been in flight — by the time the
        # "response" comes back, inv_a has since been rejected, but this
        # function still returns it because it was computed from the
        # snapshot taken before the reject happened.
        state.rejected_ids.add("inv_a")
        return [], [{"id": "inv_a", "name": "Investor A"}, {"id": "inv_b", "name": "Investor B"}], []

    monkeypatch.setattr(store, "run_and_record", fake_run_and_record)

    asyncio.run(m._dispatch_rerun("agent4", user_id))

    shortlisted_ids = {inv["id"] for inv in state.shortlisted_investors}
    assert "inv_a" not in shortlisted_ids, "a reject that landed mid-rerun must not resurface in the new shortlist"
    assert "inv_b" in shortlisted_ids
