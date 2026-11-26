# Deploying scraper0 (Cloud Run)

Two Cloud Run services built from this repo:

| Service | Image | Notes |
|---|---|---|
| `scraper0-api` | `./Dockerfile` (Python + headless Chromium) | `max-instances=1`, 60-min timeout, 2 GiB, Direct VPC egress to Cloud SQL |
| `scraper0-web` | `frontend/Dockerfile` (Next.js standalone) | proxies `/v1/*` and `/api/*` to the API |

Background work: **Cloud Tasks** queue `scraper0-jobs` calls `POST /internal/jobs/{id}/run`. **Cloud Scheduler** calls
`POST /internal/tick` every minute to run due sources and fail stale jobs. Both send `Authorization: Bearer $SCRAPER0_INTERNAL_TOKEN`.

## 1. Database (once): a `scraper0` database on the hashtaglocal Postgres instance

scraper0 shares the hashtaglocal Cloud SQL instance but gets **its own database and user**. Migrations, backups and
table names stay independent, and it can move out later with a `pg_dump`.

```sql
-- connect as an admin: gcloud sql connect <INSTANCE> --user=postgres
CREATE USER scraper0 WITH PASSWORD '<strong password>' CONNECTION LIMIT 3;   -- hard cap at the server too
CREATE DATABASE scraper0 OWNER scraper0;
```

`CONNECTION LIMIT 3` makes Postgres itself refuse a 4th connection. The app also caps its own pool (2 + 1 overflow) and
Cloud Run runs at most one instance. **If you ever raise `max-instances`, lower `SCRAPER0_DB_POOL_SIZE`** so that
`instances × (pool + overflow) ≤ 3`.

Trade-off: during a deploy, the old revision drains while the new one starts. For those seconds, a hard limit of 3 can refuse a
connection; the request then fails, and Cloud Tasks retries jobs. To avoid that, use `CONNECTION LIMIT 4` or set
`SCRAPER0_DB_POOL_SIZE=1` (1 + 1 overflow per instance).

Connection string (private IP via Direct VPC egress; the instance is at `10.110.112.7`):
`postgresql+psycopg://scraper0:<password>@10.110.112.7:5432/scraper0`

## 2. Secrets (once)

```bash
P=ai-agent-boilerplate0
s() { printf '%s' "$2" | gcloud --project $P secrets create "$1" --data-file=- 2>/dev/null || \
      printf '%s' "$2" | gcloud --project $P secrets versions add "$1" --data-file=-; }
s scraper0-db-url        'postgresql+psycopg://scraper0:<password>@10.110.112.7:5432/scraper0'
s scraper0-secret-key    "$(uv run scraper0 gen-secret-key)"        # Fernet key: encrypts AI keys + portal sessions
s scraper0-internal-token "$(openssl rand -base64 36)"
s scraper0-admin-password '<first platform-admin password, ≥ 10 chars>'
s scraper0-groq-key      ''   # optional: a Groq key to pre-seed a shared provider; otherwise add providers in the dashboard
```

**Back up `scraper0-secret-key`.** Without it, stored AI keys and BBMP sessions can't be decrypted. You would have to
re-enter the keys, and the BBMP sessions just log in again.

## 3. Deploy

```bash
PROJECT=ai-agent-boilerplate0 REGION=us-central1 SQL_INSTANCE=<instance-name> \
ADMIN_EMAIL=admin@madhyamakist.com \
ALLOWED_BACKEND_URLS=https://staging.api.smalltech.in,https://api.smalltech.in \
./deploy/deploy.sh
```

On every boot the API runs migrations under a Postgres advisory lock, then seeds idempotently: the platform admin, the shared
Groq provider and the `hashtaglocal` tenant. Check it with `curl $API_URL/api/health`, which returns `db: ok`.

## 4. Switch hashtaglocal-backend over

1. **No code change needed.** In the backend's environment, point `EVENTS_SCRAPE_URL`, `PORTALISSUE_SCRAPE_URL` and
   `PORTALISSUE_REPORT_SCRAPE_URL` at `https://<scraper0-api>/api/v1/scrape`. With `SCRAPER0_LEGACY_KEYLESS=true`, keyless calls
   are attributed to the `hashtaglocal` tenant. The request and response contract is unchanged.
2. **Then lock it down:** create an API key in the dashboard (hashtaglocal tenant → API keys). Add the header
   `X-API-Key: <key>` in `ScrapeApiClient` and `PortalIssueScrapeClient`, then redeploy scraper0 with `LEGACY_KEYLESS=false`.
3. The backend's event ingestion cron keeps working as-is. Optionally, move it to scraper0 **Sources**, then consume
   `GET /v1/items/changes?entity=event&cursor=…` so the backend only ever receives new or changed events.

Rollback: point the three backend URLs back at the old VM (`/webscraperstaging/api/v1/scrape`). The VM is untouched until you
decommission it.

## Local

```bash
cp .env.example .env
uv sync && uv run playwright install chromium     # chromium only for SMARTONEBLR
uv run scraper0 serve                               # http://127.0.0.1:5001, docs at /api/docs
cd frontend && npm install && npm run dev           # http://localhost:3000
```
