import os
import re
import json
import asyncio
import uuid
import time
import hashlib
import ipaddress
import socket
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote, urlparse, urljoin
from typing import Optional
from fastapi import FastAPI, Form, HTTPException, BackgroundTasks, Depends, Header, UploadFile, File, Request
from fastapi.responses import JSONResponse, RedirectResponse, FileResponse
from fastapi.middleware.cors import CORSMiddleware
from dotenv import load_dotenv
import httpx

# Always load .env from the project root, regardless of where uvicorn is invoked
_root_env = Path(__file__).resolve().parent / ".env"
load_dotenv(dotenv_path=_root_env, override=True)


from agents import (
    run_agent1,
    extract_pdf_text,
    analyze_founder_profile,
    analyze_investor_profile,
    search_github_users,
    verify_repo_ownership,
    run_agent3,
    run_agent4,
    run_agent5,
    run_agent6_for_founder,
    run_agent6_for_investor,
    run_agent7,
)
import agents.llm as agents_llm
from agents.agent5 import match_score
from agents.data import IDEAS_POOL, INVESTORS_POOL
from agents import store

# Shared, process-wide marketplace pools. A founder's activated idea must be
# visible to every investor's session (and vice versa) — every SessionState
# used to get its own private `list(IDEAS_POOL)` copy, so nothing posted by
# one user was ever visible to any other user. These two lists are the single
# shared source of truth; SessionState.ideas_pool/investors_pool below just
# reference them directly instead of copying.
pool_lock = threading.Lock()
GLOBAL_IDEAS_POOL = list(IDEAS_POOL)
GLOBAL_INVESTORS_POOL = list(INVESTORS_POOL)

# Meeting-request opt-in state, shared the same way: a founder's request and
# an investor's raised hand are two sides of ONE (idea, investor) relationship
# and must land on the same record, not two private per-session dicts that
# never see each other (which is what made "both_opted_in" impossible to
# reach for two different real users — it only looked like it worked in
# same-session testing, where founder and investor state happened to be the
# same object).
meeting_lock = threading.Lock()
GLOBAL_MEETING_REQUESTS = {}  # f"{idea_id}::{investor_id}" -> {idea_id, investor_id, idea, investor, founder_requested, investor_raised, both_opted_in}


def _meeting_key(idea_id, investor_id):
    return f"{idea_id}::{investor_id}"
from agents import linkedin_oauth
from agents.db_migrate import run_schema_migrations
from agents import db_store
from agents import geo

app = FastAPI(title="TechFlixx Founder-Investor Network")

MAX_PDF_UPLOAD_BYTES = 15 * 1024 * 1024  # 15 MB — caps memory/disk use per upload


@app.exception_handler(Exception)
async def _json_on_unhandled_exception(request, exc):
    # Without this, an unhandled exception falls through to Starlette's default
    # plain-text 500, which breaks every frontend call site that does
    # `await res.json()` (they get "Unexpected end of JSON input").
    # The exception's own text is logged server-side only, never returned to
    # the client — str(exc) can carry internal file paths, query fragments,
    # or library-specific detail that's a debugging aid for us and a
    # reconnaissance aid for anyone probing the API.
    import traceback; traceback.print_exc()
    return JSONResponse(status_code=500, content={"detail": "Internal server error. Please try again."})


@app.on_event("startup")
async def _startup():
    # Order matters: schema migration must create the pool-durability tables
    # before load_sessions()/_hydrate_pools_from_db() can populate the
    # in-memory pools — load_sessions() used to run at plain module-import
    # time (before this startup event ever fired), so on a fresh database
    # hydration would have queried tables that didn't exist yet.
    run_schema_migrations()
    load_sessions()
    await _hydrate_pools_from_db()
    # Off the event loop since it's a real network call — best-effort, never
    # blocks/fails startup (see validate_configured_models's own docstring).
    await asyncio.to_thread(agents_llm.validate_configured_models)

# CORS middleware for React Vite frontend integration.
# allow_origins=["*"] combined with allow_credentials=True lets Starlette
# reflect any request's Origin header verbatim (a literal "*" can't be sent
# alongside credentials per the CORS spec), which would let any website make
# credentialed requests to this API. Restrict to known local dev origins,
# overridable via CORS_ORIGINS for other environments.
_cors_origins_env = os.getenv("CORS_ORIGINS", "").strip()
if not _cors_origins_env:
    _cors_origins = ["http://localhost:3000", "http://localhost:5173", "http://localhost:5174", "http://127.0.0.1:3000"]
elif _cors_origins_env == "*":
    _cors_origins = ["*"]
else:
    _cors_origins = [o.strip() for o in _cors_origins_env.split(",") if o.strip()]

app.add_middleware(
    CORSMiddleware,
    allow_origins=_cors_origins,
    allow_credentials=True if _cors_origins != ["*"] else False,
    allow_methods=["*"],
    allow_headers=["*"],
    # Always allow *.vercel.app regardless of an ENVIRONMENT env var being
    # set — gating this behind ENVIRONMENT == "production" meant an unset
    # ENVIRONMENT var (as on Render, where nothing sets it) silently
    # rejected every request from the deployed Vercel frontend with
    # "Disallowed CORS origin", surfacing to users as a raw "Failed to
    # fetch". The regex is already scoped to vercel.app subdomains, so it
    # doesn't need an extra env-var gate to stay safe.
    allow_origin_regex=r"https://.*\.vercel\.app",
)

_VERCEL_ORIGIN_RE = re.compile(r"^https://[a-zA-Z0-9.-]+\.vercel\.app$")


def _trusted_frontend_origin(origin: str) -> Optional[str]:
    """
    Same trust boundary as the CORS config above (explicit _cors_origins list
    plus any *.vercel.app subdomain) — reused so the LinkedIn OAuth callback
    can redirect back to whichever frontend origin actually initiated the
    flow instead of a single hardcoded/env-configured URL that's easy to
    leave unset or stale in a given deployment.
    """
    origin = (origin or "").rstrip("/")
    if not origin:
        return None
    if origin in _cors_origins or _VERCEL_ORIGIN_RE.match(origin):
        return origin
    return None


# Lightweight, dependency-free abuse guard + browser hardening headers. Not a
# substitute for a real edge/WAF rate limiter in front of a production
# deployment, but a meaningful floor for this app running as-is. Per-IP
# sliding window; generous enough for this app's own polling (several
# widgets poll every 8s) while still capping scripted abuse.
_RATE_LIMIT_WINDOW_SECONDS = 60
_RATE_LIMIT_MAX_REQUESTS = 300
_rate_limit_hits: dict = {}  # client_ip -> [timestamps within the current window]
_RATE_LIMIT_SWEEP_EVERY = 500  # requests between full-dict prunes
_rate_limit_request_count = 0


@app.middleware("http")
async def _security_and_rate_limit_middleware(request, call_next):
    global _rate_limit_request_count
    client_ip = request.client.host if request.client else "unknown"
    now = time.time()
    cutoff = now - _RATE_LIMIT_WINDOW_SECONDS
    hits = _rate_limit_hits.setdefault(client_ip, [])
    while hits and hits[0] < cutoff:
        hits.pop(0)
    if len(hits) >= _RATE_LIMIT_MAX_REQUESTS:
        return JSONResponse(status_code=429, content={"detail": "Too many requests — please slow down."})
    hits.append(now)

    # An IP's own list only gets trimmed when THAT IP makes another request —
    # an IP that goes idle for good (the common case, not the abuse case)
    # would otherwise leave a small stale entry here for the rest of the
    # process's life, growing unbounded with every distinct visitor ever
    # seen. Periodically sweep the whole dict instead of relying on that.
    _rate_limit_request_count += 1
    if _rate_limit_request_count >= _RATE_LIMIT_SWEEP_EVERY:
        _rate_limit_request_count = 0
        for ip, ip_hits in list(_rate_limit_hits.items()):
            while ip_hits and ip_hits[0] < cutoff:
                ip_hits.pop(0)
            if not ip_hits:
                _rate_limit_hits.pop(ip, None)

    response = await call_next(request)

    # Standard hardening headers — cheap to set, cuts off several common
    # browser-side attack classes (clickjacking, MIME-sniffing, referrer
    # leakage, unwanted feature access) even though this API is mostly
    # consumed by our own frontend rather than rendering HTML itself.
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
    response.headers["Permissions-Policy"] = "geolocation=(), camera=(), microphone=()"
    response.headers["Strict-Transport-Security"] = "max-age=63072000; includeSubDomains"
    return response

# Static / template setup
os.makedirs("static", exist_ok=True)
os.makedirs("static/resumes", exist_ok=True)
os.makedirs("temp", exist_ok=True)

# Resumes are served through GET /api/resume/{owner_user_id} (auth required)
# instead of a public StaticFiles mount — a bare /static/resumes/<filename>
# mount has no concept of "logged in", so anyone who ever saw a resume URL
# (a matched investor's network tab, browser history, a forwarded link) could
# download it forever with no session at all. The filename itself was never
# meant to be the access control.

# ---------------------------------------------------------------------------
# Session State Model
# ---------------------------------------------------------------------------
class SessionState:
    def __init__(self):
        self.reset()

    def reset(self):
        self.session_id: str = str(uuid.uuid4())       # tags every saved agent profile for this run
        self.user_type: Optional[str] = None          # "founder" | "investor"
        self.founder_profile: Optional[dict] = None
        self.investor_profile: Optional[dict] = None
        self.idea_details: Optional[dict] = None
        self.ideas_pool = GLOBAL_IDEAS_POOL
        self.investors_pool = GLOBAL_INVESTORS_POOL
        self.shortlisted_ideas: list = []
        self.shortlisted_investors: list = []
        self.matched_pairs: list = []
        self.current_match: Optional[dict] = None
        self.rejected_ids: set = set()
        self.chat_history: list = []
        self.resume_path: Optional[str] = None
        self.founder_raw_input: Optional[dict] = None   # {"linkedin", "github", "additional"}
        self.idea_raw_text: Optional[str] = None         # only set when the idea was submitted as raw text (not PDF)
        self.investor_raw_input: Optional[dict] = None   # {"linkedin", "additional"}
        self.mvp_url: Optional[str] = None
        self.mvp_url_valid: bool = False
        # meeting-request state lives in GLOBAL_MEETING_REQUESTS now, shared
        # across founder/investor sessions (see comment near that dict).
        self.active_connections: list = []  # list of investor dicts
        self.agent_ready: bool = False       # True once background pipeline finishes
        self.linkedin_verified: Optional[dict] = None  # real OAuth-confirmed LinkedIn identity
        self.linkedin_oauth_state: Optional[str] = None  # CSRF state for the in-flight OAuth handshake
        self.active_idea_id: Optional[str] = None  # which ideas_pool entry is this founder's "live" idea
        self.idea_ever_activated: bool = False  # set the instant /api/founder/idea/activate is called,
        # before the background Agent 1/4/5 pipeline runs — lets founder_status
        # tell "pipeline currently in flight" apart from "recovered after a
        # restart, no idea ever submitted" (see founder_status below).
        self.investor_pipeline_started: bool = False  # same idea as idea_ever_activated,
        # but for /api/investor/profile + investor_status's equivalent guard.
        self.last_deal_terms: Optional[dict] = None  # {"equity_percent", "amount_usd"} from the
        # most recent /api/agreement call — reused by the Agents Dashboard's
        # "rerun agent7" button, which has no term-sheet form of its own.

    def to_dict(self) -> dict:
        return {
            "session_id": self.session_id,
            "user_type": self.user_type,
            "founder_profile": self.founder_profile,
            "investor_profile": self.investor_profile,
            "idea_details": self.idea_details,
            # ideas_pool/investors_pool are process-wide shared state now (see
            # GLOBAL_IDEAS_POOL/GLOBAL_INVESTORS_POOL) — saved separately in
            # save_sessions(), not duplicated per user here.
            "shortlisted_ideas": self.shortlisted_ideas,
            "shortlisted_investors": self.shortlisted_investors,
            "matched_pairs": self.matched_pairs,
            "current_match": self.current_match,
            "rejected_ids": list(self.rejected_ids),
            "chat_history": self.chat_history,
            "resume_path": self.resume_path,
            "founder_raw_input": self.founder_raw_input,
            "idea_raw_text": self.idea_raw_text,
            "investor_raw_input": self.investor_raw_input,
            "mvp_url": self.mvp_url,
            "mvp_url_valid": self.mvp_url_valid,
            "active_connections": self.active_connections,
            "agent_ready": self.agent_ready,
            "linkedin_verified": self.linkedin_verified,
            "active_idea_id": self.active_idea_id,
            "idea_ever_activated": self.idea_ever_activated,
            "investor_pipeline_started": self.investor_pipeline_started,
            "last_deal_terms": self.last_deal_terms,
        }

    def from_dict(self, d: dict):
        self.session_id = d.get("session_id", str(uuid.uuid4()))
        self.user_type = d.get("user_type")
        self.founder_profile = d.get("founder_profile")
        self.investor_profile = d.get("investor_profile")
        self.idea_details = d.get("idea_details")
        # Always point at the shared process-wide pools (see GLOBAL_IDEAS_POOL
        # / GLOBAL_INVESTORS_POOL) rather than restoring a private copy.
        self.ideas_pool = GLOBAL_IDEAS_POOL
        self.investors_pool = GLOBAL_INVESTORS_POOL
        self.shortlisted_ideas = d.get("shortlisted_ideas", [])
        self.shortlisted_investors = d.get("shortlisted_investors", [])
        self.matched_pairs = d.get("matched_pairs", [])
        self.current_match = d.get("current_match")
        self.rejected_ids = set(d.get("rejected_ids", []))
        self.chat_history = d.get("chat_history", [])
        self.resume_path = d.get("resume_path")
        self.founder_raw_input = d.get("founder_raw_input")
        self.idea_raw_text = d.get("idea_raw_text")
        self.investor_raw_input = d.get("investor_raw_input")
        self.mvp_url = d.get("mvp_url")
        self.mvp_url_valid = d.get("mvp_url_valid", False)
        self.active_connections = d.get("active_connections", [])
        self.agent_ready = d.get("agent_ready", False)
        self.linkedin_verified = d.get("linkedin_verified")
        self.active_idea_id = d.get("active_idea_id")
        self.idea_ever_activated = d.get("idea_ever_activated", False)
        self.investor_pipeline_started = d.get("investor_pipeline_started", False)
        self.last_deal_terms = d.get("last_deal_terms")


