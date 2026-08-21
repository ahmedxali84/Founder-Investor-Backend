import os
import json
import re
import time
import requests
from pathlib import Path
from dotenv import load_dotenv

# Load .env from the project root (one level above the agents/ folder)
_root_env = Path(__file__).resolve().parent.parent / ".env"
load_dotenv(dotenv_path=_root_env, override=True)

GROQ_API_KEY = os.getenv("GROQ_API_KEY", "").strip()
# Groq decommissioned every llama-3.x/mixtral chat model previously used here
# (confirmed via a live /v1/models lookup against this account — none of
# llama-3.3-70b-versatile, llama-3.1-8b-instant, or mixtral-8x7b-32768 exist
# any more; two 404s and a "model_decommissioned" 400 respectively). The
# gpt-oss models return the actual JSON in `content` with reasoning kept
# separate in a `reasoning` field, so callers don't need to strip anything.
GROQ_MODEL = os.getenv("GROQ_MODEL", "openai/gpt-oss-120b").strip()
GROQ_CHAT_URL = "https://api.groq.com/openai/v1/chat/completions"

# Agent 3 (resume) and Agent 7 (term sheet) draw from their own keys/quotas
# instead of sharing GROQ_API_KEY with agents 1/2/4/5/6 — one heavy agent
# maxing out the shared daily token quota used to take every agent down with
# it. Falls back to GROQ_API_KEY if a dedicated key isn't set.
GROQ_API_KEY_AGENT3 = os.getenv("GROQ_API_KEY_AGENT3", "").strip() or GROQ_API_KEY
GROQ_API_KEY_AGENT7 = os.getenv("GROQ_API_KEY_AGENT7", "").strip() or GROQ_API_KEY


class GroqQuotaExhaustedError(Exception):
    """
    A 429 whose retry-after window is measured in hours, not seconds — a
    per-day (TPD) token quota, not the per-minute rate limit the existing
    retry/backoff loop is actually built to ride out. Retrying 3x with
    exponential backoff against a daily quota just wastes ~15s failing the
    same way three times before giving the same generic error anyway, so
    this gets raised immediately instead, letting callers (agents/store.py's
    run_and_record, which already records the exception message verbatim as
    the failure reason) surface a message that actually explains what's
    wrong instead of a raw HTTPError.
    """
    pass


FALLBACK_MODELS = ["openai/gpt-oss-20b"]

def ask_llm(prompt: str, system_prompt: str = "You are a helpful assistant.", _max_retries: int = 3, api_key: str = "") -> str:
    """Send a prompt to Groq and return the text reply. Retries with backoff on 429 (rate limit).
    Falls back to secondary models if the primary model daily token quota is exhausted.
    """
    key = (api_key or GROQ_API_KEY).strip()
    if not key:
        raise RuntimeError("Missing or empty GROQ_API_KEY in .env")

    headers = {
        "Authorization": f"Bearer {key}",
        "Content-Type": "application/json"
    }

    # Attempt primary model, then secondary fallback models if quota hits
    models_to_try = [GROQ_MODEL] + [m for m in FALLBACK_MODELS if m != GROQ_MODEL]
    last_error = None

    for current_model in models_to_try:
        payload = {
            "model": current_model,
            "messages": [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": prompt}
            ],
            "temperature": 0.3,
            "max_tokens": 3000
        }

        for attempt in range(_max_retries):
            try:
                resp = requests.post(GROQ_CHAT_URL, headers=headers, json=payload, timeout=45)
                if resp.status_code == 429:
                    error_body = {}
                    try:
                        error_body = resp.json().get("error", {})
                    except Exception:
                        pass
                    
                    if error_body.get("type") == "tokens" and "per day" in error_body.get("message", "").lower():
                        # Daily quota exhausted for this model — break retry loop to try fallback model
                        break

                    last_error = requests.exceptions.HTTPError(f"429 Too Many Requests ({current_model} attempt {attempt + 1}/{_max_retries})", response=resp)
                    wait = float(resp.headers.get("Retry-After", 2 ** (attempt + 1)))
                    if attempt < _max_retries - 1:
                        time.sleep(min(wait, 15))
                        continue
                    break

                resp.raise_for_status()
                return resp.json()["choices"][0]["message"]["content"]
            except Exception as exc:
                last_error = exc
                if attempt < _max_retries - 1 and "429" not in str(exc):
                    time.sleep(2)
                    continue

    if last_error:
        raise last_error
    raise GroqQuotaExhaustedError("All Groq models & daily quotas exhausted.")

def _clean_json_string(text: str) -> str:
    text = text.strip()
    match = re.search(r"\{.*\}", text, re.DOTALL)
    if match:
        text = match.group(0)
    elif text.startswith("```"):
        text = re.sub(r"^```(?:json)?\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    # Remove trailing commas before closing braces/brackets
    text = re.sub(r",\s*([\}\]])", r"\1", text)
    return text.strip()

def ask_llm_json(prompt: str, system_prompt: str = "You are a helpful assistant that only replies in valid JSON.", api_key: str = "") -> dict:
    """Send a prompt to Groq and parse the reply as a JSON object, with retries and safe fallback."""
    reply = ask_llm(prompt, system_prompt, api_key=api_key)
    clean = _clean_json_string(reply)
    try:
        return json.loads(clean)
    except Exception:
        try:
            reply_retry = ask_llm(prompt + "\nReturn ONLY a valid, well-formed JSON object with no markdown fences or trailing text.", system_prompt, api_key=api_key)
            clean_retry = _clean_json_string(reply_retry)
            return json.loads(clean_retry)
        except Exception as err:
            print(f"[ask_llm_json] Failed to parse JSON response: {err}")
            return {"error": "Failed to parse JSON response from LLM", "raw": reply}
