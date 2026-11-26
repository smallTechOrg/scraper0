# scraper0 frontend — Next.js standalone image for Cloud Run.
#
#   docker build --build-arg SCRAPER0_API_URL=https://api.example.com -t scraper0-frontend .
#   docker run -p 8080:8080 scraper0-frontend
#
# SCRAPER0_API_URL is needed at BUILD time: Next.js compiles the /v1, /v2 and /api
# rewrites into the standalone server.

# Build in /srv/web, not /app: with the project at /app, the dashboard's /app route lives at
# /app/app/app/ and Next.js loses the root layout (no <html>/<head>, no CSS in the build).

FROM node:22.16.0-alpine AS deps
WORKDIR /srv/web
COPY package.json package-lock.json ./
RUN npm ci

FROM node:22.16.0-alpine AS build
WORKDIR /srv/web
ARG SCRAPER0_API_URL=http://127.0.0.1:5001
ENV SCRAPER0_API_URL=${SCRAPER0_API_URL} \
    NEXT_TELEMETRY_DISABLED=1
COPY --from=deps /srv/web/node_modules ./node_modules
COPY . .
RUN npm run build

FROM node:22.16.0-alpine AS run
WORKDIR /srv/web
ENV NODE_ENV=production \
    NEXT_TELEMETRY_DISABLED=1 \
    HOSTNAME=0.0.0.0 \
    PORT=8080
RUN addgroup -S app && adduser -S app -G app
COPY --from=build --chown=app:app /srv/web/.next/standalone ./
COPY --from=build --chown=app:app /srv/web/.next/static ./.next/static
COPY --from=build --chown=app:app /srv/web/public ./public
USER app
EXPOSE 8080
# server.js reads $PORT (Cloud Run sets it) and $HOSTNAME.
CMD ["node", "server.js"]