# Multi-User Persistent Session Stores
user_states = {}
state_lock = threading.Lock()
# SESSIONS_DB_FILE lets tests redirect this to a throwaway temp file before
# any test imports main_app — several code paths (_ensure_active_idea_id,
# various endpoints) call save_sessions() as a side effect of otherwise-pure
# logic, and without this override those calls silently write real session
# data into the repo's actual sessions_db.json during a test run.
DB_FILE = os.getenv("SESSIONS_DB_FILE") or "sessions_db.json"

# In-memory map of in-flight LinkedIn OAuth "state" tokens ->
# (user_id, created_at, frontend_origin). LinkedIn's redirect back to
# /api/linkedin/callback is a plain browser navigation with no Authorization
# header, so this is how we know which app user just proved who they are on
# LinkedIn — and frontend_origin (captured from the Origin/Referer of the
# /api/linkedin/login request that started this flow) is how the callback
# knows where to redirect back to, instead of relying on a single
# env-configured FRONTEND_URL that's easy to leave unset in a given
# deployment. Entries are removed once the handshake completes;
# _purge_stale_oauth_states() also sweeps out anyone who abandoned the flow
# (closed the tab, denied access) so this dict can't grow without bound over
# the process's lifetime.
linkedin_oauth_states = {}
_OAUTH_STATE_TTL_SECONDS = 600  # 10 minutes is generous for a login redirect round-trip


def _purge_stale_oauth_states():
    cutoff = time.time() - _OAUTH_STATE_TTL_SECONDS
    stale = [tok for tok, (_, created_at, _fo) in linkedin_oauth_states.items() if created_at < cutoff]
    for tok in stale:
        linkedin_oauth_states.pop(tok, None)

def save_sessions():
    with state_lock, pool_lock, meeting_lock:
        try:
            data = {
                "_global_ideas_pool": GLOBAL_IDEAS_POOL,
                "_global_investors_pool": GLOBAL_INVESTORS_POOL,
                "_global_meeting_requests": GLOBAL_MEETING_REQUESTS,
                "_users": {uid: state.to_dict() for uid, state in user_states.items()},
            }
            with open(DB_FILE, "w") as f:
                json.dump(data, f, default=str, indent=2)
        except Exception as e:
            print(f"Failed to save sessions: {e}")

def load_sessions():
    global user_states
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r") as f:
                data = json.load(f)
            with state_lock, pool_lock, meeting_lock:
                if "_users" in data:
                    GLOBAL_IDEAS_POOL[:] = data.get("_global_ideas_pool", GLOBAL_IDEAS_POOL)
                    GLOBAL_INVESTORS_POOL[:] = data.get("_global_investors_pool", GLOBAL_INVESTORS_POOL)
                    GLOBAL_MEETING_REQUESTS.clear()
                    GLOBAL_MEETING_REQUESTS.update(data.get("_global_meeting_requests", {}))
                    users_data = data["_users"]
                else:
                    # Legacy file format (pre-shared-pools) — per-user data
                    # lived at the top level; global pools default to the
                    # seed data since no shared copy existed yet.
                    users_data = data
                for uid, d in users_data.items():
                    state = SessionState()
                    state.from_dict(d)
                    user_states[uid] = state
        except Exception as e:
            print(f"Failed to load sessions: {e}")


async def _hydrate_pools_from_db():
    """
    Merges Postgres's durable copy of the shared pools into whatever
    load_sessions() just populated from sessions_db.json, so a crashed/
    restarted process doesn't lose data that only made it into the JSON
    file's last periodic snapshot. Postgres wins on an id collision (a
    write-through write is always at least as fresh as the last JSON save);
    anything present only in the JSON file — realistically just the first
    time this code runs against an existing sessions_db.json, before
    anything has been written through yet — gets backfilled into Postgres so
    every later restart converges through the same merge rule instead of
    needing a separate one-time migration step.
    """
    if not db_store.get_database_url():
        print("[startup] DATABASE_URL not set — using sessions_db.json only.")
        return

    pg_ideas = await asyncio.to_thread(db_store.load_all_ideas)
    pg_investors = await asyncio.to_thread(db_store.load_all_investors)
    pg_meetings = await asyncio.to_thread(db_store.load_all_meeting_requests)

    backfill_ideas = []
    backfill_investors = []
    backfill_meetings = []

    with pool_lock:
        json_ideas_by_id = {i["id"]: i for i in GLOBAL_IDEAS_POOL if i.get("id")}
        pg_ideas_by_id = {i["id"]: i for i in pg_ideas if i.get("id")}
        backfill_ideas = [i for iid, i in json_ideas_by_id.items() if iid not in pg_ideas_by_id]
        merged_ideas = {**json_ideas_by_id, **pg_ideas_by_id}
        GLOBAL_IDEAS_POOL[:] = list(merged_ideas.values())

        json_investors_by_id = {i["id"]: i for i in GLOBAL_INVESTORS_POOL if i.get("id")}
        pg_investors_by_id = {i["id"]: i for i in pg_investors if i.get("id")}
        backfill_investors = [i for iid, i in json_investors_by_id.items() if iid not in pg_investors_by_id]
        merged_investors = {**json_investors_by_id, **pg_investors_by_id}
        GLOBAL_INVESTORS_POOL[:] = list(merged_investors.values())

    with meeting_lock:
        pg_meetings_by_key = {_meeting_key(m["idea_id"], m["investor_id"]): m for m in pg_meetings}
        backfill_meetings = [
            slot for key, slot in GLOBAL_MEETING_REQUESTS.items() if key not in pg_meetings_by_key
        ]
        merged_meetings = {**GLOBAL_MEETING_REQUESTS, **pg_meetings_by_key}
        GLOBAL_MEETING_REQUESTS.clear()
        GLOBAL_MEETING_REQUESTS.update(merged_meetings)

    if backfill_ideas or backfill_investors or backfill_meetings:
        for idea in backfill_ideas:
            await asyncio.to_thread(db_store.save_idea, idea)
        for investor in backfill_investors:
            await asyncio.to_thread(db_store.save_investor, investor)
        for slot in backfill_meetings:
            await asyncio.to_thread(db_store.save_meeting_request, slot)
        print(
            f"[startup] Backfilled {len(backfill_ideas)} ideas, {len(backfill_investors)} investors, "
            f"{len(backfill_meetings)} meeting requests from sessions_db.json into Postgres."
        )

    # Unconditional, not just on backfill — this is also what makes a
    # Postgres-only record (recovered after a crash that happened before
    # the last sessions_db.json save, exactly the scenario this whole
    # durability layer exists for) actually show up in the JSON fallback
    # file too, not just in the live in-memory pool.
    save_sessions()


def get_user_state(user_id: str) -> SessionState:
    with state_lock:
        if user_id not in user_states:
            state = SessionState()
            if os.path.exists(DB_FILE):
                try:
                    with open(DB_FILE, "r") as f:
                        data = json.load(f)
                    users_data = data.get("_users", data) if isinstance(data, dict) else {}
                    if user_id in users_data and isinstance(users_data[user_id], dict):
                        state.from_dict(users_data[user_id])
                except Exception:
                    pass
            user_states[user_id] = state
    return user_states[user_id]


# ---------------------------------------------------------------------------
# Supabase Token Verification Dependency
# ---------------------------------------------------------------------------

# Cache successful token verifications for a short TTL, keyed by a hash of
# the token (never the raw token itself, so a memory dump can't recover live
# credentials). This is what actually matters here from a caching
# perspective — every protected endpoint depends on get_current_user_id, so
# without this every single request round-trips to Supabase's /auth/v1/user
# just to re-confirm a session that was already confirmed seconds ago. Only
# successes are cached: a failed/expired check is never cached, so a
# transient Supabase hiccup or an actual logout still take effect immediately
# rather than being masked for the rest of the TTL window.
_TOKEN_VERIFY_TTL_SECONDS = 60
_token_verify_cache: dict = {}  # sha256(token) -> (cached_at, user_data)
# Swept on every write (see _purge_stale_token_cache below), not just on
# access of the same key — Supabase issues a brand-new JWT on every session
# refresh, so most entries here are never looked up again by their own key
# once expired. Without a proactive sweep this dict would grow without bound
# for the life of the process (same failure mode _purge_stale_oauth_states
# already guards against for linkedin_oauth_states).
_LAST_TOKEN_CACHE_PURGE = [0.0]  # mutable single-element list so it's assignable from a nested function


def _purge_stale_token_cache():
    now = time.time()
    if now - _LAST_TOKEN_CACHE_PURGE[0] < _TOKEN_VERIFY_TTL_SECONDS:
        return
    _LAST_TOKEN_CACHE_PURGE[0] = now
    cutoff = now - _TOKEN_VERIFY_TTL_SECONDS
    stale = [k for k, (cached_at, _) in _token_verify_cache.items() if cached_at < cutoff]
    for k in stale:
        _token_verify_cache.pop(k, None)


async def get_supabase_user(token: str) -> Optional[dict]:
    supabase_url = os.getenv("SUPABASE_URL")
    supabase_key = os.getenv("SUPABASE_ANON_KEY")
    if not supabase_url or not supabase_key:
        return None

    cache_key = hashlib.sha256(token.encode()).hexdigest()
    cached = _token_verify_cache.get(cache_key)
    if cached:
        if time.time() - cached[0] < _TOKEN_VERIFY_TTL_SECONDS:
            return cached[1]
        # Expired — drop it now rather than just falling through to
        # overwrite it below.
        _token_verify_cache.pop(cache_key, None)

    url = f"{supabase_url.rstrip('/')}/auth/v1/user"
    headers = {
        "Authorization": f"Bearer {token}",
        "apikey": supabase_key
    }
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code == 200:
                user_data = resp.json()
                _token_verify_cache[cache_key] = (time.time(), user_data)
                _purge_stale_token_cache()
                return user_data
    except Exception as e:
        print(f"Supabase auth check failed: {e}")
    return None


async def get_current_user_id(authorization: Optional[str] = Header(None)) -> str:
    """
    Every protected route depends on this. It used to fall back to a shared
    "default_user" identity whenever a token was missing, malformed, expired,
    or Supabase's verification call itself failed for any reason (including a
    transient network blip) — meaning an unauthenticated caller, or literally
    anyone during a brief Supabase hiccup, got full API access under one
    identity shared by every other anonymous/failed caller at the same time.
    That's an authentication bypass, not a convenience fallback, so any
    failure here now rejects the request outright instead of silently
    downgrading it to a shared guest identity.
    """
    if not authorization:
        raise HTTPException(status_code=401, detail="Authentication required.")

    token = authorization[7:] if authorization.startswith("Bearer ") else authorization
    if not token or token in ("null", "undefined"):
        raise HTTPException(status_code=401, detail="Authentication required.")

    user_data = await get_supabase_user(token)
    if not user_data or "id" not in user_data:
        raise HTTPException(status_code=401, detail="Invalid or expired session — please sign in again.")
    return user_data["id"]


