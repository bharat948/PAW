# GitHub connection

6 November 2026. The `paw-space` account, private PAW Space App (ID `5639294`, slug `paw-space`) and private `paw-space/paw-sandbox` repository are verified in GitHub. PAW is connected locally. Hermès read the sandbox README through the gateway, and the owner-approved test issue was created exactly once. Implementation changes remain uncommitted.

## Confirmed scope

- Use the separate GitHub user account `paw-space`, as confirmed by the owner.
  Account creation and successful sign-in were verified in the GitHub browser.
- Connect PAW through a GitHub App with selected repository access, following 002G
  in [the phase plan](phase-plans.md). A GitHub user account and a GitHub App are
  separate setup steps. The App is registered and installed; its private key is
  configured locally.
- Keep the completed runtime-registry changes intact. GitHub work follows that
  slice. Generalist benchmark work remains a separate future task.

## Connection requirements

1. Account signup and sign-in are complete. Passwords and verification codes stay
   in the owner's browser, outside chat and project files.
2. Identify or register the PAW GitHub App under the intended account. Confirm the
   selected repositories, callback URLs and installation. Keep private keys outside
   the repository and store connector secrets encrypted.
3. Implement installation authentication and expiry-aware tokens, a connection
   flow bound to the authenticated PAW owner, disconnect/revocation handling,
   webhook signature validation and rate-limit handling.
4. Installation callback IDs alone do not prove ownership. For this single-owner
   installation, explicitly bind the configured GitHub account to `paw-space`.
   Resolve its installation server-side using the App JWT and the configured
   account login, and compare the trusted result with any callback installation ID.
   The owner-authenticated connect attempt must be session-bound, expiring and
   single-use. Account identity never comes from an agent's tool arguments.
5. Add the planned six read tools through PAW's gateway, enforcing both installation
   repository access and actor policy. Guessed or unauthorized repositories must
   be denied before their content is returned.
6. Add approval-gated issue creation in an owner-designated sandbox repository.
   The local 002F approval path now implements stored immutable arguments, owner
   step-up, single use/expiry, operation markers and uncertain-outcome reconciliation.
   The sandbox issue path passed one live owner approval and replay check;
   automatic continuation is not wired.
7. Show connection state and repository access on Integrations, then verify the
   complete connection/read/revocation flow against GitHub. Record live acceptance
   separately from fake-provider tests. Do not report a live connection from local
   implementation or fake tests alone.

## Verified external setup and remaining acceptance

- Owner approved creating the App and sandbox repository. The repository was
  created private with a README at commit `072ebf85a4e39030cfdce3c4bb6aea89a879ff70`.
- Installation review exposed five extra permissions in the registered App:
  repository creation, license policy, advisories, webhooks and workflows. These
  were removed before installation. GitHub confirmed the update, and the second
  installation review shows only contents, checks, pull requests and mandatory
  metadata read; issues read/write. Installation was subsequently approved and verified, as recorded below.
- Owner approved sandbox-only installation. GitHub installation `179190258` was
  created and verified in its settings on 6 November 2026 at 13:43 IST. It selects
  only `paw-space/paw-sandbox` (repository ID `1447989688`), with the exact approved
  read permissions and issues read/write. All current/future repositories is off.
- The owner manually installed a usable App PEM at
  `/home/bharat/.config/paw/github-app.pem` and configured all five GitHub settings
  locally. No key contents belong in chat, logs, project files or version control.
- Homepage is `http://localhost:5173`; setup callback is
  `http://localhost:8000/v1/integrations/github/installed`. User OAuth is not
  requested: the account binding is operator-configured and verified server-side.
- Webhook delivery remains inactive until an HTTPS endpoint is configured. The
  signature-checked endpoint is implemented locally. Reads independently validate
  current installation and selected repository access. Live webhook acceptance is
  still pending.
