.PHONY: test shellcheck validate

test:
	bash tests/run-tests.sh

shellcheck:
	shellcheck lib/*.sh run/*.sh tests/*.sh

validate: test shellcheck
