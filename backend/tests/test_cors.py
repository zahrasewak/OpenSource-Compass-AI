"""CORS and extension-origin behaviour.

The extension's background worker calls the API with host permission, which is NOT subject to CORS, so the backend needs no CORS
configuration for it and must not grant any by default. CORS only matters for browsers calling the API from a web page; it is
therefore closed unless an operator lists origins explicitly.
"""

from fastapi.testclient import TestClient

from app.core.config import Settings
from app.main import create_app

EXT = "chrome-extension://abcdefghijklmnopabcdefghijklmnop"


def client_with(monkeypatch, **kw) -> TestClient:
    cfg = Settings(auth_secret="s" * 48, _env_file=None, **kw)
    monkeypatch.setattr("app.main.get_settings", lambda: cfg)
    return TestClient(create_app(), raise_server_exceptions=False)


def preflight(c, origin, method="POST", headers="authorization,content-type"):
    return c.options("/api/v1/profile", headers={"Origin": origin, "Access-Control-Request-Method": method, "Access-Control-Request-Headers": headers})


def test_by_default_no_origin_gets_any_cors_grant(monkeypatch):
    c = client_with(monkeypatch)
    for origin in (EXT, "https://github.com", "https://evil.example"):
        r = c.get("/health", headers={"Origin": origin})
        assert "access-control-allow-origin" not in r.headers
        assert "access-control-allow-origin" not in preflight(c, origin).headers


def test_only_explicitly_listed_origins_are_granted_and_only_the_needed_methods_and_headers(monkeypatch):
    c = client_with(monkeypatch, cors_origins=["https://app.example.com"])
    ok = preflight(c, "https://app.example.com")
    assert ok.status_code == 200 and ok.headers["access-control-allow-origin"] == "https://app.example.com"
    assert "*" not in ok.headers["access-control-allow-origin"]
    assert "access-control-allow-credentials" not in ok.headers  # no cookies are ever involved: identity is the bearer token
    for other in (EXT, "https://github.com", "https://evil.example"):
        assert "access-control-allow-origin" not in preflight(c, other).headers
    assert preflight(c, "https://app.example.com", method="PATCH").status_code == 400  # not an allowed method
    assert preflight(c, "https://app.example.com", headers="x-custom").status_code == 400  # not an allowed header


def test_extension_style_requests_work_without_any_cors_configuration(monkeypatch):
    """A request from the worker has no Origin handling at all: it just needs a bearer token."""
    c = client_with(monkeypatch)
    assert c.get("/api/v1/auth/me").status_code == 401  # reaches the API and is refused for lack of credentials, not by CORS
    assert c.get("/health").status_code == 200
