from __future__ import annotations

import base64
import hashlib
import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.lab_fixture import CEPH_FSID, ROOK_FSID, FakeLab, host_fingerprint
from validation.lab_probe import LabProber, bounded_diagnostic, fingerprint_of
from validation.lab_profile import load_profile


class ProbeTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.lab = FakeLab(Path(self.directory.name))
        self.workspace = Path(self.directory.name) / "workspace"
        self.workspace.mkdir()
        self.ssh_log = Path(self.directory.name) / "ssh.log"
        self.keyscan_log = Path(self.directory.name) / "keyscan.log"

    def prober(self, **knobs: str) -> LabProber:
        profile = load_profile(self.lab.write_profile())
        patched = mock.patch.dict(
            os.environ,
            self.lab.environment(
                FAKE_LAB_SSH_LOG=str(self.ssh_log),
                FAKE_LAB_KEYSCAN_LOG=str(self.keyscan_log),
                **knobs,
            ),
        )
        patched.start()
        self.addCleanup(patched.stop)
        self.profile = profile
        return LabProber(profile, workspace=self.workspace)

    def logged(self, path: Path) -> list[list[str]]:
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]

    def pinned_known_hosts(self, prober: LabProber) -> Path:
        scans = [prober.scan_host_key(host) for host in self.profile.hosts]
        return prober.write_known_hosts(scans)


class HostKeyScanTests(ProbeTestCase):
    def test_scans_a_host_key_without_touching_the_operators_known_hosts(self) -> None:
        prober = self.prober()
        scan = prober.scan_host_key(self.profile.host("monitor01"))
        self.assertEqual(scan.fingerprints, (host_fingerprint("10.0.0.11"),))
        self.assertEqual(scan.keys[0].key_type, "ssh-ed25519")
        self.assertEqual(
            self.logged(self.keyscan_log), [["-T", "10", "--", "10.0.0.11"]]
        )

    def test_drops_scan_lines_that_are_not_a_plain_host_key(self) -> None:
        prober = self.prober(FAKE_LAB_KEYSCAN_JUNK="10.0.0.11")
        scan = prober.scan_host_key(self.profile.host("monitor01"))
        self.assertEqual(scan.fingerprints, (host_fingerprint("10.0.0.11"),))
        self.assertIn("unusable scan line", scan.detail)
        written = self.pinned_known_hosts(prober).read_text(encoding="utf-8")
        self.assertNotIn("@cert-authority", written)

    def test_reports_a_host_that_offers_no_key(self) -> None:
        prober = self.prober(FAKE_LAB_KEYSCAN_SILENT="10.0.0.11")
        scan = prober.scan_host_key(self.profile.host("monitor01"))
        self.assertEqual(scan.keys, ())
        self.assertIn("no host key offered", scan.detail)

    def test_deduplicates_repeated_keys(self) -> None:
        keys = json.dumps({"10.0.0.11": [["ssh-ed25519", "s"], ["ssh-ed25519", "s"]]})
        prober = self.prober(FAKE_LAB_HOST_KEYS=keys)
        scan = prober.scan_host_key(self.profile.host("monitor01"))
        self.assertEqual(len(scan.fingerprints), 1)


class KnownHostsTests(ProbeTestCase):
    def test_writes_only_accepted_fingerprints(self) -> None:
        prober = self.prober()
        scans = [prober.scan_host_key(host) for host in self.profile.hosts]
        path = prober.write_known_hosts(
            scans, accepted={"monitor01": (host_fingerprint("10.0.0.11"),)}
        )
        lines = path.read_text(encoding="utf-8").splitlines()
        self.assertEqual([line.split(" ")[0] for line in lines], ["10.0.0.11"])

    def test_the_known_hosts_file_is_owner_only(self) -> None:
        prober = self.prober()
        path = self.pinned_known_hosts(prober)
        self.assertEqual(path.stat().st_mode & 0o077, 0)


class HostnameProbeTests(ProbeTestCase):
    def test_reads_the_hostname_over_a_pinned_connection(self) -> None:
        prober = self.prober(FAKE_LAB_HOSTNAMES=json.dumps({"10.0.0.11": "monitor01"}))
        known_hosts = self.pinned_known_hosts(prober)
        outcome = prober.read_hostname(self.profile.host("monitor01"), known_hosts)
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.value, "monitor01")
        self.assertEqual(
            self.logged(self.ssh_log)[0][-2:], ["operator@10.0.0.11", "hostname"]
        )

    def test_pins_host_keys_instead_of_accepting_new_ones(self) -> None:
        prober = self.prober()
        known_hosts = self.pinned_known_hosts(prober)
        prober.read_hostname(self.profile.host("monitor01"), known_hosts)
        options = self.logged(self.ssh_log)[0]
        self.assertIn("StrictHostKeyChecking=yes", options)
        self.assertNotIn("StrictHostKeyChecking=accept-new", options)
        self.assertIn(f"UserKnownHostsFile={known_hosts}", options)

    def test_an_unpinned_host_cannot_be_reached(self) -> None:
        prober = self.prober()
        empty = prober.write_known_hosts([], name="empty_known_hosts")
        outcome = prober.read_hostname(self.profile.host("monitor01"), empty)
        self.assertFalse(outcome.ok)
        self.assertIn("Host key verification failed", outcome.detail)

    def test_reports_an_unreachable_host(self) -> None:
        prober = self.prober(FAKE_LAB_SSH_UNREACHABLE="10.0.0.11")
        known_hosts = self.pinned_known_hosts(prober)
        outcome = prober.read_hostname(self.profile.host("monitor01"), known_hosts)
        self.assertFalse(outcome.ok)
        self.assertIn("Connection refused", outcome.detail)


