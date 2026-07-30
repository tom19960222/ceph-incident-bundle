"""Offline observable-contract equivalence gate (issue #18).

Every scenario runs the shell reference and the Python candidate against one
shared fake world and compares their normalized observable contracts: exit code,
bundle member set, every artifact, the manifest, the redaction ledger, the
external command policy and the workdir lifecycle.  A difference fails the gate
unless `tests/differential/normalize.py` was reviewed to ignore it.
"""

from __future__ import annotations

import json
import shutil
import tempfile
import unittest
from pathlib import Path

from tests.differential.environment import IMPLEMENTATIONS, CollectRun, run_collect
from tests.differential.normalize import (
    candidate_environment_additions,
    contract_of,
    describe_differences,
    environment_keys,
)
from tests.differential.scenarios import SCENARIOS, Scenario


_ROOT: Path | None = None
_RUNS: dict[tuple[str, str], CollectRun] = {}


def setUpModule() -> None:
    global _ROOT
    _ROOT = Path(tempfile.mkdtemp(prefix="ceph-incident-differential."))


def tearDownModule() -> None:
    if _ROOT is not None:
        shutil.rmtree(_ROOT, ignore_errors=True)


def run_of(scenario: Scenario, implementation: str) -> CollectRun:
    """Run one implementation of one scenario once per test session."""

    key = (scenario.name, implementation)
    if key not in _RUNS:
        assert _ROOT is not None
        root = _ROOT / f"{scenario.name}.{implementation}"
        root.mkdir(parents=True)
        _RUNS[key] = run_collect(
            root,
            implementation,
            inventory_text=scenario.inventory,
            knobs=scenario.knobs,
            arguments=scenario.command_arguments,
            interrupt_after=scenario.interrupt_after,
        )
    return _RUNS[key]


class DifferentialContractTests(unittest.TestCase):
    maxDiff = None

    def assert_equivalent(self, scenario: Scenario) -> None:
        runs = {
            implementation: run_of(scenario, implementation)
            for implementation in IMPLEMENTATIONS
        }
        contracts = {
            implementation: contract_of(run) for implementation, run in runs.items()
        }
        diagnostics = "\n\n".join(
            f"--- {implementation} exit={run.exit_code}\n"
            f"stdout: {run.stdout.strip()}\nstderr: {run.stderr.strip()[-800:]}"
            for implementation, run in runs.items()
        )

        if scenario.expected_exit is not None:
            for implementation, run in runs.items():
                self.assertEqual(
                    run.exit_code,
                    scenario.expected_exit,
                    f"{implementation}: unexpected exit code\n{diagnostics}",
                )
        for implementation, run in runs.items():
            self.assertEqual(
                run.archive is not None,
                scenario.expect_archive,
                f"{implementation}: unexpected bundle presence\n{diagnostics}",
            )
        differences = describe_differences(contracts["shell"], contracts["python"])
        self.assertEqual(
            differences,
            [],
            f"observable contract differs for {scenario.name}:\n"
            + "\n".join(differences)
            + f"\n{diagnostics}",
        )

    def test_each_implementation_streams_only_its_reviewed_node_collector(self) -> None:
        """Both sides may ship different sources, but only their reviewed ones."""

        scenario = next(
            item for item in SCENARIOS if item.name == "mixed-full-collection-redacted"
        )
        requests = {
            implementation: [
                json.loads(line)
                for line in run_of(scenario, implementation).ledgers.get(
                    "node-requests.jsonl", []
                )
            ]
            for implementation in IMPLEMENTATIONS
        }
        self.assertEqual(
            [request["payload_members"] for request in requests["shell"]],
            [
                ["lib/collect-node.sh", "lib/collect-var-log.sh", "lib/common.sh"]
                for _ in requests["shell"]
            ],
        )
        self.assertEqual(
            [request["payload_members"] for request in requests["python"]],
            [["<python-node-source>"] for _ in requests["python"]],
        )
        for implementation, records in requests.items():
            self.assertTrue(records, f"{implementation} collected no node")

    def test_candidate_environment_additions_stay_documented(self) -> None:
        """The candidate may record more than the reference — but only the
        documented extras, and never fewer keys than the reference."""

        scenario = next(
            item for item in SCENARIOS if item.name == "mixed-full-collection-redacted"
        )
        keys = {
            implementation: environment_keys(run_of(scenario, implementation))
            for implementation in IMPLEMENTATIONS
        }
        self.assertTrue(keys["shell"], "the reference recorded no environment")
        additions = candidate_environment_additions(keys["python"])
        self.assertTrue(additions, "the documented candidate additions are missing")
        self.assertEqual(
            set(keys["shell"]) - set(keys["python"]),
            set(),
            "the candidate dropped a reference environment field",
        )
        self.assertEqual(
            set(keys["python"]) - set(keys["shell"]) - set(additions),
            set(),
            "the candidate recorded an undocumented environment field",
        )

    def test_no_scenario_reaches_a_default_off_command(self) -> None:
        """`cephadm shell` and `kubectl exec` stay unreachable in both runs."""

        for scenario in SCENARIOS:
            for implementation in IMPLEMENTATIONS:
                with self.subTest(scenario=scenario.name, implementation=implementation):
                    run = run_of(scenario, implementation)
                    ledger = "\n".join(
                        line
                        for name in ("ssh-argv.jsonl", "kubectl-argv.jsonl")
                        for line in run.ledgers.get(name, [])
                    )
                    self.assertNotIn("cephadm shell", ledger)
                    self.assertNotIn('"exec"', ledger)

    def test_scenario_set_stays_within_its_reviewed_size(self) -> None:
        self.assertEqual(len(SCENARIOS), len({item.name for item in SCENARIOS}))
        self.assertTrue(8 <= len(SCENARIOS) <= 13, len(SCENARIOS))
        for scenario in SCENARIOS:
            with self.subTest(scenario=scenario.name):
                self.assertTrue(scenario.coverage, "scenario claims no coverage")


def _install_scenario_tests() -> None:
    for scenario in SCENARIOS:
        def test(self: DifferentialContractTests, scenario: Scenario = scenario) -> None:
            self.assert_equivalent(scenario)

        test.__doc__ = f"{scenario.name}: {scenario.summary}"
        setattr(
            DifferentialContractTests,
            f"test_equivalent_{scenario.name.replace('-', '_')}",
            test,
        )


_install_scenario_tests()


if __name__ == "__main__":
    unittest.main()
