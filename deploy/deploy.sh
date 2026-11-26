#!/usr/bin/env bash
# Deploy scraper0 (API + dashboard) to Cloud Run. Idempotent: safe to re-run.
#
#   STAGE=staging ADMIN_EMAIL=you@example.com ./deploy/deploy.sh
#
# Creates (if missing) everything for the stage, all named scraper0-<stage>-*:
#   - database + user on the shared Cloud SQL instance (private IP, Direct VPC egress)
#   - secrets (generated, never printed): db url, Fernet key, internal token, admin password
#   - a service account that can read only those secrets and enqueue to its own queue
#   - Cloud Tasks queue, API + web Cloud Run services, Cloud Scheduler tick
set -euo pipefail

: "${PROJECT:=ai-agent-boilerplate0}"
: "${REGION:=us-central1}"
: "${STAGE:=staging}"
: "${SQL_INSTANCE:=ai-agent-db}"
: "${SQL_HOST:=10.110.112.7}"            # private IP of SQL_INSTANCE
: "${VPC_NETWORK:=default}"
: "${VPC_SUBNET:=default}"
: "${ADMIN_EMAIL:?set ADMIN_EMAIL (first platform admin login)}"
: "${ALLOWED_BACKEND_URLS:=}"           # hashtaglocal backends allowed in X-Backend-Url
: "${LEGACY_KEYLESS:=true}"             # true until hashtaglocal-backend sends X-API-Key

NAME="scraper0-${STAGE}"
DB_NAME="scraper0_${STAGE//-/_}"
DB_USER="$DB_NAME"
API_SERVICE="${NAME}-api"
WEB_SERVICE="${NAME}-web"
QUEUE="${NAME}-jobs"
SA_ID="${NAME}-run"
SA="${SA_ID}@${PROJECT}.iam.gserviceaccount.com"
REPO="${REGION}-docker.pkg.dev/${PROJECT}/scraper0"
TAG="$(git rev-parse --short HEAD)$(git diff --quiet HEAD -- src frontend migrations pyproject.toml uv.lock Dockerfile || echo -dirty)-$(date +%m%d%H%M)"
gc() { gcloud --project "$PROJECT" --quiet "$@"; }
secret_name() { echo "${NAME}-$1"; }

echo "==> [$NAME] APIs"
gc services enable run.googleapis.com cloudbuild.googleapis.com artifactregistry.googleapis.com \
  cloudtasks.googleapis.com cloudscheduler.googleapis.com secretmanager.googleapis.com sqladmin.googleapis.com >/dev/null

echo "==> artifact registry"
gc artifacts repositories describe scraper0 --location "$REGION" >/dev/null 2>&1 || \
  gc artifacts repositories create scraper0 --repository-format docker --location "$REGION"

echo "==> service account"
gc iam service-accounts describe "$SA" >/dev/null 2>&1 || \
  gc iam service-accounts create "$SA_ID" --display-name "scraper0 ${STAGE} (Cloud Run)"
gc iam service-accounts describe "${NAME}-web-run@${PROJECT}.iam.gserviceaccount.com" >/dev/null 2>&1 || \
  gc iam service-accounts create "${NAME}-web-run" --display-name "scraper0 ${STAGE} dashboard (Cloud Run, no roles)"

