from datetime import datetime, timezone
import json
import os
from pathlib import Path
import tarfile
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ceph_incident_bundle.collect.bundle import (
    BundlePublicationError,
    OWNERSHIP_MARKER,
    publish_bundle,
)


class IncidentBundlePublicationTests(unittest.TestCase):
    def test_admitted_state_is_published_with_the_exact_bundle_surface(self) -> None:
        inventory = b"[common]\nssh_user = root\n[nodes]\nnode-a = node-a.example\n"
        started_at = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            self._write_workspace(workspace, inventory)
            final_path = root / "ceph-incident-bundle-20260814T120000Z.tar.gz"

            cleanup_problem = publish_bundle(
                workspace,
                final_path,
                collector_version="0.1.0",
                started_at=started_at,
                since="24h",
                prior_partial=False,
            )

            with tarfile.open(final_path, "r:gz") as archive:
                members = archive.getmembers()
                names = {member.name for member in members}
                inventory_file = archive.extractfile(
                    "ceph-incident-bundle-20260814T120000Z/inventory.ini"
                )
                metadata_file = archive.extractfile(
                    "ceph-incident-bundle-20260814T120000Z/collection.json"
                )
                hostname_file = archive.extractfile(
                    "ceph-incident-bundle-20260814T120000Z/"
                    "nodes/node-a/probes/hostname/stdout"
                )
                assert inventory_file is not None
                assert metadata_file is not None
                assert hostname_file is not None
                bundled_inventory = inventory_file.read()
                metadata = json.load(metadata_file)
                hostname = hostname_file.read()
            candidates = list(root.glob(".*.candidate.*"))
            workspace_exists = workspace.exists()

        bundle_root = "ceph-incident-bundle-20260814T120000Z"
        self.assertIsNone(cleanup_problem)
        self.assertFalse(workspace_exists)
        self.assertEqual(candidates, [])
        self.assertEqual(bundled_inventory, inventory)
        self.assertEqual(hostname, b"node-a\n")
        self.assertEqual(
            set(metadata),
            {"collector_version", "started_at", "finished_at", "since", "outcome"},
        )
        self.assertEqual(metadata["collector_version"], "0.1.0")
        self.assertEqual(metadata["started_at"], "2026-08-14T12:00:00Z")
        self.assertEqual(metadata["since"], "24h")
        self.assertEqual(metadata["outcome"], "complete")
        self.assertTrue(metadata["finished_at"].endswith("Z"))
        for top_level in ("nodes", "ceph", "kubernetes", "prometheus"):
            self.assertIn(f"{bundle_root}/{top_level}", names)
        self.assertTrue(all(member.isdir() or member.isreg() for member in members))

    def test_existing_final_destination_is_never_replaced(self) -> None:
        inventory = b"[common]\nssh_user = root\n[nodes]\nnode-a = node-a.example\n"
        started_at = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            self._write_workspace(workspace, inventory)
            final_path = root / "ceph-incident-bundle-20260814T120000Z.tar.gz"
            final_path.write_bytes(b"existing")

            with self.assertRaises(BundlePublicationError):
                publish_bundle(
                    workspace,
                    final_path,
                    collector_version="0.1.0",
                    started_at=started_at,
                    since="24h",
                    prior_partial=False,
                )

            final_bytes = final_path.read_bytes()
            candidates = list(root.glob(".*.candidate.*"))

        self.assertEqual(final_bytes, b"existing")
        self.assertEqual(candidates, [])

    def test_candidate_cleanup_failure_rolls_back_the_published_name(self) -> None:
        inventory = b"[common]\nssh_user = root\n[nodes]\nnode-a = node-a.example\n"
        started_at = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
        real_unlink = Path.unlink
        candidate_failure_seen = False

        def fail_first_candidate_unlink(path: Path, *args: object, **kwargs: object) -> None:
            nonlocal candidate_failure_seen
            if ".candidate." in path.name and not candidate_failure_seen:
                candidate_failure_seen = True
                raise OSError("injected candidate cleanup failure")
            real_unlink(path, *args, **kwargs)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            self._write_workspace(workspace, inventory)
            final_path = root / "ceph-incident-bundle-20260814T120000Z.tar.gz"

            with (
                patch("pathlib.Path.unlink", new=fail_first_candidate_unlink),
                self.assertRaises(BundlePublicationError) as caught,
            ):
                publish_bundle(
                    workspace,
                    final_path,
                    collector_version="0.1.0",
                    started_at=started_at,
                    since="24h",
                    prior_partial=False,
                )

            final_exists = final_path.exists()
            candidates = list(root.glob(".*.candidate.*"))

        self.assertTrue(candidate_failure_seen)
        self.assertFalse(final_exists)
        self.assertEqual(candidates, [])
        self.assertIn("final publication was rolled back", str(caught.exception))

    def test_prepublication_candidate_residue_is_reported_with_its_exact_path(self) -> None:
        inventory = b"[common]\nssh_user = root\n[nodes]\nnode-a = node-a.example\n"
        started_at = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
        real_unlink = Path.unlink

        def refuse_candidate_unlink(
            path: Path, *args: object, **kwargs: object
        ) -> None:
            if ".candidate." in path.name:
                raise OSError("injected retained candidate")
            real_unlink(path, *args, **kwargs)

        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            self._write_workspace(workspace, inventory)
            final_path = root / "ceph-incident-bundle-20260814T120000Z.tar.gz"

            with (
                patch("tarfile.open", side_effect=OSError("injected archive failure")),
                patch("pathlib.Path.unlink", new=refuse_candidate_unlink),
                self.assertRaises(BundlePublicationError) as caught,
            ):
                publish_bundle(
                    workspace,
                    final_path,
                    collector_version="0.1.0",
                    started_at=started_at,
                    since="24h",
                    prior_partial=False,
                )

            candidates = list(root.glob(".*.candidate.*"))
            final_exists = final_path.exists()

        self.assertFalse(final_exists)
        self.assertEqual(len(candidates), 1)
        self.assertIn(str(candidates[0]), str(caught.exception))
        self.assertIn("cannot remove private Incident Bundle candidate", str(caught.exception))

    def test_links_fifos_and_ambiguous_components_never_enter_a_bundle(self) -> None:
        inventory = b"[common]\nssh_user = root\n[nodes]\nnode-a = node-a.example\n"
        started_at = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
        for hostile_type in ("symlink", "fifo", "backslash"):
            with self.subTest(hostile_type=hostile_type), TemporaryDirectory() as directory:
                root = Path(directory)
                workspace = root / "workspace"
                self._write_workspace(workspace, inventory)
                files = (
                    workspace
                    / "admitted"
                    / "node-contributions"
                    / "node-a"
                    / "node"
                    / "files"
                )
                hostile = files / ("bad\\name" if hostile_type == "backslash" else "hostile")
                if hostile_type == "symlink":
                    hostile.symlink_to(root / "outside")
                elif hostile_type == "fifo":
                    os.mkfifo(hostile)
                else:
                    hostile.write_bytes(b"x")
                final_path = root / "ceph-incident-bundle-20260814T120000Z.tar.gz"

                with self.assertRaises(BundlePublicationError):
                    publish_bundle(
                        workspace,
                        final_path,
                        collector_version="0.1.0",
                        started_at=started_at,
                        since="24h",
                        prior_partial=False,
                    )

                self.assertFalse(final_path.exists())
                self.assertEqual(list(root.glob(".*.candidate.*")), [])

    def test_prior_problem_is_recorded_as_partial_without_changing_delivery_status(self) -> None:
        inventory = b"[common]\nssh_user = root\n[nodes]\nnode-a = node-a.example\n"
        started_at = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            self._write_workspace(workspace, inventory)
            final_path = root / "ceph-incident-bundle-20260814T120000Z.tar.gz"

            publish_bundle(
                workspace,
                final_path,
                collector_version="0.1.0",
                started_at=started_at,
                since="24h",
                prior_partial=True,
            )

            with tarfile.open(final_path, "r:gz") as archive:
                metadata_file = archive.extractfile(
                    "ceph-incident-bundle-20260814T120000Z/collection.json"
                )
                assert metadata_file is not None
                outcome = json.load(metadata_file)["outcome"]

        self.assertEqual(outcome, "partial")

    @staticmethod
    def _write_workspace(workspace: Path, inventory: bytes) -> None:
        workspace.mkdir()
        (workspace / OWNERSHIP_MARKER).write_text(
            str(workspace.resolve()) + "\n", encoding="utf-8"
        )
        admitted = workspace / "admitted"
        contribution = admitted / "node-contributions" / "node-a" / "node"
        capture = contribution / "probes" / "hostname"
        capture.mkdir(parents=True)
        (capture / "stdout").write_bytes(b"node-a\n")
        (capture / "stderr").write_bytes(b"")
        (capture / "result.json").write_bytes(b"{}\n")
        (contribution / "files").mkdir()
        (admitted / "inventory.ini").write_bytes(inventory)
        (admitted / "kubernetes").mkdir()
        (admitted / "prometheus").mkdir()
        (workspace / "private").mkdir()


if __name__ == "__main__":
    unittest.main()
