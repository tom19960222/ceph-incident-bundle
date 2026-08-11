"""Structured, read-only interpreter identity probes for every lab node."""

from __future__ import annotations

import json
import os
import shlex
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.lab_fixture import FakeLab
from validation.lab_probe import RUNTIME_PROBE_COMMAND, LabProber
from validation.lab_profile import load_profile
from validation.lab_runtime import (
    RESULT_CHANGED,
    RESULT_UNAVAILABLE,
    RESULT_UNCHANGED,
    RuntimeProbe,
    RuntimeSnapshot,
    capture_runtime_snapshot,
    compare_runtime_snapshots,
)


class RuntimeFixture:
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.lab = FakeLab(self.root)
        self.profile = load_profile(self.lab.write_profile())
        self.known_hosts = self.root / "known_hosts"
        self.known_hosts.write_text(
            "".join(
                f"{host.address} ssh-ed25519 AAAA\n" for host in self.profile.hosts
            ),
            encoding="utf-8",
        )
        self.ssh_log = self.root / "ssh.log"

    def capture(self, **knobs: str):
        environment = self.lab.environment(
            FAKE_LAB_SSH_LOG=str(self.ssh_log), **knobs
        )
        with mock.patch.dict(os.environ, environment):
            prober = LabProber(self.profile, workspace=self.root)
            return capture_runtime_snapshot(prober, self.profile, self.known_hosts)

