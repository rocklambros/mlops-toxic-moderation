# syntax=docker/dockerfile:1.10
# DistilBERT challenger re-scorer. EC2 #3's second container, CPU, no ingress and no
# published port -- it reads the review queue and writes a second opinion back to it.
#
# This is item 3 on the delivery spec's ordered cut list. It is the ONLY image that installs
# onnxruntime and tokenizers (requirements/rescorer.txt), so cutting the challenger removes
# both from the project rather than leaving a 200 MB dependency in a graded surface.
#
# Both compiled wheels publish manylinux aarch64 distributions, which is what makes
# --only-binary=:all: viable for the t4g Graviton fleet: without an arm64 wheel this build
# would have to fall back to a source distribution and compile ONNX Runtime in the image.
FROM python:3.11-slim-bookworm@sha256:b18992999dbe963a45a8a4da40ac2b1975be1a776d939d098c647482bcad5cba AS runtime

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PYTHONPATH=/app

RUN useradd --create-home --uid 10001 appuser

WORKDIR /app

COPY requirements/base.txt requirements/rescorer.txt requirements/
RUN pip install --require-hashes --only-binary=:all: --no-deps -r requirements/rescorer.txt

COPY model/ model/
COPY rescorer/ rescorer/

USER appuser

# No HEALTHCHECK: this container publishes no port and serves no request, so there is
# nothing to probe. Its liveness signal is the review_queue rows advancing from 'pending' to
# 'rescored', which the dashboard's queue-depth caption already shows.
CMD ["python", "-m", "rescorer.worker"]
