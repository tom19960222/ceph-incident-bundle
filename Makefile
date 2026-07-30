.PHONY: test check-python test-python shellcheck validate \
	lab-status lab-profile-discover lab-profile-activate lab-preflight

PYTHON ?= python3

test: check-python
	bash tests/run-tests.sh

check-python:
	@$(PYTHON) -c 'import sys; sys.exit("Python 3.11 or newer is required") if sys.version_info < (3, 11) else None'

test-python: check-python
	$(PYTHON) -m unittest discover -s tests -p 'test_python_*.py' -v

shellcheck:
	shellcheck lib/*.sh run/*.sh tests/*.sh tests/fixtures/*.sh tests/fixtures/python-node/bin/codec-command tests/fixtures/python-node/bin/node-command tests/fixtures/python-node/bin/tar-wrapper

validate: check-python test test-python shellcheck

# Real-lab workflow.  `lab-status` is local-only; the other three are explicit
# opt-ins that touch a lab or the trusted profile, and none of them is reachable
# from `make validate`.  See docs/lab-validation-runbook.md.
#
# The full real-lab gate (`make validate-lab`) is owned by issue #20 and does not
# exist yet: a passing `lab-preflight` proves lab identity, not qualification.
lab-status: check-python require-lab-profile
	$(PYTHON) -m validation.lab status --profile "$(LAB_PROFILE)" $(LAB_ARGS)

lab-profile-discover: check-python require-lab-profile
	$(PYTHON) -m validation.lab discover --profile "$(LAB_PROFILE)" $(LAB_ARGS)

lab-profile-activate: check-python require-lab-profile
	@[ -n "$(LAB_CANDIDATE)" ] || { \
		echo "LAB_CANDIDATE=/absolute/path/to/lab.candidate.toml is required" >&2; exit 1; }
	$(PYTHON) -m validation.lab activate --profile "$(LAB_PROFILE)" \
		--candidate "$(LAB_CANDIDATE)" $(LAB_ARGS)

lab-preflight: check-python require-lab-profile
	$(PYTHON) -m validation.lab preflight --profile "$(LAB_PROFILE)" $(LAB_ARGS)

.PHONY: require-lab-profile
require-lab-profile:
	@[ -n "$(LAB_PROFILE)" ] || { \
		echo "LAB_PROFILE=/absolute/path/to/lab.toml is required" >&2; exit 1; }
