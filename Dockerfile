# syntax=docker/dockerfile:1
# SentinelSQL: one image with the API and the built UI, listening on port 8000.
#
#   docker build -t sentinelsql .
#   docker run -p 8000:8000 --env-file .env -v sentinel-data:/data sentinelsql
#
# State (demo database, audit log, review queue, caches) lives in /data, so mount a volume there.

# ---- stage 1: build the React UI
FROM node:22-slim AS ui
WORKDIR /ui
COPY sentinel-frontend/package.json sentinel-frontend/package-lock.json ./
RUN npm ci
COPY sentinel-frontend/ ./
RUN npm run build

# ---- stage 2: the API, with the UI copied in
FROM python:3.11-slim AS app
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PYTHONPATH=/app \
    UI_DIST_DIR=/app/ui
WORKDIR /app

# exact, tested versions (generated on Linux with Python 3.11; see requirements.txt for the loose list)
COPY requirements.lock ./
# the cache mount keeps downloaded wheels between builds, so a retry after a dropped connection only fetches what is missing
RUN --mount=type=cache,target=/root/.cache/pip pip install --retries 8 --timeout 120 -r requirements.lock

COPY app ./app
COPY scripts/reset_demo_db.py ./scripts/reset_demo_db.py
COPY --from=ui /ui/dist ./ui

# run as an unprivileged user; relative paths in the app (audit.db, sentinel.db, cache files) resolve inside /data
RUN useradd --create-home --uid 10001 sentinel && mkdir /data && chown sentinel:sentinel /data
USER sentinel
WORKDIR /data
VOLUME ["/data"]
EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD python -c "import urllib.request, sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/health', timeout=4).status == 200 else 1)"

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
