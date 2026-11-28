# Recipes: plans

A **plan** is how scraper0 gets a site's items, stored as data in the action's version
(`action_versions.implementation_snapshot.plans`). Learning a site produces plans; refreshing a locked version only
runs them. Code: `src/scraper0/core/plans.py` (schema and runner) and `core/plan_learn.py` (learning from what a page
fetched).

## Minimum AI

- **AI only in declared steps.** Only `ai` and `captcha` steps may call AI, and each must say `"uses_ai": true`. No
  other step ever receives an AI client. A plan without such steps runs at zero tokens.
- **Cheapest route first.** Learning tries structured data, then heuristics, then AI. An `ai` step only fills fields
  that the non-AI steps left empty, and only for the items that need them. A `captcha` step uses the non-AI solver
  before the AI.
- **Every call is accounted.** Run stats report `ai_calls`, `ai_tokens` and `ai_by_step`, and the job result
  carries them. Calls go through the tenant's AI budget like any other AI use.
- **No browser for list plans.** A locked list plan never opens a browser. Lookups and writes that need one (a
  login, a form) are **action plans**, described at the end of this page.

## Shape

```json
{
  "plan_version": 1,
  "entity": "event",
  "entry": {"direct": "https://site/events", "root": "https://site/",
            "navigate": [{"link": {"text": ["Events", "All events"], "href_contains": ["/events"]}}]},
  "steps": [
    {"id": "list", "type": "request", "method": "POST", "url": "https://api.site/search",
     "headers": {"content-type": "application/json", "origin": "https://site"},
     "body": {"json": {"page": "{page.value}", "limit": 12, "date_gt": "{today}"}},
     "paginate": {"mode": "page", "start": 1, "type": "str", "max_pages": 25,
                  "stop": {"fewer_than": 12, "total_path": "total"}},
     "items": {"json": "records.*._source"},
     "expect": {"min_items": 1},
     "retry": {"attempts": 3, "backoff_s": 1}},
    {"id": "orgs", "type": "lookup", "request": {"url": "https://api.site/orgs"}, "items": {"json": "data"},
     "field": "organisation", "key": "org_id", "id_key": "id", "value": "name"}
  ],
  "output": "list",
  "mapping": {"name": "event_name", "start_time": ["event_from_date", "event_from_time"],
              "image": {"template": "https://cdn.site/events/{image_path}"}},
  "link_template": "https://site/events/{slug}",
  "link_base": "https://site/"
}
```

### Step types

| Type | What it does |
|---|---|
| `request` | One HTTP call (GET or POST; `query`, `headers`, and `body` as `json`, `form` or `text`). With `items` it yields records: a JSON path (`records.*._source` maps over an array), or `links` matching path patterns in HTML. |
| `for_each` | One request per record of an earlier step (`over`), capped by `max`. `extract` reads fields from the HTML: `structured` (JSON-LD and microdata) and `selectors` (CSS rules per path pattern). With `optional: true`, a failed request keeps the record. |
| `lookup` | Fetches a second list and joins it in (`key` on the item → `id_key` on the other list → `value` into `field`). If it fails, only that field stays empty. |
| `any_of` | Ordered `alternatives` (any step type). The first that succeeds wins, and the run records which one. |
| `ai` | **Uses AI.** For records of `over` that are missing `fields`, reads the page with the AI and fills only those fields. |
| `captcha` | **Uses AI.** Non-AI solver first, then the AI. It runs in action plans, next to the browser steps it needs. |

`browser`, `session`, `extract` and `expect` steps belong in action plans; a list plan that contains one is rejected
with a message saying so. `code` stays reserved.

### On any step

| Key | Meaning |
|---|---|
| `paginate` | `offset` (`start`, `step`), `page` (`start`), `cursor` (`cursor_path`), or `next_link` (HTML "next" links). It stops when a page is empty, repeats the previous page, has `fewer_than` items, `total_path` has been reached, or `max_pages` (at most 25) is hit. The current value is `{page.value}`; `type: "str"` keeps it a string. |
| `capture` | Takes values from the response for later templates (`{captures.name}`): `json` (path), `regex` (first group), `header`, `cookie`. |
| `retry` | `{attempts ≤ 5, backoff_s ≤ 30}`. It retries **transient failures only**: no response, 5xx, and 429 (honouring `Retry-After`). The default is 2 attempts with a 1 s backoff. |
| `expect` | Post-checks: `min_items`, `status`, and `coverage: {fields, min_ratio}`. A failed expect is a step failure, so breakage that would otherwise be silent is caught. |
| `on_failure` | `fail` (the default; `skip` for lookups), `skip`, or `{"goto": "<later step>"}`. A step marked `fallback_only` runs only when jumped to, and its items stand in for the step that failed. |
| `when` | `{"captured": name, "op": exists\|missing\|equals\|not_equals\|in, "value"}` or `{"step": id, "outcome": ok\|failed\|skipped}`. If false, the step is skipped. |

