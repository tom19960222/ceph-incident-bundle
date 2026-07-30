from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.lab_fixture import CEPH_FSID, OTHER_FSID, ROOK_FSID, FakeLab, host_fingerprint
from validation.lab_preflight import PASS_NEXT_ACTION, preflight


class PreflightTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.lab = FakeLab(Path(self.directory.name))
        self.ssh_log = Path(self.directory.name) / "ssh.log"
        self.kubectl_log = Path(self.directory.name) / "kubectl.log"
        self.curl_log = Path(self.directory.name) / "curl.log"

    def run_preflight(self, path: Path, **knobs: str):
        environment = self.lab.environment(
            FAKE_LAB_SSH_LOG=str(self.ssh_log),
            FAKE_LAB_KUBECTL_LOG=str(self.kubectl_log),
            FAKE_LAB_CURL_LOG=str(self.curl_log),
            **knobs,
        )
        with mock.patch.dict(os.environ, environment):
            return preflight(path)

    def ssh_commands(self) -> list[str]:
        if not self.ssh_log.exists():
            return []
        return [
            json.loads(line)[-1]
            for line in self.ssh_log.read_text(encoding="utf-8").splitlines()
        ]

    def checks(self, result) -> dict[str, bool]:
        return {check.name: check.ok for check in result.checks}


