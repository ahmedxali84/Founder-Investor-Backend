"""
Real "Sign in with LinkedIn" (OpenID Connect) helper.

This is the only legitimate way to pull real LinkedIn-confirmed facts about
a specific person: LinkedIn's OpenID Connect product returns the name,
email, and profile picture of whoever just authenticated, straight from
LinkedIn's own servers. It cannot look up an arbitrary third party by name —
only the person who is actually signing in right now, which is the point:
consented, verified, real data instead of a scrape or an LLM guess.

Docs: https://learn.microsoft.com/en-us/linkedin/consumer/integrations/self-serve/sign-in-with-linkedin-v2
"""
import os
import secrets
import httpx
from pathlib import Path
from urllib.parse import urlencode
from dotenv import load_dotenv

_root_env = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=_root_env, override=True)

LINKEDIN_CLIENT_ID = os.getenv("LINKEDIN_CLIENT_ID", "").strip()
LINKEDIN_CLIENT_SECRET = os.getenv("LINKEDIN_CLIENT_SECRET", "").strip()
LINKEDIN_REDIRECT_URI = os.getenv("LINKEDIN_REDIRECT_URI", "").strip()

AUTHORIZATION_URL = "https://www.linkedin.com/oauth/v2/authorization"
TOKEN_URL = "https://www.linkedin.com/oauth/v2/accessToken"
USERINFO_URL = "https://api.linkedin.com/v2/userinfo"

SCOPES = "openid profile email"


def is_configured() -> bool:
    return bool(LINKEDIN_CLIENT_ID and LINKEDIN_CLIENT_SECRET and LINKEDIN_REDIRECT_URI)


def build_authorization_url(state: str = "") -> str:
    if not is_configured():
        raise RuntimeError(
            "LinkedIn OAuth is not configured. Set LINKEDIN_CLIENT_ID, "
            "LINKEDIN_CLIENT_SECRET, and LINKEDIN_REDIRECT_URI in .env "
            "(create an app at https://www.linkedin.com/developers/apps and "
            "enable the 'Sign In with LinkedIn using OpenID Connect' product)."
        )
    state = state or secrets.token_urlsafe(16)
    params = {
        "response_type": "code",
        "client_id": LINKEDIN_CLIENT_ID,
        "redirect_uri": LINKEDIN_REDIRECT_URI,
        "scope": SCOPES,
        "state": state,
    }
    return f"{AUTHORIZATION_URL}?{urlencode(params)}"


async def exchange_code_for_userinfo(code: str) -> dict:
    """
    Exchange the OAuth 'code' for an access token, then fetch the real,
    LinkedIn-confirmed profile of whoever just logged in.
    Returns: {"name", "given_name", "family_name", "email", "picture", "sub"}
    """
    if not is_configured():
        raise RuntimeError("LinkedIn OAuth is not configured.")

    async with httpx.AsyncClient(timeout=10.0) as client:
        token_res = await client.post(
            TOKEN_URL,
            data={
                "grant_type": "authorization_code",
                "code": code,
                "redirect_uri": LINKEDIN_REDIRECT_URI,
                "client_id": LINKEDIN_CLIENT_ID,
                "client_secret": LINKEDIN_CLIENT_SECRET,
            },
            headers={"Content-Type": "application/x-www-form-urlencoded"},
        )
        if token_res.status_code >= 400:
            # LinkedIn's actual error (e.g. invalid_client, invalid_grant) is in
            # the response body — raise_for_status() alone would hide it behind
            # a generic "400 Bad Request" with nothing actionable to log.
            raise RuntimeError(f"token exchange failed ({token_res.status_code}): {token_res.text}")
        access_token = token_res.json()["access_token"]

        userinfo_res = await client.get(
            USERINFO_URL,
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if userinfo_res.status_code >= 400:
            raise RuntimeError(f"userinfo fetch failed ({userinfo_res.status_code}): {userinfo_res.text}")
        data = userinfo_res.json()

    return {
        "sub": data.get("sub", ""),
        "name": data.get("name", ""),
        "given_name": data.get("given_name", ""),
        "family_name": data.get("family_name", ""),
        "email": data.get("email", ""),
        "picture": data.get("picture", ""),
        "verified": True,
    }