async def _get_auth_display_name(authorization: Optional[str]) -> str:
    """
    Agent 2 can only extract a name from real supplied text (a pasted
    LinkedIn bio, or a verified OAuth login) — someone who just types a bare
    LinkedIn URL with no bio text gives it nothing to work with, so `name`
    comes back empty. That's silently correct behavior for Agent 2 (better
    than inventing a name), but it left onboarded profiles with no name at
    all anywhere downstream — the founder-side "confirmed meeting" card, the
    profile page, etc. all rendered a bare "An investor"/"?" placeholder even
    once a real meeting was confirmed. Supabase already has a reliable name
    for every account (typed at signup, or the real one from Google OAuth) —
    this reuses the get_current_user_id dependency's own token-verification
    cache (same token, same request), so it's not a second real network call.
    """
    if not authorization:
        return ""
    token = authorization[7:] if authorization.startswith("Bearer ") else authorization
    if not token or token in ("null", "undefined"):
        return ""
    user_data = await get_supabase_user(token)
    if not user_data:
        return ""
    full_name = (user_data.get("user_metadata") or {}).get("full_name")
    if full_name:
        return full_name.strip()
    email = user_data.get("email") or ""
    return email.split("@")[0] if email else ""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def _merge_mvp_readiness(shortlisted_ideas: list, checked_ideas: list):
    """
    Agent 5's `checked_ideas` return value carries mvp_completion/mvp_status/
    mvp_ready, but callers only kept `matches` — investor-facing cards need
    these fields on the actual shortlist entries, so merge them back in place.
    """
    readiness_by_id = {
        c["id"]: {k: c[k] for k in ("mvp_completion", "mvp_status", "mvp_ready") if k in c}
        for c in checked_ideas
        if "id" in c
    }
    for idea in shortlisted_ideas:
        readiness = readiness_by_id.get(idea.get("id"))
        if readiness:
            idea.update(readiness)


async def _hydrate_from_db(state, user_id: str):
    """
    If this in-memory SessionState doesn't know who this user is yet (fresh
    process, or the local sessions_db.json cache was lost), check the real
    Postgres-backed profile tables before giving up — this is what lets a
    returning founder/investor skip LinkedIn/GitHub onboarding entirely
    instead of being asked again just because the Python process restarted.

    Runs the psycopg2 calls (synchronous, blocking network I/O) via
    asyncio.to_thread — this used to run them directly on the event loop, so
    ANY slow/unreachable Postgres connection attempt (e.g. transient DNS/
    network trouble on the pooler host) froze every other concurrent
    request on the whole server, not just this one, for as long as libpq
    took to give up. That's a much bigger blast radius than one slow
    request, so it's now isolated to a worker thread.
    """
    if state.user_type:
        return  # already have something in memory, nothing to do

    founder_row = await asyncio.to_thread(db_store.load_founder_profile, user_id)
    if founder_row:
        state.user_type = "founder"
        state.founder_profile = founder_row["profile"]
        state.founder_raw_input = {"linkedin": founder_row["linkedin"], "github": founder_row["github"]}
        return

    investor_row = await asyncio.to_thread(db_store.load_investor_profile, user_id)
    if investor_row:
        state.user_type = "investor"
        state.investor_profile = investor_row["profile"]
        state.investor_raw_input = {"linkedin": investor_row["linkedin"]}


def _is_valid_url(url: str) -> bool:
    """Basic format check — must start with http/https and have a domain."""
    pattern = re.compile(
        r'^https?://'
        r'(?:[A-Za-z0-9\-]+\.)+[A-Za-z]{2,}'
        r'(?:/[^\s]*)?$'
    )
    return bool(pattern.match(url.strip()))


def _is_public_hostname(hostname: str) -> bool:
    """
    Resolves hostname and rejects it unless every address it resolves to is
    an ordinary public IP — private/loopback/link-local (which covers cloud
    metadata endpoints like 169.254.169.254), multicast, reserved, and
    unspecified are all refused. Without this, _reachable_url's outbound
    request is a blind SSRF/internal-network-scanning oracle: any
    authenticated founder could point mvp_url at internal infrastructure —
    either a literal internal IP, or an attacker-controlled domain that
    simply resolves to one — and read back whether it's alive.
    Note: this doesn't fully close DNS-rebinding (a record that resolves
    differently between this check and httpx's own connection a moment
    later) — a residual risk, not one this app's threat model prioritizes
    closing further right now.
    """
    try:
        infos = socket.getaddrinfo(hostname, None)
    except socket.gaierror:
        return False
    if not infos:
        return False
    for info in infos:
        try:
            ip = ipaddress.ip_address(info[4][0])
        except ValueError:
            return False
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_multicast or ip.is_reserved or ip.is_unspecified:
            return False
    return True


async def _reachable_url(url: str) -> bool:
    """
    Quick HEAD request to check if the MVP URL is reachable — only after
    confirming the hostname resolves exclusively to public addresses (see
    _is_public_hostname). Redirects are followed manually, one hop at a
    time, re-validating the target host on every hop instead of letting
    httpx's follow_redirects silently land on an internal address that
    passed no check of its own.
    """
    try:
        current_url = url
        for _ in range(5):
            hostname = urlparse(current_url).hostname
            if not hostname or not _is_public_hostname(hostname):
                return False
            async with httpx.AsyncClient(timeout=6.0, follow_redirects=False) as client:
                resp = await client.head(current_url)
            if resp.status_code in (301, 302, 303, 307, 308) and resp.headers.get("location"):
                current_url = urljoin(current_url, resp.headers["location"])
                continue
            return resp.status_code < 500
        return False
    except Exception:
        return False


async def _ensure_conversation(founder_user_id: Optional[str], investor_user_id: Optional[str], idea_ref: Optional[str] = None):
    """
    Creates the Supabase `conversations` row via the service-role key
    (bypasses RLS) the instant both sides opt in — the browser's anon-key
    client is intentionally no longer allowed to insert conversations itself
    (see frontend/supabase/schema.sql): its old insert policy only checked
    "are you one of the two named parties," never "did a real confirmed
    meeting actually happen," which let any authenticated user open a chat
    with anyone just by naming them as the counterpart. This is the one
    place that's allowed to create a conversation now, and only after
    both_opted_in is genuinely true.
    """
    supabase_url = os.getenv("SUPABASE_URL")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not supabase_url or not service_key or not founder_user_id or not investor_user_id:
        return  # best-effort — chat just shows its "not set up" notice if this never ran
    try:
        headers = {
            "apikey": service_key,
            "Authorization": f"Bearer {service_key}",
            "Content-Type": "application/json",
            "Prefer": "resolution=ignore-duplicates",
        }
        async with httpx.AsyncClient(timeout=6.0) as client:
            resp = await client.post(
                f"{supabase_url.rstrip('/')}/rest/v1/conversations",
                headers=headers,
                # PostgREST's ignore-duplicates upsert only no-ops on a conflict
                # if told which constraint to match against — the unique
                # constraint here is (founder_user_id, investor_user_id), not
                # the primary key, so on_conflict must name it explicitly or
                # this 409s instead of silently succeeding on a repeat call.
                params={"on_conflict": "founder_user_id,investor_user_id"},
                json={
                    "founder_user_id": founder_user_id,
                    "investor_user_id": investor_user_id,
                    "idea_ref": idea_ref,
                },
            )
            if resp.status_code >= 300:
                print(f"Failed to pre-create conversation ({resp.status_code}): {resp.text}")
    except Exception as e:
        print(f"Failed to pre-create conversation: {e}")


def _run_founder_onboarding_pipeline(user_id: str, profile_data: dict):
    """
    Onboarding no longer collects an idea — this just polishes the resume
    (Agent 3) so the founder profile is ready. Idea submission/activation is
    a separate step (see _run_idea_activation_pipeline) since a founder can
    post several ideas and only one is ever "live" for matching.
    """
    state = get_user_state(user_id)
    session_ref = state.session_id
    try:
        try:
            state.resume_path = store.run_and_record(
                "agent3", user_id, session_ref, run_agent3, profile_data,
                output_dir="static/resumes", unique_id=user_id,
            )
        except Exception:
            state.resume_path = None

        state.agent_ready = True
        save_sessions()
    except Exception:
        import traceback
        traceback.print_exc()
        state.agent_ready = True
        save_sessions()


def _run_idea_activation_pipeline(user_id: str, idea_text: str, mvp_url: str, repo_url: str, screenshot_url: str, founder_title: str = ""):
    """
    Runs when a founder marks one of their posted ideas "live". Verifies the
    MVP repo actually belongs to their connected GitHub account, runs Agent 1
    on the idea text, replaces (not appends) this founder's previous live
    idea in the shared pool, then re-runs Agent 4/5 so current_match reflects
    the new idea.
    """
    state = get_user_state(user_id)
    session_ref = state.session_id
    profile_data = state.founder_profile or {}
    # active_idea_id can be stale/missing here (e.g. re-onboarding reset it)
    # even though this founder's idea is still sitting in the shared pool —
    # without healing it first, the "reuse existing id" lookup below misses
    # it and appends a brand-new duplicate idea instead of replacing theirs.
    _ensure_active_idea_id(state, user_id)
    try:
        github_username = (profile_data.get("github_insights") or {}).get("username", "")
        repo_verified = verify_repo_ownership(github_username, repo_url) if repo_url else False

        agent1_res = None
        try:
            agent1_res = store.run_and_record("agent1", user_id, session_ref, run_agent1, idea_text, is_raw_text=True)
        except Exception:
            agent1_res = None

        new_idea = None
        if agent1_res is None:
            state.idea_details = {"error": "Agent 1 failed to process the idea. See the agents dashboard for details."}
        elif not agent1_res.get("is_idea", False):
            state.idea_details = {"error": agent1_res.get("rejection_reason", "Invalid idea document.")}
        else:
            state.idea_details = agent1_res

            if founder_title.strip():
                # The founder's own title, typed in the "Post New Idea" form
                # (pitch_posts.title) — this used to never reach this function
                # at all, so every idea got a nonsense fallback name instead
                # (e.g. "A AI") stitched from the first word of Agent 1's
                # extracted solution text, regardless of what the founder
                # actually called their own startup.
                title = founder_title.strip()
            else:
                extracted_solution = agent1_res["idea"]["solution"]
                solution_words = extracted_solution.split()
                if solution_words:
                    base_word = solution_words[0].rstrip('.,;')
                    title = (base_word + " AI") if "ai" not in extracted_solution.lower() else base_word
                else:
                    title = profile_data.get("name", "My Startup")

            # resume_path may currently be unset even for a founder with a
            # real resume — /api/resume/{owner_user_id} rebuilds it on
            # demand from founder_profile if the file didn't survive a
            # redeploy, so expose the URL whenever either is present.
            resume_url = f"/api/resume/{user_id}" if (state.resume_path or state.founder_profile) else None

            # Reuse this founder's existing idea id if they already had a live
            # one, so activating a different idea REPLACES it instead of
            # appending a duplicate into the shared pool. The lookup, id
            # assignment, and pool mutation all happen under one lock since
            # ideas_pool is now shared across every founder's background task.
            with pool_lock:
                existing = next((i for i in state.ideas_pool if i.get("id") == state.active_idea_id), None)
                new_id = existing["id"] if existing else f"idea_{len(state.ideas_pool) + 1:02d}"

                has_screenshot = bool(screenshot_url)
                has_mvp_url = bool(mvp_url) and state.mvp_url_valid
                mvp_warnings = []
                if not has_screenshot:
                    mvp_warnings.append("Missing MVP screenshot/image")
                if not has_mvp_url:
                    mvp_warnings.append("Missing or unverified MVP URL")

                feasibility_base = 85 if profile_data.get("experience") == "Expert" else 70
                if not has_screenshot:
                    feasibility_base = max(40, feasibility_base - 15)
                if not has_mvp_url:
                    feasibility_base = max(35, feasibility_base - 10)

                new_idea = {
                    "id": new_id,
                    "title": title,
                    # Was profile_data.get("specialization", "SaaS") — the
                    # startup's business domain was being set to the
                    # FOUNDER's personal tech specialization (the same value
                    # that also populates founder.specialization two lines
                    # below), not the idea's actual category. That made
                    # match_score's sector-fit check (idea_domain in
                    # investor.focus_sectors, 40 of 100 points) compare a
                    # skills string like "Fullstack, AI, Data Analysis"
                    # against an investor's clean focus sectors like ["AI",
                    # "SaaS"] — never matching, so the single largest
                    # component of every match score silently scored zero.
                    # Now sourced from Agent 1's own domain classification.
                    "domain": agent1_res["idea"].get("domain") or "SaaS",
                    "problem": agent1_res["idea"]["problem"],
                    "solution": agent1_res["idea"]["solution"],
                    "target_market": agent1_res["idea"]["target_market"],
                    "features_must_have": agent1_res["mvp"]["must_have"],
                    "features_nice_to_have": agent1_res["mvp"]["nice_to_have"],
                    "roadmap": agent1_res["roadmap"]["steps"],
                    "mvp_warning": " | ".join(mvp_warnings) if mvp_warnings else None,
                    "founder": {
                        "name": profile_data.get("name", "Founder"),
                        "github": profile_data.get("github", ""),
                        "linkedin": profile_data.get("linkedin", ""),
                        "specialization": profile_data.get("specialization", "Generalist"),
                        "skills": profile_data.get("skills", []),
                        "experience": profile_data.get("experience", "Intermediate"),
                        "resume_summary": profile_data.get("resume_summary", ""),
                        "education": profile_data.get("education", ""),
                        "custom_details": profile_data.get("custom_details", {}),
                        "mvp_url": mvp_url or "",
                        "repo_url": repo_url or "",
                        "repo_verified": repo_verified,
                        "screenshot_url": screenshot_url or "",
                        "resume_url": resume_url,
                        "github_insights": profile_data.get("github_insights", {}),
                        "linkedin_verified": profile_data.get("linkedin_verified"),
                        "location": profile_data.get("location", {}),
                    },
                    "owner_user_id": user_id,
                    "base_scores": {
                        "potential": 90,
                        "feasibility": feasibility_base,
                        "market_fit": 80 if (has_screenshot and has_mvp_url) else 65,
                    },
                }

                if existing:
                    state.ideas_pool[state.ideas_pool.index(existing)] = new_idea
                else:
                    state.ideas_pool.append(new_idea)
            state.active_idea_id = new_id
            # Durability write-through — best-effort, matches the pool
            # itself (never blocks/raises on failure). Outside pool_lock:
            # this is a network call, and new_idea is a plain local dict at
            # this point, safe to read without the lock held.
            db_store.save_idea(new_idea)

        # Agent 4: Shortlist — excludes investors this founder already passed
        # on. Agent 4/5 have no notion of rejected_ids themselves (only
        # Agent 6's explicit "next best" flow does), so a fresh shortlist
        # here would otherwise resurface an investor right after rejecting
        # them, the moment a new idea is posted or the pool changes.
        # Snapshot under pool_lock — this runs on a real worker thread
        # (BackgroundTasks/asyncio.to_thread), so iterating the live shared
        # list while another thread's `with pool_lock: POOL[:] = ...`
        # reassignment is mid-flight is a genuine cross-thread hazard, not
        # just GIL trivia.
        with pool_lock:
            investors_snapshot = list(state.investors_pool)
        candidate_investors = [inv for inv in investors_snapshot if inv.get("id") not in state.rejected_ids]
        top_ideas, top_investors = [], []
        try:
            top_ideas, top_investors, _ = store.run_and_record(
                "agent4", user_id, session_ref, run_agent4, state.ideas_pool, candidate_investors, limit=25
            )
            state.shortlisted_ideas = top_ideas
            state.shortlisted_investors = top_investors
        except Exception:
            state.shortlisted_ideas, state.shortlisted_investors = [], []

        # Agent 5: Match
        matches = []
        try:
            matches, checked_ideas, _ = store.run_and_record("agent5", user_id, session_ref, run_agent5, top_ideas, top_investors)
            state.matched_pairs = matches
            _merge_mvp_readiness(state.shortlisted_ideas, checked_ideas)
        except Exception:
            state.matched_pairs = []

        # This founder is only ever shown THEIR single exclusive match — no
        # browsable shortlist grid — so find (or synthesize) exactly one.
        my_match = None
        if new_idea:
            my_match = next((m for m in matches if m["idea"]["id"] == new_idea["id"]), None)
        if not my_match and matches:
            my_match = matches[0]
        elif not my_match and top_investors:
            fallback_idea = new_idea or (top_ideas[0] if top_ideas else None)
            if fallback_idea:
                my_match = {
                    "idea": fallback_idea,
                    "investor": top_investors[0],
                    "score": 85.0,
                    "reason": "Best available match",
                }
        state.current_match = my_match

        state.agent_ready = True
        save_sessions()

    except Exception:
        import traceback
        traceback.print_exc()
        state.agent_ready = True
        save_sessions()


