"""Forgot / reset password: redirect, anti-enumeration, throttling, recovery-only tokens.

Supabase Auth is mocked. Recovery tokens are ES256 test JWTs whose ``amr`` claim
mirrors Supabase's: sessions opened from a recovery link carry method ``otp``
(implicit flow) and are what GoTrue's own ``Session.IsRecovery`` accepts.
"""

from __future__ import annotations

import json
import logging
import time
import uuid

import httpx
import pytest

from app.core.config import get_settings
from tests.conftest import auth_header, make_token

FORGOT = "/api/v1/auth/forgot-password"
RESET = "/api/v1/auth/reset-password"
GENERIC = "If an account exists for this email, a reset link has been sent."
NEW = "N3wSecurePassw0rd"


def recovery_token(*, email: str = "owner@example.com", method: str = "otp", age: int = 30, **kw) -> str:
    return make_token(email=email, extra={"amr": [{"method": method, "timestamp": int(time.time()) - age}]}, **kw)


def recover_calls(supabase) -> list[httpx.Request]:
    return [r for r in supabase.requests if r.url.path.endswith("/recover")]


def put_calls(supabase) -> list[httpx.Request]:
    return [r for r in supabase.requests if r.method == "PUT" and r.url.path.endswith("/user")]


def logout_calls(supabase) -> list[httpx.Request]:
    return [r for r in supabase.requests if r.url.path.endswith("/logout")]


@pytest.fixture
def reset_ok(supabase):
    supabase.add("PUT", "/auth/v1/user", httpx.Response(200, json={"id": str(uuid.uuid4())}))
    supabase.add("POST", "/logout", httpx.Response(204))
    return supabase


# =============================================================== forgot password
async def test_registered_and_unregistered_emails_get_identical_response(client, supabase):
    # Supabase itself answers 200 {} for unknown addresses (GoTrue recover.go).
    supabase.add("POST", "/recover", httpx.Response(200, json={}))
    known = await client.post(FORGOT, json={"email": "known@example.com"})
    unknown = await client.post(FORGOT, json={"email": "nobody@example.com"})
    assert known.status_code == unknown.status_code == 200
    assert known.json() == unknown.json() == {"success": True, "message": GENERIC, "data": None, "error": None}


@pytest.mark.parametrize("configured", [
    "sharkhire://auth/reset-password",
    "  sharkhire://auth/reset-password  ",  # surrounding whitespace from an env file is ignored
    "com.sharkhire.app://reset-password",
])
async def test_redirect_is_the_configured_deep_link(client, supabase, configured):
    get_settings().AUTH_PASSWORD_RESET_REDIRECT_URL = configured
    supabase.add("POST", "/recover", httpx.Response(200, json={}))
    for email in ("a@example.com", "b@example.com"):
        assert (await client.post(FORGOT, json={"email": email})).status_code == 200
    expected = configured.strip()  # passed through unchanged otherwise
    calls = recover_calls(supabase)
    assert [c.url.params["redirect_to"] for c in calls] == [expected, expected]  # every request, same URL
    assert json.loads(calls[0].content) == {"email": "a@example.com"}  # implicit flow: no code_challenge


