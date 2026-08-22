"""
Persists founder/investor profiles (LinkedIn + GitHub + Agent 2's analysis)
into real Postgres tables instead of only living in the Python process's
in-memory state + local sessions_db.json file. That file is fine as a fast
cache, but it isn't durable across restarts/redeploys — this is what makes
"already onboarded" survive a server restart, and what lets login skip
re-asking for LinkedIn/GitHub once a founder has already connected them.

Opt-in via DATABASE_URL, same as agents/db_migrate.py — every function here
is a no-op (returns None / False) if it isn't set, so the app works fine
without it, just without cross-restart durability.
"""
import json
from datetime import datetime, timezone
from agents.db import get_database_url, get_connection as _shared_get_connection


def _get_connection():
    if not get_database_url():
        return None
    try:
        return _shared_get_connection()
    except Exception as e:
        print(f"[db_store] Could not connect to Postgres: {e}")
        return None


def save_founder_profile(user_id: str, profile_data: dict, linkedin: str, github: str) -> bool:
    conn = _get_connection()
    if not conn:
        return False
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into public.founder_profiles (user_id, linkedin_url, github_url, profile)
                    values (%s, %s, %s, %s)
                    on conflict (user_id) do update set
                        linkedin_url = excluded.linkedin_url,
                        github_url = excluded.github_url,
                        profile = excluded.profile,
                        updated_at = now()
                    """,
                    (user_id, linkedin, github, json.dumps(profile_data, default=str)),
                )
        return True
    except Exception as e:
        print(f"[db_store] save_founder_profile failed: {e}")
        return False
    finally:
        conn.close()


def load_founder_profile(user_id: str):
    conn = _get_connection()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "select linkedin_url, github_url, profile from public.founder_profiles where user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        linkedin_url, github_url, profile = row
        return {"linkedin": linkedin_url, "github": github_url, "profile": profile}
    except Exception as e:
        print(f"[db_store] load_founder_profile failed: {e}")
        return None
    finally:
        conn.close()


def save_investor_profile(user_id: str, profile_data: dict, linkedin: str) -> bool:
    conn = _get_connection()
    if not conn:
        return False
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into public.investor_profiles (user_id, linkedin_url, profile)
                    values (%s, %s, %s)
                    on conflict (user_id) do update set
                        linkedin_url = excluded.linkedin_url,
                        profile = excluded.profile,
                        updated_at = now()
                    """,
                    (user_id, linkedin, json.dumps(profile_data, default=str)),
                )
        return True
    except Exception as e:
        print(f"[db_store] save_investor_profile failed: {e}")
        return False
    finally:
        conn.close()


def load_investor_profile(user_id: str):
    conn = _get_connection()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "select linkedin_url, profile from public.investor_profiles where user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        if not row:
            return None
        linkedin_url, profile = row
        return {"linkedin": linkedin_url, "profile": profile}
    except Exception as e:
        print(f"[db_store] load_investor_profile failed: {e}")
        return None
    finally:
        conn.close()


def save_rejected_ids(user_id: str, rejected_ids) -> bool:
    """
    Durable write-through for SessionState.rejected_ids — previously only
    ever lived in sessions_db.json on ephemeral disk, so a redeploy silently
    reset everyone's rejection history and previously-declined matches would
    resurface. `rejected_ids` may be a set or list; always stored as a
    sorted list for a stable, diffable JSON representation.
    """
    conn = _get_connection()
    if not conn:
        return False
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into public.user_rejections (user_id, rejected_ids)
                    values (%s, %s)
                    on conflict (user_id) do update set
                        rejected_ids = excluded.rejected_ids,
                        updated_at = now()
                    """,
                    (user_id, json.dumps(sorted(rejected_ids), default=str)),
                )
        return True
    except Exception as e:
        print(f"[db_store] save_rejected_ids failed: {e}")
        return False
    finally:
        conn.close()


def load_rejected_ids(user_id: str):
    """Returns a list of ids (never None on success — an unknown user just has no rejections yet)."""
    conn = _get_connection()
    if not conn:
        return None
    try:
        with conn.cursor() as cur:
            cur.execute(
                "select rejected_ids from public.user_rejections where user_id = %s",
                (user_id,),
            )
            row = cur.fetchone()
        return row[0] if row else []
    except Exception as e:
        print(f"[db_store] load_rejected_ids failed: {e}")
        return None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Write-through durability for the shared marketplace pools (GLOBAL_IDEAS_POOL
# / GLOBAL_INVESTORS_POOL / GLOBAL_MEETING_REQUESTS in main_app.py). Those
# in-memory pools stay the live, fast, process-wide working set the app
# actually reads from — these functions are a durability layer underneath
# them, not a replacement: called on every mutation (best-effort, same
# swallow-and-log philosophy as every other function in this file) and on
# startup to hydrate the pools so a crashed/restarted process doesn't lose
# data that only ever made it into sessions_db.json's last snapshot.
# ---------------------------------------------------------------------------

def save_idea(idea: dict) -> bool:
    conn = _get_connection()
    if not conn:
        return False
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into public.ideas
                        (id, owner_user_id, title, domain, problem, solution, target_market,
                         features_must_have, features_nice_to_have, roadmap, founder, base_scores)
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (id) do update set
                        owner_user_id = excluded.owner_user_id,
                        title = excluded.title,
                        domain = excluded.domain,
                        problem = excluded.problem,
                        solution = excluded.solution,
                        target_market = excluded.target_market,
                        features_must_have = excluded.features_must_have,
                        features_nice_to_have = excluded.features_nice_to_have,
                        roadmap = excluded.roadmap,
                        founder = excluded.founder,
                        base_scores = excluded.base_scores,
                        updated_at = now()
                    """,
                    (
                        idea.get("id"),
                        idea.get("owner_user_id"),
                        idea.get("title"),
                        idea.get("domain"),
                        idea.get("problem"),
                        idea.get("solution"),
                        idea.get("target_market"),
                        json.dumps(idea.get("features_must_have") or [], default=str),
                        json.dumps(idea.get("features_nice_to_have") or [], default=str),
                        json.dumps(idea.get("roadmap") or [], default=str),
                        json.dumps(idea.get("founder") or {}, default=str),
                        json.dumps(idea.get("base_scores") or {}, default=str),
                    ),
                )
        return True
    except Exception as e:
        print(f"[db_store] save_idea failed: {e}")
        return False
    finally:
        conn.close()