def _run_investor_matching(user_id: str, profile_data: dict, investor_id: str):
    """
    (Re)runs Agent 4 (shortlist) and Agent 5 (match) against the CURRENT
    shared ideas pool. Shared by onboarding and the "Refresh Shortlist"
    action — an investor's shortlist/current_match used to be computed once
    at onboarding and never again, so a founder who went live afterward
    would never show up for them without fully re-onboarding. Mirrors the
    founder side's "Refresh matches" button, which already re-runs its own
    pipeline on demand for the same reason.
    """
    state = get_user_state(user_id)
    session_ref = state.session_id

    # Agent 4: Shortlist — excludes ideas this investor already passed on
    # (see the matching comment in _run_idea_activation_pipeline). Snapshot
    # both pools under pool_lock — this runs on a real worker thread, so an
    # unlocked read can race a concurrent `with pool_lock: POOL[:] = ...`
    # write on another thread.
    with pool_lock:
        ideas_snapshot = list(state.ideas_pool)
        investors_snapshot = list(state.investors_pool)
    candidate_ideas = [i for i in ideas_snapshot if i.get("id") not in state.rejected_ids]
    top_ideas, top_investors = [], []
    try:
        top_ideas, top_investors, _ = store.run_and_record(
            "agent4", user_id, session_ref, run_agent4, candidate_ideas, investors_snapshot, limit=25
        )
        state.shortlisted_ideas = top_ideas
        state.shortlisted_investors = top_investors
    except Exception:
        state.shortlisted_ideas, state.shortlisted_investors = [], []

    # Agent 5: Match
    matches = []
    try:
        matches, checked_ideas, _ = store.run_and_record("agent5", user_id, session_ref, run_agent5, top_ideas, top_investors)
        state.matched_pairs = matches
        _merge_mvp_readiness(state.shortlisted_ideas, checked_ideas)
    except Exception:
        state.matched_pairs = []

    my_match = next((m for m in matches if m["investor"]["id"] == investor_id), None)
    if not my_match and matches:
        my_match = matches[0]
    elif not my_match and top_ideas:
        my_match = {
            "idea": top_ideas[0],
            "investor": profile_data,
            "score": 80.0,
            "reason": "Best available match",
        }
    state.current_match = my_match

    state.agent_ready = True
    save_sessions()


def _run_investor_pipeline_sync(user_id: str, profile_data: dict):
    """Run agents silently in the background for a specific investor (onboarding)."""
    state = get_user_state(user_id)
    try:
        with pool_lock:
            # Reuse this investor's existing pool entry if they already have
            # one, the same way _run_idea_activation_pipeline reuses a
            # founder's existing idea id — without this, every re-submission
            # of /api/investor/profile (re-onboarding, a retried request, a
            # double-click) appends a brand-new ghost entry that lives in the
            # shared pool forever, since nothing here ever de-dupes by
            # owner_user_id.
            existing = next(
                (i for i in state.investors_pool if i.get("owner_user_id") == user_id), None
            )
            new_id = existing["id"] if existing else f"inv_{len(state.investors_pool) + 1:02d}"
            profile_data["id"] = new_id
            profile_data["owner_user_id"] = user_id
            if existing:
                state.investors_pool[state.investors_pool.index(existing)] = profile_data
            else:
                state.investors_pool.append(profile_data)
        # Durability write-through for the marketplace pool entry itself —
        # outside pool_lock (network call), distinct from save_investor_profile
        # below, which persists to the separate investor_profiles table
        # (onboarding/login-skip), not the shared investor_listings pool
        # this founder-side matching actually reads from.
        db_store.save_investor(profile_data)

        # The endpoint already persisted profile_data to Postgres for fast
        # durability, but that happened before `id`/`owner_user_id` (just
        # assigned above) existed on this dict — re-save now so the durable
        # copy actually has them. Without this, a session-cache loss (e.g.
        # sessions_db.json wiped on an ephemeral disk) restores an investor
        # profile via _hydrate_from_db with no id, which 400s /api/raise-hand
        # and /api/investor/refresh until the user redoes onboarding.
        linkedin = (state.investor_raw_input or {}).get("linkedin", "")
        db_store.save_investor_profile(user_id, profile_data, linkedin)

        _run_investor_matching(user_id, profile_data, new_id)

    except Exception as err:
        import traceback
        traceback.print_exc()
        state.agent_ready = True
        save_sessions()


# ---------------------------------------------------------------------------
# Routes
# ---------------------------------------------------------------------------

@app.get("/api/auth/check-email")
async def check_email_exists(email: str = ""):
    """
    Supabase's signInWithPassword intentionally returns the same "Invalid
    login credentials" error for a wrong password AND a nonexistent account
    (a standard anti-enumeration protection). The login form calls this to
    show "no account found" specifically instead of that generic message —
    using the service-role key (admin-only, never exposed to the browser) to
    actually look the email up.

    Trade-off, on purpose: this does let anyone probe whether an email is
    registered here. Returns {"exists": None} (not True/False) whenever the
    lookup itself is unavailable/fails, so the caller can fall back to the
    generic message instead of asserting something we don't actually know.
    """
    email = (email or "").strip().lower()
    if not email:
        return {"exists": None}

    supabase_url = os.getenv("SUPABASE_URL")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if not supabase_url or not service_key:
        return {"exists": None}

    try:
        headers = {"Authorization": f"Bearer {service_key}", "apikey": service_key}
        async with httpx.AsyncClient(timeout=6.0) as client:
            page = 1
            while page <= 5:  # a handful of pages comfortably covers a small user base
                res = await client.get(
                    f"{supabase_url.rstrip('/')}/auth/v1/admin/users",
                    params={"page": page, "per_page": 1000},
                    headers=headers,
                )
                if res.status_code != 200:
                    return {"exists": None}
                data = res.json()
                users = data.get("users", data if isinstance(data, list) else [])
                if any((u.get("email") or "").lower() == email for u in users):
                    return {"exists": True}
                if len(users) < 1000:
                    break
                page += 1
    except Exception as e:
        print(f"check-email lookup failed: {e}")
        return {"exists": None}

    return {"exists": False}


@app.get("/api/profile")
async def get_profile(user_id: str = Depends(get_current_user_id)):
    """API called by React frontend to determine if user is onboarded."""
    state = get_user_state(user_id)
    await _hydrate_from_db(state, user_id)
    if state.user_type:
        profile = state.founder_profile if state.user_type == "founder" else state.investor_profile
        raw = (state.founder_raw_input if state.user_type == "founder" else state.investor_raw_input) or {}
        result = {
            "onboarded": True,
            "user_type": state.user_type,
            "profile": profile,
            "raw": raw,
        }
        # Agent 3's resume is ready right after onboarding — independent of
        # whether an idea has been posted/activated yet, so this must not
        # only live behind /api/founder/status (which needs a live idea).
        if state.user_type == "founder" and (state.resume_path or state.founder_profile):
            result["resume_url"] = f"/api/resume/{user_id}"
        if state.user_type == "investor":
            result["invested_in"] = _investor_deals((state.investor_profile or {}).get("id"))
        return result
    return {"onboarded": False}


@app.post("/api/reset")
async def reset_state(user_id: str = Depends(get_current_user_id)):
    state = get_user_state(user_id)
    old_resume_path = state.resume_path
    state.reset()
    save_sessions()
    # Only remove this user's own resume — not every file in the shared
    # static/resumes/ directory, which would delete every other user's
    # generated resume too.
    if old_resume_path:
        try:
            if os.path.isfile(old_resume_path):
                os.unlink(old_resume_path)
        except Exception:
            pass
    return {"status": "success"}


async def _delete_storage_prefix(client: httpx.AsyncClient, supabase_url: str, headers: dict, bucket: str, prefix: str):
    """
    Deletes every object under {prefix}/ in a Storage bucket. Storage objects
    aren't rows behind a foreign key, so deleting the Supabase Auth user
    never touches them — pitch-screenshots/{user_id}/... and
    message-attachments/{user_id}/... (see supabaseStorage.js and
    MessagingPage.jsx's upload paths) would otherwise sit there forever,
    orphaned, after an account that explicitly asked to be fully deleted.
    Best-effort: a failure here shouldn't block the rest of deletion.
    """
    try:
        limit = 1000
        # Each successful delete shrinks what a fresh list (still offset 0)
        # returns next, so looping until a page comes back under `limit` —
        # rather than listing once — covers accounts with more than 1000
        # objects instead of silently orphaning the remainder after what's
        # documented as permanent, irreversible deletion. The iteration cap
        # is just a safety net against looping forever if a delete call ever
        # silently fails to actually remove anything.
        for _ in range(50):
            list_resp = await client.post(
                f"{supabase_url.rstrip('/')}/storage/v1/object/list/{bucket}",
                headers=headers,
                json={"prefix": f"{prefix}/", "limit": limit},
            )
            if list_resp.status_code != 200:
                return
            items = list_resp.json()
            if not items:
                return
            paths = [f"{prefix}/{item['name']}" for item in items if item.get("name")]
            if paths:
                del_resp = await client.request(
                    "DELETE",
                    f"{supabase_url.rstrip('/')}/storage/v1/object/{bucket}",
                    headers=headers,
                    json={"prefixes": paths},
                )
                if del_resp.status_code >= 300:
                    return
            if len(items) < limit:
                return
    except Exception as e:
        print(f"[delete_account] storage cleanup failed for {bucket}/{prefix}: {e}")


