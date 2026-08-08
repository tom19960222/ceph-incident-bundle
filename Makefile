.PHONY: test check-python test-python validate \
	lab-status lab-profile-discover lab-profile-activate lab-preflight \
	validate-lab lab-clean

PYTHON ?= python3

# Job count for the sharded Python test runner; see tests/run-python-tests.sh.
TEST_JOBS ?= auto

test: test-python

check-python:
	@$(PYTHON) -c 'import sys; sys.exit("Python 3.11 or newer is required") if sys.version_info < (3, 11) else None'

test-python: check-python
	PYTHON="$(PYTHON)" bash tests/run-python-tests.sh $(TEST_JOBS)

validate: test-python

# Real-lab workflow.  `lab-status` is local-only; the others are explicit opt-ins
# that touch a lab or the trusted profile, and none of them is reachable from
# `make validate`.  See docs/lab-validation-runbook.md.
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

# The post-cutover real-lab gate validates the preserved #21 PASS report and
# shell bundle, proves the active lab still has the same identity, runs one
# Python full collect, compares normalized contracts, and checks stable state
# plus local/remote residue. It needs the same explicit
# confirmation as `lab-preflight` and is never reachable from `make validate`.
validate-lab: check-python require-lab-profile require-lab-baseline
	$(PYTHON) -m validation.lab qualify --profile "$(LAB_PROFILE)" \
		--baseline-report "$(LAB_BASELINE_REPORT)" $(LAB_ARGS)

# Reclaim what earlier runs left behind.  A failed `validate-lab` keeps its
# workdir on purpose and nothing deletes it automatically, so this is where the
# gigabytes go — after the failure has been read.  It needs no Lab Profile
# (nothing about one decides what is deleted) and removes nothing at all until
# CEPH_INCIDENT_LAB_CLEAN=1 is set; without it, the run is a preview.
lab-clean: check-python
	$(PYTHON) -m validation.lab clean $(LAB_ARGS)

.PHONY: require-lab-profile
require-lab-profile:
	@[ -n "$(LAB_PROFILE)" ] || { \
		echo "LAB_PROFILE=/absolute/path/to/lab.toml is required" >&2; exit 1; }

.PHONY: require-lab-baseline
require-lab-baseline:
	@[ -n "$(LAB_BASELINE_REPORT)" ] || { \
		echo "LAB_BASELINE_REPORT=/absolute/path/to/report.json is required" >&2; exit 1; }
