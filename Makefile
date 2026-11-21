# Fill in RUN, SCORE and LIMIT_S once the kit is out. Everything else works as is.
PY      ?= python3
# All data lives on WSL ext4, never in the repo or under /mnt/c. Override via env vars.
DATA_DIR    ?= $(HOME)/ss-data/input
OUT_DIR     ?= $(HOME)/ss-data/output
SCRATCH_DIR ?= $(HOME)/ss-data/scratch
IMAGE       ?= shopping-sessions:latest
CPUS        ?= 8
MEM         ?= 8g
PLATFORM    ?= linux/amd64
ANSWER      ?= $(HOME)/ss-data/downloads/dev-answer
GENERATE    ?= $(HOME)/.cargo/bin/generate
export DATA_DIR OUT_DIR SCRATCH_DIR IMAGE CPUS MEM PLATFORM
# Starter dev-check: run the image on the dev set, then exact-diff both tables.
RUN     ?= sh scripts/docker_run.sh $(DATA_DIR) $(OUT_DIR)
SCORE   ?= $(PY) scorer/score.py --pred $(OUT_DIR) --truth $(ANSWER)
OUT     ?= $(OUT_DIR)
LIMIT_S ?= 300
RUNS    ?= 3

.PHONY: check lint test run score determinism bench freeze verify-frozen clean
.PHONY: build dev run-dev check-dev fixtures

check: verify-frozen lint test fixtures run score determinism bench
	@$(PY) scripts/summary.py

lint:
	@ruff check . && ruff format --check .

test:
	@pytest -q

run: build
	@$(RUN)

score: run
	@mkdir -p out
	@$(SCORE) > out/score.json
	@cat out/score.json

determinism:
	@mkdir -p out
	@$(PY) scripts/determinism.py --out-file $(OUT) --json out/determinism.json -- $(RUN)

bench:
	@mkdir -p out
	@$(PY) scripts/bench.py --runs $(RUNS) --limit $(LIMIT_S) --json out/bench.json -- "$(RUN)"

freeze:
	@sh scripts/freeze.sh

verify-frozen:
	@sh scripts/verify_frozen.sh

clean:
	@rm -rf out .pytest_cache .ruff_cache

build:
	docker buildx build --platform $(PLATFORM) --load -t $(IMAGE) .

dev:
	rm -rf $(DATA_DIR)
	mkdir -p $(DATA_DIR)
	$(GENERATE) --seed 1 --events 1000000 --clicks 250000 --output $(DATA_DIR)

fixtures: build
	$(PY) scripts/run_fixtures.py fixtures -- sh scripts/docker_run.sh

run-dev: build
	rm -rf $(OUT_DIR)
	mkdir -p $(OUT_DIR) $(SCRATCH_DIR)
	sh scripts/docker_run.sh $(DATA_DIR) $(OUT_DIR)

check-dev:
	$(PY) scripts/compare.py $(OUT_DIR) $(ANSWER)
