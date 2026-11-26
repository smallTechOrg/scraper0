# scraper0 frontend

Next.js 15 (App Router) + React 19 + TypeScript + Tailwind CSS 4. It contains the public landing page, the tenant dashboard (`/app`) and the platform-admin dashboard (`/admin`). The HTTP contract it uses is in [`../spec/api.md`](../spec/api.md).

## How it talks to the API

The browser only ever calls this Next.js server. `next.config.ts` rewrites `/v1/*`, `/v2/*` and `/api/*` to `SCRAPER0_API_URL`, so the `s0_session` cookie is first-party and no CORS is needed. All requests go through `lib/api.ts`. That client:

- sends `credentials: "include"`, `X-Requested-With: scraper0` (the CSRF guard), and `X-Tenant: <selected tenant id>`. The selected tenant is stored in `localStorage` under `s0_tenant` and switched from the header.
- turns `{"error": {"code", "message"}}` bodies into thrown `ApiError`s.

`/app` and `/admin` are gated on `GET /v1/auth/me`. A 401 redirects to `/login?next=…`, and `/admin` also requires `is_platform_admin`. Platform admins see every tenant in the switcher, because the API lets them pick any tenant with `X-Tenant`.

Live job progress uses `EventSource` on `/v1/jobs/{id}/events`. If the stream fails, the page falls back to polling `GET /v1/jobs/{id}` every 2 s. While the stream is open, it also polls every 10 s as a safety net. Next's built-in gzip is turned off (`compress: false`) because it buffers proxied SSE. Compress at the CDN or load balancer instead, if you want compression.

## Develop

```bash
asdf install            # nodejs 22.16.0 from .tool-versions
cp .env.example .env.local
npm install
npm run dev             # http://localhost:3000, proxies to SCRAPER0_API_URL
```

Run the Python API next to it, by default on `http://127.0.0.1:5001`. To have the invite links it generates point at this app, set the API's `FRONTEND_URL` to `http://localhost:3000`.

## Checks

```bash
npm run lint
npm run typecheck       # tsc --noEmit
npm run build
```

## Environment

| Variable | Default | Notes |
|---|---|---|
| `SCRAPER0_API_URL` | `http://127.0.0.1:5001` | Base URL of the Python API. **Read at build time**, because the rewrites are compiled into the standalone server. Rebuild the image when it changes. |
| `PORT` | `3000` (dev), `8080` (Docker) | Cloud Run sets this. |

No secrets are used by the frontend.

## Deploy (Cloud Run)

```bash
gcloud builds submit --tag REGION-docker.pkg.dev/PROJECT/REPO/scraper0-frontend \
  --build-arg SCRAPER0_API_URL=https://scraper0-api-xxxxx.a.run.app   # or use a cloudbuild.yaml with docker build --build-arg

gcloud run deploy scraper0-frontend \
  --image REGION-docker.pkg.dev/PROJECT/REPO/scraper0-frontend \
  --region REGION --allow-unauthenticated \
  --timeout 3600            # long-lived SSE streams for job progress
```

The image is a multi-stage build that runs `node server.js` from `.next/standalone` as a non-root user and listens on `$PORT`.

## Layout

```
app/                 routes (landing, login, invite, app/*, admin/*)
components/          ui primitives, modal + confirm dialog, shell, forms, tables, charts
lib/api.ts           typed API client and types that mirror spec/api.md
lib/live-job.ts      SSE + polling hook for job progress
lib/hooks.ts         small useAsync data hook
```
