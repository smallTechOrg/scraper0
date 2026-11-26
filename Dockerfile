# scraper0 API — Cloud Run image. Includes headless Chromium for the SMARTONEBLR (BBMP) connector.
FROM python:3.12-slim AS build
ENV UV_COMPILE_BYTECODE=1 UV_LINK_MODE=copy UV_PYTHON_DOWNLOADS=never
COPY --from=ghcr.io/astral-sh/uv:0.9.7 /uv /bin/uv
WORKDIR /app
COPY pyproject.toml uv.lock README.md ./
RUN uv sync --frozen --no-dev --no-install-project
COPY src ./src
RUN uv sync --frozen --no-dev

FROM python:3.12-slim
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PATH="/app/.venv/bin:$PATH" PYTHONPATH=/app/src \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright SCRAPER0_LOG_FORMAT=json SCRAPER0_AUTO_MIGRATE=true
WORKDIR /app
COPY --from=build /app/.venv /app/.venv
# Chromium + its system libraries (≈ 400 MB); needed only by the browser-driven connectors
# Playwright's bundled Node needs libatomic1 before it can run its own --with-deps installer
RUN apt-get update && apt-get install -y --no-install-recommends libatomic1 \
    && playwright install --with-deps chromium && rm -rf /var/lib/apt/lists/* \
    && useradd --system --uid 10001 --home-dir /app scraper0 && chmod -R a+rx /ms-playwright
COPY alembic.ini ./
COPY migrations ./migrations
COPY src ./src
USER scraper0
EXPOSE 8080
CMD ["scraper0", "serve"]
