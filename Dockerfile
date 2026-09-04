# syntax=docker/dockerfile:1.7

FROM node:22.12-bookworm-slim AS frontend-builder
WORKDIR /build/frontend
COPY frontend/package.json frontend/package-lock.json ./
RUN npm ci
COPY frontend/ ./
RUN npm run build

FROM python:3.12-slim-bookworm AS runtime

ARG UV_VERSION=0.8.9
ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    UV_COMPILE_BYTECODE=1 \
    UV_LINK_MODE=copy \
    PLAYWRIGHT_BROWSERS_PATH=/ms-playwright

RUN pip install --no-cache-dir "uv==${UV_VERSION}"

WORKDIR /app
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --no-install-project

COPY src/ ./src/
COPY frontend/public/ ./frontend/public/
COPY --from=frontend-builder /build/frontend/dist/ ./frontend/dist/
COPY deploy/defaults/ /opt/job-applier-defaults/
COPY deploy/docker-entrypoint.sh /usr/local/bin/job-applier-entrypoint

RUN uv sync --frozen --no-dev \
    && uv run playwright install --with-deps chromium \
    && chmod 0755 /usr/local/bin/job-applier-entrypoint \
    && groupadd --gid 10001 job-applier \
    && useradd --uid 10001 --gid job-applier --home-dir /app --no-create-home --shell /usr/sbin/nologin job-applier \
    && mkdir -p /app/data /app/output/applications /app/output/applied /app/.browser_profile \
    && chown -R job-applier:job-applier /app/data /app/output /app/.browser_profile

USER job-applier
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=10s --start-period=30s --retries=5 \
    CMD ["/app/.venv/bin/python", "-c", "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8000/api/health', timeout=5)"]

ENTRYPOINT ["job-applier-entrypoint"]
CMD ["/app/.venv/bin/python", "-m", "job_applier.cli.web_app", "--host", "0.0.0.0", "--port", "8000", "--no-browser", "--no-reload"]
