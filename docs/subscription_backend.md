# SharkHire — Subscription & App-Access Backend

Backend maintenance and integration contract. (No client/SDK instructions here.)

## 1. Business model

SharkHire has **no feature tiers**. One entitlement, `app_access`, unlocks the whole app.

| Phase | Rule |
|---|---|
| Registration | Backend starts a **3-day app-managed trial** (server UTC). No store purchase, no payment method. |
| Day 4 onwards | Protected routes return `403 subscription_required` until a monthly subscription is verified. |
| Subscription | $10 USD (international) or PKR 499 (Pakistan) per month, sold by Google Play / App Store, verified via RevenueCat. Auto-renews only after the user explicitly buys it in the store. |

The trial is **not** a store introductory offer. Nobody is charged automatically on day 4.

## 2. Architecture

```
Google Play / App Store ──► RevenueCat ──(webhook, shared secret)──► POST /api/v1/subscriptions/webhook
                                 ▲                                         │ store event (idempotent)
                                 │ GET /v1/subscribers/{user_id}            ▼ apply + reconcile
FastAPI ─────────────────────────┘ (secret API key)          Supabase PostgreSQL
   │                                                         user_trials · subscriptions
   └── require_app_access (every protected route) ◄──────── subscription_events · payment_history
```

| Module | Responsibility |
|---|---|
| `app/services/billing/trial.py` | Create/read the single trial per user, remaining time |
| `app/services/billing/revenuecat.py` | RevenueCat REST v1 client (timeouts, retries, safe errors) |
| `app/services/billing/subscriptions.py` | API sync, event application, ordering guard, payments, **access decision** |
| `app/services/billing/webhook.py` | Webhook authorization, idempotent storage/claim, processing, retries |
| `app/api/deps.py::require_app_access` | FastAPI dependency (paywall) |
| `app/api/v1/subscriptions.py` | Subscription API |
| `app/api/v1/router.py` | Which routers are protected |
| `app/scripts/maintenance.py` | Scheduled retry of failed events + API reconciliation |

The backend is the only authority for access. Nothing about trials, subscriptions or payments is ever
read from a client request.

## 3. The 3-day trial

* `user_trials.user_id` is **UNIQUE**; creation is `INSERT … ON CONFLICT DO NOTHING`, so concurrent requests
  create exactly one row. `trial_ends_at = trial_started_at + 3 days` (constant `TRIAL_DURATION`), server UTC.
* Created at **signup** as soon as Supabase returns the new user id (a local `users` row with
  `email_verified=false` is created at the same time). Signup for an already-registered e-mail (Supabase
  returns an obfuscated user without identities) creates nothing.
* Otherwise created on the user's **first authenticated request**. That includes every account that existed
  before billing was deployed, so they get 3 days from their first request after the deploy.
* Login, logout, refresh, reinstall, new device and repeated signup never create a second trial or move the
  dates. `trial_status` (`active`/`expired`/`converted`) is bookkeeping; access is decided from the timestamps.

**Abuse limits (documented, by design):** a trial is tied to a Supabase user id. Local e-mail addresses are
unique, so re-registering the *same* e-mail after deleting the Supabase account fails with `account_conflict`
rather than producing a new trial. Someone can still sign up with **different e-mail addresses** to get new
trials. No device fingerprinting is done. If this becomes a problem, options are e-mail domain rules,
phone verification, or store-level checks.

## 4. Database (migration `0010`)

| Table | Key constraints | Purpose |
|---|---|---|
| `user_trials` | UNIQUE `user_id`, FK users CASCADE, CHECK `trial_ends_at > trial_started_at` | one trial per user |
| `subscriptions` | UNIQUE (`user_id`, `product_id`), FK users CASCADE, idx (`user_id`,`entitlement_active`) | current state per store product |
| `subscription_events` | UNIQUE `revenuecat_event_id`, FK users SET NULL, idx status/type | every webhook (audit + idempotency) |
| `payment_history` | UNIQUE (`store`, `transaction_id`), FK users CASCADE, FK subscriptions SET NULL | verified transactions |

`subscriptions` also stores `billing_issue_detected_at`, `grace_period_expires_at`, `refunded_at`,
`last_event_at` (ordering guard), `last_synced_at` (last API confirmation, RevenueCat clock),
`last_sync_attempt_at` and `needs_reconcile`.
`payment_history` holds store transaction ids, product, amount in the purchase currency, currency, status
(`completed`/`refunded`), date and environment. **No card, CVV or bank data is stored, ever.**
`subscription_events.event_payload` is stored **without `subscriber_attributes`** (they can hold e-mail/phone).
All four tables get RLS with no policies and no `anon`/`authenticated` grants, like the rest of the schema.

