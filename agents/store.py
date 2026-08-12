"""
Shared persistence layer for agent profiles.

Every agent in the pipeline (agent1..agent7) should have its latest run
status/output recorded here instead of only living in the in-memory
SessionState. This is what the /agents dashboard reads from, and what
survives a server restart.
"""
import os
import json
import sqlite3
import logging
import datetime
from contextlib import contextmanager

logger = logging.getLogger("agent_store")
if not logger.handlers:
    handler = logging.StreamHandler()
    handler.setFormatter(logging.Formatter("[%(asctime)s] %(name)s %(levelname)s: %(message)s"))
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)

# AGENT_PROFILES_DB_PATH lets tests redirect this to a temp file before this
# module is first imported — init_db() below runs unconditionally at import
# time, so there's no other point at which a test could safely intervene.
DB_PATH = os.getenv("AGENT_PROFILES_DB_PATH") or os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "agent_profiles.db"
)

# Registry of every trackable agent unit. Agents 2 and 6 run different logic
# depending on user type, so they get founder/investor variants.
AGENT_REGISTRY = {
    "agent1": {"label": "Agent 1 (Mubashir)", "task_type": "Idea → MVP Roadmap"},
    "agent2_founder": {"label": "Agent 2 (Eshaal) — Founder", "task_type": "Founder Profile Extraction"},
    "agent2_investor": {"label": "Agent 2 (Eshaal) — Investor", "task_type": "Investor Profile Extraction"},
    "agent3": {"label": "Agent 3 (Tazmeen)", "task_type": "Profile → Word Resume"},
    "agent4": {"label": "Agent 4 (Saad)", "task_type": "Top 10 Shortlist (Ideas & Investors)"},
    "agent5": {"label": "Agent 5 (Ashar Bhai)", "task_type": "MVP Check & Matchmaking"},
    "agent6_founder": {"label": "Agent 6 (Joun Ali Bangash) — Founder", "task_type": "Next-Best Investor Routing"},
    "agent6_investor": {"label": "Agent 6 (Joun Ali Bangash) — Investor", "task_type": "Next-Best Startup Routing"},
    "agent7": {"label": "Agent 7", "task_type": "Chat Log → Investment Agreement"},
}

_PREVIEW_LIMIT = 4000


@contextmanager
def _connect():
    conn = sqlite3.connect(DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


def init_db():
    with _connect() as conn:
        # Older versions of this table keyed rows by agent_id alone — a
        # single global row per agent type, so whoever last triggered e.g.
        # Agent 2 determined what *every* logged-in user saw on their own
        # dashboard. Detect that shape and rebuild with a (agent_id, user_id)
        # composite key instead; this table is only operational status/log
        # data, not anything that needs a real migration path.
        cols = {row[1] for row in conn.execute("PRAGMA table_info(agent_profiles)")}
        if cols and "user_id" not in cols:
            conn.execute("DROP TABLE agent_profiles")
        conn.execute("""
            CREATE TABLE IF NOT EXISTS agent_profiles (
                agent_id    TEXT,
                user_id     TEXT,
                task_type   TEXT,
                status      TEXT,
                last_output TEXT,
                error       TEXT,
                session_ref TEXT,
                updated_at  TEXT,
                PRIMARY KEY (agent_id, user_id)
            )
        """)
        conn.commit()


def _safe_json(obj) -> str:
    try:
        text = json.dumps(obj, default=str)
    except Exception:
        text = str(obj)
    if len(text) > _PREVIEW_LIMIT:
        text = text[:_PREVIEW_LIMIT] + "... (truncated)"
    return text


def save_profile(agent_id: str, user_id: str, status: str, output=None, error: str = None, session_ref: str = "—"):
    """
    Upsert the latest known status/output for one (agent_id, user_id) pair.
    Never raises — a failed write is logged loudly instead of silently
    disappearing or crashing the caller's pipeline.
    """
    task_type = AGENT_REGISTRY.get(agent_id, {}).get("task_type", agent_id)
    output_json = _safe_json(output) if output is not None else None
    now = datetime.datetime.now().isoformat(timespec="seconds")

    try:
        with _connect() as conn:
            conn.execute("""
                INSERT INTO agent_profiles (agent_id, user_id, task_type, status, last_output, error, session_ref, updated_at)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(agent_id, user_id) DO UPDATE SET
                    task_type=excluded.task_type,
                    status=excluded.status,
                    last_output=excluded.last_output,
                    error=excluded.error,
                    session_ref=excluded.session_ref,
                    updated_at=excluded.updated_at
            """, (agent_id, user_id, task_type, status, output_json, error, session_ref, now))
            conn.commit()
    except Exception:
        logger.exception("Failed to save profile for agent_id=%s user_id=%s (status=%s)", agent_id, user_id, status)


def list_profiles(user_id: str) -> list:
    """Every registry entry merged with this user's stored row, defaulting to idle if never run."""
    rows_by_id = {}
    try:
        with _connect() as conn:
            conn.row_factory = sqlite3.Row
            for row in conn.execute("SELECT * FROM agent_profiles WHERE user_id = ?", (user_id,)):
                rows_by_id[row["agent_id"]] = dict(row)
    except Exception:
        logger.exception("Failed to read agent profiles for user_id=%s", user_id)

    result = []
    for agent_id, meta in AGENT_REGISTRY.items():
        row = rows_by_id.get(agent_id)
        if row:
            result.append({
                "agent_id": agent_id,
                "label": meta["label"],
                "task_type": row["task_type"] or meta["task_type"],
                "status": row["status"],
                "last_output": row["last_output"],
                "error": row["error"],
                "session_ref": row["session_ref"],
                "updated_at": row["updated_at"],
            })
        else:
            result.append({
                "agent_id": agent_id,
                "label": meta["label"],
                "task_type": meta["task_type"],
                "status": "idle",
                "last_output": None,
                "error": None,
                "session_ref": None,
                "updated_at": None,
            })
    return result


def run_and_record(agent_id: str, user_id: str, session_ref: str, fn, *args, **kwargs):
    """
    Shared wrapper every agent invocation should go through: marks the
    agent 'running' for this user, calls it, then records 'done'/'failed'.
    Re-raises on failure so existing caller try/except logic is unaffected.
    """
    save_profile(agent_id, user_id, "running", session_ref=session_ref)
    try:
        result = fn(*args, **kwargs)
    except Exception as exc:
        save_profile(agent_id, user_id, "failed", error=str(exc), session_ref=session_ref)
        raise
    else:
        save_profile(agent_id, user_id, "done", output=result, session_ref=session_ref)
        return result


init_db()