### Templates

Templates do plain substitution only: `{a.b}` looks up a dotted path in the context, and nothing is evaluated. The
context holds `seed`, `entry`, `today` (Asia/Kolkata), `from` (the caller's option), `input`, `page`, `captures`, and
`item` (inside `for_each`). A string that is exactly one placeholder keeps the value's type.

### Entry and navigation

Steps that list items use `{entry}`, which is `entry.direct`: the deepest stable URL, such as the list page. If the
direct URL fails or yields no items, the runner follows `entry.navigate` from `root`. Each hop picks the first link
whose text or href matches one of its alternatives. The runner then runs again from the URL it reached, and records
`entry: "navigate"` and `entry_changed_to` in the run stats.

### Run stats

Each run reports (job result → `plan_runs`):

- **Volume:** `requests`, `pages`, `items` and `detail_failures`.
- **AI:** `ai_calls`, `ai_tokens` and `ai_by_step`.
- **Path taken:** `entry`, `entry_changed_to`, `alternatives` (the winner per `any_of`), `retries` (per step),
  `expect_failures` and `outcomes` (`ok`, `failed`, `skipped` or `fallback` per step).

A failed run keeps the stored items and counts toward the action's `consecutive_failures`. After three, the action
is `broken`, and a repair learns a new draft that an admin locks.

## Learning

Learning runs once per seed URL, with AI allowed.

1. **Load the page.** Static pages are fetched. JavaScript pages are rendered, recording every fetch or XHR call
   (any method, any host). Credential headers are dropped from the record, and their values stay in memory only
   while learning.
2. **API sites.**
   1. Pick the captured call whose JSON maps best to the entity, using heuristics first and AI only if they fail
      (at most 3 calls). The call becomes a `request` step.
   2. Replay it without the browser. If the replay fails, add a warm-up request for cookies, or a `capture` step
      for a token that an earlier response contained. If neither fixes it, the site is not learned, and the error
      says why.
   3. Confirm paging parameters by replaying: a new value must give different items, and the same value the same
      items. A date in the body equal to today becomes `{today}`, but only if changing it changes the results.
   4. Image values that are not URLs get a prefix template inferred from the page's `<img>` sources.
   5. Lookups (for example organiser names) come from the page's other GET calls.
3. **Server-rendered sites.** The crawler learns each detail-page layout once (usually one AI call), then reuses it
   for the other pages of that layout. The plan becomes: list page (`next_link` paging) → detail links matching the
   learned patterns → structured data and CSS rules. `navigate` records the home-page link that leads to the list
   page.
4. **Verify before keep.** Every plan runs cold, with no browser and no AI. The version's preview is that run's
   output, so a draft shows exactly what a locked refresh will produce.

## Action plans (lookups and writes)

An **action plan** does one thing on a site for a caller — look up a record, or submit a form — by driving a browser
the way a person would. It is stored in the action's version (`implementation_snapshot.plan`) and run by one generic
runner, so "track complaint" and "report complaint" are onboarded as data, not written as a connector. Code:
`src/scraper0/core/action_plans.py`. Shipped plans live in `src/scraper0/site_plans/<site_id>/<action>.json`.

```json
{
  "plan_version": 1, "kind": "action", "mode": "write",
  "inputs": {"category": {"type": "string", "required": true},
             "media_url": {"type": "string", "format": "url"},
             "latitude": {"type": "string", "coerce": true, "required": true}},
  "auth": {"required": true, "fields": ["username", "password"]},
  "block": ["**polyfill.io**"],
  "steps": [
    {"id": "open", "type": "browser", "do": "goto", "url": "https://site/home"},
    {"id": "session", "type": "session", "key": "{auth.username}",
     "logged_in": {"visible": {"text": "Welcome"}, "unless": {"visible": {"text": "Login/Register"}}},
     "login": [
       {"type": "browser", "do": "click", "target": {"text": "Login/Register"}},
       {"type": "browser", "do": "fill", "target": {"css": "#username"}, "value": "{auth.username}"},
       {"type": "browser", "do": "fill", "target": {"css": "#password"}, "value": "{auth.password}"},
       {"type": "captcha", "uses_ai": true, "attempts": 3,
        "image": {"css": "img.captcha"}, "into": [{"placeholder": "captcha"}, {"css": "input[name='captcha']"}],
        "refresh": {"css": "i.fa-refresh"},
        "submit": [{"type": "browser", "do": "click", "target": {"role": "button", "name": "Sign In"}}],
        "watch": {"url": "**/login.dwr", "fail_unless_contains": "loginid:", "fail_message": "Invalid password"},
        "success": {"visible": {"text": "Welcome"}}}]},
    {"id": "category", "type": "browser", "do": "select", "in": [{"frame_url": "ComplaintRequest"}],
     "target": {"css": "select#typeId"}, "label": "{input.category}"},
    {"id": "photo", "type": "browser", "do": "upload", "when": {"input": "media_url"},
     "target": {"css": "input[type=file]"}, "file": {"url": "{input.media_url}"}},
    {"id": "submit", "type": "browser", "do": "click", "commit": true,
     "target": [{"css": "button:has-text('Submit')"}, {"role": "button", "name": "Submit"}]},
    {"id": "ack", "type": "extract", "target": {"css": "p.ack"},
     "fields": {"complaint_id": {"regex": "Complaint ID\\s*(\\d+)", "required": true}}}
  ],
  "output": {"tracking_id": {"from": "vars.ack.complaint_id", "type": "int", "required": true}},
  "verify": {"action": "track_issue", "input": {"tracking_id": "{output.tracking_id}"}}
}
```

### Top level

| Key | Meaning |
|---|---|
| `mode` | `lookup` (a read: input → output) or `write`. |
| `inputs` | What the caller sends as `input`: per field `type` (`string`, `number`, `integer`, `boolean`), `required`, `default`, `pattern` (full match), `enum`, `format: "url"`, `coerce` (numbers accepted for a string). The input is checked **before anything touches the site**; a bad one is 422 `invalid_input`. The action's `input_schema` and the contract's curl are derived from this. |
| `auth` | The credential fields the caller sends as `auth`, and whether they are required. |
| `block` | URL globs the browser never loads. |
| `output` | Each output field: `from` (a path such as `vars.ack.complaint_id`), `first_of` (several paths), `template`, `value`, or `object` (nested). Options: `type` (`str`, `int`, `float`, `bool`), `required` + `message`, `map` (site wording → your values, case-insensitive) + `unknown_message`, `omit_if_missing`. |
| `verify` | After a write succeeded, run another action of the same site (a locked lookup) to confirm it. |

### Step types

| Type | What it does |
|---|---|
| `browser` | One browser action, `do`: `goto` (`url`), `click`, `fill` (`value`), `select` (`label` or `value`; waits for the option to appear, so lists that fill in asynchronously work), `upload` (`file.url` is downloaded through the SSRF guard with the upload size cap, then attached), `check`, `press` (`key`, with or without a target), `wait_for` (a target, `state: hidden`, or `load_state`), `wait` (`ms`). |
| `session` | Reuse or log in. The tenant's stored browser session for `key` is loaded before the page opens. If `logged_in` is false, the `login` steps run, `logged_in` is checked again, and the session is saved. Sessions are stored encrypted, per tenant and site (`connector_state`), under a hash of the key. |
| `captcha` | **Uses AI** (must declare `"uses_ai": true`). Screenshot `image` → solver (EasyOCR when configured, then the AI) → fill `into` → run `submit` → check `success`. An unreadable or rejected captcha clicks `refresh` and tries again, up to `attempts` (at most 5). `watch` reads the response to the login call: no call means the site rejected the captcha client-side (retry); a body without `fail_unless_contains` means the captcha was right and the credentials were refused (fail with `fail_message`, no retry). |
| `extract` | Reads values into `vars.<id>`: `fields` by `regex` (over the text, or `from: "html"`), `css` (+ `attr`), `json` path, or `text`; or `labels`, a details panel read as label → value. `required` + `message` fail the step in the plan's own words. |
| `expect` | A condition that must hold, with a `message` for when it does not. |
| `any_of` | Ordered `alternatives`, each one step or a list of steps: click the button, else press Enter. The run records which one worked. |

### On any step

| Key | Meaning |
|---|---|
| `target` | Where to act: one locator, or **ordered alternatives** — `label`, `role` + `name`, `text`, `placeholder`, `css`, `test_id` (optional `nth`, `exact`). The first that matches wins, and the run records the index when it was not the first. |
| `in` | Where the target lives: a list of `{"frame_url": "…"}` (an iframe by its URL), `{"frame": "css"}`, `{"css": "…", "has": "…"}` (a container, such as the login modal), applied in order. It can sit on the step or on one target (`{"any": [...], "in": [...]}`). |
| `when` | Run the step only if a condition holds, e.g. `{"input": "media_url"}`. |
| `optional` | A failure skips the step instead of failing the run. |
| `retry` | `{attempts ≤ 3, backoff_s}`. Never allowed on the commit step. |
| `message` | The error text if this step fails. |
| `note` | A progress message shown while the step runs. |
| `timeout_ms`, `settle_ms`, `wait_load` | How long to wait for the target (default 60 s, at most 120 s); a pause after the step; a load state to wait for after it. |
| `commit` | The point of no return (below). |

Conditions (`when`, `expect`, `logged_in`, `success`): `visible` / `hidden` (a target, with `timeout_ms`, and `unless`
— a second condition that answers "no" early, such as the login link showing), `url_contains`, `text_contains`,
`input`, `var`, and `not` / `any` / `all`.

Templates are the same plain substitution as list plans. The context holds `input`, `auth`, `vars` (what `extract`
steps read), `output` (in `verify`) and `today`.

### Writes: the commit step

- A write plan has **exactly one** top-level step with `"commit": true`: the click that submits. A lookup has none.
- **Before it**, any failure means nothing was submitted: 422 `action_failed`, and the same idempotency key may be
  retried. That includes not finding the control to click.
- **Once it has started**, the runner first persists "submit started" and then clicks. Any failure or timeout from
  there on is 409 `outcome_unknown`. The step is never retried, and the call is never re-run automatically.
- **After success**, `verify` runs. The write is already recorded as succeeded; the result adds
  `write.verified: true|false` and what the lookup returned, so a confirmed write can be told from an unconfirmed one.

### Safety

- **Requests:** every request the browser makes passes the same SSRF guard as rendering, plus the plan's `block`
  list. `goto` and upload URLs are checked too.
- **Secrets:** `auth` values are never logged, never put in progress messages or run stats, and are scrubbed from
  error text. Jobs store them encrypted, as before.
- **AI:** only `captcha` steps can reach the AI client, and they try the non-AI solver first. A plan without one
  runs at zero tokens.
- **Limits:** at most 80 steps, 5 captcha attempts, 3 attempts per step, and a deadline for the whole run.

### Run stats

A lookup or write job's result carries `plan_run`:

- **Path taken:** `outcomes` (ok, skipped or failed per step), `alternatives` (the winner per `any_of`), `targets`
  (which target alternative matched, when not the first), `retries` and `failed_step`.
- **Login:** `session` (`reused`, `logged_in`) and `captcha` (attempts and AI calls per captcha step).
- **AI:** `ai_calls`, `ai_tokens` and `ai_by_step`.
- **Write:** `committed`.

### Worked examples: BBMP Sahaya 2.0

Both are translations of the code connector, step for step, and are seeded as **draft** actions next to the locked
code actions on `smartoneblr-com`, so they can be compared before an admin locks them.

- **`track_issue_plan`** (`site_plans/smartoneblr-com/track_issue.json`): open the status page → fill the complaint
  number → click search, else press Enter → wait for the result → read the staff panel as label → value → map
  "Registered" / "Closed" to `OPEN` / `CLOSED`. It needs no login and no AI.
- **`report_issue_plan`** (`report_issue.json`): open the portal → reuse the session or log in through the captcha →
  open Sahaya 2.0 → category → sub-category (waits for the list) → description → photo (when `media_url` is sent) →
  location → **Submit (commit)** → confirm the popup → read the complaint ID from the acknowledgement → verify with
  `track_issue`.

### Not built yet

- **Recording or AI-drafting a plan.** Plans are written by hand (or translated from a connector) and added with the
  API. Repair of a broken step is manual: add a new version with the fixed target and lock it.
- **`request` steps inside action plans** (sites whose forms are plain API calls), and paging in lookups.
- **OTP or other steps that need a person.**

