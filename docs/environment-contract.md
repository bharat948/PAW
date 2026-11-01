# Environment contract (PAW-001D-01)

Status: **implemented for `home` config + compose + proxy; tested on one dev machine** (see Evidence).
Not yet built: `pawctl`, launcher, tunnel, backups, CI releases, real authentication (001D-02..10).

## Environments

| `PAW_ENV` | Used by | Auth/secrets | Data |
|---|---|---|---|
| `local` | `./dev.sh`, `make up` | dev defaults allowed | disposable |
| `ci` | tests | test-only | throwaway |
| `home` | `deploy/compose.home.yaml` | real secrets required | persistent, external volumes |
| `staging` / `production` | later cloud | real secrets, https CORS | managed |

## Home startup rules (enforced in `core/config.py`, tested in `tests/test_config.py`)

The API and worker refuse to start when `PAW_ENV=home` and any of these hold:
- `PAW_INSTALLATION_ID` empty.
- `PAW_PUBLIC_BASE_URL` missing or not `https://`.
- `PAW_OWNER_TOKEN`, `PAW_S3_ACCESS_KEY`, `PAW_S3_SECRET_KEY` or `PAW_DATABASE_URL` empty, or containing a
  placeholder marker (`paw_local_only`, `changeme`, `change-me`, `change_me`, `replace`, `example`, `todo`).
- Any CORS origin is `http://`.
- `PAW_AUTH_MODE` is not `google` (the dev owner token is not a login method in home), or `PAW_API_PUBLIC_URL` / `PAW_WEB_URL` is not https,
  or the Google client ID/secret or `PAW_ALLOWED_EMAILS` is missing. See `docs/auth-google-setup.md`.

New keys: `PAW_AUTH_MODE`, `PAW_GOOGLE_CLIENT_ID`, `PAW_GOOGLE_CLIENT_SECRET`, `PAW_ALLOWED_EMAILS`, `PAW_API_PUBLIC_URL`, `PAW_WEB_URL`,
`PAW_SESSION_TTL_S`, `PAW_SESSION_IDLE_S`, `PAW_STEPUP_WINDOW_S`, `PAW_INSTALLATION_ID`, `PAW_ENABLED_PROFILE` (`foundation` | `admin-agent`, default `foundation`),
`PAW_PUBLIC_BASE_URL`. Existing: `PAW_RELEASE_REVISION`. Values come from an env file outside the repo
(`deploy/config/home.env.example` documents each key). Never put a secret in a `VITE_*` value.

## Files

| Path | Purpose |
|---|---|
| `deploy/compose.home.yaml` | Project `paw-home`; db, s3, api, worker, proxy; `migrate` behind a profile |
| `deploy/proxy/` | Builds the SPA with `VITE_API_BASE_URL=/api`; nginx serves it and routes `/api` |
| `deploy/config/home.env.example` | Settings template |

## What the compose file guarantees

- **Fixed identity:** project name `paw-home`; volumes `paw_home_pgdata` and `paw_home_s3data` are `external`,
  so a missing volume stops startup (`external volume ... not found`) instead of creating an empty database.
- **One published port:** only the proxy, on `127.0.0.1:${PAW_PROXY_PORT:-8080}`. db and s3 are on an
  `internal` network. The worker is on the routable network because embedding calls need outbound access.
- **No implicit migration:** ordinary start never runs Alembic. A stale schema makes `/ready` return 503, so the
  api container is not healthy and the proxy does not start.
- **Same origin:** `/api/v1/*`, `/api/health`, `/api/ready` are proxied (prefix stripped once). Everything else under
  `/api/` is 404, `/docs` and `/openapi.json` are not exposed, other paths fall back to the SPA. SSE is unbuffered.
- **Fail fast:** every required variable uses `${VAR:?}`.

## Commands (manual until `pawctl` exists)

```
ENVF=/etc/paw/home.env          # chmod 600, outside the repo
DC="docker compose --env-file $ENVF -f deploy/compose.home.yaml"

# one-time bootstrap (owner only; never part of routine start)
docker volume create paw_home_pgdata && docker volume create paw_home_s3data
$DC up -d db s3 --wait
$DC --profile migrate run --rm migrate
$DC run --rm -e PAW_S3_CREATE_BUCKET=true api python -c \
  "from paw_server.core.config import get_settings as g; from paw_server.modules.ingestion.storage import build_store as b; b(g())"

# routine
$DC up -d --build --wait        # start (builds locally until CI releases exist)
$DC down                        # stop; volumes are kept
$DC ps ; $DC logs -f api worker
```

Never run `docker volume rm` or `down -v` on the `paw_home_*` volumes on a real install.

