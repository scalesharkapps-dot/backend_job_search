# Flutter ↔ SharkHire Authentication

Android and iOS use the same endpoints. Supabase Auth is the identity provider; the Flutter app talks
**only to the SharkHire API** for auth (no Supabase keys in the app are required for these flows).
Conventions: [API_DOCUMENTATION.md](API_DOCUMENTATION.md).

```dart
const baseUrl = 'https://<api-domain>/api/v1'; // Android emulator dev: http://10.0.2.2:8000/api/v1
```

## 1. Signup — `POST /auth/signup`
```dart
final r = await http.post(Uri.parse('$baseUrl/auth/signup'),
    headers: {'Content-Type': 'application/json'},
    body: jsonEncode({'email': email, 'password': password, 'full_name': name}));
final body = jsonDecode(r.body);
if (r.statusCode == 201 && body['data']['email_confirmation_required'] == true) {
  // Show "check your email". No session yet.
} else if (r.statusCode == 201) {
  await saveSession(body['data']['session']); // email confirmation disabled in Supabase
}
```
Password policy: ≥ 8 chars (`PASSWORD_MIN_LENGTH`), ≤ 72, upper + lower case + digit, not a common password.
`409 email_exists` is returned only when Supabase reports it; with email confirmation enabled the response for an
existing email is intentionally identical to a new signup (prevents account enumeration).

## 2. Login — `POST /auth/login`
Response `data`:
```json
{"access_token": "...", "refresh_token": "...", "token_type": "bearer", "expires_in": 3600,
 "expires_at": 1763200000,
 "user": {"id": "uuid", "email": "user@example.com", "full_name": "Ayesha", "role": "job_seeker",
          "is_active": true, "email_verified": true, "created_at": "..."}}
```
Errors: `401 invalid_credentials`, `401 email_not_confirmed`, `401 account_suspended`, `429`, `503 auth_unavailable`.

## 3–4. Token storage & Bearer header
Store tokens with **flutter_secure_storage** (Keychain on iOS, Keystore-backed EncryptedSharedPreferences on Android).
Never store them in plain SharedPreferences or logs.
```dart
final storage = const FlutterSecureStorage();
Future<Map<String, String>> authHeaders() async =>
    {'Authorization': 'Bearer ${await storage.read(key: 'access_token')}',
     'Content-Type': 'application/json'};
```

## 5. Refresh — `POST /auth/refresh`
Refresh shortly before `expires_at`, or once after any `401 token_expired`, then retry the original request once.
```dart
final r = await http.post(Uri.parse('$baseUrl/auth/refresh'),
    headers: {'Content-Type': 'application/json'},
    body: jsonEncode({'refresh_token': await storage.read(key: 'refresh_token')}));
if (r.statusCode == 200) { await saveSession(jsonDecode(r.body)['data']); }
else { await logoutLocally(); /* 401 invalid_session → send user to login */ }
```
Refresh tokens are single-use in Supabase — always persist the new pair.

## 6. Protected calls
```dart
final r = await http.get(Uri.parse('$baseUrl/auth/me'), headers: await authHeaders());
```
`GET /auth/me` returns the verified identity + `session_expires_at`.

## 7. Logout — `POST /auth/logout`
Revokes the Supabase session (refresh token) server-side; `data.revoked` is `false` if it was already invalid.
**Always delete tokens from secure storage afterwards**, even if the request fails. Note: an access token is a
stateless JWT and remains valid until it expires (≤ 1h); that's why local deletion matters.

## 8. Email verification
Supabase sends the email; the link opens `AUTH_EMAIL_REDIRECT_URL` (e.g. `sharkhire://auth/callback`) — register this
deep link in AndroidManifest / iOS URL types and in Supabase → Auth → URL configuration.
Resend: `POST /auth/resend-verification {"email": ...}` — at most **2 resends** per verification cycle
(1 initial + 2 = 3 e-mails), enforced server-side in PostgreSQL per e-mail address, 60 s cooldown between e-mails.

| Status | `error.code` | Meaning / UI |
|---|---|---|
| 200 | — | sent; show `data.remaining_attempts` (also `max_resends`, `cooldown_seconds`) |
| 429 | `resend_limit_reached` | message is exactly "You've reached the maximum resend limit. Please try again later."; `data.remaining_attempts` = 0; `Retry-After` = seconds until a new cycle (24 h) |
| 429 | `resend_cooldown` | wait `Retry-After` seconds; `data.remaining_attempts` still available |
| 429 | `rate_limited` | per-IP or Supabase e-mail rate limit (not counted as an attempt) |
| 409 | `email_already_verified` | go to sign-in |
| 503 | `auth_unavailable` | Supabase unreachable / rejected (not counted as an attempt) |

Registering again with an unverified e-mail does not reset the limit (it counts as a resend; once the limit is
reached no further e-mail is sent, but the usual `201` "check your email" response is returned).

## 9–11. Forgot / reset password
1. `POST /auth/forgot-password {"email": ...}` → always "If an account exists…".
2. The email link opens `AUTH_PASSWORD_RESET_REDIRECT_URL` (deep link). Depending on the Supabase email template it
   carries either an `access_token` (in the URL fragment) or a `token_hash` query parameter.
3. Call `POST /auth/reset-password`:
   - with fragment token: header `Authorization: Bearer <access_token from link>`, body `{"new_password": "..."}`
     (or put the token in the body as `"access_token"`)
   - with token hash: body `{"new_password": "...", "token_hash": "<token_hash>"}`
   - only a recovery-link token opened within the last hour is accepted, not the app's normal session token
4. `200` = `data.status == "SUCCESS"`. All sessions were revoked, so clear stored tokens and sign in again.
   Never log the recovery token.
5. Errors: `401 RECOVERY_LINK_INVALID_OR_EXPIRED` (expired, used or not a recovery link: offer a new link),
   `422 PASSWORD_POLICY_VIOLATION` (`error.details[].field == "new_password"`), `422 PASSWORD_UNCHANGED`,
   `429 RATE_LIMITED` (wait for `Retry-After`). Forgot-password returns `429 RATE_LIMITED` when throttled.

## 12. Expired sessions
`401` with `error.code`:
- `token_expired` → refresh, retry once.
- `invalid_token` / `invalid_session` → clear storage, go to login.
- `403 account_suspended` → show a suspension message (do not retry).

## 13. Error handling cheat-sheet
| Status | Code | UI action |
|---|---|---|
| 422 | `validation_error` | show `error.details[].message` next to fields |
| 401 | `invalid_credentials` | "Wrong email or password" |
| 401 | `email_not_confirmed` | offer "Resend verification" |
| 429 | `rate_limited` | wait `Retry-After` seconds |
| 503 | `auth_unavailable` | "Try again later" |
