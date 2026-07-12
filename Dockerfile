# syntax=docker/dockerfile:1
FROM python:3.12-slim AS runtime

# Never run as root; the app needs no privileges beyond its own files.
RUN groupadd --system app && useradd --system --gid app --home-dir /srv/app app

WORKDIR /srv/app

# Install dependencies first so code changes don't bust the dependency layer.
COPY pyproject.toml README.md ./
COPY app ./app
RUN pip install --no-cache-dir .

# Registry CSVs / SQLite land here; owned by the app user, mountable as a volume.
RUN mkdir -p /srv/app/data && chown -R app:app /srv/app

USER app

ENV WL_DATA_DIR=/srv/app/data \
    PYTHONUNBUFFERED=1

EXPOSE 8000

# python:slim has no curl; probe with the stdlib instead.
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import os,sys,urllib.request; port=os.environ.get('PORT','8000'); sys.exit(0 if urllib.request.urlopen(f'http://127.0.0.1:{port}/health', timeout=4).status < 500 else 1)"

# Bind to the platform-provided PORT (Render/Railway/Fly), default 8000 locally.
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-8000}"]
