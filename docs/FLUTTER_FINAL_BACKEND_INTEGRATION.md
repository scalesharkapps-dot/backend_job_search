# SharkHire — Final Flutter Integration Guide

One FastAPI backend serves both the **Android** and **iOS** Flutter apps through identical HTTPS JSON endpoints.
Nothing in the API is platform-specific.

## Start here
1. Conventions (envelope, errors, pagination, rate limits): [API_DOCUMENTATION.md](API_DOCUMENTATION.md)
2. Full endpoint inventory with access level: [API_ENDPOINTS.md](API_ENDPOINTS.md) (95 operations)
3. Live schema for code generation: `GET /openapi.json` (e.g. `openapi-generator` → Dart `dio` client)

## Feature guides
| Workflow | Guide |
|---|---|
| Signup, login, refresh, logout, me, verification, password reset | [FLUTTER_AUTH_INTEGRATION.md](FLUTTER_AUTH_INTEGRATION.md) |
| Profile, CV upload/review/download/delete | [PROFILE_CV_API.md](PROFILE_CV_API.md), [FLUTTER_CV_INTEGRATION.md](FLUTTER_CV_INTEGRATION.md) |
| Job search (Pakistan, international, remote), details, filters, SharkHire jobs | [FLUTTER_JOB_SEARCH_INTEGRATION.md](FLUTTER_JOB_SEARCH_INTEGRATION.md) |
| Recommendations, match %, skills, preferences | [FLUTTER_RECOMMENDATION_INTEGRATION.md](FLUTTER_RECOMMENDATION_INTEGRATION.md) |
| Saved jobs, applications, status history, interviews, reminders, statistics | [FLUTTER_JOB_MANAGEMENT_INTEGRATION.md](FLUTTER_JOB_MANAGEMENT_INTEGRATION.md) |
| Employer registration, verification, postings, applicants, CV access | [FLUTTER_EMPLOYER_API_INTEGRATION.md](FLUTTER_EMPLOYER_API_INTEGRATION.md) |
| Admin (future client) | [ADMIN_API_DOCUMENTATION.md](ADMIN_API_DOCUMENTATION.md) |

## Recommended client setup (Dart)
```dart
final dio = Dio(BaseOptions(
  baseUrl: const String.fromEnvironment('API_BASE', defaultValue: 'https://<api-domain>/api/v1'),
  connectTimeout: const Duration(seconds: 10),
  receiveTimeout: const Duration(seconds: 30),
));
dio.interceptors.add(InterceptorsWrapper(
  onRequest: (o, h) async {
    final t = await storage.read(key: 'access_token');
    if (t != null) o.headers['Authorization'] = 'Bearer $t';
    h.next(o);
  },
  onError: (e, h) async {
    final code = e.response?.data is Map ? e.response?.data['error']?['code'] : null;
    if (e.response?.statusCode == 401 && code == 'token_expired' && await refreshTokens()) {
      final retry = e.requestOptions..headers['Authorization'] = 'Bearer ${await storage.read(key: 'access_token')}';
      return h.resolve(await dio.fetch(retry));
    }
    h.next(e);
  },
));
```
Parse every response through one helper: if `success == false`, surface `error.message` and branch on `error.code`.

## Role-based navigation
After login read `data.user.role`: `job_seeker` → seeker app; `employer` → employer area (call `GET /employers/me`;
`403 employer_profile_required` means registration is not finished); `admin` → admin tooling.
Re-fetch `GET /auth/me` after employer registration (role changes server-side).

## Mobile considerations
- HTTPS only in production; Android 9+ blocks cleartext by default (allow `10.0.2.2` only in debug builds).
- Deep links for email verification/password reset must be registered on both platforms and in Supabase.
- Store tokens with `flutter_secure_storage`; never store signed file URLs.
- Uploads use multipart; show progress with `dio`'s `onSendProgress`.
- Respect `Retry-After` on 429; show offline/empty/loading states (see each guide).
- External job applications open in the browser via `url_launcher`; SharkHire never submits them.
- Reminders are data only — schedule local notifications in the app; the backend sends no push notifications.

## Not provided by the backend (by design / not yet)
Push notifications (FCM/APNs), OCR for scanned CVs, social login, in-app chat, web/admin panels.