async def test_missing_redirect_configuration_is_an_error_not_success(client, supabase):
    for unset in (None, "", "   "):
        get_settings().AUTH_PASSWORD_RESET_REDIRECT_URL = unset
        r = await client.post(FORGOT, json={"email": "a@example.com"})
        assert r.status_code == 503
    r = await client.post(FORGOT, json={"email": "b@example.com"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "password_reset_not_configured"
    assert not recover_calls(supabase)  # never let Supabase fall back to its Site URL


@pytest.mark.parametrize("payload", [
    {"error_code": "over_email_send_rate_limit", "msg": "email rate limit exceeded"},
    {"msg": "For security purposes, you can only request this after 42 seconds."},
])
async def test_supabase_rate_limit_mapped_to_429(client, supabase, payload):
    supabase.add("POST", "/recover", httpx.Response(429, json=payload, headers={"retry-after": "42"}))
    r = await client.post(FORGOT, json={"email": "a@example.com"})
    assert r.status_code == 429
    body = r.json()
    assert body["error"]["code"] == "RATE_LIMITED" and body["message"] == "Please wait a minute before trying again."
    assert r.headers["Retry-After"] == "42"


@pytest.mark.parametrize(("upstream", "status"), [
    (httpx.Response(500, json={"msg": "smtp: 10.0.0.5 refused"}), 503),
    ("timeout", 503),
])
async def test_internal_failures_are_not_reported_as_success(client, supabase, upstream, status, caplog):
    if upstream == "timeout":
        def slow(_request):
            raise httpx.ReadTimeout("timeout")
        supabase.add("POST", "/recover", slow)
    else:
        supabase.add("POST", "/recover", upstream)
    with caplog.at_level(logging.INFO):
        r = await client.post(FORGOT, json={"email": "secret-user@example.com"})
    assert r.status_code == status and r.json()["success"] is False
    assert "10.0.0.5" not in r.text
    assert "secret-user@example.com" not in caplog.text  # e-mail addresses are not logged


async def test_invalid_email_rejected(client, supabase):
    r = await client.post(FORGOT, json={"email": "not-an-email"})
    assert r.status_code == 422
    assert not recover_calls(supabase)


async def test_per_email_throttle(client, supabase):
    s = get_settings()
    s.RATE_LIMIT_EMAIL_PER_HOUR = 100
    s.RATE_LIMIT_PASSWORD_RESET_PER_EMAIL_PER_HOUR = 2
    supabase.add("POST", "/recover", httpx.Response(200, json={}))
    codes = [(await client.post(FORGOT, json={"email": e})).status_code
             for e in ("victim@example.com", "VICTIM@example.com", "victim@example.com")]
    assert codes == [200, 200, 429]
    assert (await client.post(FORGOT, json={"email": "other@example.com"})).status_code == 200
    assert len(recover_calls(supabase)) == 3
    r = await client.post(FORGOT, json={"email": "victim@example.com"})
    assert r.json()["error"]["code"] == "RATE_LIMITED" and int(r.headers["Retry-After"]) > 0


async def test_per_ip_throttle(client, supabase):
    s = get_settings()
    s.RATE_LIMIT_EMAIL_PER_HOUR = 2
    s.RATE_LIMIT_PASSWORD_RESET_PER_EMAIL_PER_HOUR = 100
    supabase.add("POST", "/recover", httpx.Response(200, json={}))
    codes = [(await client.post(FORGOT, json={"email": f"u{i}@example.com"})).status_code for i in range(3)]
    assert codes == [200, 200, 429]
    assert len(recover_calls(supabase)) == 2


async def test_throttle_applies_equally_to_unknown_addresses(client, supabase):
    """A 429 reveals nothing: unknown and known addresses are throttled the same way."""
    get_settings().RATE_LIMIT_PASSWORD_RESET_PER_EMAIL_PER_HOUR = 1
    supabase.add("POST", "/recover", httpx.Response(200, json={}))
    for email in ("known@example.com", "nobody@example.com"):
        assert (await client.post(FORGOT, json={"email": email})).status_code == 200
        assert (await client.post(FORGOT, json={"email": email})).status_code == 429


# =============================================================== reset password
@pytest.mark.parametrize("method", ["otp", "recovery", "magiclink"])
async def test_valid_recovery_token_succeeds(client, reset_ok, method):
    token = recovery_token(method=method)
    r = await client.post(RESET, json={"access_token": token, "new_password": NEW})
    assert r.status_code == 200, r.text
    assert r.json()["data"] == {"status": "SUCCESS", "sessions_revoked": True}
    [put] = put_calls(reset_ok)
    assert put.headers["Authorization"] == f"Bearer {token}" and json.loads(put.content) == {"password": NEW}
    [logout] = logout_calls(reset_ok)
    assert logout.url.params["scope"] == "global"  # this recovery session + every other session


async def test_bearer_header_still_accepted(client, reset_ok):
    r = await client.post(RESET, json={"new_password": NEW}, headers=auth_header(recovery_token()))
    assert r.status_code == 200


async def test_token_hash_flow(client, supabase, reset_ok):
    from tests.test_auth import session_payload

    supabase.add("POST", "/verify", httpx.Response(200, json=session_payload(email="owner@example.com")))
    r = await client.post(RESET, json={"token_hash": "pkce_recovery_hash", "new_password": NEW})
    assert r.status_code == 200
    verify = [x for x in supabase.requests if x.url.path.endswith("/verify")][-1]
    assert json.loads(verify.content) == {"type": "recovery", "token_hash": "pkce_recovery_hash"}


@pytest.mark.parametrize("token", ["garbage-token-value-xxxxxxxx", "a.b.c" + "x" * 30])
async def test_invalid_token_rejected(client, supabase, token):
    r = await client.post(RESET, json={"access_token": token, "new_password": NEW})
    assert r.status_code == 401 and r.json()["error"]["code"] == "RECOVERY_LINK_INVALID_OR_EXPIRED"
    assert not put_calls(supabase)


async def test_expired_token_rejected(client, supabase):
    r = await client.post(RESET, json={"access_token": recovery_token(expires_in=-120), "new_password": NEW})
    assert r.status_code == 401 and r.json()["error"]["code"] == "RECOVERY_LINK_INVALID_OR_EXPIRED"
    assert not put_calls(supabase)


async def test_stale_recovery_session_rejected(client, supabase):
    r = await client.post(RESET, json={"access_token": recovery_token(age=2 * 3600), "new_password": NEW})
    assert r.status_code == 401 and r.json()["error"]["code"] == "RECOVERY_LINK_INVALID_OR_EXPIRED"
    assert not put_calls(supabase)


@pytest.mark.parametrize("amr", [
    [{"method": "password", "timestamp": int(time.time())}],
    [{"method": "token_refresh", "timestamp": int(time.time())}],
    None,
])
async def test_arbitrary_sign_in_token_rejected(client, supabase, amr):
    extra = {"amr": amr} if amr is not None else {}
    token = make_token(extra=extra)
    r = await client.post(RESET, json={"access_token": token, "new_password": NEW})
    assert r.status_code == 401 and r.json()["error"]["code"] == "RECOVERY_LINK_INVALID_OR_EXPIRED"
    assert not put_calls(supabase)


async def test_already_used_link_rejected(client, supabase):
    """After a reset all sessions are revoked; Supabase then answers 403 session_not_found."""
    supabase.add("PUT", "/auth/v1/user", httpx.Response(200, json={}))
    supabase.add("POST", "/logout", httpx.Response(204))
    token = recovery_token()
    assert (await client.post(RESET, json={"access_token": token, "new_password": NEW})).status_code == 200
    supabase.add("PUT", "/auth/v1/user", httpx.Response(403, json={
        "code": 403, "error_code": "session_not_found", "msg": "Session from session_id claim in JWT does not exist"}))
    r = await client.post(RESET, json={"access_token": token, "new_password": "An0therPassw0rd"})
    assert r.status_code == 401 and r.json()["error"]["code"] == "RECOVERY_LINK_INVALID_OR_EXPIRED"


@pytest.mark.parametrize(("password", "fragment"), [
    ("short1A", "at least 8 characters"),
    ("nouppercase123", "an uppercase letter"),
    ("NOLOWERCASE123", "a lowercase letter"),
    ("NoDigitsHereAtAll", "a digit"),
    ("Password123"[:0] + "Welcome1", "not a commonly used password"),
    ("A1" + "a" * 80, "at most 72 characters"),
])
async def test_weak_password_rejected_with_field_errors(client, supabase, password, fragment):
    r = await client.post(RESET, json={"access_token": recovery_token(), "new_password": password})
    assert r.status_code == 422
    body = r.json()
    assert body["error"]["code"] == "PASSWORD_POLICY_VIOLATION"
    assert any(d["field"] == "new_password" and fragment in d["message"] for d in body["error"]["details"])
    assert not put_calls(supabase)


async def test_password_equal_to_email_rejected(client, supabase):
    email = "Owner.Name1@Example.com"
    r = await client.post(RESET, json={"access_token": recovery_token(email=email.lower()), "new_password": email})
    assert r.status_code == 422 and r.json()["error"]["code"] == "PASSWORD_POLICY_VIOLATION"
    assert any("e-mail" in d["message"] for d in r.json()["error"]["details"])


async def test_same_as_old_password(client, supabase):
    supabase.add("PUT", "/auth/v1/user", httpx.Response(422, json={
        "code": 422, "error_code": "same_password", "msg": "New password should be different from the old password."}))
    r = await client.post(RESET, json={"access_token": recovery_token(), "new_password": NEW})
    assert r.status_code == 422 and r.json()["error"]["code"] == "PASSWORD_UNCHANGED"
    assert not logout_calls(supabase)


async def test_supabase_weak_password_mapped(client, supabase):
    supabase.add("PUT", "/auth/v1/user", httpx.Response(422, json={"error_code": "weak_password", "msg": "Password is known to be weak"}))
    r = await client.post(RESET, json={"access_token": recovery_token(), "new_password": NEW})
    assert r.status_code == 422 and r.json()["error"]["code"] == "PASSWORD_POLICY_VIOLATION"


async def test_reset_rate_limited(client, reset_ok):
    get_settings().RATE_LIMIT_AUTH_PER_MINUTE = 2
    codes = [(await client.post(RESET, json={"access_token": "x" * 25, "new_password": NEW})).status_code for _ in range(3)]
    assert codes == [401, 401, 429]
    r = await client.post(RESET, json={"access_token": recovery_token(), "new_password": NEW})
    assert r.status_code == 429 and r.json()["error"]["code"] == "RATE_LIMITED"
    assert r.json()["message"] == "Please wait a minute before trying again." and r.headers["Retry-After"]


async def test_supabase_rate_limit_on_update(client, supabase):
    supabase.add("PUT", "/auth/v1/user", httpx.Response(429, json={"error_code": "over_request_rate_limit"}, headers={"retry-after": "30"}))
    r = await client.post(RESET, json={"access_token": recovery_token(), "new_password": NEW})
    assert r.status_code == 429 and r.json()["error"]["code"] == "RATE_LIMITED" and r.headers["Retry-After"] == "30"


async def test_missing_recovery_credential(client, supabase):
    r = await client.post(RESET, json={"new_password": NEW})
    assert r.status_code == 400 and r.json()["error"]["code"] == "RECOVERY_TOKEN_REQUIRED"


async def test_session_revocation_failure_still_reports_success(client, supabase, caplog):
    supabase.add("PUT", "/auth/v1/user", httpx.Response(200, json={}))
    supabase.add("POST", "/logout", httpx.Response(500))
    token = recovery_token()
    with caplog.at_level(logging.INFO):
        r = await client.post(RESET, json={"access_token": token, "new_password": NEW})
    assert r.status_code == 200 and r.json()["data"] == {"status": "SUCCESS", "sessions_revoked": False}
    assert token not in caplog.text and NEW not in caplog.text  # no tokens or passwords in logs


async def test_change_password_and_login_unaffected(client, supabase, user_factory):
    """Regression guard: change-password keeps using the signup policy and the caller's session."""
    from tests.test_auth import STRONG, session_payload

    user, headers = await user_factory()
    supabase.add("POST", "grant_type=password", httpx.Response(200, json=session_payload(str(user.id), user.email)))
    supabase.add("POST", "/logout", httpx.Response(204))
    supabase.add("PUT", "/auth/v1/user", httpx.Response(200, json={}))
    r = await client.post("/api/v1/auth/change-password", headers=headers,
                          json={"current_password": STRONG, "new_password": NEW})
    assert r.status_code == 200


async def test_token_hash_rate_limit_uses_stable_code(client, supabase):
    supabase.add("POST", "/verify", httpx.Response(429, json={"error_code": "over_request_rate_limit"},
                                                    headers={"retry-after": "20"}))
    r = await client.post(RESET, json={"token_hash": "pkce_recovery_hash", "new_password": NEW})
    assert r.status_code == 429 and r.json()["error"]["code"] == "RATE_LIMITED"
    assert r.headers["Retry-After"] == "20"
    assert not put_calls(supabase)


@pytest.mark.parametrize("amr", [
    [{"method": "otp", "timestamp": "not-a-number"}],
    [{"method": "otp"}],
    [{"method": "otp", "timestamp": True}],
    ["otp"],
    "otp",
])
async def test_malformed_amr_rejected_not_crashing(client, supabase, amr):
    r = await client.post(RESET, json={"access_token": make_token(extra={"amr": amr}), "new_password": NEW})
    assert r.status_code == 401 and r.json()["error"]["code"] == "RECOVERY_LINK_INVALID_OR_EXPIRED"
    assert not put_calls(supabase)


# =============================================================== deployment configuration (forgot-password)
async def test_missing_redirect_logs_one_clear_error(client, supabase, caplog):
    get_settings().AUTH_PASSWORD_RESET_REDIRECT_URL = None
    with caplog.at_level(logging.ERROR):
        r = await client.post(FORGOT, json={"email": "a@example.com"})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "password_reset_not_configured" and r.json()["success"] is False
    errors = [rec.getMessage() for rec in caplog.records if rec.levelno >= logging.ERROR]
    assert len(errors) == 1 and "AUTH_PASSWORD_RESET_REDIRECT_URL is not set" in errors[0]


@pytest.mark.parametrize("raw", ['"sharkhire://auth/reset-password"', "'sharkhire://auth/reset-password'",
                                 ' "sharkhire://auth/reset-password" '])
async def test_quoted_redirect_value_is_unquoted(client, supabase, raw):
    get_settings().AUTH_PASSWORD_RESET_REDIRECT_URL = raw
    supabase.add("POST", "/recover", httpx.Response(200, json={}))
    assert (await client.post(FORGOT, json={"email": "a@example.com"})).status_code == 200
    assert recover_calls(supabase)[-1].url.params["redirect_to"] == "sharkhire://auth/reset-password"


@pytest.mark.parametrize("upstream", [httpx.Response(500, json={"code": 500, "error_code": "unexpected_failure",
                                                                "msg": "Error sending recovery email"}),
                                      httpx.Response(502, text="bad gateway")])
async def test_supabase_unavailable_returns_auth_unavailable(client, supabase, upstream, caplog):
    supabase.add("POST", "/recover", upstream)
    with caplog.at_level(logging.WARNING):
        r = await client.post(FORGOT, json={"email": "a@example.com"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "auth_unavailable"
    assert "Error sending recovery email" not in r.text  # upstream detail stays server-side
    assert "Forgot-password failed: auth_unavailable" in caplog.text
    assert "Supabase Auth recover failed: status=" in caplog.text


async def test_supabase_mailer_not_authorized(client, supabase, caplog):
    """Built-in Supabase SMTP only mails project team members until custom SMTP is configured."""
    supabase.add("POST", "/recover", httpx.Response(400, json={
        "code": 400, "error_code": "email_address_not_authorized",
        "msg": "Email address \"x@example.com\" cannot be used as it is not authorized"}))
    with caplog.at_level(logging.ERROR):
        r = await client.post(FORGOT, json={"email": "a@example.com"})
    assert r.status_code == 503 and r.json()["error"]["code"] == "email_delivery_unavailable"
    assert "x@example.com" not in r.text and "x@example.com" not in caplog.text
    assert "configure custom SMTP" in caplog.text


async def test_supabase_rejects_address(client, supabase):
    supabase.add("POST", "/recover", httpx.Response(400, json={"error_code": "email_address_invalid"}))
    r = await client.post(FORGOT, json={"email": "a@example.com"})
    assert r.status_code == 422 and r.json()["error"]["code"] == "validation_error"


async def test_auth_health_reports_configuration_without_values(client):
    s = get_settings()
    r = await client.get("/api/v1/health/auth")
    assert r.status_code == 200
    body = r.json()
    assert body["status"] == "ok" and body["password_reset"] == "configured" and body["supabase_auth"] == "configured"
    assert "sharkhire://" not in r.text and "test-anon-key" not in r.text
    s.AUTH_PASSWORD_RESET_REDIRECT_URL = "  "
    r = await client.get("/api/v1/health/auth")
    assert r.status_code == 503
    assert r.json()["status"] == "misconfigured"
    assert r.json()["password_reset"] == "missing AUTH_PASSWORD_RESET_REDIRECT_URL"


@pytest.mark.unit
def test_startup_warnings_for_reset_redirect():
    from app.core.config import load_settings

    missing = load_settings(_env_file=None, AUTH_PASSWORD_RESET_REDIRECT_URL="")
    assert any("forgot-password returns 503 password_reset_not_configured" in w for w in missing.auth_redirect_warnings())
    quoted = load_settings(_env_file=None, AUTH_PASSWORD_RESET_REDIRECT_URL='"sharkhire://auth/reset-password"')
    assert quoted.password_reset_redirect_url == "sharkhire://auth/reset-password"
    assert any("wrapped in quotes" in w for w in quoted.auth_redirect_warnings())
    good = load_settings(_env_file=None, AUTH_PASSWORD_RESET_REDIRECT_URL="sharkhire://auth/reset-password",
                         AUTH_EMAIL_REDIRECT_URL="sharkhire://auth/callback")
    assert good.auth_redirect_warnings() == []
