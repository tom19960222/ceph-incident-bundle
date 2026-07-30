from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.lab_fixture import CEPH_FSID, OTHER_FSID, ROOK_FSID, FakeLab, host_fingerprint
from validation.lab_discovery import discover
from validation.lab_profile import load_profile


class DiscoveryTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.lab = FakeLab(Path(self.directory.name))

    def run_discovery(self, path: Path, *, knobs: dict[str, str] | None = None, **options):
        with mock.patch.dict(os.environ, self.lab.environment(**(knobs or {}))):
            return discover(path, **options)


class BootstrapDiscoveryTests(DiscoveryTestCase):
    def test_writes_a_candidate_next_to_a_bootstrap_profile(self) -> None:
        bootstrap = self.lab.write_profile(state="bootstrap", identity=False)
        result = self.run_discovery(bootstrap)
        self.assertEqual(result.status, "discovery-complete")
        self.assertTrue(result.written)
        self.assertEqual(result.candidate_path, bootstrap.parent / "lab.candidate.toml")
        candidate = load_profile(result.candidate_path)
        self.assertEqual(candidate.state, "candidate")
        self.assertEqual(candidate.ceph_fsid, CEPH_FSID)
        self.assertEqual(candidate.rook_fsid, ROOK_FSID)
        self.assertEqual(
            candidate.host("monitor01").ssh_fingerprints,
            (host_fingerprint("10.0.0.11"),),
        )
        self.assertEqual(candidate.host("monitor01").hostname, "monitor01")

    def test_leaves_the_input_profile_untouched(self) -> None:
        bootstrap = self.lab.write_profile(state="bootstrap", identity=False)
        before = bootstrap.read_text(encoding="utf-8")
        self.run_discovery(bootstrap)
        self.assertEqual(bootstrap.read_text(encoding="utf-8"), before)
        self.assertEqual(load_profile(bootstrap).state, "bootstrap")

    def test_the_candidate_records_its_provenance_as_a_comment(self) -> None:
        bootstrap = self.lab.write_profile(state="bootstrap", identity=False)
        result = self.run_discovery(bootstrap)
        text = result.candidate_path.read_text(encoding="utf-8")
        self.assertIn("Lab Profile Candidate", text)
        self.assertIn("lab-profile-discover", text)
        self.assertIn("NOT trusted", text)

    def test_the_candidate_records_what_discovery_observed(self) -> None:
        bootstrap = self.lab.write_profile(state="bootstrap", identity=False)
        result = self.run_discovery(bootstrap)
        text = result.candidate_path.read_text(encoding="utf-8")
        self.assertIn("# Observed:", text)
        self.assertIn("prometheus readiness", text)
        self.assertIn(f"host key monitor01: {host_fingerprint('10.0.0.11')}", text)
        self.assertIn("no recorded identity", text)
        # The review record is comment text, so it cannot change the identity hash.
        self.assertEqual(
            load_profile(result.candidate_path).profile_hash,
            result.candidate.profile_hash,
        )

    def test_the_candidate_is_owner_only(self) -> None:
        bootstrap = self.lab.write_profile(state="bootstrap", identity=False)
        result = self.run_discovery(bootstrap)
        self.assertEqual(result.candidate_path.stat().st_mode & 0o077, 0)

    def test_the_next_action_asks_for_review_then_activation(self) -> None:
        bootstrap = self.lab.write_profile(state="bootstrap", identity=False)
        result = self.run_discovery(bootstrap)
        self.assertIn("lab-profile-activate", result.next_action)
        self.assertIn("CEPH_INCIDENT_LAB_ACTIVATE=1", result.next_action)
        self.assertIn(str(result.candidate_path), result.next_action)

    def test_a_bootstrap_input_has_no_recorded_identity_to_compare(self) -> None:
        bootstrap = self.lab.write_profile(state="bootstrap", identity=False)
        result = self.run_discovery(bootstrap)
        self.assertEqual(result.differences, ())
        self.assertIn("no recorded identity", result.comparison_note)


