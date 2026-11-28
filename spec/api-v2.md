# scraper0 API v2: sites and actions (build contract)

The exact contract that the scraper0 implementation, the scraper0 dashboard, hashtaglocal-backend and the hashtaglocal
Ops UI are built against. This is the only way to scrape: every site is onboarded, and every action is learned (or is
a code connector) and locked first. The concepts and rationale are in [sites-and-actions.md](sites-and-actions.md),
and how learned actions run is in [recipes.md](recipes.md). Tenants, jobs history, schedules and items are in
[api.md](api.md).

Auth and errors work as in v1:
- **Auth:** `X-API-Key` for programs; a session plus `X-Requested-With: scraper0` for the dashboard.
- **Errors:** `{"error": {"code", "message"}}`.

## Sites

A site is **shared across tenants**. Its `site_id` is the host without `www.`, with dots replaced by dashes
(`https://www.teameverest.ngo` → `teameverest-ngo`). Identity is the base URL (scheme + host).

```
GET  /v2/sites?q=                          → {"data": [SiteSummary]}
POST /v2/sites      (tenant admin)         {"base_url": "https://karmana.in/anything"}
     → 201 {"data": Site, "existing": false} | 200 {"data": Site, "existing": true}
GET  /v2/sites/{site_id}                   → {"data": Site}                                    | 404 site_not_onboarded

SiteSummary = {site_id, base_url, name, actions: [{name, kind, mode: "list"|"lookup"|"write", status}]}
Site        = SiteSummary + {created_at, created_by_tenant, actions: [Action]}
```

## Actions

```
POST /v2/sites/{site_id}/actions  (tenant admin)
     {"name": "fetch_events", "kind": "read", "entity": "event",
      "seed_urls": ["https://karmana.in"]}          // optional; default [base_url]
     → 201 {"data": Action}                                         | 409 action_exists
GET  /v2/sites/{site_id}/actions/{name}             → {"data": Action}
POST /v2/sites/{site_id}/actions/{name}/learn       (tenant admin)  → 202 {"data": {"job_id", "version": n}}
     Learns from the live site and creates a new **draft** version. The job result holds the preview.
GET  /v2/sites/{site_id}/actions/{name}/versions    → {"data": [Version]}
GET  /v2/sites/{site_id}/actions/{name}/items?q=&sort=last_seen|updated|first_seen&limit=&offset=
     → {"data": [Item], "total"}   Everything stored for this action (Item as in v1 /v1/items), for the dashboard.
POST /v2/sites/{site_id}/actions/{name}/versions/{n}/lock   (tenant admin)
     → {"data": Contract}        Exactly one locked version per action; the previously locked one becomes "retired".
POST /v2/sites/{site_id}/actions/{name}/repair      (tenant admin)  → 202 {"data": {"job_id", "version": n}}
     Same as learn, but for a broken action. The new draft must be locked to take effect.
POST /v2/sites/{site_id}/actions/{name}/refresh     (tenant admin)  → 202 {"data": {"job_id", "joined": bool, "visible": bool}}
     Refresh the locked version now, ignoring freshness (the dashboard's "Refresh now"; schedules use it too). Joins a
     refresh already running for this site + action (`joined`). `visible`: the job belongs to your tenant, so
     GET /v1/jobs/{id} shows it. 404 site_not_onboarded | 404 action_not_available | 422 (not a list action).
GET  /v2/sites/{site_id}/actions/{name}/contract    → {"data": Contract}                     | 404 action_not_available

Action   = {site_id, name, kind: "read"|"write", entity, implementation: "learned"|"code",
            seed_urls, status: "draft"|"locked"|"broken", locked_version: n|null,
            input_schema, output_schema, last_success_at, last_failure_at, consecutive_failures}
Version  = {version, status: "draft"|"locked"|"retired"|"broken", created_at, locked_at, locked_by,
            preview: {items: [...first 20], count, checked_at} | null, implementation_snapshot: {...}}
Contract = {site_id, action, kind, version, input_schema, output_schema, endpoint, curl}
```

- **Learned read actions:** a version's `implementation_snapshot` holds its plans,
  `{"implementation": "learned", "plans": [{"seed", "plan"}], "learn": {...}, "check_run": [RunStats]}`
  ([recipes.md](recipes.md)). A locked version runs only its frozen plans: no browser, and no AI unless a plan step
  declares it. The preview is the output of a cold run of those plans. Learned actions are reads of the built-in
  `event` entity; anything else returns 422 `invalid_input`.
- **Code actions** come from connectors and are seeded at startup as **locked version 1** (code is reviewed and tested):

