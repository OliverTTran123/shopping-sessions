# Fill in RUN, SCORE and LIMIT_S once the kit is out. Everything else works as is.
PY      ?= python3
RUN     ?= $(PY) src/solve.py --input data/dev_input.json --output out/pred.json
SCORE   ?= $(PY) scorer/score.py --pred out/pred.json --truth data/dev_truth.json
OUT     ?= out/pred.json
LIMIT_S ?= 600
RUNS    ?= 3

.PHONY: check lint test run score determinism bench freeze verify-frozen clean

check: verify-frozen lint test run score determinism bench
	@$(PY) scripts/summary.py

lint:
	@ruff check . && ruff format --check .

test:
	@pytest -q

run:
	@mkdir -p out
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