@app.delete("/api/account")
async def delete_account(user_id: str = Depends(get_current_user_id)):
    """
    Permanent, irreversible account deletion (the frontend gates this behind
    a GitHub-style typed confirmation phrase). Three halves:

    1. Everything this process holds outside Supabase — the shared
       marketplace pools, meeting-request slots, and the in-memory session —
       has to be cleaned up by hand here, since none of it lives behind a
       foreign key Supabase could cascade through.
    2. Storage objects (uploaded pitch screenshots, message attachments)
       aren't rows either, so they need their own explicit cleanup too — see
       _delete_storage_prefix.
    3. Deleting the actual Supabase Auth user via the Admin API cascades
       through every table that references auth.users(id) on delete cascade
       (profiles, founder_profiles, investor_profiles, pitch_posts,
       conversations, messages, direct_messages) — so that one call is what
       actually erases their real data, not just this process's cache of it.
    """
    state = get_user_state(user_id)

    removed_idea_ids = set()
    removed_investor_ids = set()

    with pool_lock:
        remaining_ideas = []
        for idea in GLOBAL_IDEAS_POOL:
            if idea.get("owner_user_id") == user_id:
                removed_idea_ids.add(idea.get("id"))
            else:
                remaining_ideas.append(idea)
        GLOBAL_IDEAS_POOL[:] = remaining_ideas

        remaining_investors = []
        for inv in GLOBAL_INVESTORS_POOL:
            if inv.get("owner_user_id") == user_id:
                removed_investor_ids.add(inv.get("id"))
            else:
                remaining_investors.append(inv)
        GLOBAL_INVESTORS_POOL[:] = remaining_investors

    # Durability write-through deletes — unconditional (not gated on
    # removed_idea_ids/removed_investor_ids being non-empty) so this stays
    # correct even in the edge case where Postgres has a row the in-memory
    # pool doesn't. A plain DELETE WHERE is a no-op if nothing matches.
    await asyncio.to_thread(db_store.delete_ideas_by_owner, user_id)
    await asyncio.to_thread(db_store.delete_investors_by_owner, user_id)

    if removed_idea_ids or removed_investor_ids:
        with meeting_lock:
            for key in [
                k for k, slot in GLOBAL_MEETING_REQUESTS.items()
                if slot.get("idea_id") in removed_idea_ids or slot.get("investor_id") in removed_investor_ids
            ]:
                del GLOBAL_MEETING_REQUESTS[key]
        await asyncio.to_thread(db_store.delete_meeting_requests_by_owner, user_id)

    old_resume_path = state.resume_path
    with state_lock:
        user_states.pop(user_id, None)
    save_sessions()
    if old_resume_path:
        try:
            if os.path.isfile(old_resume_path):
                os.unlink(old_resume_path)
        except Exception:
            pass

    supabase_url = os.getenv("SUPABASE_URL")
    service_key = os.getenv("SUPABASE_SERVICE_ROLE_KEY")
    if supabase_url and service_key:
        try:
            headers = {"Authorization": f"Bearer {service_key}", "apikey": service_key}
            async with httpx.AsyncClient(timeout=10.0) as client:
                await _delete_storage_prefix(client, supabase_url, headers, "pitch-screenshots", user_id)
                await _delete_storage_prefix(client, supabase_url, headers, "message-attachments", user_id)
                resp = await client.delete(
                    f"{supabase_url.rstrip('/')}/auth/v1/admin/users/{user_id}",
                    headers=headers,
                )
                if resp.status_code not in (200, 204, 404):
                    raise HTTPException(
                        status_code=502,
                        detail="Account data was cleared, but deleting the login itself failed — please try again or contact support.",
                    )
        except HTTPException:
            raise
        except Exception:
            raise HTTPException(
                status_code=502,
                detail="Account data was cleared, but deleting the login itself failed — please try again or contact support.",
            )
    else:
        # No service role key configured on this deployment — can't reach the
        # Supabase Admin API, so the login itself can't be removed here.
        raise HTTPException(
            status_code=503,
            detail="Account deletion isn't fully configured on this server (missing SUPABASE_SERVICE_ROLE_KEY).",
        )

    return {"success": True}


# ---------------------------------------------------------------------------
# REAL GITHUB LOOKUP (search by name) + REAL LINKEDIN OAUTH IDENTITY
# ---------------------------------------------------------------------------

@app.get("/api/github/search")
async def github_search(name: str, user_id: str = Depends(get_current_user_id)):
    """
    Real GitHub Search API lookup — given a plain name, returns actual GitHub
    accounts that exist, for the user to confirm/pick from. Never guesses or
    fabricates an account.
    """
    if not name or not name.strip():
        raise HTTPException(status_code=400, detail="name query param is required.")
    candidates = search_github_users(name.strip())
    return {"query": name.strip(), "candidates": candidates}


@app.get("/api/linkedin/login")
async def linkedin_login(request: Request, user_id: str = Depends(get_current_user_id)):
    """
    Kicks off real 'Sign in with LinkedIn' (OpenID Connect). Returns the
    LinkedIn authorization URL for the frontend to redirect the browser to.
    """
    if not linkedin_oauth.is_configured():
        raise HTTPException(
            status_code=503,
            detail=(
                "LinkedIn sign-in isn't configured on this server yet. "
                "Set LINKEDIN_CLIENT_ID / LINKEDIN_CLIENT_SECRET / LINKEDIN_REDIRECT_URI in .env."
            ),
        )
    _purge_stale_oauth_states()
    state_token = str(uuid.uuid4())
    # Prefer the Origin header (present on this fetch() call from the
    # frontend); fall back to Referer's origin for any client that omits it.
    origin_header = request.headers.get("origin", "")
    if not origin_header:
        referer = request.headers.get("referer", "")
        if referer:
            parsed = urlparse(referer)
            origin_header = f"{parsed.scheme}://{parsed.netloc}" if parsed.netloc else ""
    frontend_origin = _trusted_frontend_origin(origin_header)
    linkedin_oauth_states[state_token] = (user_id, time.time(), frontend_origin)
    authorize_url = linkedin_oauth.build_authorization_url(state=state_token)
    return {"authorize_url": authorize_url}


@app.get("/api/linkedin/callback")
async def linkedin_callback(code: str = "", state: str = "", error: Optional[str] = None):
    """
    LinkedIn redirects the browser here after the person logs in and
    approves access. Exchanges the code for their real, LinkedIn-confirmed
    name/email/picture and stores it against the user who started the flow.
    """
    # Look up the state entry (if any) before branching on error/code/state,
    # so even the early error-redirect below can send the browser back to
    # the actual frontend origin that started this flow, not a static
    # fallback. LinkedIn always echoes `state` back, success or failure.
    entry = linkedin_oauth_states.pop(state, None) if state else None
    user_id, _created_at, stored_frontend_origin = entry if entry else (None, None, None)
    frontend_url = stored_frontend_origin or os.getenv("FRONTEND_URL", "http://localhost:3000").rstrip("/")

    if error or not code or not state:
        print(f"LinkedIn OAuth callback missing params: error={error!r} code_present={bool(code)} state_present={bool(state)}")
        return RedirectResponse(url=f"{frontend_url}/onboarding?linkedin=error&reason=denied_or_missing_params")

    if not user_id:
        # Most common cause: the backend process restarted (e.g. --reload
        # picked up a file change) between /api/linkedin/login and this
        # callback, wiping the in-memory state dict this checks against.
        print(f"LinkedIn OAuth state not found/expired: state={state!r}")
        return RedirectResponse(url=f"{frontend_url}/onboarding?linkedin=error&reason=state_not_found")

    try:
        userinfo = await linkedin_oauth.exchange_code_for_userinfo(code)
    except Exception as e:
        print(f"LinkedIn OAuth exchange failed: {e}")
        return RedirectResponse(url=f"{frontend_url}/onboarding?linkedin=error&reason=exchange_failed")

    state_obj = get_user_state(user_id)
    state_obj.linkedin_verified = userinfo
    save_sessions()

    display_name = quote(userinfo.get("name") or "")
    return RedirectResponse(url=f"{frontend_url}/onboarding?linkedin=connected&name={display_name}")


# ---------------------------------------------------------------------------
# FOUNDER PROFILE
# ---------------------------------------------------------------------------

@app.post("/api/founder/profile")
async def founder_profile(
    background_tasks: BackgroundTasks,
    linkedin: str = Form(""),
    github: str = Form(""),
    additional: str = Form(""),
    linkedin_bio_text: str = Form(""),
    github_name_hint: str = Form(""),
    country: str = Form(""),
    city: str = Form(""),
    district: str = Form(""),
    user_id: str = Depends(get_current_user_id),
    authorization: Optional[str] = Header(None),
):
    """
    Onboarding is idea-agnostic now: just LinkedIn + GitHub -> Agent 2
    profile + Agent 3 resume. Ideas are posted afterward (potentially
    several) via /api/founder/idea and activated one at a time via
    /api/founder/idea/activate.
    """
    try:
        state = get_user_state(user_id)
        preserved_linkedin_verified = state.linkedin_verified
        state.reset()
        state.linkedin_verified = preserved_linkedin_verified
        state.user_type = "founder"
        state.agent_ready = False

        state.founder_raw_input = {
            "linkedin": linkedin,
            "github": github,
            "additional": additional,
            "linkedin_bio_text": linkedin_bio_text,
            "linkedin_verified": state.linkedin_verified,
            "github_name_hint": github_name_hint,
        }

        # Agent 2 (Groq) and geocoding (Nominatim) are independent of each
        # other, so run them concurrently rather than paying both latencies
        # back-to-back. Both run in worker threads so neither blocks the
        # event loop for other concurrent users' requests.
        profile_data, location = await asyncio.gather(
            asyncio.to_thread(
                store.run_and_record,
                "agent2_founder", user_id, state.session_id, analyze_founder_profile,
                linkedin, github, additional, linkedin_bio_text, state.linkedin_verified, github_name_hint,
            ),
            asyncio.to_thread(geo.build_location, country, city, district),
        )
        if not profile_data.get("name"):
            profile_data["name"] = await _get_auth_display_name(authorization)
        state.founder_profile = profile_data
        profile_data["location"] = location

        # Durable save — this is what lets login skip LinkedIn/GitHub again
        # even if the Python process restarts (a no-op if DATABASE_URL isn't
        # set). Wrapped in asyncio.to_thread — this is a blocking psycopg2
        # call inside an async route handler; without this it runs directly
        # on the event loop, the same bug class already fixed for the DB
        # *read* path (_hydrate_from_db) but previously missed here.
        await asyncio.to_thread(db_store.save_founder_profile, user_id, profile_data, linkedin, github)

        # Kick off background pipeline (just the resume — Agent 3)
        background_tasks.add_task(_run_founder_onboarding_pipeline, user_id, profile_data)
        save_sessions()

        return {
            "success": True,
            "user_type": "founder",
            "founder_profile": profile_data,
            "pipeline_status": "processing",
        }

    except HTTPException:
        raise
    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail="Profile error — please try again.")


@app.post("/api/founder/idea/extract-pdf")
async def extract_idea_pdf(
    idea_pdf: UploadFile = File(...),
    user_id: str = Depends(get_current_user_id),
):
    """
    Lightweight PDF -> text extraction (no LLM call) so a founder can drop a
    pitch PDF into the "Post New Idea" form and have it pre-fill the
    description field instead of typing it out. The actual Agent 1 analysis
    still runs later, at "Go Live", on whatever text ends up in pitch_text —
    same path as a typed description, so nothing else in the pipeline needed
    to change for this.
    """
    if not idea_pdf.filename or not idea_pdf.filename.lower().endswith(".pdf"):
        raise HTTPException(status_code=400, detail="Only PDF files are supported.")

    tmp_path = f"temp/{uuid.uuid4()}.pdf"
    try:
        # Read in bounded chunks and check the running total as we go, rather
        # than idea_pdf.read() (which buffers the entire body before the size
        # check ever runs) — that made MAX_PDF_UPLOAD_BYTES bound only what
        # got written to disk, not what got read into memory first.
        total = 0
        chunk_size = 1024 * 1024
        with open(tmp_path, "wb") as f:
            while True:
                chunk = await idea_pdf.read(chunk_size)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_PDF_UPLOAD_BYTES:
                    raise HTTPException(status_code=413, detail="PDF is too large — please upload a file under 15MB.")
                f.write(chunk)
        text = extract_pdf_text(tmp_path)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    finally:
        try:
            os.unlink(tmp_path)
        except Exception:
            pass

    return {"text": text}


