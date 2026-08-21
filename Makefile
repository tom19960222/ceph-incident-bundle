PYTHON ?= python3

.PHONY: test validate

test:
	$(PYTHON) validation/run_offline.py

validate: test
