from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.lab_fixture import CEPH_FSID, OTHER_FSID, FakeLab, host_fingerprint
from validation.lab_activation import ACTIVATION_LOG_NAME, activate
from validation.lab_discovery import discover
from validation.lab_profile import load_profile


class ActivationTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.lab = FakeLab(Path(self.directory.name))

    def candidate_for(self, bootstrap: Path, **knobs: str) -> Path:
        with mock.patch.dict(os.environ, self.lab.environment(**knobs)):
            result = discover(bootstrap, replace_candidate=True)
        self.assertTrue(result.written, result.next_action)
        return result.candidate_path

    def bootstrap(self) -> Path:
        return self.lab.write_profile(state="bootstrap", identity=False)


class ActivationTests(ActivationTestCase):
    def test_promotes_a_reviewed_candidate_to_the_active_profile(self) -> None:
        bootstrap = self.bootstrap()
        candidate = self.candidate_for(bootstrap)
        result = activate(candidate, bootstrap, confirmed=True)
        self.assertEqual(result.status, "activation-complete")
        self.assertTrue(result.activated)
        active = load_profile(bootstrap)
        self.assertEqual(active.state, "active")
        self.assertEqual(active.ceph_fsid, CEPH_FSID)
        self.assertEqual(
            active.host("monitor01").ssh_fingerprints, (host_fingerprint("10.0.0.11"),)
        )

    def test_the_active_profile_is_owner_only_and_records_its_source(self) -> None:
        bootstrap = self.bootstrap()
        candidate = self.candidate_for(bootstrap)
        activate(candidate, bootstrap, confirmed=True)
        self.assertEqual(bootstrap.stat().st_mode & 0o077, 0)
        text = bootstrap.read_text(encoding="utf-8")
        self.assertIn("Active Lab Profile", text)
        self.assertIn(str(candidate), text)
        self.assertIn("local-only", text)

    def test_the_candidate_is_left_in_place(self) -> None:
        bootstrap = self.bootstrap()
        candidate = self.candidate_for(bootstrap)
        before = candidate.read_text(encoding="utf-8")
        activate(candidate, bootstrap, confirmed=True)
        self.assertEqual(candidate.read_text(encoding="utf-8"), before)

    def test_the_next_action_is_to_re_read_status(self) -> None:
        bootstrap = self.bootstrap()
        candidate = self.candidate_for(bootstrap)
        result = activate(candidate, bootstrap, confirmed=True)
        self.assertIn("lab-status", result.next_action)
        self.assertIn(str(bootstrap), result.next_action)


class AuditTrailTests(ActivationTestCase):
    def test_appends_one_audit_record_per_activation(self) -> None:
        bootstrap = self.bootstrap()
        candidate = self.candidate_for(bootstrap)
        bootstrap_hash = load_profile(bootstrap).profile_hash
        first = activate(candidate, bootstrap, confirmed=True)
        second = activate(candidate, bootstrap, confirmed=True, replace_active=True)
        log = bootstrap.parent / ACTIVATION_LOG_NAME
        self.assertEqual(first.log_path, log)
        records = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(records), 2)
        self.assertEqual(records[0]["previous_profile_hash"], bootstrap_hash)
        self.assertEqual(records[1]["previous_profile_hash"], first.active_hash)
        self.assertEqual(second.previous_hash, first.active_hash)
        self.assertTrue(second.replaced)

    def test_records_no_previous_profile_when_activating_into_a_new_path(self) -> None:
        bootstrap = self.bootstrap()
        candidate = self.candidate_for(bootstrap)
        target = self.lab.profiles / "fresh.toml"
        result = activate(candidate, target, confirmed=True)
        self.assertTrue(result.ok)
        self.assertIsNone(result.previous_hash)
        self.assertFalse(result.replaced)

    def test_the_audit_record_names_the_accepted_identity(self) -> None:
        bootstrap = self.bootstrap()
        candidate = self.candidate_for(bootstrap)
        activate(candidate, bootstrap, confirmed=True)
        log = bootstrap.parent / ACTIVATION_LOG_NAME
        record = json.loads(log.read_text(encoding="utf-8").splitlines()[0])
        self.assertEqual(record["ceph_fsid"], CEPH_FSID)
        self.assertEqual(record["profile_name"], "fake-lab")
        self.assertEqual(
            [host["name"] for host in record["hosts"]], ["monitor01", "mon02", "osd01"]
        )
        self.assertEqual(
            record["hosts"][0]["ssh_fingerprints"], [host_fingerprint("10.0.0.11")]
        )

    def test_the_audit_log_carries_no_credential_content(self) -> None:
        bootstrap = self.bootstrap()
        candidate = self.candidate_for(bootstrap)
        activate(candidate, bootstrap, confirmed=True)
        text = (bootstrap.parent / ACTIVATION_LOG_NAME).read_text(encoding="utf-8")
        self.assertNotIn("-----BEGIN", text)
        self.assertNotIn("private key placeholder", text)

    def test_the_audit_log_is_owner_only(self) -> None:
        bootstrap = self.bootstrap()
        candidate = self.candidate_for(bootstrap)
        activate(candidate, bootstrap, confirmed=True)
        log = bootstrap.parent / ACTIVATION_LOG_NAME
        self.assertEqual(log.stat().st_mode & 0o077, 0)


