.PHONY: test check-python test-python test-differential shellcheck validate

PYTHON ?= python3

test: check-python
	bash tests/run-tests.sh

check-python:
	@$(PYTHON) -c 'import sys; sys.exit("Python 3.11 or newer is required") if sys.version_info < (3, 11) else None'

test-python: check-python
	$(PYTHON) -m unittest discover -s tests -p 'test_python_*.py' -v

# Offline observable-contract equivalence gate: the shell reference and the
# Python candidate run the same scenarios in the same fake world and their
# normalized contracts are compared. See docs/differential-normalizer.md.
test-differential: check-python
	$(PYTHON) -m unittest discover -s tests -p 'test_differential_*.py' -v

shellcheck:
	shellcheck lib/*.sh run/*.sh tests/*.sh tests/fixtures/*.sh tests/fixtures/python-node/bin/codec-command tests/fixtures/python-node/bin/node-command tests/fixtures/python-node/bin/tar-wrapper

validate: check-python test test-python test-differential shellcheck