```bash
alembic upgrade head        # 0009 -> 0010
alembic downgrade 0009      # drops the four tables (data loss: billing history)
```

## 5. Access decision

`evaluate_access()` uses only trusted local data:

| Condition | Result | `access_status` |
|---|---|---|
| role in `SUBSCRIPTION_EXEMPT_ROLES` (default `admin`) | allow | `exempt` |
| verified `app_access` entitlement, renewing | allow | `subscription_active` |
| entitlement valid, auto-renew off / paused | allow until `expiration_at` | `subscription_cancelled_but_active` |
| billing issue, still inside grace period | allow | `billing_issue` |
| trial not yet ended | allow | `trial_active` |
| billing issue, grace over | **block** | `billing_issue` |
| subscription expired / refunded / revoked / transferred away | **block** | `subscription_expired` |
| no trial left, never subscribed | **block** | `payment_required` |

An entitlement counts only if RevenueCat reported it (API or authenticated webhook), it was granted by that
row's product, the row is not `transferred`/`revoked`, the environment is accepted, and
`now < expiration_at` (or `< grace_period_expires_at`). Employers need access too. Only exempt roles skip it.
`SUBSCRIPTION_ENFORCEMENT_ENABLED=false` lets everyone through but still reports the real status.

**Freshness policy**

1. Access never lasts beyond an expiration time RevenueCat reported.
2. A RevenueCat outage never shortens verified access. Local rows are only changed by verified data.
3. If a renewing subscription has just passed `expiration_at` (renewal webhook not seen yet), or a row is
   flagged `needs_reconcile`, the access check makes **one** RevenueCat lookup, at most every 5 minutes per
   user. If that lookup fails, access stays blocked until verified data arrives (webhook, `/sync`, maintenance job).

### Protected vs open routes

* **Protected** (router-level dependency): `/profile`, `/cv`, `/recommendations`, `/saved-jobs`,
  `/applications`, `/interviews`, `/employers`, `/employer`. Under `/jobs`, `search`, `suggestions`,
  `sharkhire` and `{job_id}` are protected per route.
* **Open:** `/health`, `/auth/*` (signup, login, refresh, logout, me, verification, recovery),
  `/subscriptions/*`, `/reports` (support/abuse), `/files/{token}` (signed links issued by protected routes),
  `/admin/*` (admin role), `/jobs/filters` (static metadata).
* `tests/test_access_control.py::test_every_application_route_is_behind_the_paywall` fails CI if a new route
  is added outside these lists without the dependency.

Order of checks on a protected route: missing/invalid token → `401`; suspended → `403 account_suspended`;
no access → `403 subscription_required`.

```json
HTTP 403
{"success": false,
 "message": "Your free trial or subscription has ended. Subscribe to continue using SharkHire.",
 "data": {"access_status": "payment_required", "trial_status": "expired",
          "trial_ends_at": "2026-11-22T10:00:00+00:00", "subscription_status": null, "current_period_end": null},
 "error": {"code": "subscription_required", "message": "...", "details": {...same as data...}, "request_id": "..."}}
```

## 6. RevenueCat integration

* **App User ID = Supabase user id** (`users.id`, JWT `sub`). The backend only ever looks up the caller's own id.
* API: `GET {REVENUECAT_API_BASE_URL}/subscribers/{app_user_id}` with `Authorization: Bearer <secret key>`.
  The backend uses `subscriber.entitlements[app_access]` (`expires_date`, `grace_period_expires_date`,
  `product_identifier`) and `subscriber.subscriptions[product]` (`expires_date`, `unsubscribe_detected_at`,
  `billing_issues_detected_at`, `refunded_at`, `is_sandbox`, `store`, `period_type`, `store_transaction_id`).
  Unknown ids are created by RevenueCat (`201`) and treated as "no purchases". `404` is treated as an error
  (wrong base URL), never as "no purchases".
* Retries: timeouts, network errors, `429`, `5xx` → up to `REVENUECAT_MAX_RETRIES` with exponential backoff.
  `401/403` are not retried (`502 subscription_service_auth_failed`, logged as an error).
* A sync replaces local state for the user. Products RevenueCat no longer lists become `revoked`.
* **Transfers / account switching:** keep RevenueCat's restore behaviour at **"Transfer to new App User ID"**
  (Project settings → Restore behavior). A `TRANSFER` event immediately marks the sender's rows `transferred`
  (no access). The receiver gets access only from verified data (API reconcile, `/sync` or `/restore`).
  One purchase therefore never unlocks two SharkHire accounts.
