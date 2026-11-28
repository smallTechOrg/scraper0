# NOTES — scraper0 v0.1 build journal

Branch `feature/scraper0-v0.1`, started 2026-11-24. This is a rewrite in place of `smallTechOrg/web-scraper`; the old Flask
code remains on `main`.

## How it was built
- Intake first ([spec/intake.md](spec/intake.md)), then the contracts ([spec/api.md](spec/api.md) and
  `src/scraper0/connectors/base.py`), then three parallel tracks: core/API/jobs (main), the connector port (agent),
  and the Next.js frontend (agent).
- The engine and learning code (`core/`, `events/`) came from the pre-rewrite `event_engine/` and `scraper_core/`, with
  imports rewritten. The store was rewritten onto the shared SQLAlchemy engine.

## Decisions and deviations
- **Recipes for built-in entities are shared across tenants**: public layouts are learned once for everyone. Custom
  entities are private (`entity_key = tenant_id:name`).
- **Cloud Tasks deadline is 30 min max** (< the 60-min Cloud Run timeout). The request keeps running after the deadline, and
  the retry finds the job already claimed (atomic `UPDATE … WHERE status='queued'`). So background jobs get ~60 min.
- **`/api/v1/scrape` runs the connector as a job inline**, so legacy calls appear in the dashboard and their events land in
  stored results. The error shape is a 422 JSON body. (The old Flask app actually returned a 500 HTML page for most
  handler errors, because ValidationError raised inside the view was unhandled. The backend treats any non-2xx as a failure.)
- **SMARTONEBLR `tracking_id`** is now an int. Legacy returned a string. The backend DTO is `Long`, and Jackson accepts both.
- **TeamEverest fixes** found while porting: the site moved the location key into `data-inline-binding-encoded`, so legacy
  returned `address: null`. Legacy dedupe compared `https` against `http` og:url and never matched.
- **Budget / no-provider errors are surfaced**: the event connectors re-raise them (the job fails with a clear message),
  and the generic crawler keeps structured results and reports `result.ai.errors`.
- **Page-cache fingerprint** = visible text + JSON-LD + meta + image URLs. Visible text alone hashed to the empty string
  for JSON-LD-only pages, so data edits would have been missed.
- **Items on a listing page sharing one link** get `link#hash(name,start)` keys, so they no longer overwrite each other.
- **Captcha**: vision LLM by default, so there's no PyTorch in the image. EasyOCR is still available via `uv sync --extra ocr` +
  `SCRAPER0_CAPTCHA_SOLVER=easyocr|auto`.

## After the first user test (2026-11-24): JavaScript sites
- `https://karmana.in` returned 0 items: it is a React app, so the static HTML is an empty shell (31 visible characters, no links).
- Added browser rendering for JS shells, JSON API capture, and learned API recipes (see architecture.md).
- Result on karmana.in: the first crawl renders once (~11 s) and learns `GET /api/activities`, giving 448 activities → 8 upcoming
  in-person events, with organiser names joined from `/api/changemakers` and links from the site's own anchors. **0 AI calls.**
  The second crawl uses no browser (~4 s).
- Also fixed: an address of just "Online" now marks the event online, and event type is decided by the title before the description.
- Side effect on the build machine: `playwright install chromium` removed older Chromium builds (1228, 1248) from
  `~/Library/Caches/ms-playwright`. Other repos using Playwright need `uv run playwright install chromium` again.

## Verified
- Offline suite: `uv run pytest`, 140 passed (API end-to-end, learning, engine, ingest, 70 connector tests), on SQLite
  **and on Postgres 14** with the `scraper0` user at `CONNECTION LIMIT 3` (`SCRAPER0_TEST_POSTGRES_URL=…`). `ruff` is clean.
  Migrations are idempotent on Postgres, and `alembic check` shows no drift.
- Frontend: `tsc`, `eslint` and `next build` are clean. In a real browser against the API on Postgres: login, dashboard,
  New job → TEAMEVEREST connector → live job page with 2 real events stored, and the admin tenants page. No console errors.
- Live: the server boots, migrates and bootstraps. The legacy `POST /api/v1/scrape` TEAMEVEREST call returned 2 real events
  (with addresses), appeared as a job in the dashboard API, and filled the change feed. Connector network tests:
  TEAMEVEREST 2, MYBHARATGOVIN 442, IVOLUNTEERIN 21 events.

## Not verified yet
- **Real AI calls** (no key on the build machine): providers, vision, captcha and recipe learning from a real model.
  Run `uv run pytest -m llm` once a key is in `.env`. Recipe induction was validated on live TeamEverest pages, with the
  legacy parser standing in for the AI.
- **BBMP report/track live**: needs portal credentials. Playwright is mocked in tests.
- **Docker image builds and the Cloud Run deploy**: Docker isn't installed on the build machine, and nothing has been
  deployed. Run `deploy/deploy.sh`.

## Next
- List-page recipes (many items per page from repeated cards): the biggest remaining saving on fetches.
- JS sites whose data needs clicks or login (infinite scroll beyond one screen, POST/GraphQL APIs, authenticated APIs).
- hashtaglocal-backend: send `X-API-Key`, then flip `SCRAPER0_LEGACY_KEYLESS=false`. Optionally consume `/v1/items/changes`.

