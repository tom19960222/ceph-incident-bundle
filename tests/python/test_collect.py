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

KUBERNETES_INVENTORY = b"""\
[common]
ssh_user = root
probe_timeout = 7m
[nodes]
node-a = node-a.example.test
[kubernetes]
context = lab-blue
consumer_namespace = rook-consumer
operator_namespace = rook-operator
"""

PROMETHEUS_INVENTORY = b"""\
[common]
ssh_user = root
[nodes]
node-a = node-a.example.test
[kubernetes]
context = lab-blue
[prometheus]
url = http://127.0.0.1:19090/prometheus
request_timeout = 7s
"""


class TopLevelCollectionTests(unittest.TestCase):
    def test_absent_prometheus_url_skips_capability_cleanly(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.ini"
            inventory.write_bytes(INVENTORY)
            stdout = io.StringIO()
            stderr = io.StringIO()

            with (
                patch("ceph_incident_bundle.collect.collect_node", return_value=[]),
                patch(
                    "ceph_incident_bundle.collect.collect_prometheus"
                ) as prometheus_operation,
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                status = run(inventory, "24h", root)

            bundle = next(root.glob("ceph-incident-bundle-*.tar.gz"))
            with tarfile.open(bundle, "r:gz") as archive:
                root_name = bundle.name.removesuffix(".tar.gz")
                prometheus_members = [
                    member.name
                    for member in archive.getmembers()
                    if member.name.startswith(f"{root_name}/prometheus")
                ]

        self.assertEqual(status, 0)
        prometheus_operation.assert_not_called()
        self.assertEqual(prometheus_members, [f"{root_name}/prometheus"])
        self.assertTrue(stdout.getvalue().endswith(" (complete)\n"))
        self.assertEqual(stderr.getvalue(), "")

    def test_prometheus_runs_after_kubernetes_with_shared_window_controls(self) -> None:
        events: list[str] = []

        def node_operation(*args: object, **kwargs: object) -> list[str]:
            events.append("node")
            return []

        def kubernetes_operation(*args: object, **kwargs: object) -> list[str]:
            events.append("kubernetes")
            contribution = Path(str(kwargs["contribution_directory"]))
            (contribution / "probes").mkdir(parents=True)
            return []

        def prometheus_operation(*args: object, **kwargs: object) -> list[str]:
            events.append("prometheus")
            contribution = Path(str(kwargs["contribution_directory"]))
            (contribution / "buildinfo").mkdir(parents=True)
            return ["Prometheus buildinfo request failed: timeout: stalled"]

        with TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.ini"
            inventory.write_bytes(PROMETHEUS_INVENTORY)
            stdout = io.StringIO()
            stderr = io.StringIO()

            with (
                patch(
                    "ceph_incident_bundle.collect.collect_node",
                    side_effect=node_operation,
                ),
                patch(
                    "ceph_incident_bundle.collect.collect_kubernetes",
                    side_effect=kubernetes_operation,
                ),
                patch(
                    "ceph_incident_bundle.collect.collect_prometheus",
                    side_effect=prometheus_operation,
                ) as prometheus,
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                status = run(inventory, "2d", root)

            call = prometheus.call_args

        self.assertEqual(status, 0)
        self.assertEqual(events, ["node", "kubernetes", "prometheus"])
        self.assertEqual(call.kwargs["url"], "http://127.0.0.1:19090/prometheus")
        self.assertEqual(call.kwargs["since_seconds"], 172800)
        self.assertEqual(call.kwargs["request_timeout_seconds"], 7)
        self.assertEqual(call.kwargs["staging_directory"].name, "prometheus")
        self.assertEqual(call.kwargs["contribution_directory"].name, "prometheus")
        self.assertTrue(stdout.getvalue().endswith(" (partial)\n"))
        self.assertIn("Prometheus buildinfo request failed", stderr.getvalue())

    def test_unexpected_prometheus_exception_keeps_private_staging_out_of_bundle(
        self,
    ) -> None:
        def failed_prometheus(*args: object, **kwargs: object) -> list[str]:
            staging = Path(str(kwargs["staging_directory"]))
            capture = staging / "incomplete" / "buildinfo"
            capture.mkdir(parents=True)
            (capture / "response").write_bytes(b"must stay private")
            raise RuntimeError("HTTP boundary failed\nsecond line")

        with TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.ini"
            inventory.write_bytes(PROMETHEUS_INVENTORY)
            stdout = io.StringIO()
            stderr = io.StringIO()

            with (
                patch("ceph_incident_bundle.collect.collect_node", return_value=[]),
                patch("ceph_incident_bundle.collect.collect_kubernetes", return_value=[]),
                patch(
                    "ceph_incident_bundle.collect.collect_prometheus",
                    side_effect=failed_prometheus,
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                status = run(inventory, "24h", root)

            bundle = next(root.glob("ceph-incident-bundle-*.tar.gz"))
            with tarfile.open(bundle, "r:gz") as archive:
                root_name = bundle.name.removesuffix(".tar.gz")
                metadata_file = archive.extractfile(f"{root_name}/collection.json")
                assert metadata_file is not None
                outcome = json.load(metadata_file)["outcome"]
                prometheus_members = [
                    member.name
                    for member in archive.getmembers()
                    if member.name.startswith(f"{root_name}/prometheus")
                ]
                private_members = [
                    member.name
                    for member in archive.getmembers()
                    if "incomplete" in member.name or "/private/" in member.name
                ]

        self.assertEqual(status, 0)
        self.assertEqual(outcome, "partial")
        self.assertEqual(prometheus_members, [f"{root_name}/prometheus"])
        self.assertEqual(private_members, [])
        self.assertTrue(stdout.getvalue().endswith(" (partial)\n"))
        self.assertIn(
            "Prometheus: unexpected collection failure: "
            "HTTP boundary failed\\x0asecond line",
            stderr.getvalue(),
        )

    def test_absent_kubernetes_context_skips_capability_cleanly(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.ini"
            inventory.write_bytes(INVENTORY)
            stdout = io.StringIO()
            stderr = io.StringIO()

            with (
                patch("ceph_incident_bundle.collect.collect_node", return_value=[]),
                patch(
                    "ceph_incident_bundle.collect.collect_kubernetes"
                ) as kubernetes_operation,
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                status = run(inventory, "24h", root)

            bundle = next(root.glob("ceph-incident-bundle-*.tar.gz"))
            with tarfile.open(bundle, "r:gz") as archive:
                root_name = bundle.name.removesuffix(".tar.gz")
                kubernetes_members = [
                    member.name
                    for member in archive.getmembers()
                    if member.name.startswith(f"{root_name}/kubernetes")
                ]

        self.assertEqual(status, 0)
        kubernetes_operation.assert_not_called()
        self.assertEqual(kubernetes_members, [f"{root_name}/kubernetes"])
        self.assertTrue(stdout.getvalue().endswith(" (complete)\n"))
        self.assertEqual(stderr.getvalue(), "")

    def test_configured_kubernetes_runs_after_nodes_and_returns_partial_problems(
        self,
    ) -> None:
        events: list[str] = []

        def node_operation(*args: object, **kwargs: object) -> list[str]:
            events.append("node")
            return []

        def kubernetes_operation(*args: object, **kwargs: object) -> list[str]:
            events.append("kubernetes")
            contribution = Path(str(kwargs["contribution_directory"]))
            (contribution / "probes").mkdir(parents=True)
            return ["Kubernetes consumer-events Probe failed: outcome=exited exit_code=7"]

        with TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.ini"
            inventory.write_bytes(KUBERNETES_INVENTORY)
            stdout = io.StringIO()
            stderr = io.StringIO()

            with (
                patch(
                    "ceph_incident_bundle.collect.collect_node",
                    side_effect=node_operation,
                ),
                patch(
                    "ceph_incident_bundle.collect.collect_kubernetes",
                    side_effect=kubernetes_operation,
                ) as kubernetes,
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                status = run(inventory, "24h", root)

            bundle = next(root.glob("ceph-incident-bundle-*.tar.gz"))
            call = kubernetes.call_args

        self.assertEqual(status, 0)
        self.assertEqual(events, ["node", "kubernetes"])
        self.assertEqual(call.kwargs["context"], "lab-blue")
        self.assertEqual(call.kwargs["consumer_namespace"], "rook-consumer")
        self.assertEqual(call.kwargs["operator_namespace"], "rook-operator")
        self.assertEqual(call.kwargs["probe_timeout_seconds"], 420)
        self.assertEqual(call.kwargs["staging_directory"].name, "kubernetes")
        self.assertEqual(call.kwargs["contribution_directory"].name, "kubernetes")
        self.assertTrue(stdout.getvalue().endswith(" (partial)\n"))
        self.assertIn("consumer-events Probe failed", stderr.getvalue())
        self.assertNotIn("FAIL: no Incident Bundle delivered", stderr.getvalue())

    def test_unexpected_kubernetes_exception_keeps_publication_isolated(self) -> None:
        def failed_kubernetes(*args: object, **kwargs: object) -> list[str]:
            staging = Path(str(kwargs["staging_directory"]))
            (staging / "incomplete" / "probes" / "private-capture").mkdir(
                parents=True
            )
            (staging / "incomplete" / "probes" / "private-capture" / "stdout").write_bytes(
                b"must stay private"
            )
            raise RuntimeError("kubectl boundary failed\nsecond line")

        with TemporaryDirectory() as directory:
            root = Path(directory)
            inventory = root / "inventory.ini"
            inventory.write_bytes(KUBERNETES_INVENTORY)
            stdout = io.StringIO()
            stderr = io.StringIO()

            with (
                patch("ceph_incident_bundle.collect.collect_node", return_value=[]),
                patch(
                    "ceph_incident_bundle.collect.collect_kubernetes",
                    side_effect=failed_kubernetes,
                ),
                redirect_stdout(stdout),
                redirect_stderr(stderr),
            ):
                status = run(inventory, "24h", root)

            bundle = next(root.glob("ceph-incident-bundle-*.tar.gz"))
            with tarfile.open(bundle, "r:gz") as archive:
                root_name = bundle.name.removesuffix(".tar.gz")
                metadata_file = archive.extractfile(f"{root_name}/collection.json")
                assert metadata_file is not None
                outcome = json.load(metadata_file)["outcome"]
                kubernetes_members = [
                    member.name
                    for member in archive.getmembers()
                    if member.name.startswith(f"{root_name}/kubernetes")
                ]
                private_members = [
                    member.name
                    for member in archive.getmembers()
                    if "private-capture" in member.name or "/private/" in member.name
                ]

        self.assertEqual(status, 0)
        self.assertEqual(outcome, "partial")
        self.assertEqual(kubernetes_members, [f"{root_name}/kubernetes"])
        self.assertEqual(private_members, [])
        self.assertTrue(stdout.getvalue().endswith(" (partial)\n"))
        self.assertIn(
            "Kubernetes: unexpected collection failure: "
            "kubectl boundary failed\\x0asecond line",
            stderr.getvalue(),
        )

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
