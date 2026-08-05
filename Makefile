.PHONY: test check-python test-python test-differential shellcheck validate \
	lab-status lab-profile-discover lab-profile-activate lab-preflight \
	validate-lab lab-clean

PYTHON ?= python3

# Job count for the sharded Python test runner; see tests/run-python-tests.sh.
TEST_JOBS ?= auto

test: check-python
	bash tests/run-tests.sh

check-python:
	@$(PYTHON) -c 'import sys; sys.exit("Python 3.11 or newer is required") if sys.version_info < (3, 11) else None'

test-python: check-python
	PYTHON="$(PYTHON)" bash tests/run-python-tests.sh $(TEST_JOBS)

# Offline observable-contract equivalence gate: the shell reference and the
# Python candidate run the same scenarios in the same fake world and their
# normalized contracts are compared. See docs/differential-normalizer.md.
test-differential: check-python
	$(PYTHON) -m unittest discover -s tests -p 'test_differential_*.py' -v

shellcheck:
	shellcheck lib/*.sh run/*.sh tests/*.sh tests/fixtures/*.sh tests/fixtures/python-node/bin/codec-command tests/fixtures/python-node/bin/node-command tests/fixtures/python-node/bin/tar-wrapper

validate: check-python test test-python test-differential shellcheck

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

# The full real-lab gate: identity preflight, a pre-collection stable-state
# snapshot, one shell reference full collect, one Python candidate full collect,
# structural and content-safety verification of both bundles, the normalized
# observable-contract comparison, the post-collection snapshot and the per-node
# residue check.  It runs two real collects, so it needs the same explicit
# confirmation as `lab-preflight` and is never reachable from `make validate`.
validate-lab: check-python require-lab-profile
	$(PYTHON) -m validation.lab qualify --profile "$(LAB_PROFILE)" $(LAB_ARGS)

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
