"""
Applies frontend/supabase/schema.sql directly against Postgres on every
backend startup, when DATABASE_URL is set. Opt-in: the whole app runs fine
without it (you'd just paste schema.sql into Supabase Studio manually
instead, whenever it changes). schema.sql is written to be fully idempotent
(create/alter ... if not exists, drop policy if exists before create policy)
so re-running it on every startup is safe.
"""
from pathlib import Path
from agents.db import get_database_url, get_connection

SCHEMA_PATH = Path(__file__).resolve().parent.parent.parent / "frontend" / "supabase" / "schema.sql"


def run_schema_migrations():
    if not get_database_url():
        print("[db_migrate] DATABASE_URL not set — skipping automatic schema migration.")
        return

    if not SCHEMA_PATH.exists():
        print(f"[db_migrate] schema.sql not found at {SCHEMA_PATH} — skipping.")
        return

    try:
        conn = get_connection()
    except ImportError:
        print("[db_migrate] psycopg2 not installed (pip install -r requirements.txt) — skipping.")
        return
    except Exception as e:
        print(f"[db_migrate] Could not connect to Postgres: {e}")
        return

    sql = SCHEMA_PATH.read_text(encoding="utf-8")

    try:
        conn.autocommit = True
        try:
            with conn.cursor() as cur:
                cur.execute(sql)
            print("[db_migrate] frontend/supabase/schema.sql applied successfully.")
        finally:
            conn.close()
    except Exception as e:
        # Never crash the app over a migration hiccup — log loudly and keep
        # serving. Whatever DDL succeeded before the failure already landed.
        print(f"[db_migrate] Schema migration failed: {e}")
