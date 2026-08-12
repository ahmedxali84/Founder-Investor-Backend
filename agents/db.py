"""
Shared Postgres connection helper for agents/db_migrate.py and
agents/db_store.py — both need the same DATABASE_URL cleanup, so it lives
in one place.
"""
import os
from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

# Query params some tools (Prisma, etc.) add to Supabase's pooler URL that
# plain libpq/psycopg2 doesn't recognize and will reject the whole DSN over.
_UNSUPPORTED_QUERY_PARAMS = {"pgbouncer"}


def get_database_url() -> str:
    raw = os.getenv("DATABASE_URL", "").strip()
    if not raw:
        return ""

    parsed = urlparse(raw)
    kept_params = [(k, v) for k, v in parse_qsl(parsed.query) if k not in _UNSUPPORTED_QUERY_PARAMS]
    cleaned = parsed._replace(query=urlencode(kept_params))
    return urlunparse(cleaned)


def get_connection():
    """
    Returns a new psycopg2 connection, or None if DATABASE_URL isn't set/usable.
    Always bounded by a short connect_timeout — psycopg2/libpq has NO timeout
    by default, so a wrong password/unreachable host can hang the TCP/auth
    handshake for a long time. Without this, a bad DATABASE_URL doesn't just
    fail a background save — it hangs the entire FastAPI startup event
    (run_schema_migrations runs there), taking the whole app down with it.
    """
    url = get_database_url()
    if not url:
        return None
    import psycopg2
    return psycopg2.connect(url, connect_timeout=5)