def load_all_ideas() -> list:
    conn = _get_connection()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                select id, owner_user_id, title, domain, problem, solution, target_market,
                       features_must_have, features_nice_to_have, roadmap, founder, base_scores
                from public.ideas
                """
            )
            rows = cur.fetchall()
        return [
            {
                "id": r[0],
                "owner_user_id": r[1],
                "title": r[2],
                "domain": r[3],
                "problem": r[4],
                "solution": r[5],
                "target_market": r[6],
                "features_must_have": r[7],
                "features_nice_to_have": r[8],
                "roadmap": r[9],
                "founder": r[10],
                "base_scores": r[11],
            }
            for r in rows
        ]
    except Exception as e:
        print(f"[db_store] load_all_ideas failed: {e}")
        return []
    finally:
        conn.close()


def delete_ideas_by_owner(owner_user_id: str) -> bool:
    conn = _get_connection()
    if not conn:
        return False
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("delete from public.ideas where owner_user_id = %s", (owner_user_id,))
        return True
    except Exception as e:
        print(f"[db_store] delete_ideas_by_owner failed: {e}")
        return False
    finally:
        conn.close()


def save_investor(investor: dict) -> bool:
    conn = _get_connection()
    if not conn:
        return False
    try:
        with conn:
            with conn.cursor() as cur:
                linkedin_verified = investor.get("linkedin_verified")
                cur.execute(
                    """
                    insert into public.investor_listings
                        (id, owner_user_id, name, designation, firm, experience,
                         focus_sectors, min_ticket, max_ticket, bio, custom_details, location, linkedin_verified)
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
                    on conflict (id) do update set
                        owner_user_id = excluded.owner_user_id,
                        name = excluded.name,
                        designation = excluded.designation,
                        firm = excluded.firm,
                        experience = excluded.experience,
                        focus_sectors = excluded.focus_sectors,
                        min_ticket = excluded.min_ticket,
                        max_ticket = excluded.max_ticket,
                        bio = excluded.bio,
                        custom_details = excluded.custom_details,
                        location = excluded.location,
                        linkedin_verified = excluded.linkedin_verified,
                        updated_at = now()
                    """,
                    (
                        investor.get("id"),
                        investor.get("owner_user_id"),
                        investor.get("name"),
                        investor.get("designation"),
                        investor.get("firm"),
                        investor.get("experience"),
                        json.dumps(investor.get("focus_sectors") or [], default=str),
                        investor.get("min_ticket"),
                        investor.get("max_ticket"),
                        investor.get("bio"),
                        json.dumps(investor.get("custom_details") or {}, default=str),
                        json.dumps(investor.get("location") or {}, default=str),
                        json.dumps(linkedin_verified, default=str) if linkedin_verified is not None else None,
                    ),
                )
        return True
    except Exception as e:
        print(f"[db_store] save_investor failed: {e}")
        return False
    finally:
        conn.close()


def load_all_investors() -> list:
    conn = _get_connection()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                select id, owner_user_id, name, designation, firm, experience,
                       focus_sectors, min_ticket, max_ticket, bio, custom_details, location, linkedin_verified
                from public.investor_listings
                """
            )
            rows = cur.fetchall()
        return [
            {
                "id": r[0],
                "owner_user_id": r[1],
                "name": r[2],
                "designation": r[3],
                "firm": r[4],
                "experience": r[5],
                "focus_sectors": r[6],
                "min_ticket": r[7],
                "max_ticket": r[8],
                "bio": r[9],
                "custom_details": r[10],
                "location": r[11],
                "linkedin_verified": r[12],
            }
            for r in rows
        ]
    except Exception as e:
        print(f"[db_store] load_all_investors failed: {e}")
        return []
    finally:
        conn.close()


