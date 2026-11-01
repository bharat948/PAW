# PAW home deployment: purchased tools, services and cost estimate

Audience: Bharat (owner, purchase decisions). Companion to `PAW-001D_Cloud_Deployment_Foundation.md` section 22.
Prepared 2026-11-01. **These are estimates, not quotes.** Prices marked *spec* come from the 001D spec (checked
2026-10-29); prices marked *est.* are my estimates from general knowledge and must be checked on the vendor page before
paying. Currency conversion assumes ₹88 = $1 (assumption; adjust).

## 1. What you must pay for, and what is free

| # | Item | Needed? | Why PAW needs it | Billing model | Estimate | Status |
|---|---|---|---|---|---|---|
| 1 | **OpenAI API credits** | **Yes** (or a local/other model) | Embeddings for search; the LLM for Ask answers | Prepaid credits, per token | see section 2 | **Blocked now: account has no credits** |
| 2 | **Domain name** | **Yes** for the recommended remote route | Cloudflare named tunnel needs a hostname on a Cloudflare-managed domain | Yearly | ~₹800-1,500/yr (est.) | Not bought |
| 3 | Electricity for the laptop | Yes (ongoing) | Host runs 24h | Metered | ~₹115/mo at 20 W, ₹8/kWh (spec, illustrative) | Measure real draw |
| 4 | Cloudflare Tunnel + Access | Yes (recommended) | HTTPS address and login in front of the app, no router port-forwarding | Free tier, up to 50 users (spec) | ₹0 | Account/domain not set up |
| 5 | Off-device backup storage (Cloudflare R2) | Yes before real data (backups are required, 001D-02) | Encrypted backups kept outside the laptop | 10 GB free; $0.015/GB-month above (spec) | ₹0-30/mo at 10-30 GB | **Using a local S3 stand-in for now** |
| 6 | `age` encryption | Yes | Encrypts backups before upload | Open source | ₹0 | Chosen; no purchase |
| 7 | Tailscale Personal | Optional | Private admin access to the laptop | Free personal plan (spec) | ₹0 | Optional |
| 8 | GitHub + GHCR | Already used | Source, CI, release images | Free tiers; private-repo CI minutes and registry storage have quotas (spec 14.2) | ₹0 expected | Check quota when 001D-05 lands |
| 9 | UPS / small battery backup | Optional | Ride out short power cuts, clean shutdown | One-time | ~₹3,000-6,000 (est.) | Optional |
| 10 | External disk for a second local copy | Optional | Extra recovery point | One-time | ~₹3,500-6,000 (est.) | Optional |
| 11 | Spare laptop | Already owned | Compute, database, files | None | ₹0 | Confirm CPU/RAM/SSD (spec 6.1) |

Not in this budget: PAW-002 agent model usage, any paid VPS (spec 22.2 lists options for later), monitoring tools.

## 2. OpenAI usage estimate (item 1)

Models configured now: `text-embedding-3-small` (embeddings) and `gpt-4o-mini` (chat).
Rates (*est.*, verify on openai.com/api/pricing): embeddings ~$0.02 per 1M tokens; gpt-4o-mini ~$0.15 per 1M input and
~$0.60 per 1M output tokens.

| Scenario | Assumption | Monthly cost |
|---|---|---|
| Light | 300 pages ingested once (~0.2M tokens), 100 questions (~4k in / 0.5k out each) | ~$0.10 (~₹10) |
| Normal | 2,000 pages ingested (~1.2M tokens), 600 questions | ~$0.60 (~₹55) |
| Heavy | 10,000 pages (~6M tokens), 3,000 questions | ~$3 (~₹265) |
| **Hard ceiling from current app budgets** | `PAW_EMBEDDING_DAILY_TOKEN_BUDGET=2,000,000` and `PAW_LLM_DAILY_TOKEN_BUDGET=500,000` every day for 30 days | embeddings ≤ ~$1.20; chat ≤ ~$2.25 if all input, ≤ ~$9 if all output; so **about ₹1,000 worst case** |

The daily budgets in `core/config.py` are what cap spend; lowering them lowers the ceiling. Set a monthly spend
limit and low-balance alert in the OpenAI dashboard as well. Top up the smallest allowed amount first (check the
minimum on the billing page).

## 3. Monthly total

| | Typical | Ceiling |
|---|---|---|
| Electricity | ₹115 | ₹150 |
| Domain (÷12) | ₹70-125 | ₹125 |
| OpenAI | ₹10-60 | ₹1,000 |
| R2 backup storage | ₹0 | ₹30 |
| Cloudflare / Tailscale / age / GitHub | ₹0 | ₹0 |
| **Total** | **≈ ₹200-300 / month** | **≈ ₹1,300 / month** |

One-time, optional: UPS and external disk ≈ ₹6,500-12,000.
Not a measured bill; the spec itself says no savings or cost figure is a measured production bill.

## 4. Where each service plugs in (integration cost = work, not money)

| Service | Integration point | Work package | Status |
|---|---|---|---|
| OpenAI | `PAW_EMBEDDING_*`, `PAW_LLM_*` in the home env file | exists | works once credits are added |
| Cloudflare Tunnel/Access | forwards to the proxy on `127.0.0.1:8080`; Access JWT check in API | 001D-04, 001D-06 | not built |
| R2 (or any S3) | `PAW_BACKUP_*` endpoint, bucket, keys; separate from the app's originals bucket | 001D-02 | built against a local stand-in; swapping to R2 is config only |
| `age` | backup encryption; private key kept off the laptop | 001D-02 | built (public key only on the laptop) |
| Tailscale | host service, no app change | 001D-03 | optional |
| GHCR + Actions | image publish, release manifest | 001D-05 | not built |

## 5. Decisions and actions for the owner

1. Add OpenAI credits and set a monthly limit (unblocks Ask and semantic search today).
2. Choose and buy a domain only when you are ready for 001D-04 (remote access); not needed for local use.
3. Create the R2 bucket and a scoped API token when you want real off-device backups; until then backups go to the local stand-in, which does **not** protect against laptop loss.
4. Generate the `age` key pair on a different machine; keep the private key in a password manager and a printed copy. Losing it makes every backup unreadable.
5. Decide on a UPS and external disk (optional).

## 6. Risks

- A local stand-in is not off-device. Until R2 or another remote target is configured, disk loss means data loss.
- Prices and free tiers change; re-verify before each purchase (spec 22.2 says the same).
- Domain renewal lapsing would take the remote address down.
