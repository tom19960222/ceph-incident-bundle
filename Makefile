.PHONY: test test-python shellcheck validate

PYTHON ?= python3

test:
	bash tests/run-tests.sh

test-python:
	$(PYTHON) -m unittest discover -s tests -p 'test_python_*.py' -v

shellcheck:
	shellcheck lib/*.sh run/*.sh tests/*.sh

validate: test test-python shellcheck