* Refunds: RevenueCat sends `CANCELLATION` with `cancel_reason = CUSTOMER_SUPPORT`. The row becomes
  `refunded`, the transaction becomes `refunded`, and access follows the expiration RevenueCat reports.
  `REFUND_REVERSED` restores it.

## 7. Webhook

URL: **`https://<render-service-domain>/api/v1/subscriptions/webhook`**

RevenueCat dashboard → Integrations → Webhooks:
* Authorization header value: the exact `REVENUECAT_WEBHOOK_AUTHORIZATION` value (either `xyz` or `Bearer xyz`
  works, as long as both sides match).
* Environment: production events for production. Sandbox events are ignored unless
  `REVENUECAT_ALLOW_SANDBOX=true` (default: allowed except when `APP_ENV=production`).

Processing guarantees:
1. Authorization is checked before the body (constant-time compare). Missing secret → `503`, wrong → `401`.
2. Body must contain `event.id` and `event.type`, otherwise `422`.
3. Event stored first (unique id). An already final event → `200 {"duplicate": true}` (replays are harmless).
4. Atomic claim, so concurrent duplicates are processed once (`409 event_in_progress` for the loser, and RevenueCat retries).
5. Ordering guard: event data older than `max(last_event_at, last_synced_at)` is not applied. An `EXPIRATION` for an
   earlier period than the stored one is ignored.
6. Payments are idempotent per (`store`, `transaction_id`).
7. Then reconciled with the API when the key is set. If that fails, the event is still processed, the row keeps
   `needs_reconcile=true`, and the note is stored in `last_error`.
8. Unexpected errors → rollback, event `failed`, **HTTP 500** so RevenueCat retries. A retry re-claims it.

| Event | Effect |
|---|---|
| `INITIAL_PURCHASE`, `RENEWAL`, `UNCANCELLATION`, `SUBSCRIPTION_EXTENDED`, `NON_RENEWING_PURCHASE`, `TEMPORARY_ENTITLEMENT_GRANT`, `REFUND_REVERSED` | active until `expiration_at_ms` if `entitlement_ids` contains `app_access` |
| `CANCELLATION` | auto-renew off, access until expiration; `CUSTOMER_SUPPORT` = refund |
| `EXPIRATION` | access removed (`paused` when `expiration_reason=SUBSCRIPTION_PAUSED`) |
| `BILLING_ISSUE` | `billing_issue`; access until `grace_period_expiration_at_ms` / expiration |
| `SUBSCRIPTION_PAUSED` | access until the paid period ends |
| `PRODUCT_CHANGE` | recorded; state comes from the following renewal + API |
| `TRANSFER` | sender revoked; receiver via verified sync |
| `TEST`, unknown types | stored as `ignored`, `200` |
| unknown App User ID (e.g. `$RCAnonymousID:…`) | stored as `unmatched`, `200`; retried by the maintenance job |

Statuses in `subscription_events.processing_status`: `received`, `processing`, `processed`, `failed`, `ignored`, `unmatched`.

## 8. API (all under `/api/v1`, standard envelope)

| Method & path | Auth | Purpose |
|---|---|---|
| `GET /subscriptions/status` | JWT | access + trial + subscription status |
| `POST /subscriptions/sync` | JWT, 6/min/user | re-verify with RevenueCat |
| `POST /subscriptions/restore` | JWT, 6/min/user | verify after a store restore |
| `GET /subscriptions/history?page&page_size` | JWT | own subscriptions + paginated transactions |
| `GET /subscriptions/plans` | none | plan metadata (reference prices) |
| `POST /subscriptions/webhook` | RevenueCat secret | lifecycle events |

`GET /subscriptions/status` → `200`
```json
{"success": true, "message": "Request completed successfully", "error": null,
 "data": {"user_id": "8f0c…", "access_allowed": true, "access_status": "subscription_active",
          "trial_status": "converted", "trial_started_at": "2026-11-19T10:00:00Z", "trial_ends_at": "2026-11-22T10:00:00Z",
          "trial_remaining_seconds": 0, "subscription_status": "active", "product_id": "sharkhire_monthly_pk",
          "plan": "pakistan", "store": "play_store", "current_period_end": "2026-12-22T10:00:00Z",
          "will_renew": true, "entitlement_active": true, "environment": "PRODUCTION",
          "last_verified_at": "2026-11-22T10:05:00Z"}}
```
`POST /sync` and `/restore` return the same object plus `synced` and `revenuecat_customer_found`. They take
**no request body**. Anything sent is ignored.

