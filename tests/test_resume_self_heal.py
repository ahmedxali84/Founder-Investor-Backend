"""
Regression coverage for the resume-download bug reported live in production:
a founder's "Download resume" button worked, then started 404ing, then
reverted to a permanent "Resume building..." placeholder.

Root cause: the actual .docx (static/resumes/) and state.resume_path (only
ever written to sessions_db.json, never to Postgres via db_store) both live
on Render's ephemeral disk — a redeploy can wipe either or both, while
founder_profile survives (durably persisted via db_store when DATABASE_URL
is set, confirmed set in production). /api/resume/{owner_user_id} used to
404 outright whenever resume_path/the file was missing, even though the
profile it's built from was still there.

Fixed by rebuilding the resume on demand from founder_profile (fetching it
from Postgres via _hydrate_from_db first if the in-memory SessionState
doesn't have it either — e.g. right after a fresh process restart) instead
of just 404ing, and by exposing resume_url whenever founder_profile exists,
not only when resume_path happens to be currently set.
"""
import os
import tempfile

from fastapi.testclient import TestClient

import main_app as m
from agents import db_store


def _client_as(user_id):
    client = TestClient(m.app)
    m.app.dependency_overrides[m.get_current_user_id] = lambda: user_id
    return client


def test_resume_rebuilds_on_demand_when_file_is_missing_but_profile_survived(monkeypatch, tmp_path):
    owner_id = "founder-with-wiped-resume-file"
    owner_state = m.get_user_state(owner_id)
    owner_state.user_type = "founder"
    owner_state.founder_profile = {"name": "Ada Founder", "skills": ["Python"]}
    owner_state.resume_path = None  # simulates the sessions_db.json record having been wiped too

    fake_resume = tmp_path / "ada_founder_resume.docx"
    fake_resume.write_bytes(b"fake docx bytes")

    def fake_run_agent3(profile_data, output_dir, unique_id):
        assert profile_data == owner_state.founder_profile
        return str(fake_resume)

    monkeypatch.setattr(m, "run_agent3", fake_run_agent3)

    client = _client_as("some-investor-viewing-it")
    try:
        resp = client.get(f"/api/resume/{owner_id}")
        assert resp.status_code == 200
        assert resp.content == b"fake docx bytes"
        assert owner_state.resume_path == str(fake_resume)
    finally:
        m.app.dependency_overrides.pop(m.get_current_user_id, None)


def test_resume_rehydrates_from_postgres_after_a_cold_process_restart(monkeypatch, tmp_path):
    owner_id = "founder-cold-after-restart"
    # Simulate a fresh process: nothing in memory for this user at all.
    with m.state_lock:
        m.user_states.pop(owner_id, None)

    def fake_load_founder_profile(uid):
        assert uid == owner_id
        return {"profile": {"name": "Cold Founder"}, "linkedin": "https://linkedin.com/in/cold", "github": ""}

    monkeypatch.setattr(db_store, "load_founder_profile", fake_load_founder_profile)

    fake_resume = tmp_path / "cold_founder_resume.docx"
    fake_resume.write_bytes(b"rehydrated docx")
    monkeypatch.setattr(m, "run_agent3", lambda profile_data, output_dir, unique_id: str(fake_resume))

    client = _client_as("some-investor")
    try:
        resp = client.get(f"/api/resume/{owner_id}")
        assert resp.status_code == 200
        assert resp.content == b"rehydrated docx"
    finally:
        m.app.dependency_overrides.pop(m.get_current_user_id, None)


def test_resume_still_404s_when_no_profile_exists_anywhere(monkeypatch):
    owner_id = "nonexistent-user-made-up-id"
    with m.state_lock:
        m.user_states.pop(owner_id, None)
    monkeypatch.setattr(db_store, "load_founder_profile", lambda uid: None)
    monkeypatch.setattr(db_store, "load_investor_profile", lambda uid: None)

    client = _client_as("some-investor")
    try:
        resp = client.get(f"/api/resume/{owner_id}")
        assert resp.status_code == 404
    finally:
        m.app.dependency_overrides.pop(m.get_current_user_id, None)


def test_resume_url_exposed_via_profile_endpoint_even_when_resume_path_is_currently_unset():
    owner_id = "founder-profile-endpoint-check"
    state = m.get_user_state(owner_id)
    state.user_type = "founder"
    state.founder_profile = {"name": "Founder With Profile"}
    state.resume_path = None

    client = _client_as(owner_id)
    try:
        resp = client.get("/api/profile")
        assert resp.status_code == 200
        assert resp.json().get("resume_url") == f"/api/resume/{owner_id}"
    finally:
        m.app.dependency_overrides.pop(m.get_current_user_id, None)