class PassingPreflightTests(PreflightTestCase):
    def test_passes_on_a_matching_lab(self) -> None:
        result = self.run_preflight(self.lab.write_profile())
        self.assertEqual(result.status, "preflight-pass")
        self.assertTrue(result.ok)
        self.assertEqual(
            [check.name for check in result.checks],
            [
                "profile-state",
                "credential-paths",
                "ssh-fingerprints",
                "required-hosts",
                "ceph-identity",
                "rook-identity",
                "prometheus-readiness",
            ],
        )
        self.assertTrue(all(check.ok for check in result.checks))

    def test_records_the_verified_identity_for_the_report(self) -> None:
        result = self.run_preflight(self.lab.write_profile())
        self.assertEqual(result.identity["ceph_fsid"], CEPH_FSID)
        self.assertEqual(result.identity["rook_fsid"], ROOK_FSID)
        self.assertEqual(result.identity["prometheus"], {"url": "http://10.0.0.11:9095", "ready": True})
        self.assertEqual(
            result.identity["hosts"][0],
            {
                "name": "monitor01",
                "address": "10.0.0.11",
                "hostname": "monitor01",
                "ssh_fingerprints_verified": [host_fingerprint("10.0.0.11")],
            },
        )

    def test_a_pass_is_not_qualification_evidence(self) -> None:
        result = self.run_preflight(self.lab.write_profile())
        self.assertEqual(result.next_action, PASS_NEXT_ACTION)
        self.assertIn("#20", result.next_action)
        self.assertIn("not implemented", result.next_action)

    def test_pins_host_keys_and_never_accepts_new_ones(self) -> None:
        self.run_preflight(self.lab.write_profile())
        options = [
            option
            for line in self.ssh_log.read_text(encoding="utf-8").splitlines()
            for option in json.loads(line)
        ]
        self.assertIn("StrictHostKeyChecking=yes", options)
        self.assertNotIn("StrictHostKeyChecking=accept-new", options)
        self.assertFalse(any(str(Path.home()) in option for option in options))

    def test_uses_only_read_only_commands(self) -> None:
        self.run_preflight(self.lab.write_profile())
        self.assertEqual(
            sorted(set(self.ssh_commands())), ["ceph fsid", "hostname"]
        )
        kubectl = [
            json.loads(line)
            for line in self.kubectl_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertEqual([entry[4] for entry in kubectl], ["get"])


class ProfileStateTests(PreflightTestCase):
    def test_refuses_a_bootstrap_profile_without_connecting(self) -> None:
        result = self.run_preflight(
            self.lab.write_profile(state="bootstrap", identity=False)
        )
        self.assertEqual(result.status, "profile-not-active")
        self.assertEqual(self.ssh_commands(), [])
        self.assertIn("lab-profile-discover", result.next_action)

    def test_refuses_a_candidate_profile_without_connecting(self) -> None:
        result = self.run_preflight(self.lab.write_profile(state="candidate"))
        self.assertEqual(result.status, "profile-not-active")
        self.assertEqual(self.ssh_commands(), [])
        self.assertIn("lab-profile-activate", result.next_action)


class CredentialPathTests(PreflightTestCase):
    def test_refuses_a_missing_ssh_key_without_connecting(self) -> None:
        profile = self.lab.write_profile(ssh_key=self.lab.credentials / "absent")
        result = self.run_preflight(profile)
        self.assertEqual(result.status, "credential-path-invalid")
        self.assertEqual(self.ssh_commands(), [])
        self.assertIn("does not exist", result.blocked_reason or "")

    def test_refuses_a_group_readable_ssh_key(self) -> None:
        self.lab.ssh_key.chmod(0o640)
        result = self.run_preflight(self.lab.write_profile())
        self.assertEqual(result.status, "credential-path-invalid")
        self.assertIn("readable by other users", result.blocked_reason or "")

    def test_refuses_a_missing_kubeconfig(self) -> None:
        profile = self.lab.write_profile(kubeconfig=self.lab.credentials / "absent")
        result = self.run_preflight(profile)
        self.assertEqual(result.status, "credential-path-invalid")
        self.assertIn("kubeconfig", result.blocked_reason or "")

    def test_refuses_a_credential_path_that_is_a_directory(self) -> None:
        profile = self.lab.write_profile(ssh_key=self.lab.credentials)
        result = self.run_preflight(profile)
        self.assertEqual(result.status, "credential-path-invalid")
        self.assertIn("not a regular file", result.blocked_reason or "")

    def test_follows_a_symlinked_credential_to_its_target(self) -> None:
        link = self.lab.credentials / "linked-key"
        link.symlink_to(self.lab.ssh_key)
        result = self.run_preflight(self.lab.write_profile(ssh_key=link))
        self.assertTrue(result.ok, result.blocked_reason)
        broken = self.lab.credentials / "broken-key"
        broken.symlink_to(self.lab.credentials / "absent")
        result = self.run_preflight(self.lab.write_profile(ssh_key=broken))
        self.assertEqual(result.status, "credential-path-invalid")


class FingerprintTests(PreflightTestCase):
    def test_a_rotated_host_key_stops_the_run_before_any_collect_probe(self) -> None:
        keys = json.dumps({"10.0.0.11": [["ssh-ed25519", "rebuilt-lab-key"]]})
        result = self.run_preflight(self.lab.write_profile(), FAKE_LAB_HOST_KEYS=keys)
        self.assertEqual(result.status, "ssh-fingerprint-mismatch")
        self.assertEqual(self.ssh_commands(), [])
        self.assertIn("monitor01 offered untrusted host key", result.blocked_reason or "")

    def test_never_suggests_editing_the_recorded_fingerprints(self) -> None:
        keys = json.dumps({"10.0.0.11": [["ssh-ed25519", "rebuilt-lab-key"]]})
        result = self.run_preflight(self.lab.write_profile(), FAKE_LAB_HOST_KEYS=keys)
        self.assertIn("lab-profile-discover", result.next_action)
        self.assertIn("never edit the recorded fingerprints", result.next_action)

    def test_a_host_that_offers_no_key_fails_closed(self) -> None:
        result = self.run_preflight(
            self.lab.write_profile(), FAKE_LAB_KEYSCAN_SILENT="10.0.0.21"
        )
        self.assertEqual(result.status, "ssh-fingerprint-mismatch")
        self.assertIn("osd01", result.blocked_reason or "")

    def test_reports_every_mismatching_host_in_one_run(self) -> None:
        keys = json.dumps(
            {
                "10.0.0.11": [["ssh-ed25519", "rebuilt-a"]],
                "10.0.0.21": [["ssh-ed25519", "rebuilt-b"]],
            }
        )
        result = self.run_preflight(self.lab.write_profile(), FAKE_LAB_HOST_KEYS=keys)
        self.assertIn("monitor01", result.blocked_reason or "")
        self.assertIn("osd01", result.blocked_reason or "")

    def test_an_extra_untrusted_key_type_is_a_mismatch(self) -> None:
        keys = json.dumps(
            {
                "10.0.0.11": [
                    ["ssh-ed25519", "fake-host-key-10.0.0.11"],
                    ["ssh-rsa", "an-untrusted-second-key"],
                ]
            }
        )
        result = self.run_preflight(self.lab.write_profile(), FAKE_LAB_HOST_KEYS=keys)
        self.assertEqual(result.status, "ssh-fingerprint-mismatch")

    def test_a_trusted_key_the_host_no_longer_offers_is_not_a_mismatch(self) -> None:
        profile = self.lab.write_profile(
            fingerprints={
                "monitor01": (
                    host_fingerprint("10.0.0.11"),
                    host_fingerprint("10.0.0.99"),
                )
            }
        )
        self.assertTrue(self.run_preflight(profile).ok)


class HostIdentityTests(PreflightTestCase):
    def test_a_renamed_host_stops_the_run(self) -> None:
        result = self.run_preflight(
            self.lab.write_profile(),
            FAKE_LAB_HOSTNAMES=json.dumps({"10.0.0.12": "renamed"}),
        )
        self.assertEqual(result.status, "host-identity-mismatch")
        self.assertIn("reports hostname renamed", result.blocked_reason or "")
        self.assertNotIn("ceph fsid", self.ssh_commands())

    def test_an_unreachable_host_stops_the_run(self) -> None:
        result = self.run_preflight(
            self.lab.write_profile(), FAKE_LAB_SSH_UNREACHABLE="10.0.0.12"
        )
        self.assertEqual(result.status, "host-unreachable")
        self.assertIn("Connection refused", result.blocked_reason or "")
        self.assertNotIn("ceph fsid", self.ssh_commands())


class ClusterIdentityTests(PreflightTestCase):
    def test_a_different_ceph_cluster_stops_the_run(self) -> None:
        result = self.run_preflight(
            self.lab.write_profile(), FAKE_LAB_CEPH_FSID=OTHER_FSID
        )
        self.assertEqual(result.status, "ceph-identity-mismatch")
        self.assertIn("different Ceph cluster", result.next_action)
        self.assertFalse(self.kubectl_log.exists())

    def test_an_unreadable_ceph_identity_stops_the_run(self) -> None:
        result = self.run_preflight(self.lab.write_profile(), FAKE_LAB_CEPH_FAIL="1")
        self.assertEqual(result.status, "ceph-identity-mismatch")
        self.assertIn("could not read the Ceph FSID", result.blocked_reason or "")

    def test_a_different_rook_cluster_stops_the_run(self) -> None:
        result = self.run_preflight(
            self.lab.write_profile(), FAKE_LAB_ROOK_FSID=OTHER_FSID
        )
        self.assertEqual(result.status, "rook-identity-mismatch")
        self.assertFalse(self.curl_log.exists())

    def test_a_missing_cephcluster_stops_the_run(self) -> None:
        result = self.run_preflight(self.lab.write_profile(), FAKE_LAB_ROOK_MODE="empty")
        self.assertEqual(result.status, "rook-identity-mismatch")
        self.assertIn("no CephCluster", result.blocked_reason or "")


class PrometheusTests(PreflightTestCase):
    def test_an_unready_prometheus_fails_the_preflight(self) -> None:
        for mode in ("down", "notready"):
            with self.subTest(mode=mode):
                result = self.run_preflight(
                    self.lab.write_profile(), FAKE_LAB_PROM_MODE=mode
                )
                self.assertEqual(result.status, "prometheus-not-ready")
                self.assertIn("is not ready", result.blocked_reason or "")


class ReportingTests(PreflightTestCase):
    def test_every_outcome_carries_exactly_one_next_action(self) -> None:
        cases: tuple[dict[str, str], ...] = (
            {},
            {"FAKE_LAB_CEPH_FSID": OTHER_FSID},
            {"FAKE_LAB_ROOK_MODE": "empty"},
            {"FAKE_LAB_PROM_MODE": "down"},
            {"FAKE_LAB_SSH_UNREACHABLE": "10.0.0.11"},
        )
        for knobs in cases:
            with self.subTest(knobs=knobs):
                result = self.run_preflight(self.lab.write_profile(), **knobs)
                self.assertTrue(result.next_action.strip())
                self.assertNotIn("\n", result.next_action)

    def test_the_summary_carries_no_credential_content(self) -> None:
        result = self.run_preflight(self.lab.write_profile())
        serialised = json.dumps(result.summary())
        self.assertNotIn("-----BEGIN", serialised)
        self.assertNotIn("placeholder", serialised)
        self.assertIn("profile_hash", serialised)


if __name__ == "__main__":
    unittest.main()
