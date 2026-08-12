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

# ARROW_DEFAULT_MEMORY_POOL=system is a crash fix, not a tuning knob. pyarrow 25 on
# aarch64 defaults to its bundled mimalloc, and Streamlit runs every script run on a fresh
# ScriptRunner thread -- so every page load enters mimalloc's per-thread heap setup from a
# brand-new thread. On 2026-08-12 that segfaulted the monitoring dashboard, five times, with
# the core naming mi_thread_init under arrow::py::NdarrayToArrow under Table.from_pandas,
# which is what st.dataframe calls. The system allocator has no such path. At this project's
# data sizes the allocator choice is not measurable; tests/unit/test_arrow_allocator.py holds
# every Streamlit image to it.
#
# Shipped WITHOUT a reproduction, which is why PYTHONFAULTHANDLER rides along. Two attempts
# to reproduce -- 60 concurrent threads x 40 conversions, and 1500 sequential thread
# create/destroy cycles -- both survived on mimalloc, so 'the system pool fixes it' is
# inference from the backtrace rather than measurement. Frame 0 is where the process died,
# and an allocator is the usual victim of heap corruption caused somewhere else; if that is
# what this is, changing pools moves the symptom instead of removing it. faulthandler makes
# the next occurrence say so: it prints the Python frame to stderr on SIGSEGV, and the
# awslogs driver ships stderr to CloudWatch, so a recurrence arrives as a stack trace
# rather than as another stripped core nobody can read.
ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app \
    ARROW_DEFAULT_MEMORY_POOL=system \
    PYTHONFAULTHANDLER=1

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
