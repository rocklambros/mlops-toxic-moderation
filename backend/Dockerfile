# syntax=docker/dockerfile:1.10
# CPU serve image for the FastAPI moderation backend. The pinned digest is the multi-arch
# index, which carries linux/arm64/v8 as well as linux/amd64, so it resolves correctly on
# both the aarch64 build box and the Graviton (t4g) deploy target.
FROM python:3.11-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1

# 10001 is outside the host's ordinary uid range, so a bind-mounted volume cannot be written
# as a real host user by accident. /var/lib/toxic is the spool directory: the degraded
# persistence path must be writable by the unprivileged runtime user or it fails closed to
# 503 on the first database hiccup.
RUN useradd --create-home --uid 10001 appuser \
 && install -d -o appuser -g appuser /var/lib/toxic

WORKDIR /app

COPY requirements/base.txt requirements/serve.txt requirements/
# --require-hashes pins what is installed; --only-binary=:all: is what stops a hash-matching
# source distribution from executing its setup.py during the build. --no-deps is safe and
# deliberate because pip-compile already resolved the full transitive closure into the lock.
RUN pip install --require-hashes --only-binary=:all: --no-deps -r requirements/serve.txt

COPY model/ model/
COPY backend/ backend/
COPY MODEL_CARD.md MODEL_CARD.md

USER appuser
EXPOSE 8000

# start-period covers the measured 51-78 s cold load of the 382 MB classical artifact:
# uvicorn does not bind until the lifespan finishes, so every earlier probe is refused and a
# short start period turns a healthy container into a restart loop. timeout is 5 s because
# /health does a database round trip bounded by DB_TIMEOUT_SECONDS (2 s) plus connect.
HEALTHCHECK --interval=30s --timeout=5s --start-period=180s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8000/health').status == 200 else 1)"

CMD ["uvicorn", "backend.app:create_app", "--factory", "--host", "0.0.0.0", "--port", "8000", "--workers", "1"]
