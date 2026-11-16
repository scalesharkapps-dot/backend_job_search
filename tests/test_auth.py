"""Phase 2 — authentication & authorization tests.

JWT tests use real ES256 signatures against a test JWKS. Supabase Auth HTTP calls
are mocked (MockTransport) — these tests do NOT verify a real Supabase project.
"""

from __future__ import annotations

import json
import uuid

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import ec, rsa
from fastapi import APIRouter, Depends

from app.api.deps import require_role
from app.core.config import get_settings
from app.core.exceptions import AuthenticationError
from app.core.security import verify_access_token
from app.models.user import User, UserRole
from tests.conftest import auth_header, make_token

STRONG = "Str0ngPassw0rd"


def session_payload(user_id: str | None = None, email: str = "new@example.com") -> dict:
    uid = user_id or str(uuid.uuid4())
    return {
        "access_token": make_token(uid, email=email),
        "refresh_token": "refresh-token-abcdef",
        "token_type": "bearer",
        "expires_in": 3600,
        "expires_at": 1999999999,
        "user": {
            "id": uid,
            "email": email,
            "email_confirmed_at": "2026-11-01T00:00:00Z",
            "user_metadata": {"full_name": "New User"},
        },
    }


# ---------------- Signup ----------------
async def test_signup_with_immediate_session(client, supabase, db):
    payload = session_payload()
    supabase.add("POST", "/auth/v1/signup", httpx.Response(200, json=payload))
    r = await client.post(
        "/api/v1/auth/signup", json={"email": "new@example.com", "password": STRONG, "full_name": "New User"}
    )
    assert r.status_code == 201, r.text
    data = r.json()["data"]
    assert data["email_confirmation_required"] is False
    assert data["session"]["access_token"]
    assert data["user"]["role"] == "job_seeker"
    assert "password" not in r.text.lower().replace("password", "", 0) or STRONG not in r.text
    sent = json.loads(supabase.requests[-1].content)
    assert sent["data"] == {"full_name": "New User"}
    assert supabase.requests[-1].headers["apikey"] == "test-anon-key"
    assert await db.get(User, uuid.UUID(payload["user"]["id"])) is not None


async def test_signup_requiring_confirmation(client, supabase):
    supabase.add("POST", "/signup", httpx.Response(200, json={"id": str(uuid.uuid4()), "email": "a@b.com", "identities": []}))
    r = await client.post("/api/v1/auth/signup", json={"email": "a@b.com", "password": STRONG})
    assert r.status_code == 201
    assert r.json()["data"] == {"user": None, "session": None, "email_confirmation_required": True}


@pytest.mark.parametrize("email", ["bad", "a@", "@b.com", ""])
async def test_signup_invalid_email(client, email):
    r = await client.post("/api/v1/auth/signup", json={"email": email, "password": STRONG})
    assert r.status_code == 422


@pytest.mark.parametrize("password", ["short1A", "alllowercase1", "ALLUPPER123", "NoDigitsHere", "Password1"[:0] + "password123"])
async def test_signup_weak_password(client, supabase, password):
    r = await client.post("/api/v1/auth/signup", json={"email": "a@b.com", "password": password})
    assert r.status_code == 422
    assert not supabase.requests  # never forwarded


async def test_signup_duplicate_email(client, supabase):
    supabase.add("POST", "/signup", httpx.Response(422, json={"code": 422, "error_code": "user_already_exists", "msg": "User already registered"}))
    r = await client.post("/api/v1/auth/signup", json={"email": "dup@example.com", "password": STRONG})
    assert r.status_code == 409
    assert r.json()["error"]["code"] == "email_exists"


async def test_signup_supabase_weak_password(client, supabase):
    supabase.add("POST", "/signup", httpx.Response(422, json={"error_code": "weak_password", "msg": "Password should be..."}))
    r = await client.post("/api/v1/auth/signup", json={"email": "x@example.com", "password": STRONG})
    assert r.status_code == 422
    assert r.json()["error"]["code"] == "weak_password"