| site_id | base_url | action | kind | connector → action |
|---|---|---|---|---|
| `teameverest-ngo` | https://www.teameverest.ngo | `fetch_events` | read | TEAMEVEREST → FETCH_EVENTS |
| `mybharat-gov-in` | https://mybharat.gov.in | `fetch_events` | read | MYBHARATGOVIN → FETCH_EVENTS |
| `ivolunteer-in` | https://www.ivolunteer.in | `fetch_events` | read | IVOLUNTEERIN → FETCH_EVENTS |
| `smartoneblr-com` | https://www.smartoneblr.com | `track_issue` | read (lookup) | SMARTONEBLR → TRACK_ISSUE |
| `smartoneblr-com` | https://www.smartoneblr.com | `report_issue` | write | SMARTONEBLR → REPORT_ISSUE |

- **Broken:** after `3` consecutive failed runs (an error, or zero valid items where the last run had some), the action is
  `broken`, and its locked version is still kept. The dashboard shows it, and a repair creates a draft.

## Read: list actions (items)

```
POST /v2/sites/{site_id}/actions/{name}/read
{
  "options": {
    "event_filter": "in-person civic volunteering…",   // optional: AI relevance filter (text)
    "category_filter": "environment, civic…",          // optional: extra context for the filter
    "categories": ["CLEANLINESS_DRIVE", "BEACH_CLEANUP", "…", "OTHER"],   // optional: caller's category list
    "include_online": false,                            // default false
    "from": "2026-11-26"                                // default: today (Asia/Kolkata); items starting earlier are dropped
  },
  "freshness_hours": 48,     // default 48; refresh only when the last successful scrape is older
  "timeout_s": 60            // default 60, max 300; how long to wait for a refresh before answering
}
→ 200 {
  "data": {
    "site_id", "action", "version",
    "items": [ {"id": "itm_<24 hex>", "updated_at": "…", "data": EventData} ],
    "complete": true|false,              // false: a refresh was still running at the timeout
    "scraped_at": "…"|null,              // when the returned data was scraped
    "refresh_job_id": "…"|null,
    "health": "ok"|"broken"              // broken: these are stored items, and refreshes are failing
  }
}
errors: 404 site_not_onboarded | 404 action_not_available (no locked version) | 422 invalid_input
```

`EventData` = `{name, start_time, end_time, address, link, organisation, image, image_expires_at, category, description,
latitude, longitude, is_online, price, source_url}`, where times are ISO-8601 UTC (`2026-03-14T04:30:00.000Z`). `category` is one of the
caller's `categories`, or `null` when none were sent.

- **Shared store:** items are stored per (site, action), not per tenant, in the one `items` table (v1 `/v1/items`
  lists the same rows). Every tenant reading a site sees the same items. A read returns the items seen by the last
  successful refresh only. Item `id` = `itm_` + the first 24 hex characters of sha256(site_id | action | item_key), where `item_key` is the canonical link,
  or link + `#` + identity hash when several items share a link. It is stable for the life of the item. `updated_at`
  changes only when the item's content changes.
- **Filtering per request:** `from`, `include_online`, then the AI filter and categories. AI verdicts are cached in
  `(tenant, item id, item content hash, options hash)`, so repeated reads cost no AI. Items the filter rejects are
  left out. If AI is unavailable, nothing is filtered out, `category` is `null`, and the response carries
  `"ai_error": "<message>"`.
- **Images are links, not copies.** scraper0 stores the site's image URL; copying the image is the caller's job.
  `image_expires_at` is set when the link is signed and carries its expiry (Airtable, S3, Google Cloud Storage,
  CloudFront, Azure SAS), else `null`: copy those images straight away. A read whose stored items' image links expire
  within 10 minutes rescrapes first (within `timeout_s`), even inside `freshness_hours`, so the links it returns still
  work. It won't rescrape again within 10 minutes of the last scrape.
- **Refresh:** at most one refresh runs per (site, action) at a time. A concurrent read joins the running one.
- **Contract test:** `curl` in the Contract runs this call with the default options.

## Read: lookup actions (input → output)

```
POST /v2/sites/smartoneblr-com/actions/track_issue/read
{"input": {"tracking_id": "21025835"}, "auth": {"username": "…", "password": "…"}, "timeout_s": 120}
→ 200 {"data": {"site_id", "action", "version", "output": {"status": "OPEN"|"CLOSED", "meta_data": {...}}}}
→ 202 {"data": {"job_id"}}                       // timed out; GET /v1/jobs/{id} for the result
→ 422 {"error": {"code": "action_failed", "message": "…"}}
```

## Write actions (idempotent)

```
POST /v2/sites/smartoneblr-com/actions/report_issue/write
Idempotency-Key: hashtaglocal-issue-1234          // optional; default = sha256(tenant|site|action|canonical input)
{"input": {category, sub_category, description, media_url, latitude, longitude},
 "auth": {"username", "password"}, "force": false, "timeout_s": 300}
→ 200 {"data": {"status": "succeeded", "output": {"tracking_id": 21025835}, "idempotency_key", "job_id", "replayed": bool}}
→ 202 {"data": {"status": "running", "idempotency_key", "job_id"}}
→ 409 {"error": {"code": "outcome_unknown", "message", "job_id"}}  // may have been submitted; a person checks; retry needs force or a new key
→ 422 {"error": {"code": "action_failed", "message"}}             // definitely not submitted; the same key may be retried
```

