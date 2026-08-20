from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timezone
import io
import json
from pathlib import Path
import tarfile
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ceph_incident_bundle.collect import run
from ceph_incident_bundle.collect.bundle import BundlePublicationError


INVENTORY = b"""\
[common]
ssh_user = root
[nodes]
node-a = node-a.example.test
"""

MULTI_NODE_INVENTORY = b"""\
[common]
ssh_user = root
[nodes]
node-a = node-a.example.test
node-b = node-b.example.test
[ceph]
source = node-a
"""


class TopLevelCollectionTests(unittest.TestCase):
    def test_node_problems_are_accumulated_in_inventory_order_after_an_exception(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.ini"
            inventory.write_bytes(MULTI_NODE_INVENTORY)
            stdout = io.StringIO()
            stderr = io.StringIO()

            with (
                patch(
                    "ceph_incident_bundle.collect.collect_node",
                    side_effect=[
                        ["Target Node node-a: connection failed"],
                        RuntimeError("receive failed\nsecond line"),
                    ],
                ) as node_operation,
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                status = run(inventory, "24h", root)

            bundles = list(root.glob("ceph-incident-bundle-*.tar.gz"))
            self.assertEqual(len(bundles), 1)
            with tarfile.open(bundles[0], "r:gz") as archive:
                root_name = bundles[0].name.removesuffix(".tar.gz")
                metadata_file = archive.extractfile(f"{root_name}/collection.json")
                assert metadata_file is not None
                outcome = json.load(metadata_file)["outcome"]

        self.assertEqual(status, 0)
        self.assertEqual(outcome, "partial")
        self.assertEqual(
            [call.args[0].inventory_name for call in node_operation.call_args_list],
            ["node-a", "node-b"],
        )
        self.assertTrue(node_operation.call_args_list[0].kwargs["ceph_allowed"])
        self.assertFalse(node_operation.call_args_list[1].kwargs["ceph_allowed"])
        self.assertIn("Target Node node-a: connection failed", stderr.getvalue())
        self.assertIn(
            "Target Node node-b: unexpected collection failure: "
            "receive failed\\x0asecond line",
            stderr.getvalue(),
        )
        self.assertEqual(stdout.getvalue(), f"{bundles[0].resolve()} (partial)\n")

    def test_unexpected_node_exception_still_publishes_truthful_partial_bundle(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.ini"
            inventory.write_bytes(INVENTORY)
            stdout = io.StringIO()
            stderr = io.StringIO()

            with (
                patch(
                    "ceph_incident_bundle.collect.collect_node",
                    side_effect=RuntimeError("receive failed\nsecond line"),
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                status = run(inventory, "24h", root)

            bundles = list(root.glob("ceph-incident-bundle-*.tar.gz"))
            self.assertEqual(len(bundles), 1)
            with tarfile.open(bundles[0], "r:gz") as archive:
                root_name = bundles[0].name.removesuffix(".tar.gz")
                metadata_file = archive.extractfile(f"{root_name}/collection.json")
                assert metadata_file is not None
                outcome = json.load(metadata_file)["outcome"]
                node_members = [
                    member.name
                    for member in archive.getmembers()
                    if member.name.startswith(f"{root_name}/nodes/")
                ]

        self.assertEqual(status, 0)
        self.assertEqual(outcome, "partial")
        self.assertEqual(node_members, [])
        self.assertEqual(stdout.getvalue(), f"{bundles[0].resolve()} (partial)\n")
        self.assertIn("receive failed\\x0asecond line", stderr.getvalue())
        self.assertNotIn("FAIL: no Incident Bundle delivered", stderr.getvalue())

    def test_invalid_startup_never_reaches_the_ssh_boundary(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.ini"
            inventory.write_bytes(INVENTORY)
            missing_output = root / "missing"
            stdout = io.StringIO()
            stderr = io.StringIO()

            with (
                patch("ceph_incident_bundle.collect.collect_node") as node_operation,
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                status = run(inventory, "yesterday", missing_output)

        self.assertNotEqual(status, 0)
        node_operation.assert_not_called()
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("invalid evidence window", stderr.getvalue())
        self.assertIn("cannot use output directory", stderr.getvalue())
        self.assertTrue(
            stderr.getvalue().endswith("FAIL: no Incident Bundle delivered\n")
        )

    def test_enormous_since_is_controlled_before_workspace_creation(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.ini"
            inventory.write_bytes(INVENTORY)
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch("ceph_incident_bundle.collect.tempfile.mkdtemp") as make_workspace,
                patch("ceph_incident_bundle.collect.collect_node") as node_operation,
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                status = run(inventory, f"{'9' * 5000}h", root)

        self.assertNotEqual(status, 0)
        make_workspace.assert_not_called()
        node_operation.assert_not_called()
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("invalid evidence window", stderr.getvalue())
        self.assertTrue(
            stderr.getvalue().endswith("FAIL: no Incident Bundle delivered\n")
        )

    def test_exact_timestamp_collision_preserves_destination_before_collection(
        self,
    ) -> None:
        started_at = datetime(2026, 8, 15, 1, 2, 3, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.ini"
            inventory.write_bytes(INVENTORY)
            final_path = root / "ceph-incident-bundle-20260815T010203Z.tar.gz"
            final_path.write_bytes(b"existing bundle bytes")
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch("ceph_incident_bundle.collect.datetime") as clock,
                patch(
                    "ceph_incident_bundle.collect.tempfile.mkdtemp"
                ) as make_workspace,
                patch("ceph_incident_bundle.collect.collect_node") as node_operation,
                patch("ceph_incident_bundle.collect.publish_bundle") as publication,
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                clock.now.return_value = started_at
                status = run(inventory, "24h", root)

            final_bytes = final_path.read_bytes()
            candidates = list(root.glob(".*.candidate.*"))

        self.assertNotEqual(status, 0)
        self.assertEqual(final_bytes, b"existing bundle bytes")
        make_workspace.assert_not_called()
        node_operation.assert_not_called()
        publication.assert_not_called()
        self.assertEqual(candidates, [])
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn("final destination already exists", stderr.getvalue())
        self.assertTrue(
            stderr.getvalue().endswith("FAIL: no Incident Bundle delivered\n")
        )

    def test_publication_failure_is_controlled_after_workspace_handoff(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.ini"
            inventory.write_bytes(INVENTORY)
            workspace = root / "owned-workspace"
            workspace.mkdir()
            stdout = io.StringIO()
            stderr = io.StringIO()
            with (
                patch(
                    "ceph_incident_bundle.collect.tempfile.mkdtemp",
                    return_value=str(workspace),
                ),
                patch("ceph_incident_bundle.collect.collect_node", return_value=[]),
                patch(
                    "ceph_incident_bundle.collect.publish_bundle",
                    side_effect=BundlePublicationError(
                        "injected archive write failure"
                    ),
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                status = run(inventory, "24h", root)

            workspace_still_exists = workspace.exists()

        self.assertNotEqual(status, 0)
        self.assertTrue(workspace_still_exists)
        self.assertEqual(stdout.getvalue(), "")
        self.assertIn(
            "cannot publish Incident Bundle: injected archive write failure",
            stderr.getvalue(),
        )
        self.assertTrue(
            stderr.getvalue().endswith("FAIL: no Incident Bundle delivered\n")
        )

if __name__ == "__main__":
    unittest.main()
