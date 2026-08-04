"""The per-node remote residue check: it observes, attributes and never cleans."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.lab_fixture import FakeLab
from validation.lab_probe import RESIDUE_PROBE, LabProber
from validation.lab_profile import load_profile
from validation.lab_residue import (
    RESULT_CLEAN,
    RESULT_RESIDUE,
    ResidueListing,
    ResidueUnavailable,
    compare_residue,
    observe_residue,
)


class ResidueProbeShapeTests(unittest.TestCase):
    def test_the_probe_never_removes_or_signals_anything(self) -> None:
        for verb in ("rm ", "rmdir", "kill", "unlink", "truncate", "mv "):
            with self.subTest(verb=verb):
                self.assertNotIn(verb, RESIDUE_PROBE)

    def test_the_probe_cannot_report_itself(self) -> None:
        # The script's own text shows up in `ps` on the node, so the markers it
        # searches for must not appear in it literally.
        self.assertNotIn("collect-node.sh", RESIDUE_PROBE)
        self.assertNotIn("ceph_incident_node.py", RESIDUE_PROBE)


class ObservationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.lab = FakeLab(self.root)
        self.profile = load_profile(self.lab.write_profile())
        self.known_hosts = self.root / "known_hosts"
        self.known_hosts.write_text(
            "10.0.0.11 ssh-ed25519 AAAA\n10.0.0.12 ssh-ed25519 AAAA\n"
            "10.0.0.21 ssh-ed25519 AAAA\n",
            encoding="utf-8",
        )
        self.ssh_log = self.root / "ssh.log"

    def observe(self, host_name: str = "monitor01", **knobs: str) -> ResidueListing:
        environment = self.lab.environment(FAKE_LAB_SSH_LOG=str(self.ssh_log), **knobs)
        with mock.patch.dict(os.environ, environment):
            prober = LabProber(self.profile, workspace=self.root)
            return observe_residue(prober, self.profile.host(host_name), self.known_hosts)

    def test_a_clean_node_lists_nothing(self) -> None:
        listing = self.observe()
        self.assertEqual(listing.workspaces, ())
        self.assertEqual(listing.processes, ())

    def test_reads_workspaces_and_helper_processes(self) -> None:
        self.lab.leave_residue("10.0.0.11", "workspace\t/tmp/ceph-incident-node.abcd1234")
        self.lab.leave_residue("10.0.0.11", "process\tbash /tmp/x/lib/collect-node.sh --out /tmp/x/out")
        listing = self.observe()
        self.assertEqual(listing.workspaces, ("/tmp/ceph-incident-node.abcd1234",))
        self.assertEqual(len(listing.processes), 1)

    def test_pins_host_keys_like_every_other_probe(self) -> None:
        self.observe()
        options = [
            option
            for line in self.ssh_log.read_text(encoding="utf-8").splitlines()
            for option in json.loads(line)
        ]
        self.assertIn("StrictHostKeyChecking=yes", options)
        self.assertIn(f"UserKnownHostsFile={self.known_hosts}", options)

    def test_an_unreadable_node_fails_closed(self) -> None:
        with self.assertRaises(ResidueUnavailable) as raised:
            self.observe(FAKE_LAB_RESIDUE_FAIL="10.0.0.11")
        self.assertIn("monitor01", str(raised.exception))


class AttributionTests(unittest.TestCase):
    def test_only_what_appeared_during_the_run_is_this_runs_residue(self) -> None:
        baseline = ResidueListing("osd01", ("/tmp/ceph-incident-node.older",), ())
        after = ResidueListing(
            "osd01", ("/tmp/ceph-incident-node.older", "/tmp/ceph-incident-node.newer"), ()
        )
        result, detail = compare_residue(baseline, after)
        self.assertEqual(result, RESULT_RESIDUE)
        self.assertIn("/tmp/ceph-incident-node.newer", detail)
        self.assertNotIn("older", detail)

    def test_a_pre_existing_leftover_is_reported_but_not_blamed(self) -> None:
        baseline = ResidueListing("osd01", ("/tmp/ceph-incident-node.older",), ())
        result, detail = compare_residue(baseline, baseline)
        self.assertEqual(result, RESULT_CLEAN)
        self.assertIn("pre-existing", detail)

    def test_a_workspace_carrying_an_invocation_id_is_named_as_such(self) -> None:
        invocation = "0" * 31 + "7"
        after = ResidueListing("osd01", (f"/tmp/ceph-incident-node-{invocation}-ab",), ())
        result, detail = compare_residue(
            ResidueListing("osd01", (), ()), after, invocation_ids=(invocation,)
        )
        self.assertEqual(result, RESULT_RESIDUE)
        self.assertIn(f"invocation {invocation}", detail)

    def test_a_new_helper_process_is_residue(self) -> None:
        after = ResidueListing("osd01", (), ("python3 /tmp/w/ceph_incident_node.py",))
        result, detail = compare_residue(ResidueListing("osd01", (), ()), after)
        self.assertEqual(result, RESULT_RESIDUE)
        self.assertIn("helper process", detail)


if __name__ == "__main__":
    unittest.main()
