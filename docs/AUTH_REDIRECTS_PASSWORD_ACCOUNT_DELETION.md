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

### `POST /forgot-password` (unchanged)
Same response whether or not the e-mail exists. The link goes to `AUTH_PASSWORD_RESET_REDIRECT_URL`.
`/reset-password` (unchanged) accepts the recovery `token_hash` or the recovery access token. Reused or expired
tokens -> `401 invalid_token`.

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

No new environment variables. Uses existing `SUPABASE_URL`, `SUPABASE_SERVICE_ROLE_KEY` (already required for
Supabase Storage), `AUTH_EMAIL_REDIRECT_URL`, `AUTH_PASSWORD_RESET_REDIRECT_URL`, storage buckets.
Migration: `alembic upgrade head` (0011 adds `deleted_accounts`).

## 4. Known limits

* Only files referenced in the database are deleted. Objects orphaned by earlier failures are not listed or removed.
* Store subscriptions keep billing until cancelled in Google Play or the App Store (`active_store_subscription`).
  RevenueCat keeps its customer record. Use the RevenueCat dashboard or API for full erasure.
* Deleting an account and re-registering the same e-mail creates a new account, and with it a new 3-day trial.
