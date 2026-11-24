#!/usr/bin/env bash
# Deploy scraper0 (API + frontend) to Cloud Run. Idempotent: safe to re-run.
#
#   PROJECT=ai-agent-boilerplate0 REGION=us-central1 SQL_INSTANCE=<cloud-sql-instance-name> ./deploy/deploy.sh
#
# First run only (see docs/deploy.md): create the `scraper0` database + user on the existing Cloud SQL
# instance, and the secrets scraper0-db-url, scraper0-secret-key, scraper0-internal-token,
# scraper0-admin-password, scraper0-groq-key.
set -euo pipefail

: "${PROJECT:?set PROJECT}"
: "${REGION:=us-central1}"
: "${SQL_INSTANCE:?set SQL_INSTANCE (Cloud SQL instance name of the hashtaglocal Postgres)}"
: "${VPC_NETWORK:=default}"            # Direct VPC egress to reach Cloud SQL private IP
: "${VPC_SUBNET:=default}"
: "${API_SERVICE:=scraper0-api}"
: "${WEB_SERVICE:=scraper0-web}"
: "${QUEUE:=scraper0-jobs}"
: "${ADMIN_EMAIL:?set ADMIN_EMAIL (first platform admin)}"
: "${ALLOWED_BACKEND_URLS:=}"           # e.g. https://staging.api.smalltech.in,https://api.smalltech.in
: "${LEGACY_KEYLESS:=true}"             # true until hashtaglocal-backend sends X-API-Key
REPO="${REGION}-docker.pkg.dev/${PROJECT}/scraper0"
TAG="$(git rev-parse --short HEAD)"
SA="scraper0-run@${PROJECT}.iam.gserviceaccount.com"
gc() { gcloud --project "$PROJECT" "$@"; }

echo "==> APIs, registry, service account"
gc services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  cloudtasks.googleapis.com cloudscheduler.googleapis.com secretmanager.googleapis.com sqladmin.googleapis.com >/dev/null
gc artifacts repositories describe scraper0 --location "$REGION" >/dev/null 2>&1 || \
  gc artifacts repositories create scraper0 --repository-format docker --location "$REGION"
gc iam service-accounts describe "$SA" >/dev/null 2>&1 || \
  gc iam service-accounts create scraper0-run --display-name "scraper0 Cloud Run"
for role in roles/secretmanager.secretAccessor roles/cloudsql.client roles/cloudtasks.enqueuer roles/iam.serviceAccountUser; do
  gc projects add-iam-policy-binding "$PROJECT" --member "serviceAccount:$SA" --role "$role" --condition=None >/dev/null
done

echo "==> Cloud Tasks queue"
gc tasks queues describe "$QUEUE" --location "$REGION" >/dev/null 2>&1 || \
  gc tasks queues create "$QUEUE" --location "$REGION" --max-concurrent-dispatches 2 --max-attempts 3 --min-backoff 30s

echo "==> build API image"
gc builds submit --tag "$REPO/api:$TAG" .

echo "==> deploy API"
# max-instances=1 + pool 2 (+1 overflow) keeps scraper0 at ≤ 3 connections on the shared Postgres instance.
# 60-minute request timeout: long jobs run inside a request (wait=true, or Cloud Tasks → /internal/jobs/{id}/run).
gc run deploy "$API_SERVICE" --image "$REPO/api:$TAG" --region "$REGION" --service-account "$SA" \
  --allow-unauthenticated --port 8080 --timeout 3600 --cpu 1 --memory 2Gi --concurrency 20 \
  --min-instances 0 --max-instances 1 \
  --network "$VPC_NETWORK" --subnet "$VPC_SUBNET" --vpc-egress private-ranges-only \
  --set-env-vars "^@^SCRAPER0_ENV=production@SCRAPER0_JOB_MODE=cloud_tasks@SCRAPER0_DB_POOL_SIZE=2@SCRAPER0_DB_MAX_OVERFLOW=1@SCRAPER0_COOKIE_SECURE=true@SCRAPER0_LEGACY_KEYLESS=${LEGACY_KEYLESS}@SCRAPER0_BOOTSTRAP_ADMIN_EMAIL=${ADMIN_EMAIL}@SCRAPER0_CLOUD_TASKS_QUEUE=projects/${PROJECT}/locations/${REGION}/queues/${QUEUE}@SCRAPER0_ALLOWED_BACKEND_URLS=${ALLOWED_BACKEND_URLS}" \
  --set-secrets "SCRAPER0_DATABASE_URL=scraper0-db-url:latest,SCRAPER0_SECRET_KEY=scraper0-secret-key:latest,SCRAPER0_INTERNAL_TOKEN=scraper0-internal-token:latest,SCRAPER0_BOOTSTRAP_ADMIN_PASSWORD=scraper0-admin-password:latest,SCRAPER0_BOOTSTRAP_GROQ_API_KEY=scraper0-groq-key:latest"
API_URL="$(gc run services describe "$API_SERVICE" --region "$REGION" --format 'value(status.url)')"

echo "==> build frontend image (the API URL is compiled into the Next.js rewrites)"
gc builds submit frontend --config deploy/cloudbuild-web.yaml \
  --substitutions "_IMAGE=$REPO/web:$TAG,_API_URL=$API_URL"

echo "==> deploy frontend"
gc run deploy "$WEB_SERVICE" --image "$REPO/web:$TAG" --region "$REGION" --allow-unauthenticated \
  --port 8080 --cpu 1 --memory 512Mi --min-instances 0 --max-instances 3
WEB_URL="$(gc run services describe "$WEB_SERVICE" --region "$REGION" --format 'value(status.url)')"

echo "==> wire URLs back into the API"
gc run services update "$API_SERVICE" --region "$REGION" \
  --update-env-vars "^@^SCRAPER0_PUBLIC_BASE_URL=${API_URL}@SCRAPER0_FRONTEND_URL=${WEB_URL}@SCRAPER0_CORS_ORIGINS=[\"${WEB_URL}\"]"

echo "==> Cloud Scheduler tick (every minute)"
TOKEN="$(gc secrets versions access latest --secret scraper0-internal-token)"
if gc scheduler jobs describe scraper0-tick --location "$REGION" >/dev/null 2>&1; then
  gc scheduler jobs update http scraper0-tick --location "$REGION" --schedule "* * * * *" \
    --uri "${API_URL}/internal/tick" --http-method POST --update-headers "Authorization=Bearer ${TOKEN}"
else
  gc scheduler jobs create http scraper0-tick --location "$REGION" --schedule "* * * * *" \
    --uri "${API_URL}/internal/tick" --http-method POST --headers "Authorization=Bearer ${TOKEN}" \
    --attempt-deadline 300s
fi

echo
echo "API: $API_URL   (health: $API_URL/api/health, docs: $API_URL/api/docs)"
echo "Web: $WEB_URL"
echo "hashtaglocal-backend: set EVENTS_SCRAPE_URL / PORTALISSUE_SCRAPE_URL / PORTALISSUE_REPORT_SCRAPE_URL to ${API_URL}/api/v1/scrape"
