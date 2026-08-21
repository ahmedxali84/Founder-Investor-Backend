"""
Regression coverage for agents/llm.py's TPD (daily quota) fast-fail path
added this session — a real Groq daily-quota response on one model should
raise immediately within that model (not burn through all 3 retries), but
still fall through to try each of the other FALLBACK_MODELS once before
finally raising GroqQuotaExhaustedError — that fallback chain is the whole
point of FALLBACK_MODELS existing, so "fail fast" means no wasted retries
per model, not "give up on the first model's quota without trying the rest."
A per-minute 429 (no "per day" in the message) must still use the existing
retry/backoff behavior unchanged.
"""
import time
import pytest
import agents.llm as llm_module


class _FakeResponse:
    def __init__(self, status_code, json_body, headers=None):
        self.status_code = status_code
        self._json_body = json_body
        self.headers = headers or {}

    def json(self):
        return self._json_body

    def raise_for_status(self):
        pass


TPD_BODY = {
    "error": {
        "message": (
            "Rate limit reached for model `llama-3.3-70b-versatile` in organization "
            "`org_test` service tier `on_demand` on tokens per day (TPD): "
            "Limit 100000, Used 99913, Requested 101. Please try again in 12s."
        ),
        "type": "tokens",
        "code": "rate_limit_exceeded",
    }
}

TPM_BODY = {
    "error": {
        "message": "Rate limit reached ... on tokens per minute (TPM): Limit 12000, Used 12000, Requested 500.",
        "type": "tokens",
        "code": "rate_limit_exceeded",
    }
}


def test_tpd_quota_fails_fast_without_exhausting_retries(monkeypatch):
    call_count = 0

    def fake_post(url, headers, json, timeout):
        nonlocal call_count
        call_count += 1
        return _FakeResponse(429, TPD_BODY)

    monkeypatch.setattr(llm_module.requests, "post", fake_post)
    monkeypatch.setattr(llm_module, "GROQ_API_KEY", "fake-key-for-test")
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    with pytest.raises(llm_module.GroqQuotaExhaustedError):
        llm_module.ask_llm("hello", _max_retries=3)

    # One call per model in the fallback chain (primary + 1 fallback) —
    # each fails fast on its own hopeless daily quota (no 3x retry loop per
    # model) but the chain itself still runs to completion before giving up.
    assert call_count == 2, "should fail fast within each model, but still try every fallback model"


def test_per_minute_429_still_retries_as_before(monkeypatch):
    call_count = 0

    def fake_post(url, headers, json, timeout):
        nonlocal call_count
        call_count += 1
        if call_count < 3:
            return _FakeResponse(429, TPM_BODY, headers={"Retry-After": "0"})
        return _FakeResponse(200, {"choices": [{"message": {"content": "ok"}}]})

    monkeypatch.setattr(llm_module.requests, "post", fake_post)
    monkeypatch.setattr(llm_module, "GROQ_API_KEY", "fake-key-for-test")
    monkeypatch.setattr(time, "sleep", lambda *_: None)

    result = llm_module.ask_llm("hello", _max_retries=3)

    assert result == "ok"
    assert call_count == 3, "existing per-minute retry/backoff behavior must be unchanged"
