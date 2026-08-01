# syntax=docker/dockerfile:1.10
# User-facing moderation UI (rubric 3.1). Streamlit, port 8501, EC2 #2.
#
# The pinned digest is the multi-arch index, which carries linux/arm64/v8 as well as
# linux/amd64, so it resolves on the aarch64 build box and on the Graviton (t4g) deploy
# target from one pin. Same digest as backend/Dockerfile: one base image to audit, not four.
#
# This image carries NO database driver -- requirements/ui.txt is asserted free of one by
# tests/unit/test_supply_chain.py. Every write goes through the backend API, so a credential
# reaching this instance still cannot write the graded metric (premortem H12, H16).
FROM python:3.11-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

COPY requirements/ui.txt requirements/
# --require-hashes pins what is installed; --only-binary=:all: is what stops a hash-matching
# source distribution from executing its setup.py during the build. --no-deps is safe
# because pip-compile already resolved the full transitive closure into the lock.
RUN pip install --require-hashes --only-binary=:all: --no-deps -r requirements/ui.txt

# Only what the UI imports. `backend/` contributes two standard-library-only modules -- the
# closed verdict vocabulary and the session-fingerprint header name -- which are imported
# rather than restated so there is one definition of each, not two.
COPY model/ model/
COPY backend/__init__.py backend/feedback.py backend/fingerprint.py backend/
COPY infra/__init__.py infra/exposure.py infra/
COPY frontend/ frontend/

USER appuser
EXPOSE 8501

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8501/_stcore/health').status == 200 else 1)"

CMD ["streamlit", "run", "frontend/ui.py", "--server.port=8501", "--server.address=0.0.0.0", "--server.headless=true", "--browser.gatherUsageStats=false"]