def delete_investors_by_owner(owner_user_id: str) -> bool:
    conn = _get_connection()
    if not conn:
        return False
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute("delete from public.investor_listings where owner_user_id = %s", (owner_user_id,))
        return True
    except Exception as e:
        print(f"[db_store] delete_investors_by_owner failed: {e}")
        return False
    finally:
        conn.close()


def save_meeting_request(slot: dict) -> bool:
    """
    Takes the full in-memory slot dict (idea_id, investor_id,
    founder_requested, investor_raised, both_opted_in, created_at,
    updated_at, idea, investor) — extracts founder_user_id/investor_user_id
    from the embedded idea/investor snapshots itself so callers don't need
    to compute them separately at each call site.
    """
    conn = _get_connection()
    if not conn:
        return False
    try:
        idea_snapshot = slot.get("idea") or {}
        investor_snapshot = slot.get("investor") or {}
        founder_user_id = idea_snapshot.get("owner_user_id")
        investor_user_id = investor_snapshot.get("owner_user_id")
        created_at = slot.get("created_at") or datetime.now(timezone.utc).isoformat()
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    """
                    insert into public.meeting_requests
                        (idea_id, investor_id, founder_user_id, investor_user_id,
                         founder_requested, investor_raised, both_opted_in,
                         idea_snapshot, investor_snapshot, created_at, updated_at,
                         completed, completed_at)
                    values (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, now(), %s, %s)
                    on conflict (idea_id, investor_id) do update set
                        founder_user_id = excluded.founder_user_id,
                        investor_user_id = excluded.investor_user_id,
                        founder_requested = excluded.founder_requested,
                        investor_raised = excluded.investor_raised,
                        both_opted_in = excluded.both_opted_in,
                        idea_snapshot = excluded.idea_snapshot,
                        investor_snapshot = excluded.investor_snapshot,
                        completed = excluded.completed,
                        completed_at = excluded.completed_at,
                        updated_at = now()
                    """,
                    (
                        slot.get("idea_id"),
                        slot.get("investor_id"),
                        founder_user_id,
                        investor_user_id,
                        bool(slot.get("founder_requested")),
                        bool(slot.get("investor_raised")),
                        bool(slot.get("both_opted_in")),
                        json.dumps(idea_snapshot, default=str),
                        json.dumps(investor_snapshot, default=str),
                        created_at,
                        bool(slot.get("completed")),
                        slot.get("completed_at"),
                    ),
                )
        return True
    except Exception as e:
        print(f"[db_store] save_meeting_request failed: {e}")
        return False
    finally:
        conn.close()


def load_all_meeting_requests() -> list:
    """
    Returns a plain list, not pre-keyed — main_app.py builds the
    f"{idea_id}::{investor_id}" key itself via its own _meeting_key helper,
    so that format lives in exactly one place.
    """
    conn = _get_connection()
    if not conn:
        return []
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                select idea_id, investor_id, founder_requested, investor_raised, both_opted_in,
                       idea_snapshot, investor_snapshot, created_at, updated_at,
                       completed, completed_at
                from public.meeting_requests
                """
            )
            rows = cur.fetchall()
        return [
            {
                "idea_id": r[0],
                "investor_id": r[1],
                "founder_requested": r[2],
                "investor_raised": r[3],
                "both_opted_in": r[4],
                "idea": r[5],
                "investor": r[6],
                "created_at": r[7].isoformat() if r[7] else None,
                "updated_at": r[8].isoformat() if r[8] else None,
                "completed": r[9],
                "completed_at": r[10].isoformat() if r[10] else None,
            }
            for r in rows
        ]
    except Exception as e:
        print(f"[db_store] load_all_meeting_requests failed: {e}")
        return []
    finally:
        conn.close()


def delete_meeting_requests_by_owner(owner_user_id: str) -> bool:
    conn = _get_connection()
    if not conn:
        return False
    try:
        with conn:
            with conn.cursor() as cur:
                cur.execute(
                    "delete from public.meeting_requests where founder_user_id = %s or investor_user_id = %s",
                    (owner_user_id, owner_user_id),
                )
        return True
    except Exception as e:
        print(f"[db_store] delete_meeting_requests_by_owner failed: {e}")
        return False
    finally:
        conn.close()
