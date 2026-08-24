PYTHON ?= python3
FAST_TESTS = \
	test_bundle \
	test_collect \
	test_inventory \
	test_kubernetes \
	test_node_archive \
	test_prometheus \
	test_remote_collector

.PHONY: test validate

test:
	PYTHONPATH=src:tests/python PYTHONDONTWRITEBYTECODE=1 \
		$(PYTHON) -m unittest -v $(FAST_TESTS)

validate:
	$(PYTHON) validation/run_offline.py