async def test_signup_supabase_error(client, supabase):
    supabase.add("POST", "/signup", httpx.Response(500, json={"msg": "internal db error at host 10.0.0.1"}))
    r = await client.post("/api/v1/auth/signup", json={"email": "x@example.com", "password": STRONG})
    assert r.status_code == 503
    assert "10.0.0.1" not in r.text


# ---------------- Login ----------------
async def test_login_success(client, supabase, db):
    payload = session_payload(email="login@example.com")
    supabase.add("POST", "grant_type=password", httpx.Response(200, json=payload))
    r = await client.post("/api/v1/auth/login", json={"email": "login@example.com", "password": STRONG})
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["token_type"] == "bearer"
    assert data["expires_in"] == 3600
    assert data["refresh_token"] == "refresh-token-abcdef"
    assert data["user"]["email"] == "login@example.com"
    user = await db.get(User, uuid.UUID(payload["user"]["id"]))
    assert user.last_login_at is not None and user.email_verified


async def test_login_invalid_credentials(client, supabase):
    supabase.add("POST", "grant_type=password", httpx.Response(400, json={"error_code": "invalid_credentials", "msg": "Invalid login credentials"}))
    r = await client.post("/api/v1/auth/login", json={"email": "a@b.com", "password": "wrong"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_credentials"


async def test_login_legacy_error_format(client, supabase):
    supabase.add("POST", "grant_type=password", httpx.Response(400, json={"error": "invalid_grant", "error_description": "Invalid login credentials"}))
    r = await client.post("/api/v1/auth/login", json={"email": "a@b.com", "password": "wrong"})
    assert r.status_code == 401


async def test_login_email_not_confirmed(client, supabase):
    supabase.add("POST", "grant_type=password", httpx.Response(400, json={"error_code": "email_not_confirmed", "msg": "Email not confirmed"}))
    r = await client.post("/api/v1/auth/login", json={"email": "a@b.com", "password": STRONG})
    assert r.json()["error"]["code"] == "email_not_confirmed"


async def test_login_invalid_email_format(client):
    r = await client.post("/api/v1/auth/login", json={"email": "nope", "password": "x"})
    assert r.status_code == 422


async def test_login_supabase_unavailable(client, supabase):
    def fail(_):
        raise httpx.ConnectError("down")

    supabase.add("POST", "grant_type=password", fail)
    r = await client.post("/api/v1/auth/login", json={"email": "a@b.com", "password": STRONG})
    assert r.status_code == 503
    assert r.json()["error"]["code"] == "auth_unavailable"


async def test_login_timeout(client, supabase):
    def slow(_):
        raise httpx.ReadTimeout("slow")

    supabase.add("POST", "grant_type=password", slow)
    r = await client.post("/api/v1/auth/login", json={"email": "a@b.com", "password": STRONG})
    assert r.status_code == 503


async def test_login_suspended_account(client, supabase, user_factory):
    user, _ = await user_factory(is_active=False)
    supabase.add("POST", "grant_type=password", httpx.Response(200, json=session_payload(str(user.id), user.email)))
    supabase.add("POST", "/logout", httpx.Response(204))
    r = await client.post("/api/v1/auth/login", json={"email": user.email, "password": STRONG})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "account_suspended"


async def test_login_rate_limited(client, supabase):
    get_settings().RATE_LIMIT_AUTH_PER_MINUTE = 3
    supabase.add("POST", "grant_type=password", httpx.Response(400, json={"error_code": "invalid_credentials"}))
    codes = [
        (await client.post("/api/v1/auth/login", json={"email": "a@b.com", "password": "x"})).status_code
        for _ in range(4)
    ]
    assert codes[:3] == [401, 401, 401]
    assert codes[3] == 429
    r = await client.post("/api/v1/auth/login", json={"email": "a@b.com", "password": "x"})
    assert int(r.headers["Retry-After"]) >= 1


# ---------------- Refresh / logout / recovery ----------------
async def test_refresh_success(client, supabase):
    supabase.add("POST", "grant_type=refresh_token", httpx.Response(200, json=session_payload()))
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": "refresh-token-abcdef"})
    assert r.status_code == 200
    assert r.json()["data"]["access_token"]


async def test_refresh_invalid(client, supabase):
    supabase.add("POST", "grant_type=refresh_token", httpx.Response(400, json={"error_code": "refresh_token_not_found", "msg": "Invalid Refresh Token"}))
    r = await client.post("/api/v1/auth/refresh", json={"refresh_token": "invalid-refresh"})
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "invalid_session"


async def test_logout_revokes(client, supabase, user_factory):
    _, headers = await user_factory()
    supabase.add("POST", "/logout", httpx.Response(204))
    r = await client.post("/api/v1/auth/logout", headers=headers)
    assert r.status_code == 200
    assert r.json()["data"]["revoked"] is True
    assert supabase.requests[-1].headers["Authorization"] == headers["Authorization"]


async def test_logout_already_invalid_session(client, supabase, user_factory):
    _, headers = await user_factory()
    supabase.add("POST", "/logout", httpx.Response(403, json={"error_code": "session_not_found"}))
    r = await client.post("/api/v1/auth/logout", headers=headers)
    assert r.status_code == 200
    assert r.json()["data"]["revoked"] is False


async def test_logout_upstream_failure_not_reported_as_success(client, supabase, user_factory):
    _, headers = await user_factory()
    supabase.add("POST", "/logout", httpx.Response(500))
    r = await client.post("/api/v1/auth/logout", headers=headers)
    assert r.status_code == 503
    assert r.json()["success"] is False


async def test_logout_requires_auth(client):
    r = await client.post("/api/v1/auth/logout")
    assert r.status_code == 401


async def test_forgot_password_generic_response(client, supabase):
    supabase.add("POST", "/recover", httpx.Response(200, json={}))
    r = await client.post("/api/v1/auth/forgot-password", json={"email": "anyone@example.com"})
    assert r.status_code == 200
    assert "If an account exists" in r.json()["message"]


async def test_forgot_password_rate_limited_upstream(client, supabase):
    supabase.add("POST", "/recover", httpx.Response(429, json={"error_code": "over_email_send_rate_limit"}, headers={"retry-after": "60"}))
    r = await client.post("/api/v1/auth/forgot-password", json={"email": "a@example.com"})
    assert r.status_code == 429
    assert r.headers["Retry-After"] == "60"


async def test_resend_verification(client, supabase):
    supabase.add("POST", "/resend", httpx.Response(200, json={}))
    r = await client.post("/api/v1/auth/resend-verification", json={"email": "a@example.com"})
    assert r.status_code == 200
    assert json.loads(supabase.requests[-1].content)["type"] == "signup"


async def test_reset_password_with_bearer_token(client, supabase):
    supabase.add("PUT", "/auth/v1/user", httpx.Response(200, json={"id": str(uuid.uuid4())}))
    token = make_token()
    r = await client.post("/api/v1/auth/reset-password", json={"new_password": STRONG}, headers=auth_header(token))
    assert r.status_code == 200
    assert supabase.requests[-1].headers["Authorization"] == f"Bearer {token}"


async def test_reset_password_with_token_hash(client, supabase):
    supabase.add("POST", "/verify", httpx.Response(200, json=session_payload()))
    supabase.add("PUT", "/auth/v1/user", httpx.Response(200, json={}))
    r = await client.post("/api/v1/auth/reset-password", json={"new_password": STRONG, "token_hash": "hash-12345678"})
    assert r.status_code == 200


async def test_reset_password_expired_link(client, supabase):
    supabase.add("POST", "/verify", httpx.Response(403, json={"error_code": "otp_expired"}))
    r = await client.post("/api/v1/auth/reset-password", json={"new_password": STRONG, "token_hash": "hash-12345678"})
    assert r.status_code == 401


async def test_reset_password_requires_token(client):
    r = await client.post("/api/v1/auth/reset-password", json={"new_password": STRONG})
    assert r.status_code == 400


# ---------------- JWT verification ----------------
async def test_me_with_valid_token_creates_user(client, db):
    uid = uuid.uuid4()
    r = await client.get("/api/v1/auth/me", headers=auth_header(make_token(uid, email="Me@Example.com")))
    assert r.status_code == 200, r.text
    data = r.json()["data"]
    assert data["id"] == str(uid)
    assert data["email"] == "me@example.com"
    assert data["role"] == "job_seeker"


async def test_me_ignores_role_claims_in_token(client):
    token = make_token(extra={"app_metadata": {"role": "admin"}, "user_role": "admin"})
    r = await client.get("/api/v1/auth/me", headers=auth_header(token))
    assert r.json()["data"]["role"] == "job_seeker"


async def test_me_without_token(client):
    r = await client.get("/api/v1/auth/me")
    assert r.status_code == 401
    assert r.headers["WWW-Authenticate"] == "Bearer"


@pytest.mark.parametrize(
    "token_kwargs,code",
    [
        ({"expires_in": -120}, "token_expired"),
        ({"issuer": "https://evil.example.com/auth/v1"}, "invalid_token"),
        ({"audience": "someone-else"}, "invalid_token"),
        ({"drop": ("sub",)}, "invalid_token"),
        ({"drop": ("exp",)}, "invalid_token"),
        ({"extra": {"sub": "not-a-uuid"}}, "invalid_token"),
        ({"role": "anon"}, "invalid_token"),
        ({"role": "service_role"}, "invalid_token"),
        ({"kid": "unknown-kid"}, "invalid_token"),
    ],
)
async def test_invalid_tokens_rejected(client, token_kwargs, code):
    r = await client.get("/api/v1/auth/me", headers=auth_header(make_token(**token_kwargs)))
    assert r.status_code == 401
    assert r.json()["error"]["code"] == code


async def test_invalid_signature(client):
    other = ec.generate_private_key(ec.SECP256R1())
    r = await client.get("/api/v1/auth/me", headers=auth_header(make_token(key=other)))
    assert r.status_code == 401


async def test_tampered_payload(client):
    header, payload, sig = make_token().split(".")
    other_payload = make_token(email="attacker@example.com").split(".")[1]
    r = await client.get("/api/v1/auth/me", headers=auth_header(f"{header}.{other_payload}.{sig}"))
    assert r.status_code == 401


@pytest.mark.parametrize("token", ["garbage", "a.b", "a.b.c", "x" * 9000])
async def test_malformed_tokens(client, token):
    r = await client.get("/api/v1/auth/me", headers=auth_header(token))
    assert r.status_code == 401


async def test_unsigned_none_algorithm_rejected(client):
    token = jwt.encode({"sub": str(uuid.uuid4()), "exp": 9999999999}, key=None, algorithm="none")
    r = await client.get("/api/v1/auth/me", headers=auth_header(token))
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unsupported_algorithm"


async def test_hs256_rejected_when_no_secret(client):
    token = make_token(key="some-shared-secret-value-123456789", algorithm="HS256")
    r = await client.get("/api/v1/auth/me", headers=auth_header(token))
    assert r.status_code == 401
    assert r.json()["error"]["code"] == "unsupported_algorithm"


async def test_unsupported_algorithm_rs512(client):
    key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = make_token(key=key, algorithm="RS512")
    r = await client.get("/api/v1/auth/me", headers=auth_header(token))
    assert r.json()["error"]["code"] == "unsupported_algorithm"


async def test_leftover_legacy_secret_does_not_enable_hs256(client):
    """A configured legacy secret alone must not make HS256 tokens acceptable."""
    secret = "legacy-jwt-secret-with-at-least-32-characters"
    get_settings().SUPABASE_JWT_SECRET = __import__("pydantic").SecretStr(secret)
    forged = make_token(key=secret, algorithm="HS256")
    r = await client.get("/api/v1/auth/me", headers=auth_header(forged))
    assert r.status_code == 401 and r.json()["error"]["code"] == "unsupported_algorithm"


async def test_legacy_hs256_secret_supported(client):
    secret = "legacy-jwt-secret-with-at-least-32-characters"
    get_settings().SUPABASE_JWT_SECRET = __import__("pydantic").SecretStr(secret)
    get_settings().SUPABASE_JWT_ALGORITHMS = "ES256,RS256,HS256"  # explicit opt-in
    r = await client.get("/api/v1/auth/me", headers=auth_header(make_token(key=secret, algorithm="HS256")))
    assert r.status_code == 200
    bad = make_token(key="wrong-secret-wrong-secret-wrong-secret", algorithm="HS256")
    assert (await client.get("/api/v1/auth/me", headers=auth_header(bad))).status_code == 401


async def test_jwks_fetched_on_unknown_kid(supabase):
    from app.core.security import jwks_cache
    from tests.conftest import _PUBLIC_JWK

    jwks_cache.clear()
    supabase.add("GET", ".well-known/jwks.json", httpx.Response(200, json={"keys": [_PUBLIC_JWK]}))
    claims = await verify_access_token(make_token(email="j@example.com"))
    assert claims.email == "j@example.com"
    assert any(".well-known/jwks.json" in str(r.url) for r in supabase.requests)


async def test_jwks_unavailable(supabase):
    from app.core.exceptions import ServiceUnavailableError
    from app.core.security import jwks_cache

    jwks_cache.clear()
    supabase.add("GET", ".well-known/jwks.json", httpx.Response(500))
    with pytest.raises(ServiceUnavailableError):
        await verify_access_token(make_token())


async def test_missing_bearer_scheme(client):
    r = await client.get("/api/v1/auth/me", headers={"Authorization": f"Token {make_token()}"})
    assert r.status_code == 401


# ---------------- Authorization ----------------
_ROLE_ROUTES_ADDED = False


@pytest.fixture
def role_app_routes():
    from app.main import app

    router = APIRouter(prefix="/api/v1/_test")

    @router.get("/admin-only")
    async def admin_only(user=Depends(require_role(UserRole.ADMIN))):
        return {"ok": True, "role": user.role.value}

    @router.get("/employer-or-admin")
    async def emp(user=Depends(require_role(UserRole.EMPLOYER, UserRole.ADMIN))):
        return {"ok": True}

    global _ROLE_ROUTES_ADDED
    if not _ROLE_ROUTES_ADDED:
        app.include_router(router)
        _ROLE_ROUTES_ADDED = True
    return app


async def test_role_authorized(client, role_app_routes, user_factory):
    _, headers = await user_factory(UserRole.ADMIN)
    r = await client.get("/api/v1/_test/admin-only", headers=headers)
    assert r.status_code == 200


async def test_role_unauthorized(client, role_app_routes, user_factory):
    _, headers = await user_factory(UserRole.JOB_SEEKER)
    r = await client.get("/api/v1/_test/admin-only", headers=headers)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "insufficient_role"
    _, emp_headers = await user_factory(UserRole.EMPLOYER)
    assert (await client.get("/api/v1/_test/employer-or-admin", headers=emp_headers)).status_code == 200


async def test_inactive_account_blocked(client, role_app_routes, user_factory):
    _, headers = await user_factory(UserRole.ADMIN, is_active=False)
    r = await client.get("/api/v1/_test/admin-only", headers=headers)
    assert r.status_code == 403
    assert r.json()["error"]["code"] == "account_suspended"


async def test_concurrent_first_login_sync(db):
    from app.services.user_service import sync_user

    uid = uuid.uuid4()
    u1 = await sync_user(db, user_id=uid, email="c@example.com")
    u2 = await sync_user(db, user_id=uid, email="c@example.com")
    assert u1.id == u2.id


@pytest.mark.unit
async def test_verify_rejects_empty():
    with pytest.raises(AuthenticationError):
        await verify_access_token("")


async def test_login_banned_at_supabase(client, supabase):
    """Supabase returns 400 user_banned for users suspended via the admin API (observed live)."""
    supabase.add("POST", "grant_type=password", httpx.Response(400, json={"code": 400, "error_code": "user_banned", "msg": "User is banned"}))
    r = await client.post("/api/v1/auth/login", json={"email": "banned@example.com", "password": STRONG})
    assert r.status_code == 401 and r.json()["error"]["code"] == "account_suspended"