class ActivationGuardTests(ActivationTestCase):
    def test_requires_an_explicit_confirmation(self) -> None:
        bootstrap = self.bootstrap()
        candidate = self.candidate_for(bootstrap)
        result = activate(candidate, bootstrap, confirmed=False)
        self.assertEqual(result.status, "activation-blocked")
        self.assertFalse(result.activated)
        self.assertIn("CEPH_INCIDENT_LAB_ACTIVATE=1", result.next_action)
        self.assertEqual(load_profile(bootstrap).state, "bootstrap")

    def test_refuses_to_activate_anything_that_is_not_a_candidate(self) -> None:
        bootstrap = self.bootstrap()
        active = self.lab.write_profile("already-active.toml")
        for source, described in ((bootstrap, "bootstrap"), (active, "active")):
            with self.subTest(source=described):
                result = activate(source, self.lab.profiles / "target.toml", confirmed=True)
                self.assertEqual(result.status, "activation-blocked")
                self.assertIn(described, result.blocked_reason or "")
                self.assertFalse((self.lab.profiles / "target.toml").exists())

    def test_refuses_to_replace_an_active_profile_without_a_second_opt_in(self) -> None:
        bootstrap = self.bootstrap()
        candidate = self.candidate_for(bootstrap)
        activate(candidate, bootstrap, confirmed=True)
        first_hash = load_profile(bootstrap).profile_hash
        drifted = self.candidate_for(bootstrap, FAKE_LAB_CEPH_FSID=OTHER_FSID)
        result = activate(drifted, bootstrap, confirmed=True)
        self.assertEqual(result.status, "activation-blocked")
        self.assertIn("already an active Lab Profile", result.blocked_reason or "")
        self.assertIn("--replace-active", result.next_action)
        self.assertEqual(load_profile(bootstrap).profile_hash, first_hash)

    def test_replaces_an_active_profile_when_asked_twice(self) -> None:
        bootstrap = self.bootstrap()
        activate(self.candidate_for(bootstrap), bootstrap, confirmed=True)
        drifted = self.candidate_for(bootstrap, FAKE_LAB_CEPH_FSID=OTHER_FSID)
        result = activate(drifted, bootstrap, confirmed=True, replace_active=True)
        self.assertEqual(result.status, "activation-complete")
        self.assertEqual(load_profile(bootstrap).ceph_fsid, OTHER_FSID)

    def test_refuses_when_the_candidate_and_the_target_are_the_same_file(self) -> None:
        bootstrap = self.bootstrap()
        candidate = self.candidate_for(bootstrap)
        result = activate(candidate, candidate, confirmed=True)
        self.assertEqual(result.status, "activation-blocked")
        self.assertIn("same file", result.blocked_reason or "")

    def test_refuses_to_replace_an_unreadable_target_without_a_second_opt_in(self) -> None:
        bootstrap = self.bootstrap()
        candidate = self.candidate_for(bootstrap)
        target = self.lab.profiles / "broken.toml"
        target.write_text("this is not a lab profile\n", encoding="utf-8")
        result = activate(candidate, target, confirmed=True)
        self.assertEqual(result.status, "activation-blocked")
        self.assertIn("not a readable", result.blocked_reason or "")
        self.assertEqual(target.read_text(encoding="utf-8"), "this is not a lab profile\n")

    def test_exactly_one_next_action_is_reported(self) -> None:
        bootstrap = self.bootstrap()
        candidate = self.candidate_for(bootstrap)
        for confirmed in (False, True):
            with self.subTest(confirmed=confirmed):
                result = activate(candidate, bootstrap, confirmed=confirmed)
                self.assertTrue(result.next_action.strip())
                self.assertNotIn("\n", result.next_action)


if __name__ == "__main__":
    unittest.main()