- The local UI now has connection controls, a verified repository list, and an
  approval inbox displaying the exact issue title/body. The gateway has six read
  tools and an approval request tool. Admin Hermès profile version 3 grants the
  approval request tool only for the selected sandbox repository.
- Approval payloads and bindings are immutable, expire in 24 hours, and execute
  once from stored arguments. A committed STARTED operation survives process
  failure. Unknown outcomes are searched by marker and never automatically POSTed
  again. Conversation deletion/redaction invalidates pending approvals and removes
  stored payloads. Live issue creation and owner step-up acceptance passed.
  Provider marker reconciliation remains unverified because no uncertain outcome
  occurred. Automatic continuation runs are not yet wired.
- The local database is at migration `0017`. PAW has an active owner GitHub
  connection; the gateway registered all seven GitHub tools. Admin Hermès profile
  version 2 granted six read tools. A version 2 Hermès run called
  `github_list_repos` successfully. A separate live run called `github_get_file`
  for `paw-space/paw-sandbox/README.md` at `main`, and reported file SHA
  `48141a400cfad67ebd4ea0cc1dbe2276bb8132d2`; the gateway recorded
  `allowed/OK`. A direct provider check also listed the sandbox and read its
  README with a source SHA.
- Admin Hermès profile version 3 now additionally grants
  `github_create_issue`, scoped to `paw-space/paw-sandbox`. Hermès was recreated,
  reports a matching config and is ready. A live run requested one issue with
  approval ID `22c23c4c-be51-4fd2-95f0-639b9400991e`. Its exact title/body
  and repository were confirmed in the owner inbox. The owner approved that exact
  payload. PAW reported SUCCEEDED with
  [sandbox issue #1](https://github.com/paw-space/paw-sandbox/issues/1), a second
  decision returned 409, and a fresh GitHub read found exactly one matching issue.
  A direct read of `paw-space/not-selected` returned `out_of_scope`. A reversible
  PAW disconnect denied the next read as `not_connected`; reconnect restored only
  the selected sandbox. GitHub-side uninstall/webhook revocation remains untested
  while webhook delivery is inactive.
- The current HTTPS remote-access and outside-network phone web demo scope is
  unchanged. Benchmark work is a separate later task; no benchmark execution here.

## Primary references

- [GitHub App registration](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app).
- [Manifest registration](https://docs.github.com/en/apps/sharing-github-apps/registering-a-github-app-from-a-manifest).
- [Setup URL and installation ownership](https://docs.github.com/en/apps/creating-github-apps/registering-a-github-app/about-the-setup-url).
- [Installation access tokens](https://docs.github.com/en/apps/creating-github-apps/authenticating-with-a-github-app/generating-an-installation-access-token-for-a-github-app).

## Local verification (6 November 2026)

- GitHub transport, callback, encryption, revocation, repository scope and credential
  persistence/log scan; approval execution/recovery; MCP integration: **37 passed**
  (`/tmp/paw-github-final.log`). This includes the corrected MCP write-tool hint.
- Additional active-profile revocation coverage: approval suite **13 passed**
  (`/tmp/paw-approval-profile-final.log`).
- Broader gateway/profile/privacy/Calendar/migration batch: **395 passed**, with
  one old test asserting that every tool was read-only. That assertion was updated
  to check the declared side-effect class and passed in the targeted run above.
- Web: **69 passed**, typecheck passed, production build passed. Existing lint
  warnings remain. Repository-scope changes are included in owner profile review.
- Server lint and typecheck passed (115 source files); diff whitespace check passed.
- The checks above used disposable local databases and fake provider responses.
  They did not prove the live flow; the subsequent acceptance bullets record the
  live checks separately. Changes remain uncommitted.

## Manual configuration and live acceptance

The owner configured the connection manually using
[Configure GitHub manually](operations-github.md). The guide remains useful for
rotation and recovery. The owner authorized a small number of paid live Hermès
checks; the README read and the owner-approved issue creation above are complete.
The approved issue is intentionally left in the disposable sandbox as the live
acceptance record.
