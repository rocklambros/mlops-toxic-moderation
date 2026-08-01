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

.PHONY: ui-lock

# The Streamlit surfaces resolve on their own, into their own hashed lock, and are NOT
# installed into the development venv. Nothing in the test suite imports streamlit: the UI
# modules import it inside the functions that draw, so the pure logic -- the label payload,
# the challenger column, the client -- is unit-tested without a 200 MB dependency in the
# unit CI job. `requirements/ui.txt` is what the two UI images install, wheels-only and
# hash-checked, same posture as `lock` and `serve-deps`.
ui-lock: check-py
	$(PY) -m venv .venv-lock
	.venv-lock/bin/pip install --only-binary=:all: pip-tools==7.4.1
	.venv-lock/bin/pip-compile --generate-hashes \
	  --output-file requirements/ui.txt requirements/ui.in
	rm -rf .venv-lock

.PHONY: monitor-lock rescorer-lock

# The monitoring dashboard's own surface. It carries a database driver, unlike the two
# user-facing Streamlit images, because rubric 3.2 requires the dashboard to read the
# database directly -- as `monitoring_ro`, a read-only role.
monitor-lock: check-py
	$(PY) -m venv .venv-lock
	.venv-lock/bin/pip install --only-binary=:all: pip-tools==7.4.1
	.venv-lock/bin/pip-compile --generate-hashes \
	  --output-file requirements/monitor.txt requirements/monitor.in
	rm -rf .venv-lock

# The challenger re-scorer. Installed by nothing else, so cutting it (ordered cut list item
# 3) removes onnxruntime and tokenizers from the project entirely.
rescorer-lock: check-py
	$(PY) -m venv .venv-lock
	.venv-lock/bin/pip install --only-binary=:all: pip-tools==7.4.1
	.venv-lock/bin/pip-compile --generate-hashes \
	  --output-file requirements/rescorer.txt requirements/rescorer.in
	rm -rf .venv-lock

# -s so the measured percentiles reach the operator's terminal, not just the report file.
loadtest:
	PYTHONHASHSEED=0 $(BIN)/pytest -m perf -s

serve:
	$(BIN)/uvicorn backend.app:create_app --factory --host 127.0.0.1 --port 8000

purge:
	$(BIN)/python -m backend.retention

.PHONY: heldout seed-demo seed-demo-purge

# The dashboard's data source (premortem C5). `heldout` exports the LOCKED test split, so
# the replayed comments are ones the model never trained on -- replaying training rows would
# make live accuracy a measurement of memorisation. `seed-demo` then replays them through a
# running backend and exits non-zero if the resulting dataset would leave a graded panel
# degenerate.
RAW_CSV ?= data/raw/jigsaw-toxic-comment-train.csv
SEED_CSV ?= data/heldout.csv
SEED_N ?= 2000
SEED_DAYS ?= 14

heldout:
	PYTHONHASHSEED=0 $(BIN)/python -m scripts.export_heldout --csv $(RAW_CSV) --out $(SEED_CSV)

seed-demo:
	PYTHONHASHSEED=0 $(BIN)/python -m scripts.seed_demo --csv $(SEED_CSV) --n $(SEED_N) \
	  --days $(SEED_DAYS)

seed-demo-purge:
	PYTHONHASHSEED=0 $(BIN)/python -m scripts.seed_demo --csv $(SEED_CSV) --purge --n 0
