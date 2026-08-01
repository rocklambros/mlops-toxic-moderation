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

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

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
