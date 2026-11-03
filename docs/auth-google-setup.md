# Google sign-in setup (PAW-001D-06)

PAW signs the owner in with Google (OpenID Connect, authorization code + PKCE). PAW verifies Google's signed token itself
and keeps a revocable server-side session; only the Google accounts in `PAW_ALLOWED_EMAILS` can sign in. The long-lived
developer token still works in `PAW_AUTH_MODE=token|both` for local development and is **rejected in the home environment**.

## 1. Create the OAuth client in Google Cloud (about 5 minutes)

1. Open https://console.cloud.google.com and create or choose a project.
2. **APIs & Services > OAuth consent screen**: user type **External**, app name "PAW", your email as support and contact.
   Leave it in **Testing** and add your Google account(s) under **Test users** (no Google review is needed for personal use;
   test-mode refresh limits do not matter because PAW only uses the sign-in, not Google APIs).
3. **APIs & Services > Credentials > Create credentials > OAuth client ID > Web application**.
4. **Authorized redirect URIs** (exact match, no trailing slash):
   - local: `http://localhost:8000/v1/auth/google/callback`
   - home deployment: `https://<your-domain>/api/v1/auth/google/callback`
5. Copy the **client ID** and **client secret**.

Scopes requested: `openid email` only. PAW never asks for access to Gmail, Drive or any Google data.

## 2. Configure PAW (never commit these values)

`paw-server/.env` (local development):
```
PAW_AUTH_MODE=both                 # keep the developer token while testing; use google to disable it
PAW_GOOGLE_CLIENT_ID=...apps.googleusercontent.com
PAW_GOOGLE_CLIENT_SECRET=...
PAW_ALLOWED_EMAILS=you@example.com # comma-separated, case-insensitive
```
Then **restart the API fully** (`./dev.sh`): settings are read once at startup. Defaults that normally need no change:
`PAW_API_PUBLIC_URL=http://localhost:8000`, `PAW_WEB_URL=http://localhost:5173`.

Home deployment: set the same three values plus `PAW_PUBLIC_BASE_URL` in the env file (`deploy/config/home.env.example`);
the compose file derives `PAW_API_PUBLIC_URL` and `PAW_WEB_URL` from it and forces `PAW_AUTH_MODE=google`.

## 3. How it behaves

| Topic | Behaviour |
|---|---|
| Sign in | "Continue with Google" -> Google -> back to PAW. Accounts not on the allow-list get no session |
| Session | Opaque HttpOnly `paw_session` cookie (`SameSite=Lax`, `Secure` over https); only its hash is stored. 24 h absolute, 4 h idle (`PAW_SESSION_TTL_S`, `PAW_SESSION_IDLE_S`) |
| Sign out | Revokes the session on the server; a copied cookie stops working |
| Writes | Cookie-authenticated POSTs need the `X-CSRF-Token` header (the web app does this) |
| Step-up | Sensitive actions (approvals, PAW-002F) require a Google sign-in within `PAW_STEPUP_WINDOW_S` (default 12 h); the UI sends the owner to `/v1/auth/google/start?purpose=stepup`, which forces Google to re-authenticate |
| Login errors | The browser returns to the app with `?auth_error=not_allowed|failed|expired|cancelled` and a message |

## 4. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| Google shows `redirect_uri_mismatch` | the redirect URI in Cloud Console must match `<PAW_API_PUBLIC_URL>/v1/auth/google/callback` exactly (scheme, host, port, path) |
| Google shows `access_denied` / app not verified | the account is not listed as a Test user on the consent screen |
| "That Google account is not allowed" | the account is not in `PAW_ALLOWED_EMAILS` (check the address Google signs in, case does not matter) |
| "Google sign-in could not be verified" | wrong client ID/secret, clock skew on the server, or no route to Google; check the API log line `google sign-in rejected` (it names a reason code, never an address or token) |
| No Google button on the login page | `PAW_AUTH_MODE` is `token`, or the API was not restarted after editing `.env` |
| Signed in, but every call returns 401 in the browser | cookies blocked, or the web and API are on different sites; locally both must be `localhost` (any port) |
| 403 `csrf_failed` | the page is stale or the session changed; reload |
| 403 `stepup_required` | sign in again when prompted (step-up), then retry |

## 5. Security notes

- The ID token is checked for signature (Google's published keys), issuer, audience, expiry, nonce and `email_verified`;
  algorithm confusion and `alg: none` are rejected (tests in `tests/test_auth_oidc.py`).
- `state` is single-use, expires after 10 minutes and is stored hashed; PKCE binds the code to this browser.
- `return_to` only accepts same-site relative paths (open-redirect tests included).
- Auth log lines carry reason codes only: no tokens, cookies, codes or email addresses.
- Not covered yet: multi-device session list, account lockout beyond the 30/min rate limit, Google account removal webhooks.

## Google Calendar (read-only connector)

1. In Google Cloud, enable the **Google Calendar API** for the same project, and add the scope
   `.../auth/calendar.readonly` to the consent screen.
2. Add a second **Authorized redirect URI** to the same Web client:
   `http://localhost:8000/v1/integrations/google-calendar/callback`
   (`https://<domain>/api/v1/integrations/google-calendar/callback` at home).
3. Generate a key that encrypts the stored Google tokens and put it in `paw-server/.env`
   (keep a copy outside the repo; losing it only means you reconnect):
   `PAW_CONNECTOR_KEY=<python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())">`
4. Restart `./dev.sh`, sign in, then connect from the Integrations page.

Note: while the OAuth consent screen is in **Testing**, Google expires refresh tokens after 7 days, so the
connection will show "needs reconnect" weekly. Publishing the app (even unverified, single user) removes that.
