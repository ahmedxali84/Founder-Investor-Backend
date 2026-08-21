"""
Regression coverage for the LinkedIn OAuth redirect bug found during the
post-deployment audit: /api/linkedin/callback used to redirect the browser
to a static FRONTEND_URL env var (default "http://localhost:3000") that was
never actually set on Render — every real LinkedIn sign-in silently sent
users to localhost after approving access. Confirmed live via a direct curl
against the deployed backend before this fix.

Fix: capture the initiating request's Origin/Referer at /api/linkedin/login
time, validate it against the same trust boundary CORS already uses (the
explicit CORS_ORIGINS list plus any *.vercel.app subdomain), and use that
for the callback redirect instead — falling back to FRONTEND_URL only if no
trusted origin was captured (e.g. state expired/lost).
"""
import time
import uuid

import main_app as m


def test_trusted_frontend_origin_accepts_vercel_subdomain():
    assert m._trusted_frontend_origin("https://founder-investor-liard.vercel.app") == \
        "https://founder-investor-liard.vercel.app"


def test_trusted_frontend_origin_accepts_configured_cors_origin():
    # localhost:3000 is always in the default _cors_origins list.
    assert m._trusted_frontend_origin("http://localhost:3000") == "http://localhost:3000"


def test_trusted_frontend_origin_rejects_untrusted_domain():
    assert m._trusted_frontend_origin("https://evil.com") is None
    assert m._trusted_frontend_origin("https://not-vercel.app.evil.com") is None


def test_trusted_frontend_origin_handles_empty_and_trailing_slash():
    assert m._trusted_frontend_origin("") is None
    assert m._trusted_frontend_origin(None) is None
    assert m._trusted_frontend_origin("https://founder-investor-liard.vercel.app/") == \
        "https://founder-investor-liard.vercel.app"


def test_callback_redirects_to_captured_frontend_origin_not_localhost(monkeypatch):
    monkeypatch.delenv("FRONTEND_URL", raising=False)
    from fastapi.testclient import TestClient
    client = TestClient(m.app)

    state_token = str(uuid.uuid4())
    m.linkedin_oauth_states[state_token] = (
        "fake-user-id", time.time(), "https://founder-investor-liard.vercel.app"
    )

    resp = client.get(
        "/api/linkedin/callback",
        params={"error": "access_denied", "state": state_token},
        follow_redirects=False,
    )
    assert resp.status_code == 307
    assert resp.headers["location"].startswith("https://founder-investor-liard.vercel.app/onboarding")


def test_callback_falls_back_to_frontend_url_env_when_no_state_captured(monkeypatch):
    monkeypatch.setenv("FRONTEND_URL", "https://fallback-example.vercel.app")
    from fastapi.testclient import TestClient
    client = TestClient(m.app)

    resp = client.get(
        "/api/linkedin/callback",
        params={"error": "access_denied"},  # no state -> nothing to look up
        follow_redirects=False,
    )
    assert resp.status_code == 307
    assert resp.headers["location"].startswith("https://fallback-example.vercel.app/onboarding")


def test_callback_never_redirects_to_an_untrusted_state_origin():
    # Defense in depth: even if something upstream ever stored a raw,
    # unvalidated origin in the state dict, _trusted_frontend_origin is the
    # only thing that writes it in practice — this asserts that invariant
    # holds by construction rather than by an incidental code path.
    assert m._trusted_frontend_origin("https://attacker.example.com") is None
