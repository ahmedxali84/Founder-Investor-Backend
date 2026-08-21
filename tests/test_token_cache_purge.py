"""
Regression coverage for the unbounded _token_verify_cache growth found
during the strict post-deployment audit: entries were only evicted when the
SAME token was looked up again after its TTL expired, but Supabase issues a
brand-new JWT on every session refresh — so most entries were never looked
up again by their own key and would accumulate for the life of the process.
Fixed with a proactive sweep (_purge_stale_token_cache), throttled to run at
most once per TTL window so it doesn't turn every cache write into an O(n)
scan.
"""
import time

import main_app as m


def test_purge_removes_stale_entries():
    now = time.time()
    for i in range(20):
        m._token_verify_cache[f"stale-{i}"] = (now - m._TOKEN_VERIFY_TTL_SECONDS - 5, {"id": f"user-{i}"})
    m._LAST_TOKEN_CACHE_PURGE[0] = 0.0  # force the throttle to allow an immediate purge

    m._purge_stale_token_cache()

    assert len(m._token_verify_cache) == 0


def test_purge_keeps_fresh_entries():
    now = time.time()
    m._token_verify_cache.clear()
    m._token_verify_cache["fresh"] = (now, {"id": "still-active-user"})
    m._token_verify_cache["stale"] = (now - m._TOKEN_VERIFY_TTL_SECONDS - 5, {"id": "gone-user"})
    m._LAST_TOKEN_CACHE_PURGE[0] = 0.0

    m._purge_stale_token_cache()

    assert "fresh" in m._token_verify_cache
    assert "stale" not in m._token_verify_cache


def test_purge_is_throttled_to_once_per_ttl_window():
    now = time.time()
    m._token_verify_cache.clear()
    m._token_verify_cache["stale"] = (now - m._TOKEN_VERIFY_TTL_SECONDS - 5, {"id": "gone-user"})
    m._LAST_TOKEN_CACHE_PURGE[0] = now  # pretend a purge just ran

    m._purge_stale_token_cache()  # should no-op — within the throttle window

    assert "stale" in m._token_verify_cache, "purge should be throttled, not run on every call"
