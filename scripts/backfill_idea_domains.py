"""
One-off backfill: reclassifies `domain` for every existing idea whose domain
was set by the old bug (main_app.py used to copy the founder's own
`specialization` into `idea.domain` — see the fix in agent1.py/main_app.py
this session). Only touches ideas where domain still exactly equals
founder.specialization, so it's safe to run more than once — anything
already correct (fixed by this script, or created after the code fix) is
left alone.

Re-derives domain from each idea's already-extracted problem/solution/
target_market via agents.agent1.classify_idea_domain, rather than re-running
the full Agent 1 pipeline (the original raw PDF/text isn't kept around).

Touches both storage layers this app actually has:
  - Postgres `public.ideas` (durable copy, if DATABASE_URL is set)
  - sessions_db.json's _global_ideas_pool (the fallback/cache file)
A running backend process's in-memory GLOBAL_IDEAS_POOL is NOT touched
directly — restart the backend after running this for real so
_hydrate_pools_from_db() picks the corrected values back up.

Usage (from backend/, with the venv active):
    python scripts/backfill_idea_domains.py --dry-run   # preview only
    python scripts/backfill_idea_domains.py              # apply for real
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from agents.agent1 import classify_idea_domain
from agents.db import get_database_url, get_connection


def _looks_like_old_bug(domain: str, specialization: str) -> bool:
    domain = (domain or "").strip()
    specialization = (specialization or "").strip()
    return bool(domain) and bool(specialization) and domain == specialization


def backfill_postgres(dry_run: bool) -> int:
    if not get_database_url():
        print("[backfill] DATABASE_URL not set — skipping Postgres.")
        return 0

    conn = get_connection()
    if not conn:
        print("[backfill] Could not connect to Postgres — skipping.")
        return 0

    updated = 0
    try:
        with conn.cursor() as cur:
            cur.execute("select id, title, domain, problem, solution, target_market, founder from public.ideas")
            rows = cur.fetchall()

        for idea_id, title, domain, problem, solution, target_market, founder in rows:
            founder = founder or {}
            specialization = founder.get("specialization") if isinstance(founder, dict) else None
            if not _looks_like_old_bug(domain, specialization):
                continue

            new_domain = classify_idea_domain(problem or "", solution or "", target_market or "")
            print(f"[backfill:postgres] {idea_id} ({title!r}): {domain!r} -> {new_domain!r}")
            updated += 1

            if not dry_run:
                with conn:
                    with conn.cursor() as cur:
                        cur.execute(
                            "update public.ideas set domain = %s, updated_at = now() where id = %s",
                            (new_domain, idea_id),
                        )
    finally:
        conn.close()

    verb = "would be updated" if dry_run else "updated"
    print(f"[backfill:postgres] {updated} idea(s) {verb} (of {len(rows)} total).")
    return updated


def backfill_sessions_json(dry_run: bool) -> int:
    path = os.getenv("SESSIONS_DB_FILE") or "sessions_db.json"
    if not os.path.exists(path):
        print(f"[backfill:json] {path} not found — skipping.")
        return 0

    with open(path, "r") as f:
        data = json.load(f)
    pool = data.get("_global_ideas_pool", [])

    updated = 0
    for idea in pool:
        founder = idea.get("founder") or {}
        specialization = founder.get("specialization") if isinstance(founder, dict) else None
        if not _looks_like_old_bug(idea.get("domain"), specialization):
            continue

        new_domain = classify_idea_domain(
            idea.get("problem", ""), idea.get("solution", ""), idea.get("target_market", "")
        )
        print(f"[backfill:json] {idea.get('id')} ({idea.get('title')!r}): {idea.get('domain')!r} -> {new_domain!r}")
        idea["domain"] = new_domain
        updated += 1

    if not dry_run and updated:
        with open(path, "w") as f:
            json.dump(data, f, default=str, indent=2)

    verb = "would be updated" if dry_run else "updated"
    print(f"[backfill:json] {updated} idea(s) {verb} (of {len(pool)} total).")
    return updated


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true", help="Show what would change without writing anything.")
    args = parser.parse_args()

    pg_count = backfill_postgres(args.dry_run)
    json_count = backfill_sessions_json(args.dry_run)

    if args.dry_run:
        print("\n[backfill] Dry run only — nothing was written. Re-run without --dry-run to apply.")
    elif pg_count or json_count:
        print("\n[backfill] Done. Restart the backend so GLOBAL_IDEAS_POOL picks up the corrected data.")
    else:
        print("\n[backfill] Nothing needed fixing.")
