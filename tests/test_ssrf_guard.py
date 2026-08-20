"""
Regression coverage for the SSRF fix in _is_public_hostname/_reachable_url
(main_app.py) — an authenticated founder's mvp_url used to be checked only
for URL *shape*, then handed straight to a real outbound HEAD request with
redirects auto-followed. That made /api/validate-mvp-url a blind internal-
network-scanning oracle: point it at a cloud metadata hostname or any
attacker-DNS-controlled domain resolving to an internal IP, and read back
whether it's alive.

DNS is mocked throughout (monkeypatching socket.getaddrinfo) rather than
hitting real resolvers — deterministic, fast, and doesn't depend on network
access being available wherever this runs (including CI).
"""
import asyncio
import socket

import pytest

import main_app as m


def _addrinfo_for(ip):
    return [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (ip, 0))]


@pytest.mark.parametrize("internal_ip", [
    "127.0.0.1",        # loopback
    "169.254.169.254",  # link-local — the cloud metadata endpoint IP
    "10.0.0.5",          # RFC1918 private
    "192.168.1.1",       # RFC1918 private
    "172.16.0.1",        # RFC1918 private
    "0.0.0.0",           # unspecified
    "224.0.0.1",         # multicast
])
def test_is_public_hostname_rejects_internal_targets(monkeypatch, internal_ip):
    monkeypatch.setattr(m.socket, "getaddrinfo", lambda host, port: _addrinfo_for(internal_ip))
    assert m._is_public_hostname("attacker-controlled.example") is False


def test_is_public_hostname_accepts_a_real_public_target(monkeypatch):
    monkeypatch.setattr(m.socket, "getaddrinfo", lambda host, port: _addrinfo_for("8.8.8.8"))
    assert m._is_public_hostname("real-startup-mvp.example") is True


def test_is_public_hostname_rejects_unresolvable_host(monkeypatch):
    def raise_gaierror(host, port):
        raise socket.gaierror("name resolution failed")
    monkeypatch.setattr(m.socket, "getaddrinfo", raise_gaierror)
    assert m._is_public_hostname("does-not-exist.invalid") is False


class _FakeResponse:
    def __init__(self, status_code, headers=None):
        self.status_code = status_code
        self.headers = headers or {}


class _FakeAsyncClient:
    """Records every constructor call and every .head() URL it's asked for."""
    instances = []

    def __init__(self, *args, **kwargs):
        self.kwargs = kwargs
        self.calls = []
        _FakeAsyncClient.instances.append(self)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def head(self, url):
        self.calls.append(url)
        return _FakeAsyncClient.responses.pop(0)


def _all_calls():
    return [url for inst in _FakeAsyncClient.instances for url in inst.calls]


@pytest.fixture(autouse=True)
def fake_httpx(monkeypatch):
    _FakeAsyncClient.instances = []
    _FakeAsyncClient.responses = []
    monkeypatch.setattr(m.httpx, "AsyncClient", _FakeAsyncClient)
    yield _FakeAsyncClient


def test_reachable_url_never_makes_a_request_for_an_internal_target(monkeypatch, fake_httpx):
    monkeypatch.setattr(m.socket, "getaddrinfo", lambda host, port: _addrinfo_for("169.254.169.254"))
    result = asyncio.run(m._reachable_url("http://metadata.internal.example/"))
    assert result is False
    assert _all_calls() == [], "no outbound request should ever be attempted against an internal target"


def test_reachable_url_revalidates_every_redirect_hop(monkeypatch, fake_httpx):
    """
    An externally-hosted, legitimately-public-looking URL that then redirects
    to an internal target must be rejected on the redirect hop, not just the
    first one — this is exactly the bypass httpx's own follow_redirects would
    have allowed.
    """
    def fake_getaddrinfo(host, port):
        if host == "internal.example":
            return _addrinfo_for("127.0.0.1")
        return _addrinfo_for("8.8.8.8")
    monkeypatch.setattr(m.socket, "getaddrinfo", fake_getaddrinfo)

    fake_httpx.responses = [_FakeResponse(302, headers={"location": "http://internal.example/"})]

    result = asyncio.run(m._reachable_url("http://public-looking.example/"))

    assert result is False
    # Only the first hop's request should have actually happened — the
    # redirect target was rejected before any request was made to it.
    assert _all_calls() == ["http://public-looking.example/"]


def test_reachable_url_disables_httpxs_own_redirect_following(monkeypatch, fake_httpx):
    """
    Each hop must be re-validated manually — confirms the fix didn't leave
    follow_redirects=True (which would silently bypass the per-hop check)
    anywhere in the client construction.
    """
    monkeypatch.setattr(m.socket, "getaddrinfo", lambda host, port: _addrinfo_for("8.8.8.8"))
    fake_httpx.responses = [_FakeResponse(200)]

    asyncio.run(m._reachable_url("http://real-startup-mvp.example/"))

    assert fake_httpx.instances[0].kwargs.get("follow_redirects") is False


def test_reachable_url_accepts_a_genuinely_reachable_public_target(monkeypatch, fake_httpx):
    monkeypatch.setattr(m.socket, "getaddrinfo", lambda host, port: _addrinfo_for("8.8.8.8"))
    fake_httpx.responses = [_FakeResponse(200)]
    assert asyncio.run(m._reachable_url("http://real-startup-mvp.example/")) is True
