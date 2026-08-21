"""
Regression coverage for the two in-flight-request guards added during the
strict concurrency audit:

- /api/agents/{agent_id}/rerun: a double-click (or slow-network retry)
  firing two overlapping reruns for the same (agent_id, user_id) used to
  race their "running"/"done" SQLite writes, with no guarantee the later
  write actually corresponds to the later-finishing call.
- /api/reject: same shape of problem — two overlapping rejects on the same
  match both kick off their own (wasted) Agent 6 call.

Both now reject the second overlapping request with 409 instead of letting
them race.
"""
import threading

from fastapi.testclient import TestClient

import main_app as m


def _client_as(user_id):
    client = TestClient(m.app)
    m.app.dependency_overrides[m.get_current_user_id] = lambda: user_id
    return client


def test_second_overlapping_rerun_for_same_agent_and_user_is_rejected():
    user_id = "test-user-rerun-guard"
    key = ("agent1", user_id)
    client = _client_as(user_id)
    try:
        with m._in_flight_reruns_lock:
            m._in_flight_reruns.add(key)  # simulate a rerun already in flight

        resp = client.post(f"/api/agents/agent1/rerun")
        assert resp.status_code == 409
        assert "already running" in resp.json()["detail"].lower()
    finally:
        with m._in_flight_reruns_lock:
            m._in_flight_reruns.discard(key)
        m.app.dependency_overrides.pop(m.get_current_user_id, None)


def test_rerun_guard_releases_the_key_after_the_request_completes():
    user_id = "test-user-rerun-guard-release"
    client = _client_as(user_id)
    try:
        # agent1 with no cached idea text -> a clean 400, not a crash, but
        # the guard must still release its key in the `finally` regardless
        # of how the request ends.
        resp = client.post(f"/api/agents/agent1/rerun")
        assert resp.status_code == 400
        assert ("agent1", user_id) not in m._in_flight_reruns
    finally:
        m.app.dependency_overrides.pop(m.get_current_user_id, None)


def test_second_overlapping_reject_for_same_user_is_rejected():
    user_id = "test-user-reject-guard"
    client = _client_as(user_id)
    try:
        with m._in_flight_rejects_lock:
            m._in_flight_rejects.add(user_id)  # simulate a reject already in flight

        resp = client.post("/api/reject")
        assert resp.status_code == 409
        assert "still processing" in resp.json()["detail"].lower()
    finally:
        with m._in_flight_rejects_lock:
            m._in_flight_rejects.discard(user_id)
        m.app.dependency_overrides.pop(m.get_current_user_id, None)


def test_reject_guard_releases_the_key_after_the_request_completes():
    user_id = "test-user-reject-guard-release"
    client = _client_as(user_id)
    try:
        # No current_match -> a clean 400, but the guard must still release.
        resp = client.post("/api/reject")
        assert resp.status_code == 400
        assert user_id not in m._in_flight_rejects
    finally:
        m.app.dependency_overrides.pop(m.get_current_user_id, None)
