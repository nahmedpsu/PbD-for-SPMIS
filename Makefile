.PHONY: install lint test test-policy bundle contracts demo serve check clean

PY ?= python

install:
	$(PY) -m pip install -e ".[dev,openimis,gateway]"

lint:
	ruff check src tests scripts integrations
	ruff format --check src tests scripts integrations

format:
	ruff format src tests scripts integrations
	ruff check --fix src tests scripts integrations

test:
	$(PY) -m pytest

test-policy:
	opa check --v1-compatible policy/rego
	opa test --v1-compatible policy/rego policy/bundle -v

bundle:
	$(PY) scripts/build_bundle.py

contracts:
	$(PY) scripts/export_contracts.py

check: lint test test-policy
	$(PY) scripts/build_bundle.py --check
	$(PY) scripts/export_contracts.py --check
	pbd-spmis catalog validate

demo:
	pbd-spmis demo

# Integration test inside the real openIMIS backend; see integrations/openimis/validation/README.md
validate-openimis:
	python integrations/openimis/validation/run_validation.py

serve:
	pbd-spmis serve all --reload

clean:
	rm -rf data .pytest_cache .ruff_cache build dist *.egg-info
