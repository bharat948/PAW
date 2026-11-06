# Configure GitHub manually

Prepared for Bharat on 6 November 2026. The owner has since installed the key,
connected PAW, and completed a live README read and owner-approved sandbox issue
test. Keep this guide for recovery and key rotation on the local machine. The
implementation changes are uncommitted.

## 1. Confirm the existing GitHub setup

These resources are already created. Do not create another App or installation.

| Setting | Verified value |
| --- | --- |
| GitHub account | `paw-space` |
| App name / slug | `PAW Space` / `paw-space` |
| App ID | `5639294` |
| Installation ID | `179190258` |
| Selected private repository | `paw-space/paw-sandbox` |
| Repository ID | `1447989688` |
| Read permissions | Contents, checks, pull requests, metadata |
| Write permission | Issues only |
| Webhook delivery | Inactive for now |

Review [the installed App](https://github.com/settings/installations/179190258)
while signed in as **paw-space**. “Only select repositories” must show just
`paw-space/paw-sandbox`. The sandbox has a `README.md` on `main` for the first read.
The separately connected Codex GitHub account `bharat948` is not this installation.

## 2. Download and store the private key

Open [PAW Space key pairs](https://github.com/settings/apps/paw-space/key_pairs)
in your regular browser, signed in as `paw-space`. Click **New key** yourself and
save the downloaded `.pem` file. In-app browser download capture did not work.
The owner has installed a usable private PEM locally. For rotation, generate a
replacement in GitHub and follow the guarded install procedure below.

Store the downloaded file at the approved destination below. Replace the source
path in this example with the actual downloaded filename. Run these as your normal
user, without `sudo`:

```sh
install -d -m 700 /home/bharat/.config/paw
if test -e /home/bharat/.config/paw/github-app.pem; then
  echo "Destination already exists. Inspect it before replacing a working key."
else
  install -m 600 -- /absolute/path/to/downloaded-key.pem /home/bharat/.config/paw/github-app.pem
fi
```

After the file has been installed, verify that it can be read without printing
its contents:

```sh
openssl pkey -in /home/bharat/.config/paw/github-app.pem -noout -check
stat -c '%a %U %n' /home/bharat/.config/paw/github-app.pem
```

Expected: a valid key, mode `600`, owned by `bharat`. Do not paste the key into
chat, `.env`, source files or logs. Keep it outside the checkout and Docker build
contexts. Retain the download securely until connection succeeds. Afterwards,
review old keys in GitHub settings and remove any unused key yourself; do not
remove the working replacement.

## 3. Set the local PAW configuration

Edit `/home/bharat/PAW/paw-server/.env`. Replace any existing definitions of these
five settings rather than adding duplicate lines:

```dotenv
PAW_GITHUB_APP_ID=5639294
PAW_GITHUB_APP_SLUG=paw-space
PAW_GITHUB_ACCOUNT=paw-space
PAW_GITHUB_PRIVATE_KEY_FILE=/home/bharat/.config/paw/github-app.pem
PAW_GITHUB_SANDBOX_REPOSITORY=paw-space/paw-sandbox
```

Leave `PAW_GITHUB_WEBHOOK_SECRET` empty while webhook delivery is inactive. A
GitHub OAuth client secret and a personal access token are not needed for this
App installation flow. The installation ID is verified server-side; there is no
`PAW_GITHUB_INSTALLATION_ID` setting to add.

The local configuration was checked without displaying secrets:
`PAW_AUTH_MODE=both`, an existing `PAW_CONNECTOR_KEY` is present, and the API URL
uses `http://localhost:8000`. **Keep the existing connector key unchanged.** Calendar
connections rely on it. Keep the existing database, Google login, runtime and
model-provider settings too.

API and gateway must receive the same five GitHub settings and connector key.
The current `dev.sh` runs both on the host from `paw-server`, where settings load
`.env`. Exported shell variables take precedence over `.env`; remove or correct
stale exported `PAW_GITHUB_*` values before restarting.

For a future container deployment, the host path above is not automatically
available inside the container. Mount the PEM read-only into the API and gateway,
set `PAW_GITHUB_PRIVATE_KEY_FILE` to that container-visible path, and pass the
settings to both services. Do not give the PEM to the Hermès runtime. Existing
home deployment files have not been updated or verified for this integration.

## 4. Apply local migrations and restart

Wait for active agent work to finish, then stop the current foreground `dev.sh`
with Ctrl-C. From `/home/bharat/PAW`, start the existing development launcher:

```sh
cd /home/bharat/PAW
./dev.sh
```

The launcher starts local infrastructure, applies migrations and starts services.
It also synchronizes dependencies, exports the active runtime profile and recreates
the Hermès container when the existing image and secrets file are available.
This is the normal local development restart, not a shared deployment command.
Migrations `0015` (runtime registry), `0016` (GitHub) and `0017` (approvals) must all
be applied. Do not run them against a shared database without separate approval.

If you operate services separately instead, use the existing local database
configuration and run these from `/home/bharat/PAW/paw-server` before restarting
the API, gateway and agent worker through your existing process setup:

```sh
uv run --no-sync alembic -c alembic.ini upgrade head
uv run --no-sync alembic -c alembic.ini current
```

Expected current revision: `0017` for this checkout. Confirm
[API readiness](http://localhost:8000/ready) succeeds. `/health` alone does not prove
that migrations are applied. The agent also needs its existing Hermès image,
runtime credentials and gateway configuration; GitHub settings do not supply them.

## 5. Finish connecting in PAW

1. Open [PAW Integrations](http://localhost:5173/#/integrations) and sign in as the
   owner using the existing login method.
2. On GitHub, choose **Finish connecting**. The App is already installed. A recent
   Google sign-in is required for cookie sessions; follow the reauthentication
   link if prompted. Local development bearer mode also supports this button.
   “Select repositories in GitHub” uses a cookie-session redirect, so skip that
   link when using only a development token.
3. Confirm GitHub changes to **Connected**, account `paw-space`, with exactly
   `paw-space/paw-sandbox` in the verified repository list.

The server resolves the configured account's installation with the App credential.
A browser callback installation ID alone never attaches a connection. Newly added
repositories do not silently enter PAW's saved connection: reconnect after
reviewing any later repository-access changes.

## 6. Allow Admin Hermès to use the tools

Connection and actor permission are separate. Existing profile grants were not
expanded automatically.

Open [the profile](http://localhost:5173/#/profile), preserve the existing tools,
and select the GitHub read tools you want:

```text
github_list_repos
github_get_file
github_list_prs
github_get_pr
github_get_checks
github_list_issues
```

Save a new version, review the differences and activate it with owner confirmation.
Restart `./dev.sh` after activation so the runtime loads the exported tool list.
Confirm the profile no longer reports an older runtime configuration. Do not
bypass attestation to make a tool call succeed.

For narrower actor access, the profile API supports this optional structure in a
new profile spec; the web profile shows these scopes but does not edit them:

```json
{
  "tools": {
    "grants": ["github_list_repos", "github_get_file"],
    "resource_scopes": {
      "github_list_repos": ["paw-space/paw-sandbox"],
      "github_get_file": ["paw-space/paw-sandbox"]
    }
  }
}
```

This is a fragment, not a complete replacement spec. Preserve the rest of the
profile and other required grants. An omitted tool scope allows the connection's
selected repositories; an empty list permits no repositories. Both installation
access and actor scope apply to each read.

## 7. Verify reads before enabling issue creation

Ask Admin Hermès:

> List the GitHub repositories I connected, then read README.md from
> paw-space/paw-sandbox at main. Give the source commit or file SHA.

Confirm the result contains the sandbox, README text and source SHA. Then verify
an unselected private repository cannot be read. List issues and PRs; empty lists
are valid for a fresh sandbox. Reading a PR or check run needs a repository fixture
that actually contains one. Agent messages use the configured model and may incur
normal provider charges; connecting and listing repositories from Integrations
does not require a model call.

Do not treat a green connection card or fake-provider test as proof that the
agent's gateway/profile/runtime path works. Record which live checks you ran.

## 8. Optionally enable owner-approved sandbox issues

Only when ready to test writes, grant `github_create_issue` through a new profile
version and restart the runtime as above. Ask it to propose one issue in
`paw-space/paw-sandbox` with a title and body you recognize.

No issue should exist yet. Open **Integrations → Issue approvals**, refresh, and
review the exact repository, title and body. Choose **Reject** to cancel it, or
**Approve and create issue** to execute that stored payload. Recent owner sign-in
is required. Verify the resulting issue link at GitHub; repeating the approval
must not create another issue. Requests expire after 24 hours.

PAW appends `<!-- paw-op:{operation-key} -->` to support recovery. If the response
is lost after GitHub may have accepted the write, the receipt remains UNKNOWN.
**Check GitHub outcome** searches for the existing marker and never sends a second
create POST. If no marker is found or access is unavailable, it stays UNKNOWN.
Do not request a replacement issue until you have checked whether the first exists.
A restart leaving STARTED uses the same reconciliation path. Platform events and
operation receipts are durable; automatic conversation continuation is not wired.

## Troubleshooting

| What you see | Next check |
| --- | --- |
| GitHub unavailable | Confirm all five settings and the existing connector key reach both API and gateway; restart them. |
| Connection fails with key unavailable | Check the PEM path, owner permissions and key validation; do not print its contents. |
| App not installed / no repositories | Sign in as `paw-space` and review installation `179190258`, not another account's App. |
| Sign in again / step-up required | Reauthenticate as the PAW owner, return to Integrations and retry. |
| CSRF failure | Reload PAW, sign in again and use its buttons; do not turn off CSRF checks. |
| Connected but tool denied | Check the active profile grants, repository scope and runtime configuration/attestation. |
| Database readiness fails | Check the local database and migration revision before retrying. |
| GitHub rate limit | Wait before retrying; do not loop connection attempts. |
| Access revoked | Restore the intended selected-repository installation, then reconnect in PAW. |
| UNKNOWN issue outcome | Use Check GitHub outcome and inspect GitHub; do not blindly create a replacement. |

Disconnecting in PAW removes its saved connection; it does not uninstall the App.
To revoke provider access, suspend or uninstall it from the GitHub installation
settings. Every operation rechecks live installation/repository access. Verify a
subsequent read fails after revocation before claiming live revocation acceptance.

## Later: signed webhooks and remote web access

Keep webhook delivery inactive until a public HTTPS API endpoint exists. Then
configure a strong webhook secret identically in the API environment and App
settings, use `https://<public-api>/v1/integrations/github/webhook`, and enable
lifecycle delivery. Verify a real signed delivery. The local endpoint validates
raw-body signatures, deduplicates delivery IDs and handles uninstall/suspension;
repository additions do not widen the saved access snapshot.

HTTPS remote access and the outside-network phone web demo remain in the current
project scope; they are not prerequisites for the local manual connection steps
above. Benchmark documentation and execution remain a separate later task.