class CephIdentityProbeTests(ProbeTestCase):
    def test_reads_the_fsid_with_the_direct_ceph_runner(self) -> None:
        prober = self.prober()
        outcome = prober.read_ceph_fsid(self.pinned_known_hosts(prober))
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.value, CEPH_FSID)
        self.assertEqual([entry[-1] for entry in self.logged(self.ssh_log)], ["ceph fsid"])

    def test_falls_back_to_sudo_but_never_to_cephadm_shell(self) -> None:
        prober = self.prober(FAKE_LAB_CEPH_DIRECT_FAIL="1")
        outcome = prober.read_ceph_fsid(self.pinned_known_hosts(prober))
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.value, CEPH_FSID)
        remote_commands = [entry[-1] for entry in self.logged(self.ssh_log)]
        self.assertEqual(remote_commands, ["ceph fsid", "sudo -n ceph fsid"])
        self.assertFalse(any("cephadm" in command for command in remote_commands))

    def test_reports_both_runner_failures(self) -> None:
        prober = self.prober(FAKE_LAB_CEPH_FAIL="1")
        outcome = prober.read_ceph_fsid(self.pinned_known_hosts(prober))
        self.assertFalse(outcome.ok)
        self.assertIn("ceph fsid", outcome.detail)
        self.assertIn("sudo -n ceph fsid", outcome.detail)


class RookIdentityProbeTests(ProbeTestCase):
    def test_reads_the_cephcluster_fsid_with_a_local_read_only_get(self) -> None:
        prober = self.prober()
        outcome = prober.read_rook_fsid()
        self.assertTrue(outcome.ok)
        self.assertEqual(outcome.value, ROOK_FSID)

    def test_fails_closed_on_every_unusable_answer(self) -> None:
        for mode, expected in (
            ("empty", "no CephCluster in namespace rook-ceph"),
            ("nostatus", "no CephCluster reports status.ceph.fsid"),
            ("two", "different CephCluster FSIDs"),
            ("fail", "kubectl get: exit 1"),
            ("badjson", "unparseable CephCluster list"),
        ):
            with self.subTest(mode=mode):
                prober = self.prober(FAKE_LAB_ROOK_MODE=mode)
                outcome = prober.read_rook_fsid()
                self.assertFalse(outcome.ok)
                self.assertIn(expected, outcome.detail)


class PrometheusProbeTests(ProbeTestCase):
    def test_confirms_readiness(self) -> None:
        prober = self.prober()
        outcome = prober.check_prometheus_ready()
        self.assertTrue(outcome.ok)
        self.assertIn("Ready", outcome.detail)

    def test_fails_closed_when_prometheus_is_down_or_not_ready(self) -> None:
        for mode in ("down", "notready"):
            with self.subTest(mode=mode):
                prober = self.prober(FAKE_LAB_PROM_MODE=mode)
                outcome = prober.check_prometheus_ready()
                self.assertFalse(outcome.ok)
                self.assertIn("curl", outcome.detail)


class MissingCommandTests(ProbeTestCase):
    def test_a_missing_workstation_command_is_a_failed_probe(self) -> None:
        prober = self.prober()
        with mock.patch.dict(os.environ, {"PATH": str(self.workspace)}):
            scan = prober.scan_host_key(self.profile.host("monitor01"))
            rook = prober.read_rook_fsid()
            prometheus = prober.check_prometheus_ready()
        self.assertEqual(scan.keys, ())
        self.assertIn("command not found", rook.detail)
        self.assertIn("command not found", prometheus.detail)


class HelperTests(unittest.TestCase):
    def test_fingerprint_matches_the_openssh_form(self) -> None:
        # `ssh-keygen -lf` prints unpadded base64 of the SHA256 of the key blob.
        digest = hashlib.sha256(base64.b64decode("YWJj")).digest()
        expected = "SHA256:" + base64.b64encode(digest).decode("ascii").rstrip("=")
        self.assertEqual(fingerprint_of("YWJj"), expected)
        self.assertEqual(len(expected), len("SHA256:") + 43)

    def test_rejects_a_blob_that_is_not_base64(self) -> None:
        with self.assertRaises(ValueError):
            fingerprint_of("not base64!!")

    def test_bounded_diagnostic_keeps_one_short_line(self) -> None:
        self.assertEqual(bounded_diagnostic("\n\nfirst\nsecond\n"), "first")
        self.assertEqual(len(bounded_diagnostic("x" * 500)), 200)

    def test_bounded_diagnostic_redacts_credential_material(self) -> None:
        self.assertEqual(
            bounded_diagnostic("-----BEGIN OPENSSH PRIVATE KEY-----"),
            "[redacted diagnostic]",
        )
        self.assertEqual(
            bounded_diagnostic("Authorization: Bearer secret"), "[redacted diagnostic]"
        )


if __name__ == "__main__":
    unittest.main()