`GET /subscriptions/history` → `data = {"items": [payment…], "pagination": {...}, "subscriptions": [subscription…]}`.

`GET /subscriptions/plans` → international `10.00 USD` and Pakistan `499 PKR` with configured product ids,
entitlement `app_access`, offering `default`, `trial_days: 3`, `trial_type: "app_managed"`. These are
reference prices only. The store shows the localized charged price, and plan data is never proof of purchase.

Error codes: `401 not_authenticated`, `401 invalid_webhook_authorization`, `403 subscription_required`,
`409 event_in_progress`, `422 validation_error`, `429 rate_limited`,
`500 webhook_processing_failed`, `502 subscription_service_auth_failed` / `invalid_subscription_response` /
`subscription_service_error`, `503 subscription_service_unavailable` / `subscriptions_not_configured` /
`webhook_not_configured` / `database_unavailable`.

## 9. Environment variables

| Variable | Default | Notes |
|---|---|---|
| `SUBSCRIPTION_ENFORCEMENT_ENABLED` | `true` | paywall master switch |
| `SUBSCRIPTION_EXEMPT_ROLES` | `admin` | comma-separated roles |
| `REVENUECAT_SECRET_API_KEY` | — | secret `sk_…` key, server only |
| `REVENUECAT_WEBHOOK_AUTHORIZATION` | — | long random string, same value in RevenueCat |
| `REVENUECAT_API_BASE_URL` | `https://api.revenuecat.com/v1` | |
| `REVENUECAT_ENTITLEMENT_ID` | `app_access` | |
| `REVENUECAT_OFFERING_ID` | `default` | reported by `/plans` |
| `REVENUECAT_PRODUCT_INTERNATIONAL` / `_PAKISTAN` | — | comma-separated store product ids |
| `REVENUECAT_ALLOW_SANDBOX` | unset = not in production | |
| `REVENUECAT_TIMEOUT_SECONDS` / `_MAX_RETRIES` / `_RETRY_BACKOFF_SECONDS` | `10` / `2` / `0.5` | |

Missing values fail closed and are logged by name (never value) at startup. No bank details belong in code,
config or logs.

## 10. Deployment (Render)

1. Configure RevenueCat and the stores (section 11) **before** turning enforcement on in production. Every
   existing user's 3-day trial starts on their first request after the deploy, so with no way to pay, users
   are locked out after 3 days.
2. Render → Environment: add the variables above. Generate the webhook secret with e.g.
   `python -c "import secrets; print(secrets.token_urlsafe(48))"`.
3. Apply migrations: `alembic upgrade head` (or the existing release command).
4. Deploy, then check the startup log has no `Subscription configuration:` warnings.
5. RevenueCat → Webhooks: set the URL above and the Authorization value, then **Send test event**. Expect a
   `subscription_events` row with `event_type=TEST`, status `ignored`.
6. Schedule `python -m app.scripts.maintenance` (Render Cron Job, e.g. every 15 minutes). It retries
   `failed`/`unmatched` events and reconciles `needs_reconcile` rows.
7. Make a sandbox purchase on staging with `REVENUECAT_ALLOW_SANDBOX=true` and confirm `INITIAL_PURCHASE` →
   `processed`, then `/subscriptions/status` → `subscription_active`.

## 11. External configuration checklist

* **Google Play Console:** monthly auto-renewing subscription(s) for the international and Pakistan prices (no
  free-trial offer), service-account credentials linked in RevenueCat, Real-time developer notifications to RevenueCat.
* **App Store Connect:** monthly auto-renewable subscription(s), PKR pricing for Pakistan, In-App Purchase key
  and App Store Server Notifications V2 URL from RevenueCat.
* **RevenueCat:** products attached to entitlement `app_access`, offering `default`, secret API key, webhook
  (URL + Authorization), restore behaviour "Transfer to new App User ID".
* **Company:** confirm whether employers must pay (currently yes, see `SUBSCRIPTION_EXEMPT_ROLES`).

## 12. Testing

```bash
pytest tests/test_trials.py tests/test_subscriptions.py tests/test_access_control.py   # billing
pytest                                                                                  # everything
```
RevenueCat is mocked with `httpx.MockTransport` (payloads follow the documented v1 shapes). The suite does not
prove real store purchases, real webhook delivery or live RevenueCat responses. Verify those in sandbox
(section 10, step 7).