class ActiveProfileProtectionTests(DiscoveryTestCase):
    def test_never_overwrites_or_activates_the_active_profile(self) -> None:
        active = self.lab.write_profile()
        before = active.read_text(encoding="utf-8")
        result = self.run_discovery(active)
        self.assertTrue(result.written)
        self.assertNotEqual(result.candidate_path, active)
        self.assertEqual(active.read_text(encoding="utf-8"), before)
        self.assertEqual(load_profile(result.candidate_path).state, "candidate")

    def test_refuses_to_write_a_candidate_over_an_active_profile(self) -> None:
        active = self.lab.write_profile()
        other = self.lab.write_profile("other.toml", state="bootstrap", identity=False)
        result = self.run_discovery(other, candidate_path=active)
        self.assertEqual(result.status, "discovery-blocked")
        self.assertFalse(result.written)
        self.assertIn("active", result.blocked_reason or "")
        self.assertIn("lab-profile-discover", result.next_action)

    def test_refuses_to_write_a_candidate_over_an_unreadable_file(self) -> None:
        bootstrap = self.lab.write_profile(state="bootstrap", identity=False)
        occupied = self.lab.profiles / "occupied.candidate.toml"
        occupied.write_text("not a lab profile\n", encoding="utf-8")
        result = self.run_discovery(bootstrap, candidate_path=occupied)
        self.assertEqual(result.status, "discovery-blocked")
        self.assertIn("unreadable", result.blocked_reason or "")
        self.assertEqual(occupied.read_text(encoding="utf-8"), "not a lab profile\n")

    def test_refuses_to_write_a_candidate_over_a_bootstrap_profile(self) -> None:
        bootstrap = self.lab.write_profile(state="bootstrap", identity=False)
        other = self.lab.write_profile(
            "other.candidate.toml", state="bootstrap", identity=False
        )
        result = self.run_discovery(bootstrap, candidate_path=other)
        self.assertEqual(result.status, "discovery-blocked")
        self.assertIn("bootstrap", result.blocked_reason or "")

    def test_refuses_to_write_a_candidate_over_its_own_input(self) -> None:
        bootstrap = self.lab.write_profile(state="bootstrap", identity=False)
        result = self.run_discovery(bootstrap, candidate_path=bootstrap)
        self.assertEqual(result.status, "discovery-blocked")
        self.assertFalse(result.written)
        self.assertIn("input", result.blocked_reason or "")

    def test_refuses_to_replace_an_existing_candidate_without_being_asked(self) -> None:
        bootstrap = self.lab.write_profile(state="bootstrap", identity=False)
        first = self.run_discovery(bootstrap)
        marker = "# reviewed by hand\n"
        first.candidate_path.write_text(
            marker + first.candidate_path.read_text(encoding="utf-8"), encoding="utf-8"
        )
        second = self.run_discovery(bootstrap)
        self.assertEqual(second.status, "discovery-blocked")
        self.assertFalse(second.written)
        self.assertIn("already exists", second.blocked_reason or "")
        self.assertIn("--replace-candidate", second.next_action)
        self.assertIn(marker, first.candidate_path.read_text(encoding="utf-8"))

    def test_replaces_an_existing_candidate_when_asked(self) -> None:
        bootstrap = self.lab.write_profile(state="bootstrap", identity=False)
        first = self.run_discovery(bootstrap)
        second = self.run_discovery(bootstrap, replace_candidate=True)
        self.assertEqual(second.status, "discovery-complete")
        self.assertTrue(second.written)
        self.assertEqual(second.candidate_path, first.candidate_path)