- **Sticky outcomes:** for the same (tenant, site, action, key), `succeeded` and `running` return the stored outcome
  (`replayed: true`), and `outcome_unknown` returns 409 again. Only a definitive `action_failed` allows a re-run with the same key.
- **Explicit override:** `force: true`, or a new key, always runs again.
- **Unknown outcome:** a write that times out, or crashes after its submit step began, is `outcome_unknown`. It is never retried
  automatically.
- **Credentials:** `auth` is never stored in clear (Fernet, as v1 jobs) and never echoed back.

## Errors (v2 codes)

`site_not_onboarded`, `action_not_available`, `action_exists`, `invalid_input`, `action_failed`, `outcome_unknown`,
`forbidden`, `unauthenticated`.

## Behaviour details

- **writes and timeouts.** The first call that times out returns 409 `outcome_unknown` (not 202).
  202 `running` is returned to a call with the same key while the first one is still running; it also carries
  `"replayed": true`. If the timed-out job later finishes, its record takes the real outcome (`succeeded` with output,
  or `failed` when it failed before the submit step), so a later replay returns that. Write `timeout_s` max is 600.
- **"submit step began"** is signalled by the connector through a progress event (`submitting…`, or
  data `{"submit_started": true}`), persisted before the submit click. Failure or crash after it → `outcome_unknown`;
  before it → `failed` (422 `action_failed`, same key may re-run).
- **refresh jobs belong to the tenant whose read started them.** Another tenant that joins gets the same
  `refresh_job_id` but `GET /v1/jobs/{id}` returns 404 for it; just call read again.
- **what the store returns.** A read returns the items seen in the *last successful* scrape (items that
  disappeared from the site are not returned). The store keeps items whose end (or start) is yesterday (IST) or later.
  A run with 0 items after a run that had items counts as a failure and leaves the stored items untouched.
- **broken actions** keep serving stored items with `health: "broken"` and retry their refresh at most
  once an hour; a successful refresh, or locking a repaired version, makes them `locked` again.
- **learn / repair.** Both create a new draft version immediately (returned `version`), filled by the
  job, and always look at the live site again. A failed learn (or a learned plan that produces no items when run
  without AI) leaves that version `broken` with `preview.error`. On a code list action, learn or repair is a
  verification run of the connector (draft code version). Lookup and write actions can't be learned: 422.
- **lock.** Unknown version → 404 `not_found`; a version still being learned or `broken` → 422
  `invalid_input`. Locking resets freshness, so the next read refreshes with the new version.
- **categories.** If the model answers a category not in the caller's list, `OTHER` is used when the
  list has it, else the last category. Verdicts are only cached when the AI answered.
- **Extra fields:** `Version` also has `job_id`; `Site.created_by_tenant` is the tenant slug (null for seeded sites);
  versions are listed newest first; lookups run as a `connector` job (visible in `GET /v1/jobs/{id}`). Only list
  refreshes count towards `consecutive_failures` / broken (lookups and writes fail for caller reasons too).

## Plan actions (lookups and writes as data)

An action can run from an **action plan** ([recipes.md](recipes.md), "Action plans") instead of a code connector.
`Action.implementation` is then `"plan"`.

```
POST /v2/sites/{site_id}/actions  (tenant admin)
     {"name": "track_issue", "plan": {…action plan…}, "entity": "issue_status"}     // kind and mode come from the plan
     → 201 {"data": Action}      draft; version 1 (draft) holds the plan           | 422 invalid_input (plan: …)
POST /v2/sites/{site_id}/actions/{name}/versions  (tenant admin)   {"plan": {…}}
     → 201 {"data": Version}     a new draft version (an edited or repaired plan)   | 422 if the mode changes
POST …/versions/{n}/lock        as for any action; the action's input and output schema follow the locked plan
```

- **Calls are unchanged:** `…/read` with `{"input", "auth"}` for a lookup, `…/write` with `Idempotency-Key` for a
  write, and the same responses and error codes.
- **Input is checked against the plan first.** A bad input is 422 `invalid_input`, and nothing reaches the site.
- **Writes:** a failure before the plan's commit step is 422 `action_failed`; from the commit step on it is 409
  `outcome_unknown`.
- **Verification:** when the plan has `verify`, the write job's result has `write.verified` and `write.verify`
  (`GET /v1/jobs/{job_id}`). The 200 response itself is unchanged.
- **Jobs:** a plan lookup runs as a `site_lookup` job; its result has `data` and `plan_run`.
- **Seeded drafts:** `smartoneblr-com` has `track_issue_plan` and `report_issue_plan` as draft plan actions next to
  the locked code actions. They are not callable until an admin locks them.