class RuntimeProbeTests(RuntimeFixture, unittest.TestCase):
    def test_cpython_310_is_recorded_for_every_inventory_node(self) -> None:
        snapshot = self.capture()

        self.assertEqual(
            [probe.host for probe in snapshot.probes],
            ["monitor01", "mon02", "osd01"],
        )
        for probe in snapshot.probes:
            self.assertEqual(probe.status, "ok")
            self.assertEqual(probe.exit_code, 0)
            self.assertIsNotNone(probe.runtime)
            assert probe.runtime is not None
            self.assertEqual(probe.runtime.executable, "/usr/bin/python3")
            self.assertEqual(probe.runtime.implementation, "cpython")
            self.assertEqual(
                probe.runtime.version_info.document(),
                {
                    "major": 3,
                    "minor": 10,
                    "micro": 14,
                    "releaselevel": "final",
                    "serial": 0,
                },
            )

    def test_runtime_probe_uses_the_exact_pinned_read_only_ssh_argv(self) -> None:
        self.capture()

        invocations = [
            json.loads(line)
            for line in self.ssh_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual(len(invocations), len(self.profile.hosts))
        for host, arguments in zip(self.profile.hosts, invocations):
            self.assertEqual(
                arguments,
                [
                    "-i",
                    str(self.profile.ssh_key_path),
                    "-o",
                    "BatchMode=yes",
                    "-o",
                    "IdentitiesOnly=yes",
                    "-o",
                    "IdentityAgent=none",
                    "-o",
                    "LogLevel=ERROR",
                    "-o",
                    "ConnectTimeout=10",
                    "-o",
                    "StrictHostKeyChecking=yes",
                    "-o",
                    f"UserKnownHostsFile={self.known_hosts}",
                    self.profile.ssh_target(host),
                    RUNTIME_PROBE_COMMAND,
                ],
            )
        self.assertNotIn(str(self.profile.ssh_key_path), RUNTIME_PROBE_COMMAND)
        self.assertNotIn("cephadm", RUNTIME_PROBE_COMMAND)
        self.assertNotIn("kubectl", RUNTIME_PROBE_COMMAND)
        self.assertEqual(
            shlex.split(RUNTIME_PROBE_COMMAND)[:5],
            ["python3", "-I", "-B", "-S", "-c"],
        )


class RuntimeFailureAndComparisonTests(RuntimeFixture, unittest.TestCase):
    def runtime(self, *, implementation: str = "cpython", minor: int = 10) -> str:
        return json.dumps(
            {
                "executable": "/opt/python/bin/python3",
                "implementation": implementation,
                "version_info": {
                    "major": 3,
                    "minor": minor,
                    "micro": 9,
                    "releaselevel": "final",
                    "serial": 0,
                },
            }
        )

    def outputs(self, **by_address: str) -> str:
        return json.dumps(by_address)

    def test_newer_cpython_and_non_cpython_are_preserved_as_observed(self) -> None:
        snapshot = self.capture(
            FAKE_LAB_RUNTIME_OUTPUTS=self.outputs(
                **{
                    "10.0.0.11": self.runtime(minor=12),
                    "10.0.0.12": self.runtime(implementation="pypy", minor=10),
                }
            )
        )

        monitor, mon02, _ = snapshot.probes
        self.assertIsNotNone(monitor.runtime)
        self.assertIsNotNone(mon02.runtime)
        assert monitor.runtime is not None
        assert mon02.runtime is not None
        self.assertEqual(monitor.runtime.version_info.minor, 12)
        self.assertEqual(mon02.runtime.implementation, "pypy")
        self.assertTrue(all(probe.status == "ok" for probe in snapshot.probes))

    def test_missing_python_malformed_output_and_transport_failure_are_explicit(self) -> None:
        snapshot = self.capture(
            FAKE_LAB_RUNTIME_EXITS=json.dumps(
                {"10.0.0.11": 127, "10.0.0.21": 255}
            ),
            FAKE_LAB_RUNTIME_OUTPUTS=self.outputs(**{"10.0.0.12": "not-json"}),
        )

        self.assertEqual(
            [(probe.host, probe.exit_code, probe.status) for probe in snapshot.probes],
            [
                ("monitor01", 127, "failed"),
                ("mon02", 0, "failed"),
                ("osd01", 255, "failed"),
            ],
        )
        self.assertIn("python3", snapshot.probes[0].detail)
        self.assertIn("malformed", snapshot.probes[1].detail)
        self.assertIn("transport", snapshot.probes[2].detail)

    def test_missing_extra_and_wrongly_typed_fields_fail_closed(self) -> None:
        missing = json.loads(self.runtime())
        del missing["version_info"]["serial"]
        extra = json.loads(self.runtime())
        extra["version_info"]["build"] = "custom"
        wrong_type = json.loads(self.runtime())
        wrong_type["version_info"]["releaselevel"] = []
        snapshot = self.capture(
            FAKE_LAB_RUNTIME_OUTPUTS=self.outputs(
                **{
                    "10.0.0.11": json.dumps(missing),
                    "10.0.0.12": json.dumps(extra),
                    "10.0.0.21": json.dumps(wrong_type),
                }
            )
        )

        self.assertTrue(all(probe.status == "failed" for probe in snapshot.probes))
        self.assertTrue(all(probe.exit_code == 0 for probe in snapshot.probes))
        self.assertTrue(all(probe.runtime is None for probe in snapshot.probes))

    def test_identical_structured_runtime_snapshots_compare_unchanged(self) -> None:
        result, differences = compare_runtime_snapshots(self.capture(), self.capture())
        self.assertEqual(result, RESULT_UNCHANGED)
        self.assertEqual(differences, ())

    def test_any_structured_runtime_field_drift_is_reported(self) -> None:
        before = self.capture()
        after = self.capture(
            FAKE_LAB_RUNTIME_OUTPUTS=self.outputs(
                **{"10.0.0.21": self.runtime(minor=11)}
            )
        )

        result, differences = compare_runtime_snapshots(before, after)
        self.assertEqual(result, RESULT_CHANGED)
        self.assertTrue(any("osd01" in item and "minor" in item for item in differences))

    def test_a_failed_probe_cannot_compare_as_unchanged(self) -> None:
        failed = self.capture(
            FAKE_LAB_RUNTIME_EXITS=json.dumps({"10.0.0.12": 127})
        )
        result, differences = compare_runtime_snapshots(failed, failed)
        self.assertEqual(result, RESULT_UNAVAILABLE)
        self.assertTrue(any("mon02" in item for item in differences))

    def test_many_failed_nodes_still_produce_a_bounded_comparison(self) -> None:
        failed = RuntimeSnapshot(
            tuple(
                RuntimeProbe(f"node-{index}", 255, "failed", None, "transport failed")
                for index in range(75)
            )
        )

        result, differences = compare_runtime_snapshots(failed, failed)
        self.assertEqual(result, RESULT_UNAVAILABLE)
        self.assertLessEqual(len(differences), 51)
        self.assertIn("not listed", differences[-1])

    def test_failure_diagnostics_are_bounded_and_do_not_persist_credentials(self) -> None:
        secret_marker = "PRIVATE KEY"
        snapshot = self.capture(
            FAKE_LAB_RUNTIME_EXITS=json.dumps({"10.0.0.11": 1}),
            FAKE_LAB_RUNTIME_DIAGNOSTICS=json.dumps(
                {"10.0.0.11": secret_marker + "x" * 1000}
            ),
        )

        detail = snapshot.probes[0].detail
        self.assertNotIn(secret_marker, detail)
        self.assertLessEqual(len(detail), 240)


if __name__ == "__main__":
    unittest.main()