class IdentityDriftTests(DiscoveryTestCase):
    def test_reports_a_changed_ceph_fsid(self) -> None:
        active = self.lab.write_profile()
        result = self.run_discovery(active, knobs={"FAKE_LAB_CEPH_FSID": OTHER_FSID})
        self.assertEqual(result.status, "discovery-complete")
        self.assertIn(
            f"ceph fsid changed: {CEPH_FSID} -> {OTHER_FSID}", result.differences
        )
        self.assertIn("difference", result.next_action)

    def test_reports_a_rotated_host_key(self) -> None:
        active = self.lab.write_profile()
        keys = json.dumps({"10.0.0.11": [["ssh-ed25519", "rebuilt-lab-key"]]})
        result = self.run_discovery(active, knobs={"FAKE_LAB_HOST_KEYS": keys})
        self.assertTrue(any("monitor01 host keys changed" in item for item in result.differences))

    def test_the_candidate_records_every_difference_for_review(self) -> None:
        active = self.lab.write_profile()
        result = self.run_discovery(active, knobs={"FAKE_LAB_CEPH_FSID": OTHER_FSID})
        text = result.candidate_path.read_text(encoding="utf-8")
        self.assertIn(f"# Comparison: {result.comparison_note}", text)
        for difference in result.differences:
            self.assertIn(f"#   difference: {difference}", text)

    def test_reports_a_changed_hostname(self) -> None:
        active = self.lab.write_profile()
        result = self.run_discovery(
            active, knobs={"FAKE_LAB_HOSTNAMES": json.dumps({"10.0.0.11": "renamed"})}
        )
        self.assertIn("monitor01 hostname changed: monitor01 -> renamed", result.differences)

    def test_no_differences_when_the_lab_still_matches(self) -> None:
        active = self.lab.write_profile()
        result = self.run_discovery(active)
        self.assertEqual(result.differences, ())
        self.assertEqual(
            load_profile(active).profile_hash.split(":")[0],
            result.candidate.profile_hash.split(":")[0],
        )


class IncompleteDiscoveryTests(DiscoveryTestCase):
    def test_writes_no_candidate_when_a_host_offers_no_key(self) -> None:
        bootstrap = self.lab.write_profile(state="bootstrap", identity=False)
        result = self.run_discovery(
            bootstrap, knobs={"FAKE_LAB_KEYSCAN_SILENT": "10.0.0.12"}
        )
        self.assertEqual(result.status, "discovery-incomplete")
        self.assertFalse(result.written)
        self.assertIsNone(result.candidate)
        self.assertFalse(result.candidate_path.exists())
        self.assertIn("mon02", result.next_action)

    def test_writes_no_candidate_when_ceph_identity_is_unreadable(self) -> None:
        bootstrap = self.lab.write_profile(state="bootstrap", identity=False)
        result = self.run_discovery(bootstrap, knobs={"FAKE_LAB_CEPH_FAIL": "1"})
        self.assertEqual(result.status, "discovery-incomplete")
        self.assertFalse(result.written)
        self.assertIn("ceph fsid", result.next_action)

    def test_writes_no_candidate_when_prometheus_is_not_ready(self) -> None:
        bootstrap = self.lab.write_profile(state="bootstrap", identity=False)
        result = self.run_discovery(bootstrap, knobs={"FAKE_LAB_PROM_MODE": "down"})
        self.assertEqual(result.status, "discovery-incomplete")
        self.assertFalse(result.written)
        self.assertIn("prometheus", result.next_action.lower())

    def test_reports_every_finding_even_when_one_fails(self) -> None:
        bootstrap = self.lab.write_profile(state="bootstrap", identity=False)
        result = self.run_discovery(bootstrap, knobs={"FAKE_LAB_ROOK_MODE": "empty"})
        subjects = [finding.subject for finding in result.findings]
        self.assertIn("rook fsid", subjects)
        self.assertIn("host key monitor01", subjects)
        self.assertIn("prometheus readiness", subjects)
        self.assertEqual(
            [finding.subject for finding in result.findings if not finding.ok],
            ["rook fsid"],
        )


class SecretBoundaryTests(DiscoveryTestCase):
    def test_no_credential_content_reaches_the_candidate(self) -> None:
        bootstrap = self.lab.write_profile(state="bootstrap", identity=False)
        self.lab.ssh_key.write_text(
            "-----BEGIN OPENSSH PRIVATE KEY-----\nsecret\n", encoding="utf-8"
        )
        result = self.run_discovery(bootstrap)
        text = result.candidate_path.read_text(encoding="utf-8")
        self.assertNotIn("-----BEGIN", text)
        self.assertIn(str(self.lab.ssh_key), text)

    def test_exactly_one_next_action_is_reported(self) -> None:
        bootstrap = self.lab.write_profile(state="bootstrap", identity=False)
        for knobs in ({}, {"FAKE_LAB_CEPH_FAIL": "1"}):
            with self.subTest(knobs=knobs):
                result = self.run_discovery(
                    bootstrap, knobs=knobs, replace_candidate=True
                )
                self.assertIsInstance(result.next_action, str)
                self.assertTrue(result.next_action.strip())
                self.assertNotIn("\n", result.next_action)


if __name__ == "__main__":
    unittest.main()