echo "==> database ${DB_NAME} on ${SQL_INSTANCE}"
DB_PASSWORD_SECRET="$(secret_name db-password)"
# Cloud SQL password policy: lower + upper + digit + symbol. Symbols kept URL-safe ("-" "_" ".") for the DB URL.
gen_db_password() { printf '%s-Aa1.%s' "$(openssl rand -hex 12)" "$(openssl rand -base64 12 | tr -d '/+=\n')"; }
strong() { [[ "$1" =~ [a-z] && "$1" =~ [A-Z] && "$1" =~ [0-9] && "$1" =~ [-_.] && ${#1} -ge 16 ]]; }
if ! gc secrets describe "$DB_PASSWORD_SECRET" >/dev/null 2>&1; then
  gen_db_password | gc secrets create "$DB_PASSWORD_SECRET" --data-file=- >/dev/null
fi
DB_PASSWORD="$(gc secrets versions access latest --secret "$DB_PASSWORD_SECRET")"
if ! strong "$DB_PASSWORD"; then
  gen_db_password | gc secrets versions add "$DB_PASSWORD_SECRET" --data-file=- >/dev/null
  DB_PASSWORD="$(gc secrets versions access latest --secret "$DB_PASSWORD_SECRET")"
fi
if gc sql users list --instance "$SQL_INSTANCE" --format="value(name)" | grep -qx "$DB_USER"; then
  gc sql users set-password "$DB_USER" --instance "$SQL_INSTANCE" --password "$DB_PASSWORD" >/dev/null
else
  gc sql users create "$DB_USER" --instance "$SQL_INSTANCE" --password "$DB_PASSWORD" >/dev/null
fi
gc sql databases describe "$DB_NAME" --instance "$SQL_INSTANCE" >/dev/null 2>&1 || \
  gc sql databases create "$DB_NAME" --instance "$SQL_INSTANCE" >/dev/null

echo "==> secrets"
ensure_secret() {   # name, value-generating command (only run when the secret is new)
  local s; s="$(secret_name "$1")"
  if ! gc secrets describe "$s" >/dev/null 2>&1; then
    eval "$2" | gc secrets create "$s" --data-file=- >/dev/null
  fi
}
printf 'postgresql+psycopg://%s:%s@%s:5432/%s' "$DB_USER" "$DB_PASSWORD" "$SQL_HOST" "$DB_NAME" > /tmp/.s0dburl.$$
if gc secrets describe "$(secret_name db-url)" >/dev/null 2>&1; then
  [ "$(gc secrets versions access latest --secret "$(secret_name db-url)")" = "$(cat /tmp/.s0dburl.$$)" ] || \
    gc secrets versions add "$(secret_name db-url)" --data-file=/tmp/.s0dburl.$$ >/dev/null
else
  gc secrets create "$(secret_name db-url)" --data-file=/tmp/.s0dburl.$$ >/dev/null
fi
rm -f /tmp/.s0dburl.$$
unset DB_PASSWORD
ensure_secret secret-key     "uv run --quiet scraper0 gen-secret-key | tr -d '\n'"
ensure_secret internal-token "openssl rand -base64 36 | tr -d '\n'"
ensure_secret admin-password "openssl rand -base64 18 | tr -d '/+=\n'"
for s in db-url secret-key internal-token admin-password; do
  gc secrets add-iam-policy-binding "$(secret_name $s)" --member "serviceAccount:$SA" \
    --role roles/secretmanager.secretAccessor >/dev/null
done

echo "==> Cloud Tasks queue"
gc tasks queues describe "$QUEUE" --location "$REGION" >/dev/null 2>&1 || \
  gc tasks queues create "$QUEUE" --location "$REGION" --max-concurrent-dispatches 2 --max-attempts 3 --min-backoff 30s
gc tasks queues add-iam-policy-binding "$QUEUE" --location "$REGION" --member "serviceAccount:$SA" \
  --role roles/cloudtasks.enqueuer >/dev/null

echo "==> build API image ($TAG)"
gc builds submit --tag "$REPO/api:$TAG" .

echo "==> deploy API"
# max-instances=1 + pool 2 (+1 overflow): ≤ 3 connections on the shared instance.
# 60-min timeout: long jobs run inside a request (wait=true, or Cloud Tasks → /internal/jobs/{id}/run).
gc run deploy "$API_SERVICE" --image "$REPO/api:$TAG" --region "$REGION" --service-account "$SA" \
  --allow-unauthenticated --port 8080 --timeout 3600 --cpu 1 --memory 2Gi --concurrency 20 \
  --min-instances 0 --max-instances 1 --cpu-boost \
  --network "$VPC_NETWORK" --subnet "$VPC_SUBNET" --vpc-egress private-ranges-only \
  --set-env-vars "^|^SCRAPER0_ENV=${STAGE}|SCRAPER0_JOB_MODE=cloud_tasks|SCRAPER0_DB_POOL_SIZE=2|SCRAPER0_DB_MAX_OVERFLOW=1|SCRAPER0_COOKIE_SECURE=true|SCRAPER0_LEGACY_KEYLESS=${LEGACY_KEYLESS}|SCRAPER0_BOOTSTRAP_ADMIN_EMAIL=${ADMIN_EMAIL}|SCRAPER0_BOOTSTRAP_LEGACY_TENANT=hashtaglocal|SCRAPER0_CLOUD_TASKS_QUEUE=projects/${PROJECT}/locations/${REGION}/queues/${QUEUE}|SCRAPER0_ALLOWED_BACKEND_URLS=${ALLOWED_BACKEND_URLS}" \
  --set-secrets "SCRAPER0_DATABASE_URL=$(secret_name db-url):latest,SCRAPER0_SECRET_KEY=$(secret_name secret-key):latest,SCRAPER0_INTERNAL_TOKEN=$(secret_name internal-token):latest,SCRAPER0_BOOTSTRAP_ADMIN_PASSWORD=$(secret_name admin-password):latest"
API_URL="$(gc run services describe "$API_SERVICE" --region "$REGION" --format 'value(status.url)')"

echo "==> build + deploy dashboard (API URL is compiled into the Next.js rewrites)"
gc builds submit frontend --config deploy/cloudbuild-web.yaml --substitutions "_IMAGE=$REPO/web:$TAG,_API_URL=$API_URL"
gc run deploy "$WEB_SERVICE" --image "$REPO/web:$TAG" --region "$REGION" --allow-unauthenticated \
  --service-account "${NAME}-web-run@${PROJECT}.iam.gserviceaccount.com" \
  --port 8080 --cpu 1 --memory 512Mi --min-instances 0 --max-instances 3
WEB_URL="$(gc run services describe "$WEB_SERVICE" --region "$REGION" --format 'value(status.url)')"

echo "==> wire URLs back into the API"
gc run services update "$API_SERVICE" --region "$REGION" \
  --update-env-vars "^|^SCRAPER0_PUBLIC_BASE_URL=${API_URL}|SCRAPER0_FRONTEND_URL=${WEB_URL}|SCRAPER0_CORS_ORIGINS=[\"${WEB_URL}\"]"

echo "==> Cloud Scheduler tick (every minute)"
TOKEN="$(gc secrets versions access latest --secret "$(secret_name internal-token)")"
if gc scheduler jobs describe "${NAME}-tick" --location "$REGION" >/dev/null 2>&1; then
  gc scheduler jobs update http "${NAME}-tick" --location "$REGION" --schedule "* * * * *" \
    --uri "${API_URL}/internal/tick" --http-method POST --update-headers "Authorization=Bearer ${TOKEN}" >/dev/null
else
  gc scheduler jobs create http "${NAME}-tick" --location "$REGION" --schedule "* * * * *" \
    --uri "${API_URL}/internal/tick" --http-method POST --headers "Authorization=Bearer ${TOKEN}" \
    --attempt-deadline 300s >/dev/null
fi
unset TOKEN

echo
echo "API:       $API_URL   (health: $API_URL/api/health, docs: $API_URL/api/docs)"
echo "Dashboard: $WEB_URL"
echo "Login:     $ADMIN_EMAIL — password: gcloud --project $PROJECT secrets versions access latest --secret $(secret_name admin-password)"
echo "Legacy endpoint for hashtaglocal-backend: ${API_URL}/api/v1/scrape"