@app.post("/api/founder/idea/activate")
async def activate_idea(
    background_tasks: BackgroundTasks,
    idea_text: str = Form(...),
    mvp_url: str = Form(""),
    repo_url: str = Form(""),
    screenshot_url: str = Form(""),
    title: str = Form(""),
    user_id: str = Depends(get_current_user_id),
):
    """
    Marks one of a founder's posted ideas "live" for matching. Verifies the
    linked repo actually belongs to their connected GitHub account, then
    (re)runs the Agent 1/4/5 pipeline on this idea in the background.
    """
    state = get_user_state(user_id)
    if not state.founder_profile:
        raise HTTPException(status_code=400, detail="Complete onboarding (LinkedIn/GitHub) before posting an idea.")
    if not idea_text or not idea_text.strip():
        raise HTTPException(status_code=400, detail="idea_text is required.")

    state.agent_ready = False
    state.idea_ever_activated = True
    state.idea_raw_text = idea_text.strip()

    url_valid = False
    url_msg = "No MVP URL provided. Investor access will remain locked."
    if mvp_url and mvp_url.strip():
        state.mvp_url = mvp_url.strip()
        if _is_valid_url(state.mvp_url):
            url_valid = await _reachable_url(state.mvp_url)
            url_msg = "MVP URL verified and reachable." if url_valid else "MVP URL is not reachable."
        else:
            url_msg = "MVP URL format is invalid."
    state.mvp_url_valid = url_valid

    background_tasks.add_task(
        _run_idea_activation_pipeline, user_id, idea_text.strip(), mvp_url.strip(), repo_url.strip(), screenshot_url.strip(), title.strip(),
    )
    save_sessions()

    return {
        "success": True,
        "mvp_url_valid": url_valid,
        "mvp_url_message": url_msg,
        "pipeline_status": "processing",
    }


@app.get("/api/founder/status")
async def founder_status(user_id: str = Depends(get_current_user_id)):
    state = get_user_state(user_id)
    await _hydrate_from_db(state, user_id)
    if not state.founder_profile:
        raise HTTPException(status_code=400, detail="No founder session active.")
    _ensure_active_idea_id(state, user_id)

    # Recovered from the DB after a restart with no idea ever activated in
    # this session — treat as "ready" (with an empty match) rather than
    # leaving the frontend stuck on an infinite "agents are working" state.
    # (Checking idea_ever_activated, not just an empty ideas_pool: the pool is
    # shared across every user now, so it's non-empty as soon as ANY founder
    # has an idea live — that must not short-circuit a still-in-flight
    # pipeline for a founder who just activated their own idea.)
    if not state.agent_ready and not state.idea_ever_activated:
        state.agent_ready = True

    ready = state.agent_ready
    result: dict = {
        "ready": ready,
        "mvp_url_valid": state.mvp_url_valid,
        "founder_profile": state.founder_profile,
    }

    if ready:
        result["idea_details"] = state.idea_details
        # state.idea_details reflects only the LAST activate/refresh attempt —
        # if that attempt failed (e.g. Agent 1 hit a Groq rate limit) it's just
        # {"error": ...}, even though the live idea's actual analysis is still
        # sitting intact in the shared pool (a failed refresh never overwrites
        # it — see _run_idea_activation_pipeline). Same fragile-snapshot-vs-
        # durable-pool split active_idea_id had; source the founder's own
        # idea (with its roadmap/MVP scope) from the pool instead so a failed
        # refresh attempt doesn't blank out the last known-good analysis.
        result["my_idea"] = next(
            (i for i in state.ideas_pool if i.get("id") == state.active_idea_id), None
        )
        result["resume_url"] = f"/api/resume/{user_id}" if (state.resume_path or state.founder_profile) else None
        result["top_investors"] = state.shortlisted_investors[:8]
        result["meeting_requests"] = _serialize_meeting_requests(state)
        result["current_match"] = state.current_match

    return result


