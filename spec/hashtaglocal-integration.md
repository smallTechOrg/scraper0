# hashtaglocal ↔ scraper0 v2 integration (build contract)

This is how hashtaglocal-backend and the hashtaglocal Ops UI (hashtaglocal-frontend-web, `/ops`) use the scraper0 v2 API
([api-v2.md](api-v2.md)). The JSON on hashtaglocal's own API is **snake_case** (the backend's Jackson uses SNAKE_CASE globally).

## hashtaglocal-backend

### Settings
| Property | Env | Default |
|---|---|---|
| `scraper0.base-url` | `SCRAPER0_BASE_URL` | (empty = event ingestion and portal issues disabled, with a clear log line) |
| `scraper0.api-key` | `SCRAPER0_API_KEY` | (empty = no `X-API-Key` header; works only while scraper0 has legacy keyless on) |
| `scraper0.read-timeout-s` | `SCRAPER0_READ_TIMEOUT_S` | `60` (sent as `timeout_s`; the HTTP read timeout is this + 30 s) |
| `scraper0.freshness-hours` | `SCRAPER0_FRESHNESS_HOURS` | `48` (the default for new event sources) |
| `events.scrape.cron` | `EVENTS_SCRAPE_CRON` | `0 */30 * * * *` (in-process; on Cloud Run, Cloud Scheduler calls `POST /jobs/event-ingestion`) |

### Event sources (table `event_sources`)
`id, name, site_id, action (default 'fetch_events'), event_filter, category_filter, include_online (default false),
freshness_hours (default 48), enabled (default true), last_run_at, last_status ('ok'|'partial'|'error'|null),
last_error, last_item_count, last_imported_count, created_at, updated_at`. `(site_id, action)` is unique.

A Flyway migration seeds the three current portals, using the filter texts that are currently hardcoded in `EventPortalModel`:
`teameverest-ngo`, `mybharat-gov-in`, `ivolunteer-in` (all `fetch_events`, enabled). The filter strings are then
removed from the enum. The enum values stay, because existing rows use them.

### Admin API (ROLE_ADMIN, under `/admin/**`)
```
GET    /admin/event-sources                      → [EventSource]
POST   /admin/event-sources                      EventSourceInput → 201 EventSource      | 409 duplicate site_id+action
PUT    /admin/event-sources/{id}                 EventSourceInput → EventSource
DELETE /admin/event-sources/{id}                 → 204
POST   /admin/event-sources/{id}/test            → TestResult        (reads now, imports nothing)
POST   /admin/event-sources/run                  → 202 {"started": true}   (runs ingestion now, async)
GET    /admin/event-sources/scraper0-sites?q=    → [{site_id, base_url, name, actions: [{name, kind, status}]}]
                                                    (proxied from GET /v2/sites; only sites with a locked read action are usable)

EventSourceInput = {name, site_id, action, event_filter, category_filter, include_online, freshness_hours, enabled}
EventSource      = EventSourceInput + {id, last_run_at, last_status, last_error, last_item_count, last_imported_count,
                                       created_at, updated_at}
TestResult       = {item_count, complete, scraped_at, health, ai_error,
                    items: [{id, name, start_time, end_time, address, link, image, category, is_online}]  // first 20}
```
Errors from scraper0 come back as 502, with `{"message": "<scraper0 code>: <message>"}`. A `site_not_onboarded` or
`action_not_available` error is shown to the admin as "Onboard this site in scraper0 first".

### Event ingestion (`POST /jobs/event-ingestion` and the in-process cron)
For each enabled source:
1. Call `POST /v2/sites/{site_id}/actions/{action}/read` with:
   - `options`: `{event_filter, category_filter, categories: EventTypeModel names, include_online, from: today}`
   - `freshness_hours`
   - `timeout_s`
2. Import each item:
   - **Dedupe** on `events.external_id` (= scraper0 item `id`; new nullable, unique column), and keep the existing
     `name + start_time` check for older rows.
   - **Keep the current rules:** an event needs a name, a start time and an image (copied to GCS), and it goes to the PENDING approval queue.
   - **Portal:** the enum value for the three known sites (`teameverest-ngo`→TEAMEVEREST, `mybharat-gov-in`→MYBHARATGOVIN,
     `ivolunteer-in`→IVOLUNTEERIN), otherwise a new enum value `WEB`. The new column `events.source_site_id` stores the
     `site_id`.
   - **Type** = `category` mapped to `EventTypeModel`, with `OTHER` as the fallback.
   - **Insert only:** an item whose `id` already exists is skipped. Updates are left for later (`updated_at` is available).
3. Record `last_*` on the source. `complete: false` → `partial`. An error → `error`, and the run continues with the next source.
4. Geocoding runs afterwards, as today.

### Portal issues (BBMP)
- **Report:** `POST /v2/sites/smartoneblr-com/actions/report_issue/write`, with `Idempotency-Key: hashtaglocal-issue-{issueId}`.
  - 200 → the tracking id, as today.
  - 202 → "still running, try again shortly"; retrying with the same issue returns the same job.
  - 409 `outcome_unknown` → "may have been filed; check the portal before retrying".
  - 422 → the error message, as today.
- **Track:** `POST /v2/sites/smartoneblr-com/actions/track_issue/read` with `{"input": {"tracking_id"}, "auth"}`.
- **No change for the Ops UI:** report and track keep the same endpoints and response shapes.

The legacy `/api/v1/scrape` clients (`ScrapeApiClient`, and the v1 parts of `PortalIssueScrapeClient` and
`PortalComplaintReportingService`) are replaced.

## hashtaglocal-frontend-web: Ops → Event sources (`/ops/event-sources`)

- **List:** name, site, enabled toggle, last run (time, status badge, items and imported counts, error on hover), and Edit / Test / Delete.
- **Create / edit form:**
  - **Site:** a searchable picker from `scraper0-sites`, showing only sites with a locked read action, with the base URL as a hint.
  - **Action:** defaults to `fetch_events`.
  - **Also:** name, event filter (textarea), category filter, "include online events" switch, freshness in hours, enabled.
- **Test:** shows the TestResult in a dialog: a table of the first 20 items (image thumbnail, name, start, address,
  category, online). If `ai_error` is set it shows a notice, and if `complete` is false it shows "partial — the site is still refreshing".
- **"Run ingestion now"** button → `POST /admin/event-sources/run`.
- **Empty state:** explains that sites are onboarded in scraper0 first, with a link to the scraper0 dashboard (the URL comes from env
  `NEXT_PUBLIC_SCRAPER0_DASHBOARD_URL`).
