"""
Regression coverage for a live user report: every AI pipeline step on the
Agents Dashboard reset to "Waiting" after a backend redeploy, even though
the underlying work (profile, resume, matches) was untouched.

Root cause: agents/store.py only ever wrote agent run status to a local
SQLite file (agent_profiles.db) on Render's ephemeral disk — the exact same
durability class as the resume-file and rejected_ids bugs fixed earlier the
same night, just for a third piece of state nobody had audited yet. Fixed
with a Postgres write-through (agent_run_status table) and a per-agent
fallback in list_profiles for whenever SQLite doesn't have a row.
"""
import agents.store as store


def test_save_profile_write_throughs_to_postgres(monkeypatch):
    monkeypatch.setattr(store, "get_database_url", lambda: "postgres://fake")
    calls = []
    monkeypatch.setattr(
        store, "_pg_save_profile",
        lambda *args: calls.append(args),
    )

    store.save_profile("agent1", "test-user-durability", "done", output={"x": 1}, session_ref="sess-1")

    assert len(calls) == 1
    agent_id, user_id, task_type, status = calls[0][0], calls[0][1], calls[0][2], calls[0][3]
    assert agent_id == "agent1"
    assert user_id == "test-user-durability"
    assert status == "done"


def test_list_profiles_falls_back_to_postgres_when_sqlite_is_empty(monkeypatch):
    user_id = "test-user-durability-fallback"

    monkeypatch.setattr(
        store, "_pg_load_profiles",
        lambda uid: {
            "agent1": {
                "task_type": "Idea → MVP Roadmap", "status": "done",
                "last_output": '{"ok": true}', "error": None,
                "session_ref": "sess-old", "updated_at": "2026-01-01T00:00:00",
            },
        } if uid == user_id else {},
    )

    profiles = store.list_profiles(user_id)
    agent1 = next(p for p in profiles if p["agent_id"] == "agent1")

    # SQLite has nothing for this brand-new user_id, so this must come from
    # the Postgres fallback rather than defaulting to "idle".
    assert agent1["status"] == "done"
    assert agent1["session_ref"] == "sess-old"


def test_list_profiles_prefers_sqlite_over_postgres_when_both_have_a_row(monkeypatch):
    user_id = "test-user-durability-prefer-sqlite"

    # A real SQLite row exists (via the normal save_profile path).
    store.save_profile("agent1", user_id, "running", session_ref="sess-live")

    pg_load_called = {"count": 0}
    def fake_pg_load(uid):
        pg_load_called["count"] += 1
        return {"agent1": {"task_type": "x", "status": "done", "last_output": None, "error": None, "session_ref": "sess-stale-pg", "updated_at": None}}
    monkeypatch.setattr(store, "_pg_load_profiles", fake_pg_load)

    profiles = store.list_profiles(user_id)
    agent1 = next(p for p in profiles if p["agent_id"] == "agent1")

    # SQLite already has agent1 -> Postgres shouldn't be consulted for it at
    # all (list_profiles only queries Postgres for agent_ids missing from
    # SQLite), and the live SQLite status must win.
    assert agent1["status"] == "running"
    assert agent1["session_ref"] == "sess-live"


def test_pg_helpers_are_no_ops_without_database_url(monkeypatch):
    monkeypatch.setattr(store, "get_database_url", lambda: "")
    # Must not raise even though nothing is configured.
    store._pg_save_profile("agent1", "u", "t", "done", None, None, "s", "now")
    assert store._pg_load_profiles("u") == {}
