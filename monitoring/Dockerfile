# syntax=docker/dockerfile:1.10
# Monitoring dashboard (rubric 3.2). Streamlit, port 8502, EC2 #3 -- a different EC2 server
# from the user UI, which is what the rubric clause asks for.
#
# Unlike the two user-facing images this one DOES install a database driver, because rubric
# 3.2 requires the dashboard to exchange data through the database rather than through JSON
# files. It connects as `monitoring_ro`, a SELECT-only role created by
# infra/postgres-init/02-monitoring-role.sql, so the driver cannot carry a write
# (premortem H16).
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

COPY requirements/monitor.txt requirements/
RUN pip install --require-hashes --only-binary=:all: --no-deps -r requirements/monitor.txt

COPY model/ model/
COPY monitoring/ monitoring/

USER appuser
EXPOSE 8502

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
  CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://127.0.0.1:8502/_stcore/health').status == 200 else 1)"

CMD ["streamlit", "run", "monitoring/dashboard.py", "--server.port=8502", "--server.address=0.0.0.0", "--server.headless=true", "--browser.gatherUsageStats=false"]