@app.get("/api/resume/{owner_user_id}")
async def get_resume(owner_user_id: str, user_id: str = Depends(get_current_user_id)):
    """
    Replaces the old public /static/resumes/<filename> mount. Any
    authenticated Techflix account may fetch a resume — same trust model this
    app already applies to `profiles` (readable by any signed-in account so
    matching/search works) — but a valid session is now actually required,
    instead of the file being reachable by anyone who ever saw its URL.
    """
    # A plain lookup first, not get_user_state() — that helper creates a
    # fresh empty SessionState for any id it's never seen, which would let a
    # request with a made-up owner_user_id silently pollute user_states with
    # junk entries. Only fall through to get_user_state()+_hydrate_from_db
    # below once we already know (from Postgres) that this id is real.
    with state_lock:
        owner_state = user_states.get(owner_user_id)

    # The actual .docx (under static/resumes/) and owner_state.resume_path
    # (only ever written to sessions_db.json, never to Postgres) both live
    # on Render's ephemeral disk — a redeploy can wipe either or both, while
    # founder_profile itself survives (durably persisted via db_store when
    # DATABASE_URL is set). Rather than 404 a real founder whose file just
    # didn't survive the last deploy, rebuild it on demand from the profile
    # that's still there.
    if not owner_state or not owner_state.founder_profile:
        owner_state = get_user_state(owner_user_id)
        await _hydrate_from_db(owner_state, owner_user_id)

    if not owner_state.founder_profile:
        raise HTTPException(status_code=404, detail="Resume not found.")

    if not owner_state.resume_path or not os.path.isfile(owner_state.resume_path):
        try:
            owner_state.resume_path = await asyncio.to_thread(
                run_agent3, owner_state.founder_profile,
                output_dir="static/resumes", unique_id=owner_user_id,
            )
            save_sessions()
        except Exception:
            raise HTTPException(status_code=503, detail="Could not rebuild the resume right now — please try again.")

    return FileResponse(
        owner_state.resume_path,
        filename=os.path.basename(owner_state.resume_path),
        media_type="application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    )


# ---------------------------------------------------------------------------
# INVESTOR PROFILE
# ---------------------------------------------------------------------------

@app.post("/api/investor/profile")
async def investor_profile(
    background_tasks: BackgroundTasks,
    linkedin: str = Form(""),
    firm: str = Form(""),
    designation: str = Form(""),
    focus_sectors: str = Form(""),
    min_ticket: int = Form(100000),
    max_ticket: int = Form(1000000),
    additional: str = Form(""),
    linkedin_bio_text: str = Form(""),
    country: str = Form(""),
    city: str = Form(""),
    district: str = Form(""),
    user_id: str = Depends(get_current_user_id),
    authorization: Optional[str] = Header(None),
):
    try:
        state = get_user_state(user_id)
        # Same reasoning as founder_profile: /api/linkedin/callback already
        # stamped this onto `state` (role-agnostic — it doesn't know or care
        # whether this user_id is a founder or investor), so a plain
        # state.reset() right after would silently throw away a real OAuth
        # login the investor just completed seconds earlier in this exact flow.
        preserved_linkedin_verified = state.linkedin_verified
        state.reset()
        state.linkedin_verified = preserved_linkedin_verified
        state.user_type = "investor"
        state.agent_ready = False
        state.investor_pipeline_started = True

        state.investor_raw_input = {
            "linkedin": linkedin,
            "additional": additional,
            "linkedin_bio_text": linkedin_bio_text,
            "linkedin_verified": state.linkedin_verified,
        }

        # Agent 2 (Groq) and geocoding (Nominatim) are independent — run them
        # concurrently (see founder_profile above for the same reasoning).
        profile_data, location = await asyncio.gather(
            asyncio.to_thread(
                store.run_and_record,
                "agent2_investor", user_id, state.session_id, analyze_investor_profile,
                linkedin, additional, linkedin_bio_text, state.linkedin_verified,
            ),
            asyncio.to_thread(geo.build_location, country, city, district),
        )
        if not profile_data.get("name"):
            profile_data["name"] = await _get_auth_display_name(authorization)
        state.investor_profile = profile_data

        # Merge form data
        if firm: profile_data["firm"] = firm
        if designation: profile_data["designation"] = designation
        if focus_sectors:
            profile_data["focus_sectors"] = [s.strip() for s in focus_sectors.split(",") if s.strip()]
        profile_data["min_ticket"] = min_ticket
        profile_data["max_ticket"] = max_ticket
        profile_data["location"] = location

        # Same asyncio.to_thread fix as founder_profile above — was
        # previously called directly, blocking the event loop.
        await asyncio.to_thread(db_store.save_investor_profile, user_id, profile_data, linkedin)

        background_tasks.add_task(_run_investor_pipeline_sync, user_id, profile_data)
        save_sessions()

        return {
            "success": True,
            "user_type": "investor",
            "investor_profile": profile_data,
            "pipeline_status": "processing",
        }

    except Exception as e:
        import traceback; traceback.print_exc()
        raise HTTPException(status_code=500, detail="Profile error — please try again.")


@app.get("/api/investor/status")
async def investor_status(user_id: str = Depends(get_current_user_id)):
    state = get_user_state(user_id)
    await _hydrate_from_db(state, user_id)
    if not state.investor_profile:
        raise HTTPException(status_code=400, detail="No investor session active.")

    # See founder_status's matching comment: check the per-session
    # "did we ever kick off a pipeline run" flag, not shortlisted_ideas
    # emptiness — that's also empty for a perfectly normal in-flight
    # Agent 4/5 run right after this investor's own profile submission.
    if not state.agent_ready and not state.investor_pipeline_started:
        state.agent_ready = True

    ready = state.agent_ready
    result: dict = {
        "ready": ready,
        "investor_profile": state.investor_profile,
    }

    if ready:
        result["top_ideas"] = state.shortlisted_ideas[:8]
        result["meeting_requests"] = _serialize_meeting_requests(state)
        result["current_match"] = state.current_match

    return result


@app.post("/api/investor/refresh")
async def refresh_investor_shortlist(background_tasks: BackgroundTasks, user_id: str = Depends(get_current_user_id)):
    """
    Re-runs Agent 4/5 against the current shared ideas pool for an
    already-onboarded investor — the "Refresh Shortlist" button. Without
    this, an investor's shortlist/current_match was frozen at whatever it
    was the moment they finished onboarding; any founder who went live
    afterward would never show up for them at all.
    """
    state = get_user_state(user_id)
    if not state.investor_profile:
        raise HTTPException(status_code=400, detail="Complete onboarding before refreshing your shortlist.")

    investor_id = state.investor_profile.get("id")
    if not investor_id:
        raise HTTPException(status_code=400, detail="No investor profile found for this account.")

    state.agent_ready = False
    background_tasks.add_task(_run_investor_matching, user_id, state.investor_profile, investor_id)
    save_sessions()

    return {"success": True, "pipeline_status": "processing"}


# ---------------------------------------------------------------------------
# LOCATION SEARCH — founders search investors, investors search founder
# ideas. Direction is derived from the caller's own role, never client-
# chosen. Every match is returned (no radius cutoff), just ordered
# closest-first using H3/haversine distance from the caller's own saved
# location (or an explicit `near` override), and paginated for infinite
# scroll.
# ---------------------------------------------------------------------------

@app.get("/api/search")
async def location_search(
    country: str = "",
    city: str = "",
    district: str = "",
    near: str = "",  # optional — search from a different place instead of the caller's own location
    q: str = "",  # optional free-text keyword — name/firm/skills/sectors/idea title, etc.
    offset: int = 0,
    limit: int = 10,
    user_id: str = Depends(get_current_user_id),
):
    state = get_user_state(user_id)
    await _hydrate_from_db(state, user_id)
    if not state.user_type:
        raise HTTPException(status_code=400, detail="Complete onboarding before searching.")

    # A founder searches investors; an investor searches founders (via their
    # live ideas). This is derived server-side, not a client-supplied choice.
    search_type = "investors" if state.user_type == "founder" else "ideas"

    with pool_lock:
        pool = list(GLOBAL_INVESTORS_POOL) if search_type == "investors" else list(GLOBAL_IDEAS_POOL)

    # Distance origin: an explicit `near` override, else the caller's own
    # saved location. If neither is available, everyone still shows —
    # just unordered by distance (distance_km stays null for all of them).
    origin = None
    near = near.strip()
    if near:
        origin = await asyncio.to_thread(geo.geocode, near)
        if not origin:
            raise HTTPException(status_code=400, detail=f"Couldn't find a real place matching '{near}'.")
    else:
        my_profile = state.founder_profile if state.user_type == "founder" else state.investor_profile
        my_loc = (my_profile or {}).get("location") or {}
        if my_loc.get("lat") is not None and my_loc.get("lon") is not None:
            origin = {"lat": my_loc["lat"], "lon": my_loc["lon"]}

    result = geo.search_pool(
        pool, search_type,
        country=country, city=city, district=district, q=q,
        origin=origin, offset=offset, limit=limit,
    )

    # An investor's completed deals are a durable track record, not something
    # that makes them disappear from search once a deal is done — surface it
    # on their card instead, so a founder searching by name still finds them.
    if search_type == "investors":
        for item in result["results"]:
            item["invested_in"] = _investor_deals(item.get("id"))

    return result


# ---------------------------------------------------------------------------
# MVP URL VALIDATION
# ---------------------------------------------------------------------------

@app.post("/api/validate-mvp-url")
async def validate_mvp_url(data: dict, user_id: str = Depends(get_current_user_id)):
    state = get_user_state(user_id)
    url = data.get("url", "").strip()
    if not url:
        return {"valid": False, "message": "No URL provided."}

    if not _is_valid_url(url):
        return {"valid": False, "message": "Invalid URL format. Must start with http:// or https://."}

    reachable = await _reachable_url(url)
    if reachable:
        state.mvp_url = url
        state.mvp_url_valid = True
        save_sessions()
        return {"valid": True, "message": "MVP URL verified and saved. Investor access unlocked!"}
    else:
        return {"valid": False, "message": "URL is not reachable. Please check your link."}


# ---------------------------------------------------------------------------
# OPT-IN MEETING SYSTEM
# ---------------------------------------------------------------------------

def _ensure_active_idea_id(state, user_id: str):
    """
    active_idea_id is this founder's own pointer to "which ideas_pool entry is
    mine" — it can go stale (re-submitting onboarding calls state.reset(),
    which clears it) or never get (re)set in the first place (a "Refresh
    matches" attempt whose Agent 1 call fails, e.g. a Groq rate limit, returns
    before reaching the line that sets it). ideas_pool and
    GLOBAL_MEETING_REQUESTS are both process-wide shared stores independent of
    this per-session pointer, so the founder's actual live idea and any
    confirmed meetings against it survive completely intact even while this
    pointer is missing — but every feature keyed off active_idea_id
    (meeting-request visibility, requesting new meetings, generating
    agreements) silently breaks until it's restored. Self-heal by re-deriving
    it from the pool whenever it's missing, instead of requiring the founder
    to successfully re-run the full activation pipeline again.
    """
    if state.active_idea_id:
        return
    owned = next((i for i in state.ideas_pool if i.get("owner_user_id") == user_id), None)
    if owned:
        state.active_idea_id = owned["id"]
        save_sessions()


def _investor_deals(investor_id: str) -> list[dict]:
    """
    Confirmed deals for a given investor — every GLOBAL_MEETING_REQUESTS slot
    where both sides opted in, regardless of how the negotiation/term sheet
    played out afterward. This is a durable track record (shown on the
    investor's own profile and on their search card as "Invested in ...")
    rather than something that disappears once the deal itself has wrapped up.
    """
    if not investor_id:
        return []
    deals = []
    with meeting_lock:
        for slot in GLOBAL_MEETING_REQUESTS.values():
            if slot.get("investor_id") != investor_id or not slot.get("both_opted_in"):
                continue
            idea = slot.get("idea") or {}
            deals.append({
                "idea_id": slot.get("idea_id"),
                "title": idea.get("title"),
                "domain": idea.get("domain"),
            })
    return deals


def _investor_has_other_confirmed_deal(investor_id: str, excluding_idea_id: str) -> bool:
    """True if this investor already has a *different* confirmed deal — used
    to block a new founder from requesting a meeting with an investor who's
    already committed elsewhere. They stay visible in search (see
    _investor_deals), just not approachable for a second deal; new
    connections still only happen through the Agent 4/5 matching pipeline."""
    with meeting_lock:
        return any(
            slot.get("investor_id") == investor_id
            and slot.get("both_opted_in")
            and slot.get("idea_id") != excluding_idea_id
            for slot in GLOBAL_MEETING_REQUESTS.values()
        )


def _idea_has_other_confirmed_deal(idea_id: str, excluding_investor_id: str) -> bool:
    """Mirror of _investor_has_other_confirmed_deal for the founder/idea side —
    blocks a new investor from raising their hand on a startup that's already
    confirmed a deal with someone else."""
    with meeting_lock:
        return any(
            slot.get("idea_id") == idea_id
            and slot.get("both_opted_in")
            and slot.get("investor_id") != excluding_investor_id
            for slot in GLOBAL_MEETING_REQUESTS.values()
        )


def _serialize_meeting_requests(state):
    """
    Meeting slots live in the shared GLOBAL_MEETING_REQUESTS store now (keyed
    by idea+investor), not per-session — this filters that shared store down
    to the entries relevant to the calling user and reshapes each one to the
    same {id, founder_requested, investor_raised, both_opted_in, investor?,
    idea?} contract the frontend already expects, where `id` is "the other
    party's id" (investor_id for a founder's view, idea_id for an investor's).
    """
    result = []
    with meeting_lock:
        if state.user_type == "founder":
            my_idea_id = state.active_idea_id
            if not my_idea_id:
                return result
            for slot in GLOBAL_MEETING_REQUESTS.values():
                if slot["idea_id"] != my_idea_id:
                    continue
                result.append({
                    "id": slot["investor_id"],
                    "founder_requested": slot.get("founder_requested", False),
                    "investor_raised": slot.get("investor_raised", False),
                    "both_opted_in": slot.get("both_opted_in", False),
                    "investor": slot.get("investor"),
                    "updated_at": slot.get("updated_at") or slot.get("created_at"),
                })
        elif state.user_type == "investor":
            my_investor_id = (state.investor_profile or {}).get("id")
            if not my_investor_id:
                return result
            for slot in GLOBAL_MEETING_REQUESTS.values():
                if slot["investor_id"] != my_investor_id:
                    continue
                result.append({
                    "id": slot["idea_id"],
                    "founder_requested": slot.get("founder_requested", False),
                    "investor_raised": slot.get("investor_raised", False),
                    "both_opted_in": slot.get("both_opted_in", False),
                    "idea": slot.get("idea"),
                    "updated_at": slot.get("updated_at") or slot.get("created_at"),
                })
    return result


@app.post("/api/request-meeting")
async def request_meeting(data: dict, user_id: str = Depends(get_current_user_id)):
    state = get_user_state(user_id)
    if state.user_type != "founder":
        raise HTTPException(status_code=403, detail="Only founders can request meetings.")

    if not state.mvp_url_valid:
        raise HTTPException(
            status_code=403,
            detail="You must submit a valid MVP/prototype URL before requesting investor meetings."
        )

    investor_id = data.get("investor_id", "")
    if not investor_id:
        raise HTTPException(status_code=400, detail="investor_id is required.")

    _ensure_active_idea_id(state, user_id)
    idea_id = state.active_idea_id
    if not idea_id:
        raise HTTPException(status_code=400, detail="No active idea to request a meeting for.")

    if _investor_has_other_confirmed_deal(investor_id, idea_id):
        raise HTTPException(
            status_code=409,
            detail="This investor already has a confirmed deal with another startup and isn't taking new meetings right now.",
        )

    investor = next((inv for inv in state.shortlisted_investors if inv.get("id") == investor_id), None)
    if not investor:
        # Not in this founder's own AI shortlist — but state.shortlisted_investors
        # is a snapshot from this founder's last idea activation/refresh, so an
        # investor who joined afterward and already raised their hand on this
        # exact idea (real, already-verified interest) would otherwise 404 here
        # just because Agent 4 hasn't re-run since. Accept them from the slot.
        with meeting_lock:
            existing_slot = GLOBAL_MEETING_REQUESTS.get(_meeting_key(idea_id, investor_id))
        if existing_slot and existing_slot.get("investor_raised") and existing_slot.get("investor"):
            investor = existing_slot["investor"]
    if not investor:
        raise HTTPException(status_code=404, detail="Investor not found.")

    idea = next((i for i in state.ideas_pool if i.get("id") == idea_id), None)

    with meeting_lock:
        slot = GLOBAL_MEETING_REQUESTS.setdefault(_meeting_key(idea_id, investor_id), {
            "idea_id": idea_id, "investor_id": investor_id,
            "founder_requested": False, "investor_raised": False, "both_opted_in": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        slot["updated_at"] = datetime.now(timezone.utc).isoformat()
        slot["investor"] = investor
        # raise_hand already sets slot["idea"] from its own side — mirror it
        # here so a slot that starts from a founder requesting a meeting
        # first still carries the idea for the investor's side to render
        # (title, id for their own Raise Hand/agreement calls), not just the
        # bare idea_id. Guard with `if idea` so a lookup miss doesn't
        # clobber an idea already on the slot from a prior raise-hand.
        if idea:
            slot["idea"] = idea
        slot["founder_requested"] = True
        if slot["founder_requested"] and slot["investor_raised"]:
            slot["both_opted_in"] = True
        both_opted_in = slot["both_opted_in"]
        slot_copy = dict(slot)

    # Durability write-through — shallow-copied above while still under
    # meeting_lock, so this serialization can't race a concurrent mutation
    # of the same shared slot dict from another request.
    await asyncio.to_thread(db_store.save_meeting_request, slot_copy)

    if both_opted_in:
        await _ensure_conversation(user_id, investor.get("owner_user_id"), idea_id)

    save_sessions()
    return {
        "success": True,
        "investor_id": investor_id,
        "both_opted_in": both_opted_in,
        "message": (
            "Meeting confirmed! You can now start a conversation."
            if both_opted_in
            else "Meeting request sent. Waiting for the investor to confirm."
        ),
    }


@app.post("/api/raise-hand")
async def raise_hand(data: dict, user_id: str = Depends(get_current_user_id)):
    state = get_user_state(user_id)
    if state.user_type != "investor":
        raise HTTPException(status_code=403, detail="Only investors can raise hand.")

    idea_id = data.get("idea_id", "")
    if not idea_id:
        raise HTTPException(status_code=400, detail="idea_id is required.")

    investor_id = (state.investor_profile or {}).get("id")
    if not investor_id:
        raise HTTPException(status_code=400, detail="No investor profile found for this account.")

    if _idea_has_other_confirmed_deal(idea_id, investor_id):
        raise HTTPException(
            status_code=409,
            detail="This founder already has a confirmed deal with another investor and isn't taking new meetings right now.",
        )

    idea = next((i for i in state.shortlisted_ideas if i.get("id") == idea_id), None)
    if not idea:
        # Mirror of request_meeting's same fallback: state.shortlisted_ideas is
        # a snapshot from this investor's last onboarding/refresh, so a founder
        # who requested a meeting on this exact idea afterward would otherwise
        # 404 here just because Agent 4 hasn't re-run since. Accept the idea
        # already attached to the slot by that request instead.
        with meeting_lock:
            existing_slot = GLOBAL_MEETING_REQUESTS.get(_meeting_key(idea_id, investor_id))
        if existing_slot and existing_slot.get("founder_requested") and existing_slot.get("idea"):
            idea = existing_slot["idea"]
    if not idea:
        raise HTTPException(status_code=404, detail="Startup idea not found.")

    with meeting_lock:
        slot = GLOBAL_MEETING_REQUESTS.setdefault(_meeting_key(idea_id, investor_id), {
            "idea_id": idea_id, "investor_id": investor_id,
            "founder_requested": False, "investor_raised": False, "both_opted_in": False,
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
        slot["updated_at"] = datetime.now(timezone.utc).isoformat()
        slot["idea"] = idea
        # request_meeting (founder-initiated) already sets slot["investor"] —
        # do the same here so a slot that starts from an investor raising
        # their hand first still carries their profile for the founder's
        # side to render (name, firm, id for the Request Meeting call), not
        # just their id.
        slot["investor"] = state.investor_profile
        slot["investor_raised"] = True
        if slot["founder_requested"] and slot["investor_raised"]:
            slot["both_opted_in"] = True
        both_opted_in = slot["both_opted_in"]
        slot_copy = dict(slot)

    # Durability write-through — same pattern as request_meeting.
    await asyncio.to_thread(db_store.save_meeting_request, slot_copy)

    if both_opted_in:
        await _ensure_conversation(idea.get("owner_user_id"), user_id, idea_id)

    save_sessions()
    return {
        "success": True,
        "idea_id": idea_id,
        "both_opted_in": both_opted_in,
        "message": (
            "Connection confirmed! You can now start a conversation."
            if both_opted_in
            else "Interest noted. Waiting for the founder to request a meeting."
        ),
    }


@app.get("/api/my-connections")
async def my_connections(user_id: str = Depends(get_current_user_id)):
    state = get_user_state(user_id)
    confirmed = [slot for slot in _serialize_meeting_requests(state) if slot.get("both_opted_in")]
    return {"connections": confirmed}


# ---------------------------------------------------------------------------
# REJECTION / NEXT-BEST MATCH (Agent 6)
# ---------------------------------------------------------------------------

# A double-click (or two tabs on the same match) both reading the same
# state.current_match/shortlisted_* snapshot and each kicking off their own
# Agent 6 call is a wasted-LLM-call/lost-update race, not data corruption
# (rejected_ids.add is idempotent either way) — but there's no reason to pay
# for two concurrent Groq calls to reject the same match once. Same pattern
# as _in_flight_reruns above, scoped to user_id since only one reject is
# ever meaningful in flight per user at a time.
_in_flight_rejects: set = set()
_in_flight_rejects_lock = threading.Lock()


@app.post("/api/reject")
async def reject_match(user_id: str = Depends(get_current_user_id)):
    with _in_flight_rejects_lock:
        if user_id in _in_flight_rejects:
            raise HTTPException(status_code=409, detail="Your last reject is still processing — please wait.")
        _in_flight_rejects.add(user_id)
    try:
        return await _reject_match_impl(user_id)
    finally:
        with _in_flight_rejects_lock:
            _in_flight_rejects.discard(user_id)


async def _reject_match_impl(user_id: str):
    state = get_user_state(user_id)
    if not state.current_match:
        raise HTTPException(status_code=400, detail="No active match to reject.")

    session_ref = state.session_id

    if state.user_type == "founder":
        idea = state.current_match["idea"]
        rejected_investor_id = state.current_match["investor"].get("id")
        if rejected_investor_id:
            state.rejected_ids.add(rejected_investor_id)

        try:
            next_investor, log_msg = await asyncio.to_thread(
                store.run_and_record,
                "agent6_founder", user_id, session_ref,
                run_agent6_for_founder, idea, state.shortlisted_investors, state.rejected_ids,
            )
        except Exception as exc:
            import traceback; traceback.print_exc()
            raise HTTPException(status_code=500, detail="Agent 6 failed — please try again.")

        if not next_investor:
            state.current_match = None
            save_sessions()
            return {"success": True, "match": None, "message": log_msg}

        state.current_match = {
            "idea": idea,
            "investor": next_investor,
            "score": match_score(idea, next_investor),
            "reason": log_msg,
        }
        save_sessions()
        return {"success": True, "match": state.current_match, "message": log_msg}

    elif state.user_type == "investor":
        investor = state.current_match["investor"]
        rejected_idea_id = state.current_match["idea"].get("id")
        if rejected_idea_id:
            state.rejected_ids.add(rejected_idea_id)

        try:
            next_idea, log_msg = await asyncio.to_thread(
                store.run_and_record,
                "agent6_investor", user_id, session_ref,
                run_agent6_for_investor, investor, state.shortlisted_ideas, state.rejected_ids,
            )
        except Exception as exc:
            import traceback; traceback.print_exc()
            raise HTTPException(status_code=500, detail="Agent 6 failed — please try again.")

        if not next_idea:
            state.current_match = None
            save_sessions()
            return {"success": True, "match": None, "message": log_msg}

        state.current_match = {
            "idea": next_idea,
            "investor": investor,
            "score": match_score(next_idea, investor),
            "reason": log_msg,
        }
        save_sessions()
        return {"success": True, "match": state.current_match, "message": log_msg}

    else:
        raise HTTPException(status_code=403, detail="No active session.")


# ---------------------------------------------------------------------------
# AGREEMENT (only after meeting)
#
# Chat itself is now real, human-to-human messaging over Supabase Realtime
# (see frontend/src/components/matches/RealtimeChatPanel.jsx) — the old
# /api/chat endpoint here had the LLM roleplay the counterpart, which this
# session's work replaced. Agent 7 still drafts the term sheet; it takes the
# real transcript from the client instead of an in-memory AI chat history.
# ---------------------------------------------------------------------------

def _build_agreement_inputs(user_id: str, slot: Optional[dict] = None):
    """
    Prefers the confirmed meeting slot's own idea/investor (always correct
    for whichever meeting was actually confirmed) over state.current_match,
    which is just a snapshot from this user's last pipeline run and can be
    None or point at someone else entirely — the same staleness gap that
    caused current_match to miss real raised-hand/request-meeting interest
    (see PendingInterestCard). Falls back to current_match only for the
    debug "rerun agent7" path, which has no slot to work from.
    """
    state = get_user_state(user_id)
    slot_investor = (slot or {}).get("investor")
    slot_idea = (slot or {}).get("idea")

    if state.user_type == "founder":
        investor_data = slot_investor or (state.current_match or {}).get("investor")
        if not investor_data:
            raise HTTPException(status_code=400, detail="No active match found.")
        founder_data = state.founder_profile
        # state.idea_details is just the LAST activate/refresh attempt's
        # result — if that attempt failed (e.g. Agent 1 hit a Groq rate
        # limit) it's {"error": ...}, which would otherwise get handed
        # straight to Agent 7 as this founder's "idea", producing a term
        # sheet built from an error message instead of the real idea even
        # though the live idea's actual analysis is still intact in the pool.
        # Same durable-pool-over-fragile-snapshot fix as active_idea_id/my_idea.
        my_idea = next((i for i in state.ideas_pool if i.get("id") == state.active_idea_id), None)
        if my_idea:
            idea_data = {
                "idea": {
                    "problem": my_idea.get("problem"),
                    "solution": my_idea.get("solution"),
                    "target_market": my_idea.get("target_market"),
                },
                "mvp": {
                    "must_have": my_idea.get("features_must_have", []),
                    "nice_to_have": my_idea.get("features_nice_to_have", []),
                },
                "roadmap": {"steps": my_idea.get("roadmap", [])},
            }
        else:
            idea_data = state.idea_details
    else:
        idea = slot_idea or (state.current_match or {}).get("idea")
        if not idea:
            raise HTTPException(status_code=400, detail="No active match found.")
        founder_data = idea.get("founder")
        idea_data = {
            "idea": {
                "problem": idea.get("problem"),
                "solution": idea.get("solution"),
                "target_market": idea.get("target_market"),
            },
            "mvp": {
                "must_have": idea.get("features_must_have", []),
                "nice_to_have": idea.get("features_nice_to_have", []),
            },
            "roadmap": {"steps": idea.get("roadmap", [])},
        }
        investor_data = state.investor_profile

    return founder_data, idea_data, investor_data


@app.post("/api/agreement")
async def generate_agreement(data: dict, user_id: str = Depends(get_current_user_id)):
    state = get_user_state(user_id)
    slot_id = data.get("slot_id", "")
    if state.user_type == "founder":
        _ensure_active_idea_id(state, user_id)
    key = (
        _meeting_key(state.active_idea_id, slot_id) if state.user_type == "founder"
        else _meeting_key(slot_id, (state.investor_profile or {}).get("id"))
    )
    with meeting_lock:
        slot = GLOBAL_MEETING_REQUESTS.get(key)
    if not slot or not slot.get("both_opted_in"):
        raise HTTPException(status_code=403, detail="Agreement requires a confirmed meeting.")

    founder_data, idea_data, investor_data = _build_agreement_inputs(user_id, slot)

    # Prefer the real transcript from the Supabase realtime chat (sent by the
    # client as [{"sender": "Founder"|"Investor", "text": "..."}]); fall back
    # to the legacy in-memory chat_history for any older/rerun call sites.
    transcript = data.get("messages") or state.chat_history

    # Equity share and USD amount are asked directly in the term sheet modal
    # rather than left for Agent 7 to guess at from the chat log — that used
    # to misread ambiguous negotiation back-and-forth into the wrong numbers.
    try:
        equity_percent = float(data.get("equity_percent"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="equity_percent is required.")
    if not (0 < equity_percent <= 100):
        raise HTTPException(status_code=400, detail="equity_percent must be between 0 and 100.")
    try:
        amount_usd = float(data.get("amount_usd"))
    except (TypeError, ValueError):
        raise HTTPException(status_code=400, detail="amount_usd is required.")
    if amount_usd <= 0:
        raise HTTPException(status_code=400, detail="amount_usd must be greater than 0.")
    deal_terms = {"equity_percent": equity_percent, "amount_usd": amount_usd}
    state.last_deal_terms = deal_terms

    agreement_md = await asyncio.to_thread(
        store.run_and_record,
        "agent7", user_id, state.session_id, run_agent7, founder_data, idea_data, investor_data, transcript, deal_terms
    )

    return {"success": True, "agreement": agreement_md}


# ---------------------------------------------------------------------------
# AGENTS DASHBOARD
# ---------------------------------------------------------------------------

@app.get("/api/agents/status")
async def agents_status(user_id: str = Depends(get_current_user_id)):
    return {"agents": store.list_profiles(user_id)}


# Guards against a double-click (or a slow-network auto-retry) firing two
# overlapping reruns for the same (agent_id, user_id). Without this, two
# concurrent store.run_and_record calls for the same key race their
# "running"/"done" SQLite writes — each opens its own connection and the
# ON CONFLICT DO UPDATE is atomic per-write, but there's no coordination
# between the two calls, so whichever happens to finish last wins even if
# it was the one that started first (an older, possibly-failed run's status
# can clobber a newer, successful one's). Rejecting the second request
# outright is simpler and safer than trying to reconcile out-of-order
# completions after the fact.
_in_flight_reruns: set = set()
_in_flight_reruns_lock = threading.Lock()


@app.post("/api/agents/{agent_id}/rerun")
async def rerun_agent(agent_id: str, user_id: str = Depends(get_current_user_id)):
    key = (agent_id, user_id)
    with _in_flight_reruns_lock:
        if key in _in_flight_reruns:
            raise HTTPException(status_code=409, detail="This step is already running — please wait for it to finish.")
        _in_flight_reruns.add(key)
    try:
        res = await _dispatch_rerun(agent_id, user_id)
        save_sessions()
        return res
    except HTTPException:
        raise
    except Exception as exc:
        raise HTTPException(status_code=500, detail=str(exc))
    finally:
        with _in_flight_reruns_lock:
            _in_flight_reruns.discard(key)


async def _dispatch_rerun(agent_id: str, user_id: str):
    state = get_user_state(user_id)
    session_ref = state.session_id

    # Every branch below calls into an agent that may hit Groq — run_and_record
    # is run in a worker thread throughout so a slow/rerun LLM call can't
    # freeze the event loop for every other concurrent user.
    if agent_id == "agent1":
        if not state.idea_raw_text:
            raise HTTPException(
                status_code=400,
                detail="No cached idea text available. Submit the founder form with raw idea text first.",
            )
        await asyncio.to_thread(store.run_and_record, "agent1", user_id, session_ref, run_agent1, state.idea_raw_text, is_raw_text=True)

    elif agent_id == "agent2_founder":
        if not state.founder_raw_input:
            raise HTTPException(status_code=400, detail="No cached founder input. Submit the founder form first.")
        await asyncio.to_thread(
            store.run_and_record,
            "agent2_founder", user_id, session_ref, analyze_founder_profile, **state.founder_raw_input
        )

    elif agent_id == "agent2_investor":
        if not state.investor_raw_input:
            raise HTTPException(status_code=400, detail="No cached investor input. Submit the investor form first.")
        await asyncio.to_thread(
            store.run_and_record,
            "agent2_investor", user_id, session_ref, analyze_investor_profile, **state.investor_raw_input
        )

    elif agent_id == "agent3":
        if not state.founder_profile:
            raise HTTPException(status_code=400, detail="No founder profile available yet. Submit the founder form first.")
        # Delete old resume file before regenerating so stale cached .docx is never served
        old_path = state.resume_path
        if old_path and os.path.isfile(old_path):
            try:
                os.unlink(old_path)
            except Exception:
                pass
        state.resume_path = None
        state.resume_path = await asyncio.to_thread(
            store.run_and_record,
            "agent3", user_id, session_ref, run_agent3, state.founder_profile,
            output_dir="static/resumes", unique_id=user_id,
        )

    elif agent_id == "agent4":
        # Snapshot under pool_lock — a concurrent pipeline run for another
        # user (BackgroundTasks/asyncio.to_thread, real worker threads) can
        # be mid-write on these same shared lists.
        with pool_lock:
            ideas_snapshot = list(state.ideas_pool)
            investors_snapshot = list(state.investors_pool)
        candidate_ideas = [i for i in ideas_snapshot if i.get("id") not in state.rejected_ids] if state.user_type == "investor" else ideas_snapshot
        candidate_investors = [inv for inv in investors_snapshot if inv.get("id") not in state.rejected_ids] if state.user_type == "founder" else investors_snapshot
        top_ideas, top_investors, _ = await asyncio.to_thread(
            store.run_and_record,
            "agent4", user_id, session_ref, run_agent4, candidate_ideas, candidate_investors, limit=25
        )
        # Re-filter against rejected_ids as it stands NOW, not as it stood
        # before the (potentially multi-second) Groq call above — a reject
        # made from another tab while this was in flight must not resurface
        # here just because it was assembled from a stale pre-await snapshot.
        state.shortlisted_ideas = [i for i in top_ideas if i.get("id") not in state.rejected_ids]
        state.shortlisted_investors = [inv for inv in top_investors if inv.get("id") not in state.rejected_ids]

    elif agent_id == "agent5":
        if not state.shortlisted_ideas or not state.shortlisted_investors:
            raise HTTPException(status_code=400, detail="No shortlist available yet. Run Agent 4 first.")
        matches, checked_ideas, _ = await asyncio.to_thread(
            store.run_and_record,
            "agent5", user_id, session_ref, run_agent5, state.shortlisted_ideas, state.shortlisted_investors
        )
        state.matched_pairs = matches
        _merge_mvp_readiness(state.shortlisted_ideas, checked_ideas)

    elif agent_id in ("agent6_founder", "agent6_investor"):
        raise HTTPException(
            status_code=400,
            detail="Agent 6 triggers from the 'Reject / Find next match' action in the app, not a standalone rerun.",
        )

    elif agent_id == "agent7":
        if not state.last_deal_terms:
            raise HTTPException(
                status_code=400,
                detail="No deal terms on file yet — generate a term sheet from the Matches page first, then rerun.",
            )
        founder_data, idea_data, investor_data = _build_agreement_inputs(user_id)
        await asyncio.to_thread(
            store.run_and_record,
            "agent7", user_id, session_ref, run_agent7,
            founder_data, idea_data, investor_data, state.chat_history, state.last_deal_terms,
        )

    else:
        raise HTTPException(status_code=404, detail=f"Unknown agent_id: {agent_id}")

    return {"success": True, "agent_id": agent_id}