## Evidence (2026-11-01, dev laptop, commit `4feffa9` + uncommitted work, not a release acceptance)

Throwaway volumes, fake embedding/LLM providers, then removed.

| Test | Result |
|---|---|
| D-T02 container-only stack boots, services resolve | pass |
| D-T03 invalid home config rejected | pass (unit tests); missing env var rejected by compose |
| D-T05 original upload/download | pass, sha256 matches |
| D-T06 exposure | only the proxy is published; db/s3 not published. Controller/MCP not built yet |
| D-T07 anonymous / bad token | 401 / 401 |
| D-T09 ingestion | upload reached `COMPLETED` through the worker |
| D-T10 retrieval | 1 result for the fixture query |
| D-T11 container replacement | `down` + `up`, data and original hash intact |
| D-T24 missing volume | refuses to start |

Not tested: real OpenAI providers in the home stack, mobile data, reboot, `--build` on the target laptop,
D-T08/T12-T23/T25-T42.

## Known gaps and notes

- Migrations still use the application DB role (role separation is 001D-02).
- Images are built on the host; immutable CI images and the release manifest are 001D-05.
- The dev `paw-server/compose.yaml` puts the worker on an internal-only network, so with
  `PAW_EMBEDDING_PROVIDER=openai` under `make up` the worker cannot reach OpenAI. Home compose does not have this.
- `PAW_OWNER_TOKEN` bearer auth is still the interim auth; Cloudflare Access verification is 001D-06.

## Backups and restore (001D-02)

Tool: `scripts/backup/pawbackup.py`. Run with
`uv run --project paw-server --with pyrage python scripts/backup/pawbackup.py <command>`.

| Command | What it does |
|---|---|
| `keygen` | New `age` key pair. **Generate on another machine**; the laptop only ever holds the public recipient. |
| `run` | Stops the worker, `pg_dump -Fc`, exports every object with sha256, checks every DB-referenced object is present, tars and encrypts (`age`), uploads, re-downloads and hashes the remote copy, writes `manifest.json` **last** (that is what makes a backup "complete"), cleans local staging, applies retention, restarts the worker. |
| `list` / `prune` | Completed backups / retention (default 7 newest + 4 weekly; never removes the newest). |
| `restore ID` | Validates first (hash, decrypt, per-object hashes, all references), then restores into a **new isolated** project and volumes. Refuses `paw-home` and any existing volume. Does not activate anything. |
| `drill ID` | `restore`, then removes the target. Use after material changes. |

Settings (environment): `PAW_HOME_ENV`, `PAW_BACKUP_ENDPOINT`, `PAW_BACKUP_BUCKET`, `PAW_BACKUP_ACCESS_KEY`,
`PAW_BACKUP_SECRET_KEY`, `PAW_BACKUP_AGE_RECIPIENT`, optional `PAW_BACKUP_REGION`, `PAW_BACKUP_STAGING`
(must be under `$HOME` for Docker Desktop on Linux), `PAW_BACKUP_KEEP_DAILY`, `PAW_BACKUP_KEEP_WEEKLY`.
The destination is any S3-compatible bucket, so moving from the local stand-in to Cloudflare R2 is configuration only.

Consistency argument: the original object is stored before its database row commits and objects are never deleted,
so a dump taken first and an object export taken after hold every object the dump references. Restore re-checks this.

Limits: the whole backup is held in memory while encrypting, so it suits small data sets (tens of MB to low GB)
and needs streaming before it is used for large ones. Not scheduled yet (no timer or missed-run catch-up).
The `age` private key is the only way to read a backup: losing it makes every backup unreadable.

### Evidence (2026-11-01, dev laptop, throwaway stack, local S3 stand-in as destination)

| Test | Result |
|---|---|
| Backup of 2 documents / 2 objects | complete in 4.6 s, 7.4 kB encrypted, staging cleaned, worker restarted |
| D-T13 isolated restore (drill) | restored same migration revision and document count, objects verified, 21.5 s including stack start; live stack untouched |
| D-T28 bad credentials / unreachable destination | fails before changing anything; nothing marked complete; worker running |
| D-T29 corrupt blob | rejected on hash, no volumes created |
| D-T29 set with an object removed | rejected, no volumes created |
| Wrong decryption key | rejected, no volumes created |
| Restore into live project name | refused |
| Retention (keep 2 of 4) | pruned the two oldest |

Not tested: real R2, a restore on a second machine, backups while uploads are in flight, large data, scheduling,
disk-full, the RPO/RTO targets on the real laptop (the 21.5 s figure is for a tiny fixture, not a recovery-time claim),
D-T14 (clean host rebuild), D-T37 (missed schedule). The drill restores to the same host; a different host is untested.
