.PHONY: check-py lock venv lint test data fetch-data

# The build box's /usr/bin/python3.11 is 3.11.0rc1. CI's setup-python fetches a release
# build, so leaving this unguarded puts local and CI on different interpreters and any
# divergence gets debugged in the wrong place. Point PY at a release 3.11.
PY ?= python3.11
VENV ?= .venv
BIN := $(VENV)/bin
CSV ?= tests/fixtures/mini_jigsaw.csv
SEED ?= 42

# Fails loudly rather than building the whole project on a pre-release interpreter.
check-py:
	@$(PY) -c "import sys; v = sys.version_info; \
	assert v[:2] == (3, 11), 'need Python 3.11, got %s' % '.'.join(map(str, v[:3])); \
	assert v.releaselevel == 'final', 'refusing pre-release interpreter: %s' % sys.version.split()[0]" \
	|| { echo ""; \
	     echo "  Set PY to a release build of 3.11. On this box:"; \
	     echo "    make venv PY=\$$HOME/anaconda3/envs/py311/bin/python"; \
	     echo ""; exit 1; }

# pip-tools lives in a throwaway venv so the resolver never shares an environment with
# the project. Wheels only, so nothing executes a setup.py on a box holding live keys.
lock: check-py
	$(PY) -m venv .venv-lock
	.venv-lock/bin/pip install --only-binary=:all: pip-tools==7.4.1
	.venv-lock/bin/pip-compile --generate-hashes --allow-unsafe \
	  --output-file requirements/dev.lock requirements/dev.txt
	rm -rf .venv-lock

venv: check-py
	$(PY) -m venv $(VENV)
	$(BIN)/pip install --require-hashes --only-binary=:all: -r requirements/dev.lock

lint:
	$(BIN)/ruff check .

test:
	PYTHONHASHSEED=0 $(BIN)/pytest -m "not integration"

data:
	PYTHONHASHSEED=0 $(BIN)/python -m model.data.run --csv $(CSV) --seed $(SEED)

fetch-data:
	./scripts/fetch_jigsaw.sh

.PHONY: serve-deps test-integration serve purge loadtest

# Same supply-chain posture as `lock` and `venv`: pip-tools resolves in a throwaway venv so
# the resolver never shares an environment with the project, and every install is
# wheels-only so nothing executes a setup.py on a box holding live keys.
serve-deps: check-py
	$(PY) -m venv .venv-lock
	.venv-lock/bin/pip install --only-binary=:all: pip-tools==7.4.1
	.venv-lock/bin/pip-compile --generate-hashes \
	  --output-file requirements/serve.txt requirements/serve.in
	rm -rf .venv-lock
	$(BIN)/pip install --require-hashes --only-binary=:all: -r requirements/serve.txt

test-integration:
	PYTHONHASHSEED=0 $(BIN)/pytest -m integration

# -s so the measured percentiles reach the operator's terminal, not just the report file.
loadtest:
	PYTHONHASHSEED=0 $(BIN)/pytest -m perf -s

serve:
	$(BIN)/uvicorn backend.app:create_app --factory --host 127.0.0.1 --port 8000

purge:
	$(BIN)/python -m backend.retention