## v0.2: sites and actions API (2026-11-26)
Contract: [spec/api-v2.md](spec/api-v2.md) (deviations recorded at its end); design: [spec/sites-and-actions.md](spec/sites-and-actions.md).
- **Code:** `api/routes_v2.py` (HTTP), `services/sites.py` (site ids, schemas, seeding, versions, lock, contract),
  `services/site_calls.py` (read: freshness, one refresh per site+action, `from` / online filtering, per-tenant AI
  verdict cache; lookup; idempotent write), `services/site_runs.py` (jobs `site_learn`, `site_refresh`, `site_write`
  and the shared item store). Migration `0002` adds `sites`, `site_actions`, `action_versions`, `site_items`,
  `ai_verdicts`, `write_records`. Startup seeds the five code actions as locked version 1 (idempotent).
- **One refresh in flight:** the read claims `site_actions.refresh_job_id` with a conditional UPDATE in the same
  transaction that inserts the job; concurrent reads join that job. A claim left by a crashed worker is cleared when
  its job is terminal; one that was never dispatched is failed after 15 min.
- **Refresh scrapes everything** (online, past-today; connectors with empty filter data), normalised by the engine's
  postprocess, upserted by `item_key` (link, or `link#hash` when items share a link). `updated_at` moves only when the
  content hash changes (image URL excluded, as in v1). AI runs per read, per tenant, batched 8 per call, with a generic
  prompt built from the caller's `categories` (not hashtaglocal's hardcoded list), and is cached in `ai_verdicts`.
- **Writes:** `write_records` per (tenant, site, action, key). `auth` travels Fernet-encrypted in the job input only.
  SMARTONEBLR REPORT_ISSUE already reports "Submitting complaint" before the click; that marks the submit step.
- **Verified:** `uv run pytest` 154 passed (14 new in `tests/test_v2.py`) on SQLite and on Postgres 14 (`scraper0` user,
  `CONNECTION LIMIT 3`); migration up/down/up and `alembic check` clean on both; `ruff` clean.
- **Not verified:** live learn of a real site through v2, real AI filtering, BBMP writes live (same gaps as v0.1).

## One item store (2026-11-28)
The user's call: one `items` entity, related to sites; every rerun updates the items; no versions, just when they were
last seen and last changed.
- **Model:** `items` (migration `0004`) replaces v1 per-tenant `items` and v2 `site_items`. Site items (site_id +
  action) are shared; private items (`tenant_id` set: uploads/ingest, tenant-defined entities) are per tenant.
  `item_changes` is one global log; a tenant's feed joins it with what the tenant can see.
- **Writers:** `services/items.store_run_items` for v1 jobs (crawl → seed site; connector → its code action's site;
  ingest → private) and `upsert_rows` (shared with `site_runs._store`, which also sets `seen_seq` and the action's
  freshness). v1 events go through the same `normalise` + `event_data` as v2 refreshes, so ids and data match.
- **Migration:** site_items keep their ids; v1 items with an http link become site items (new sites registered), the
  rest private; a collision keeps the most recently seen row and the v2 `seen_seq`; the change feed restarts with one
  "created" per item. Downgrade splits them back.
- **Dashboard:** Items has site filter + sort (last seen default) and Site / Last seen / Last changed / First seen
  columns; Sites → action → Items tab.
- **Verified:** 161 tests on SQLite and Postgres 14 (CONNECTION LIMIT 3); migration up/down/up + `alembic check` clean
  on both, with seeded v1 + v2 rows; ruff, tsc, lint and build clean.



## Images: links only (2026-11-28)

scraper0 briefly kept its own copies of item images (migration 0003, `/v2/media`). Decided against it: storing every
scraped image isn't scraper0's job, and the caller (hashtaglocal) copies images itself. Migration 0005 puts the
original links back and drops the `media` table. For signed links that expire (Airtable's last ~2 h), items carry
`image_expires_at` (`core/signed_urls.py`), and a read rescrapes first when stored links are about to expire, so callers
always get links that still work at the time of the read.


## Onboarding only (v0.3, 2026-11-28)

Decided with the user: no ad-hoc URL crawls and no uploads. Every scrape goes through an onboarded site + locked action
(learned, or a code connector).
- `POST /v1/jobs` → 422 pointing at `/v2/sites`; `/v1/jobs/ingest` is gone. `connector` jobs remain internal (v2 lookups,
  legacy `/api/v1/scrape`). Old crawl/ingest jobs stay as read-only history.
- `POST /v2/sites/{site}/actions/{name}/refresh` (admin): refresh now, ignoring freshness, joining a running refresh.
- Sources are schedules on site actions (`site_id`, `action`). Migration 0006 converts connector sources to their
  code action and URL sources to the site's locked `fetch_events`; others are deleted. It also drops `job_files` and
  deletes private (upload) items with their change rows.
- Dashboard: New job page removed; Jobs = history linked to sites; Sites action panel has "Refresh now" with live
  progress; Sources edits schedules on locked site actions.
