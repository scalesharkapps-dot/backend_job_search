# E-mail redirects, password management and account deletion (backend)

## 1. Why e-mail links open `localhost:3000`

The backend contains **no** `localhost:3000` URL. Supabase sends users to the project **Site URL**
(Supabase's default is `http://localhost:3000`) whenever the redirect it was given is missing or not allowed.

| Cause | Where it is fixed |
|---|---|
| `redirect_to` not sent: `AUTH_EMAIL_REDIRECT_URL` / `AUTH_PASSWORD_RESET_REDIRECT_URL` unset on Render | Render environment (backend now logs `Auth e-mail configuration: …` at startup) |
| `redirect_to` sent but **not in the allow-list** -> Supabase silently uses the Site URL | Supabase Dashboard -> Authentication -> URL Configuration -> Redirect URLs: add `sharkhire://auth/callback` and `sharkhire://auth/reset-password` (or `sharkhire://**`) |
| Site URL still `http://localhost:3000` | Dashboard -> URL Configuration -> Site URL: set a production URL (fallback for every link) |
| E-mail template uses `{{ .SiteURL }}` instead of `{{ .ConfirmationURL }}` | Dashboard -> Authentication -> Email Templates ("Confirm signup", "Reset password") |
| The app calls Supabase directly (`signUp`, `resend`, `resetPasswordForEmail`) without `emailRedirectTo` / `redirectTo` | Flutter app (outside this repository) |

The backend always sends `redirect_to` as a query parameter on `/signup`, `/resend` (fixed in issue 02) and
`/recover`. Backend changes alone cannot fix the Dashboard settings or direct client calls above, and
they have not been verified against the live project.

## 2. Endpoints (all under `/api/v1/auth`, standard envelope)

### `POST /verify-email` (no auth)
For confirmation templates that hand `{{ .TokenHash }}` to the app (e.g.
`sharkhire://auth/callback?token_hash={{ .TokenHash }}&type=signup`).
```json
{"token_hash": "pkce_…", "type": "signup"}
```
`200` = session (same shape as `/login`), local user marked `email_verified=true`.
`401 invalid_token` = expired, already used or unknown link (Supabase answers `otp_expired` for all three).

### Password recovery: supported flow

**Implicit flow** (plus an optional token-hash template). The backend calls Supabase `POST /auth/v1/recover`
with `{"email"}` only and no PKCE `code_challenge`, so Supabase does not use PKCE. Source: GoTrue `recover.go`
uses PKCE only when a `code_challenge` is sent.

1. `POST /auth/forgot-password` -> Supabase e-mails a link. Every request sends
   `redirect_to = AUTH_PASSWORD_RESET_REDIRECT_URL` unchanged (the Flutter deep link, e.g.
   `sharkhire://auth/reset-password`). When it is not configured the backend returns `503` and sends nothing,
   so it never omits `redirect_to` or lets Supabase fall back to its Site URL.
2. The link opens Supabase `/auth/v1/verify`, which redirects to the deep link:
   `sharkhire://auth/reset-password#access_token=…&refresh_token=…&expires_in=3600&type=recovery`.
   An expired or used link redirects with `#error=access_denied&error_code=otp_expired&error_description=…`.
3. Android/iOS open the SharkHire app. It reads `access_token` from the URL **fragment** and calls
   `POST /auth/reset-password`.

PKCE (`?code=` links) is **not** supported. The code verifier would have to come from the client that started
the request, and the backend starts it. If the e-mail template is changed to
`sharkhire://auth/reset-password?token_hash={{ .TokenHash }}&type=recovery`, send `token_hash` instead.
A custom-scheme link only opens on a phone with the app installed. On a desktop mail client it cannot open.

### `POST /forgot-password` (no auth)
```bash
curl -X POST "$API/api/v1/auth/forgot-password" -H "Content-Type: application/json" \
     -d '{"email":"user@example.com"}'
```
`200` is the same for registered and unregistered addresses (Supabase also answers `200 {}` for unknown ones):
```json
{"success": true, "message": "If an account exists for this email, a reset link has been sent.", "data": null, "error": null}
```
| Status | `error.code` | When |
|---|---|---|
| 422 | `validation_error` | not a valid e-mail (`EmailStr`) |
| 429 | `RATE_LIMITED` | per-IP (`RATE_LIMIT_EMAIL_PER_HOUR`) or per-address (`RATE_LIMIT_PASSWORD_RESET_PER_EMAIL_PER_HOUR`) limit, or Supabase `429` / `over_email_send_rate_limit`. Message "Please wait a minute before trying again.", with a `Retry-After` header |
| 503 | `auth_unavailable` | Supabase down or erroring: reported as an error, never as success |
| 503 | `password_reset_not_configured` | no redirect configured (the backend refuses rather than letting Supabase use its Site URL) |

Per-address keys are SHA-256 hashes. Limits are per process (in memory), so with N workers the effective limit is
N times higher. E-mail addresses, passwords and tokens are never logged.

### `POST /reset-password` (recovery token from the deep link)
Existing Flutter contract (unchanged request shape):
```bash
curl -X POST "$API/api/v1/auth/reset-password" -H "Content-Type: application/json" \
     -H "Authorization: Bearer <access_token from the deep-link fragment>" \
     -d '{"new_password":"NewSecurePassword123"}'
```
Also accepted: the token in the body, `{"access_token": "…", "new_password": "…"}`, or
`{"token_hash": "…", "new_password": "…"}` for a token-hash template.
```json
{"success": true, "message": "Password updated successfully. Please sign in again.",
 "data": {"status": "SUCCESS", "sessions_revoked": true}, "error": null}
```
How the token is checked:
1. Signature, issuer, audience and expiry are verified locally (Supabase JWKS).
2. It must be a **recovery session**. Its `amr` claim must contain `otp`, `magiclink` or `recovery` (GoTrue's own
   `Session.IsRecovery()` rule), dated within the last hour. An ordinary sign-in token (`amr: password`) is rejected.
3. The password is updated with Supabase `PUT /auth/v1/user` using that recovery session.
4. All sessions are revoked (`POST /logout?scope=global`). Supabase only revokes *other* sessions on a password
   change, so without this step the same link could be used again. Afterwards Supabase rejects the token with
   `403 session_not_found`.

| Status | `error.code` | When |
|---|---|---|
| 400 | `RECOVERY_TOKEN_REQUIRED` | neither `access_token` nor `token_hash` |
| 401 | `RECOVERY_LINK_INVALID_OR_EXPIRED` | invalid, expired, already used, not a recovery session, or older than 1 hour |
| 422 | `PASSWORD_POLICY_VIOLATION` | 8–72 characters, upper and lower case, a digit, not a common password, not the account e-mail. `error.details` = `[{"field": "new_password", "message": …, "type": "password_policy"}]` |
| 422 | `PASSWORD_UNCHANGED` | same as the current password (Supabase `same_password`) |
| 429 | `RATE_LIMITED` | per-IP limit (`RATE_LIMIT_AUTH_PER_MINUTE`) or Supabase rate limit, with `Retry-After` |

**App contract:** the app handles `sharkhire://auth/reset-password`. When the fragment contains `error_code`,
show "link expired" and offer forgot-password again. Otherwise take `access_token` from the fragment and send it as
the Bearer token, or as `access_token`, with the new password. On `200`, clear any stored session (all sessions were
revoked) and go to sign-in. On `401 RECOVERY_LINK_INVALID_OR_EXPIRED`, show "link expired". This is not a
"session expired, log out" case.

### Required settings (outside this repository)
* **Render:** `AUTH_PASSWORD_RESET_REDIRECT_URL=sharkhire://auth/reset-password` (already set according to the
  team; not verified here). Startup logs `Auth e-mail configuration: …` when it's missing or not an absolute
  URL/deep link.
* **Supabase -> Authentication -> URL Configuration -> Redirect URLs:** must contain
  `sharkhire://auth/reset-password` (or `sharkhire://**`). Otherwise Supabase silently uses the **Site URL**,
  the localhost redirect. Set the Site URL itself to a real production URL, never `http://localhost:3000`.
* **Supabase -> Email Templates -> Reset password:** keep `{{ .ConfirmationURL }}` (not `{{ .SiteURL }}`).
* **App:** `sharkhire` scheme registered (AndroidManifest intent filter, iOS URL Types). If the app calls
  Supabase directly for recovery, it must pass the same redirect.

### `POST /change-password` (Bearer token)
```json
{"current_password": "Old…1", "new_password": "N3wPassw0rdX"}
```
* The current password is checked against Supabase for the **token owner's** e-mail only. A wrong password
  returns `403 invalid_password` and nothing is changed.
* The new password follows the signup policy (`422`). Reusing the old one returns `422 same_password`.
* The change is made with the caller's own session. If Supabase "Secure password change" requires a recent
  sign-in, the change is made with a fresh session, returned in `data.session` (client must store it).
* Missing, expired or invalid token -> `401`. Suspended account -> `403 account_suspended`.
```json
{"success": true, "message": "Password changed successfully", "data": {"password_changed": true, "session": null}, "error": null}
```

### `POST /delete-account` (Bearer token)
```json
{"password": "current password", "confirmation": "DELETE"}
```
```json
{"success": true, "message": "Your account has been permanently deleted.",
 "data": {"deleted": true, "user_id": "8f0c…", "storage_objects_deleted": 3, "active_store_subscription": false},
 "error": null}
```
| Status / code | Meaning |
|---|---|
| `401` | missing, invalid or expired token; `account_deleted` after deletion (old tokens) |
| `403 invalid_password` | re-authentication failed |
| `403 admin_account_deletion_not_allowed` | administrators are removed by another admin |
| `409 account_deletion_in_progress` | duplicate concurrent request |
| `422 confirmation_required` | `confirmation` is not exactly `DELETE` |
| `503 storage_unavailable` / `auth_unavailable` | nothing was deleted from the database; retry |
| `503 account_deletion_not_configured` | `SUPABASE_SERVICE_ROLE_KEY` missing |
| `500 account_deletion_incomplete` | sign-in removed, data cleanup failed: retrying with the same token completes it |

**Order:** re-authenticate -> lock the account -> delete files (CVs, profile photo, company logo) -> delete the
Supabase Auth user (service-role key, server-side only; removes sessions and refresh tokens) -> one database
transaction:
* the employer's unapplied drafts are deleted and other postings archived, so applicants keep their records;
* the e-mail verification tracker is removed;
* a `deleted_accounts` tombstone (user id + time only) is written;
* the `users` row is deleted. Profile, CVs, saved jobs, applications, recommendations, preferences, employer
  profile, trial, subscriptions and payment history cascade. Reports, audit log, jobs and status history keep
  the record with a `NULL` user reference.

The tombstone stops a deleted user's still-valid access token (valid until its expiry) from re-creating the
account.

## 3. Configuration

New: `RATE_LIMIT_PASSWORD_RESET_PER_EMAIL_PER_HOUR` (default 3). `AUTH_PASSWORD_RESET_REDIRECT_URL` is now required for
forgot-password. Also uses existing `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` (already required for
Supabase Storage), `AUTH_EMAIL_REDIRECT_URL`, `AUTH_PASSWORD_RESET_REDIRECT_URL`, storage buckets.
Migration: `alembic upgrade head` (0011 adds `deleted_accounts`).

## 4. Known limits

* Only files referenced in the database are deleted. Objects orphaned by earlier failures are not listed or removed.
* Store subscriptions keep billing until cancelled in Google Play or the App Store (`active_store_subscription`).
  RevenueCat keeps its customer record. Use the RevenueCat dashboard or API for full erasure.
* Deleting an account and re-registering the same e-mail creates a new account, and with it a new 3-day trial.
