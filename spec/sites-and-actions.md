# Sites and actions: the scraper0 contract (design)

Onboarding is the only way in. Schedules (`/v1/sources`) refresh locked site actions, and admins can "Refresh now"
any locked list action. How a learned action runs is in [recipes.md](recipes.md).

## The idea in one paragraph

A caller never sends scraper0 "go scrape this URL somehow". An admin first **onboards a site** and **locks
an action** on it, such as "fetch events", "track complaint" or "report complaint". The locked action is the contract: a
`site_id`, an action name, an input schema, an output schema, and a curl command to test it. Callers like hashtaglocal call
only locked actions. Onboarding has some friction, but every call hits something verified. When a site
changes and the action breaks, it is repaired (automatically if possible). Callers keep using the same
`site_id` + action and start working again without changes.

## Concepts

| Concept | Meaning |
|---|---|
| **Site** | A website, identified by its base URL (`https://karmana.in`) and a stable `site_id` (`karmana-in`). **Shared across tenants**: what scraper0 learns about a site serves everyone. |
| **Action** | Something scraper0 can do on a site. Each action is declared as a **read** (fetch events, track status) or a **write** (report a complaint). It has an input schema, an output schema and an implementation. |
| **Implementation** | How the action runs: *learned* (selector or JSON-API recipes held as data in the DB, which are repairable and can be re-learned) or *code* (a connector in this repo, such as BBMP login/report). For now, writes are code only. |
| **Version** | Each implementation change creates a new version. Only a **locked** version serves callers. A repair produces a new version, which is locked again after a test. |
| **Subscription** | A tenant's use of a site + action. Holds the tenant's options: filters, categories, freshness, online on/off. It does not affect what is scraped, only what is returned. |

## Onboarding (admin, in the dashboard)

1. **Enter the base URL.** If the site already exists (onboarded by another tenant or the platform admin), the
   admin sees it and its existing actions. They can **reuse** one, or **define their own**.
2. **Define the action.** For reads: "fetch events", or a custom entity. scraper0 learns it: it renders the page,
   finds structured data or the JSON API, learns the recipe, and shows the extracted items. The admin checks the results and can
   adjust or re-learn.
3. **Lock it.** Locking freezes that version and shows the contract: `site_id`, action, input and output schemas, and a
   ready-to-run **curl** with the tenant's API key.
4. **Subscribe.** The tenant's options for that action, for example #local's categories and filter text.

Calls to a site or action that is not onboarded and locked return an error, not a best effort:

| Error code | When |
|---|---|
| `site_not_onboarded` | No site with that `site_id`, or the base URL is unknown |
| `action_not_available` | The site exists but has no locked action with that name |
| `action_broken` | The locked version is failing verification; a repair is pending |

## Reads

```
POST /v2/sites/{site_id}/actions/{action}/read
{
  "options":   {"event_filter": "...", "categories": ["CLEANLINESS_DRIVE", ...], "include_online": false,
                "from": "2026-11-26"},              // tenant options: these override the subscription defaults
  "freshness_hours": 48,                            // re-scrape only if the last scrape is older than this
  "timeout_s": 60                                   // how long the caller is willing to wait
}
→ 200 {
  "items": [{"id": "itm_…", "updated_at": "…", "data": {...}}],   // stable ids → the caller dedupes on id
  "complete": true | false,          // false = a refresh was still running at the timeout
  "scraped_at": "…",                  // age of the data returned
  "refresh_job_id": "job_…" | null    // set when a refresh was started; poll it, or just call again later
}
```

- **Freshness:** if the site was scraped within `freshness_hours`, the stored items are returned at once with no
  scraping. Otherwise a refresh starts. If it finishes within `timeout_s`, fresh items are returned. If not, the
  stored items are returned with `complete: false`, and anything the refresh finds is picked up by the next call.
- **What is returned:** every item dated today or later (`from`), passed through the tenant's options. scraper0 does not
  apply business rules such as city. That is the caller's job. Online events are scraped and returned when
  `include_online` is true. `is_online` is always reported.
- **Categories:** the caller sends its own category list, and scraper0 classifies into it. AI verdicts (filter and
  category) are cached per item content and per tenant option set, so a repeat call costs no AI.
- **Idempotency:** item `id`s are stable across scrapes. The same event always has the same id, and an
  `updated_at` change means its details changed. The caller decides what to do with updates.

## Writes

```
POST /v2/sites/{site_id}/actions/{action}/write
Idempotency-Key: <caller key>      // optional; default = hash(site, action, normalised input, tenant)
{"input": {...}, "auth": {...}, "force": false, "timeout_s": 300}
→ 200 {"status": "succeeded", "output": {...}}
→ 202 {"status": "running", "job_id": "…"}                 // still in progress: same key → same job
→ 409 {"status": "outcome_unknown", "job_id": "…"}         // may have been submitted; needs a person or an explicit retry
```

- **Idempotent by default:** the same key returns the first result or the running job. It never submits twice.
- **Explicit override only:** a new `Idempotency-Key`, or `force: true`, to deliberately submit again.
- **Never retried blindly:** if a write times out after it may already have been submitted, it becomes `outcome_unknown`.
- **Code only, for now:** write implementations are code connectors (BBMP report/track). Learned write
  workflows (login, multi-step forms) are deferred. See the open questions below.

## Breakage and repair

1. Every run verifies its output (required fields present and valid). Repeated failures mark the locked version
   `broken`. The tenant admins subscribed to it, and the platform admin, see it in the dashboard.
2. **Auto-repair** (learned implementations): re-learn from the live site and show the new results. The admin locks
   the new version, and callers keep working with no change on their side.
3. **Code fix** (last resort): when re-learning can't fix it, the scraper0 team changes the code and deploys.
   The admin runs repair again and locks it.

## What hashtaglocal changes

1. **Sites in #local's admin, not a Java enum.** Each row holds a `site_id`, an action (`fetch_events`), and #local's options
   (filter text, its own category list, `include_online: false`). Adding a site means onboarding it in scraper0, then adding the
   row in #local. No deploy is needed.
2. **Event ingestion every 30 minutes** (Cloud Scheduler → `POST /jobs/event-ingestion`, which today runs daily). For each
   site, a read with `freshness_hours` (e.g. 48) and `timeout_s` (e.g. 60). This is cheap: most calls return stored items.
3. **Import dedupes on the scraper0 item `id`** (stored on the event), instead of name + start time. #local decides
   what to do when `updated_at` changes.
4. **#local's own business rules stay in #local:** city, approval, image required.
5. **Complaint report/track become writes and reads** on the BBMP site, with an `Idempotency-Key` derived from #local's
   issue id, so a retried report never files twice.
6. **`EVENTS_SCRAPE_URL` and the portal URLs point at scraper0.** Production currently points at the *staging* web-scraper.

## Open questions (deferred)

- **Learned write workflows:** multi-step writes (login → form → submit) will be plans with browser, session, captcha,
  commit and verify steps (reserved in [recipes.md](recipes.md)). Until those exist, writes are code only.
- **Knowledge durability:** learned implementations live in the database. If the database is lost, that knowledge is lost. One option: scraper0
  exports every locked version to a separate git repo it maintains, for history, review and disaster recovery.
- **Defaults:** `freshness_hours` (proposed 48), `timeout_s` (proposed 60), and the `from` date default (today, in the site's
  timezone).
