"""The stable-state snapshot: what it keeps, what it drops, and when it refuses."""

from __future__ import annotations

import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from tests.lab_fixture import FakeLab
from validation.lab_probe import LabProber
from validation.lab_profile import load_profile
from validation.lab_snapshot import (
    RESULT_CHANGED,
    RESULT_UNCHANGED,
    SNAPSHOT_SCHEMA_VERSION,
    SnapshotUnavailable,
    capture_stable_state,
    compare_snapshots,
    project_config,
    project_crush_topology,
    project_monitors,
    project_pools,
    project_workloads,
)


class ProjectionTests(unittest.TestCase):
    """Filtering is the schema: a projection keeps configuration, not weather."""

    def test_monitor_identity_survives_a_re_election(self) -> None:
        before = project_monitors(
            {
                "epoch": 7,
                "fsid": "abc",
                "modified": "2026-07-31T00:00:00Z",
                "election_epoch": 42,
                "quorum": [0, 1],
                "mons": [{"rank": 0, "name": "a", "public_addr": "10.0.0.1:6789/0"}],
            }
        )
        after = project_monitors(
            {
                "epoch": 9,
                "fsid": "abc",
                "modified": "2026-07-31T02:00:00Z",
                "election_epoch": 51,
                "quorum": [1, 0],
                "mons": [{"rank": 0, "name": "a", "public_addr": "10.0.0.1:6789/0"}],
            }
        )
        self.assertEqual(before, after)

    def test_a_monitor_moving_is_not_hidden(self) -> None:
        document = {"fsid": "abc", "mons": [{"rank": 0, "name": "a", "public_addr": "x"}]}
        moved = {"fsid": "abc", "mons": [{"rank": 0, "name": "a", "public_addr": "y"}]}
        self.assertNotEqual(project_monitors(document), project_monitors(moved))

    def test_an_osd_going_down_is_not_a_configuration_change(self) -> None:
        up = {"nodes": [{"id": 0, "name": "osd.0", "type": "osd", "crush_weight": 1.0, "status": "up", "reweight": 1.0}]}
        down = {"nodes": [{"id": 0, "name": "osd.0", "type": "osd", "crush_weight": 1.0, "status": "down", "reweight": 0.0}]}
        self.assertEqual(project_crush_topology(up), project_crush_topology(down))

    def test_a_crush_weight_change_is_a_configuration_change(self) -> None:
        before = {"nodes": [{"id": 0, "name": "osd.0", "type": "osd", "crush_weight": 1.0}]}
        after = {"nodes": [{"id": 0, "name": "osd.0", "type": "osd", "crush_weight": 0.5}]}
        self.assertNotEqual(project_crush_topology(before), project_crush_topology(after))

    def test_pool_usage_moves_but_pool_configuration_does_not(self) -> None:
        pool = {
            "pool_id": 1,
            "pool_name": "replicapool",
            "size": 3,
            "min_size": 2,
            "pg_num": 32,
            "crush_rule": 0,
            "application_metadata": {"rbd": {}},
        }
        self.assertEqual(
            project_pools([{**pool, "last_change": "10", "stats": {"bytes_used": 1}}]),
            project_pools([{**pool, "last_change": "77", "stats": {"bytes_used": 999}}]),
        )
        self.assertNotEqual(
            project_pools([pool]), project_pools([{**pool, "min_size": 1}])
        )

    def test_a_persisted_config_option_is_the_field_the_gate_cares_about(self) -> None:
        before = project_config([{"section": "global", "name": "debug_ms", "value": "0"}])
        after = project_config([{"section": "global", "name": "debug_ms", "value": "20"}])
        self.assertNotEqual(before, after)

    def test_workload_rescheduling_is_not_a_desired_state_change(self) -> None:
        workload = {
            "kind": "Deployment",
            "metadata": {"name": "operator", "namespace": "rook-ceph"},
            "spec": {"replicas": 1, "template": {"spec": {"containers": [{"image": "rook:v1"}]}}},
        }
        self.assertEqual(
            project_workloads({"items": [{**workload, "status": {"readyReplicas": 1}}]}),
            project_workloads({"items": [{**workload, "status": {"readyReplicas": 0}}]}),
        )

    def test_an_image_or_replica_change_is_reported(self) -> None:
        workload = {
            "kind": "Deployment",
            "metadata": {"name": "operator", "namespace": "rook-ceph"},
            "spec": {"replicas": 1, "template": {"spec": {"containers": [{"image": "rook:v1"}]}}},
        }
        scaled = json.loads(json.dumps(workload))
        scaled["spec"]["replicas"] = 2
        self.assertNotEqual(
            project_workloads({"items": [workload]}),
            project_workloads({"items": [scaled]}),
        )

    def test_ordering_is_not_a_change(self) -> None:
        first = {"section": "global", "name": "a", "value": "1"}
        second = {"section": "global", "name": "b", "value": "2"}
        self.assertEqual(project_config([first, second]), project_config([second, first]))


class CaptureTests(unittest.TestCase):
    def setUp(self) -> None:
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.root = Path(self.directory.name)
        self.lab = FakeLab(self.root)
        self.profile = load_profile(self.lab.write_profile())
        self.known_hosts = self.root / "known_hosts"
        self.known_hosts.write_text(
            "".join(f"{address} ssh-ed25519 AAAA\n" for _, address in
                    (("monitor01", "10.0.0.11"), ("mon02", "10.0.0.12"), ("osd01", "10.0.0.21"))),
            encoding="utf-8",
        )
        self.ssh_log = self.root / "ssh.log"
        self.kubectl_log = self.root / "kubectl.log"

    def capture(self, **knobs: str):
        environment = self.lab.environment(
            FAKE_LAB_SSH_LOG=str(self.ssh_log),
            FAKE_LAB_KUBECTL_LOG=str(self.kubectl_log),
            **knobs,
        )
        with mock.patch.dict(os.environ, environment):
            prober = LabProber(self.profile, workspace=self.root)
            return capture_stable_state(prober, self.profile, self.known_hosts)

    def test_captures_every_declared_source(self) -> None:
        snapshot = self.capture()
        self.assertEqual(snapshot.schema_version, SNAPSHOT_SCHEMA_VERSION)
        self.assertEqual(
            sorted(snapshot.fields),
            [
                "ceph_config",
                "ceph_crush_topology",
                "ceph_monitors",
                "ceph_pools",
                "k8s_daemonsets_rook-ceph",
                "k8s_deployments_rook-ceph",
                "k8s_statefulsets_rook-ceph",
                "rook_cephclusters",
            ],
        )

    def test_uses_only_read_only_commands(self) -> None:
        self.capture()
        remotes = [
            json.loads(line)[-1]
            for line in self.ssh_log.read_text(encoding="utf-8").splitlines()
        ]
        self.assertTrue(all(remote.startswith("ceph ") for remote in remotes), remotes)
        self.assertNotIn("cephadm", " ".join(remotes))
        verbs = {
            json.loads(line)[4]
            for line in self.kubectl_log.read_text(encoding="utf-8").splitlines()
        }
        self.assertEqual(verbs, {"get"})

    def test_two_captures_of_an_idle_lab_are_identical(self) -> None:
        result, _, differences = compare_snapshots(self.capture(), self.capture())
        self.assertEqual(differences, ())
        self.assertEqual(result, RESULT_UNCHANGED)

    def test_a_persisted_option_between_captures_is_reported(self) -> None:
        before = self.capture()
        changed = json.dumps(
            {
                "config dump --format json": [
                    {"section": "global", "name": "osd_pool_default_size", "value": "2"}
                ]
            }
        )
        after = self.capture(FAKE_LAB_CEPH_STATE=changed)
        result, schema, differences = compare_snapshots(before, after)
        self.assertEqual(result, RESULT_CHANGED)
        self.assertEqual(schema, str(SNAPSHOT_SCHEMA_VERSION))
        self.assertTrue(any("ceph_config" in line for line in differences), differences)

    def test_an_unreadable_ceph_source_fails_closed(self) -> None:
        with self.assertRaises(SnapshotUnavailable) as raised:
            self.capture(FAKE_LAB_CEPH_STATE_FAIL="osd tree --format json")
        self.assertIn("ceph_crush_topology", str(raised.exception))

    def test_an_unreadable_workload_source_fails_closed(self) -> None:
        with self.assertRaises(SnapshotUnavailable) as raised:
            self.capture(FAKE_LAB_WORKLOAD_FAIL="daemonsets")
        self.assertIn("k8s_daemonsets_rook-ceph", str(raised.exception))

    def test_a_separate_operator_namespace_is_read_once_more(self) -> None:
        self.profile = load_profile(
            self.lab.write_profile("split.toml", operator_namespace="rook-operators")
        )
        snapshot = self.capture()
        self.assertIn("k8s_deployments_rook-operators", snapshot.fields)
        self.assertIn("k8s_deployments_rook-ceph", snapshot.fields)


if __name__ == "__main__":
    unittest.main()
