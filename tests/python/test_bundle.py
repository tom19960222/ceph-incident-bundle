from datetime import datetime, timezone
import json
import os
from pathlib import Path
import stat
import tarfile
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ceph_incident_bundle.collect.bundle import (
    BundlePublicationError,
    publish_bundle,
)


class IncidentBundlePublicationTests(unittest.TestCase):
    def test_admitted_state_is_published_with_the_exact_bundle_surface(self) -> None:
        inventory = b"[common]\n[nodes]\nnode-a = node-a.example\n"
        started_at = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            self._write_workspace(workspace, inventory)
            final_path = root / "ceph-incident-bundle-20260814T120000Z.tar.gz"

            result = publish_bundle(
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
        self.assertTrue(result.delivered)
        self.assertFalse(result.interrupted)
        self.assertEqual(result.residue, ())
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

    def test_published_mode_respects_restrictive_process_umask(self) -> None:
        inventory = b"[common]\n[nodes]\nnode-a = node-a.example\n"
        started_at = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
        previous_umask = os.umask(0o027)
        try:
            with TemporaryDirectory() as directory:
                root = Path(directory)
                workspace = root / "workspace"
                self._write_workspace(workspace, inventory)
                final_path = (
                    root / "ceph-incident-bundle-20260814T120000Z.tar.gz"
                )

                publish_bundle(
                    workspace,
                    final_path,
                    collector_version="0.1.0",
                    started_at=started_at,
                    since="24h",
                    prior_partial=False,
                )

                bundle_mode = stat.S_IMODE(final_path.stat().st_mode)
                observed_umask = os.umask(0o027)
        finally:
            os.umask(previous_umask)

        self.assertEqual(bundle_mode, 0o640)
        self.assertEqual(observed_umask, 0o027)

    def test_existing_final_destination_is_never_replaced(self) -> None:
        inventory = b"[common]\n[nodes]\nnode-a = node-a.example\n"
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

    def test_links_fifos_and_ambiguous_components_never_enter_a_bundle(self) -> None:
        inventory = b"[common]\n[nodes]\nnode-a = node-a.example\n"
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

    def test_symlinked_admitted_root_never_enters_a_bundle(self) -> None:
        inventory = b"[common]\n[nodes]\nnode-a = node-a.example\n"
        started_at = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            outside_workspace = root / "outside-workspace"
            self._write_workspace(outside_workspace, inventory)
            outside_capture = (
                outside_workspace
                / "admitted"
                / "node-contributions"
                / "node-a"
                / "node"
                / "probes"
                / "hostname"
                / "stdout"
            )
            workspace = root / "workspace"
            workspace.mkdir()
            (workspace / "private").mkdir()
            (workspace / "admitted").symlink_to(
                outside_workspace / "admitted", target_is_directory=True
            )
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

            outside_bytes = outside_capture.read_bytes()

        self.assertFalse(final_path.exists())
        self.assertEqual(outside_bytes, b"node-a\n")

    def test_trusted_admitted_source_is_not_identity_revalidated(
        self,
    ) -> None:
        inventory = b"[common]\n[nodes]\nnode-a = node-a.example\n"
        started_at = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
        real_addfile = tarfile.TarFile.addfile
        swapped = False

        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            self._write_workspace(workspace, inventory)
            node = (
                workspace
                / "admitted"
                / "node-contributions"
                / "node-a"
                / "node"
            )
            moved_node = node.with_name("original-node")
            outside_node = root / "outside-node"
            outside_capture = outside_node / "probes" / "hostname"
            outside_capture.mkdir(parents=True)
            (outside_capture / "stdout").write_bytes(b"outside\n")
            (outside_capture / "stderr").write_bytes(b"")
            (outside_capture / "result.json").write_bytes(b"{}\n")
            (outside_node / "files").mkdir()

            def swap_then_add(
                archive: tarfile.TarFile,
                member: tarfile.TarInfo,
                fileobj=None,
            ) -> None:
                nonlocal swapped
                if member.name.endswith("/inventory.ini") and not swapped:
                    node.rename(moved_node)
                    node.symlink_to(outside_node, target_is_directory=True)
                    swapped = True
                real_addfile(archive, member, fileobj)

            final_path = root / "ceph-incident-bundle-20260814T120000Z.tar.gz"
            with patch("tarfile.TarFile.addfile", new=swap_then_add):
                publish_bundle(
                    workspace,
                    final_path,
                    collector_version="0.1.0",
                    started_at=started_at,
                    since="24h",
                    prior_partial=False,
                )

            with tarfile.open(final_path, "r:gz") as archive:
                bundled = archive.extractfile(
                    "ceph-incident-bundle-20260814T120000Z/"
                    "nodes/node-a/probes/hostname/stdout"
                )
                assert bundled is not None
                bundled_bytes = bundled.read()
            candidates = list(root.glob(".*.candidate.*"))
            final_exists = final_path.exists()

        self.assertTrue(swapped)
        self.assertTrue(final_exists)
        self.assertEqual(candidates, [])
        self.assertEqual(bundled_bytes, b"outside\n")

    def test_output_parent_swap_cannot_redirect_candidate_or_final(self) -> None:
        inventory = b"[common]\n[nodes]\nnode-a = node-a.example\n"
        started_at = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
        real_tar_open = tarfile.open
        swapped = False
        outside_candidate: Path | None = None

        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            self._write_workspace(workspace, inventory)
            output = root / "output"
            output.mkdir()
            moved_output = root / "original-output"
            outside = root / "outside"
            outside.mkdir()
            outside_sentinel = outside / "sentinel"
            outside_sentinel.write_bytes(b"outside stays unchanged")
            final_path = output / "ceph-incident-bundle-20260814T120000Z.tar.gz"

            def swap_then_open(*args, **kwargs):
                nonlocal outside_candidate, swapped
                if not swapped:
                    output.rename(moved_output)
                    output.symlink_to(outside, target_is_directory=True)
                    candidate_names = [
                        path.name
                        for path in moved_output.iterdir()
                        if ".candidate." in path.name
                    ]
                    self.assertEqual(len(candidate_names), 1)
                    outside_candidate = outside / candidate_names[0]
                    outside_candidate.write_bytes(b"outside candidate")
                    swapped = True
                return real_tar_open(*args, **kwargs)

            with patch("tarfile.open", new=swap_then_open):
                publish_bundle(
                    workspace,
                    final_path,
                    collector_version="0.1.0",
                    started_at=started_at,
                    since="24h",
                    prior_partial=False,
                )

            outside_bytes = outside_sentinel.read_bytes()
            assert outside_candidate is not None
            outside_candidate_bytes = outside_candidate.read_bytes()
            outside_names = {path.name for path in outside.iterdir()}
            moved_names = {path.name for path in moved_output.iterdir()}
            redirected_final_exists = (outside / final_path.name).exists()

        self.assertTrue(swapped)
        self.assertEqual(outside_bytes, b"outside stays unchanged")
        self.assertEqual(outside_candidate_bytes, b"outside candidate")
        self.assertEqual(outside_names, {"sentinel", outside_candidate.name})
        self.assertFalse(redirected_final_exists)
        self.assertEqual(moved_names, {final_path.name})

    def test_missing_posix_no_follow_support_fails_closed(self) -> None:
        inventory = b"[common]\n[nodes]\nnode-a = node-a.example\n"
        started_at = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            self._write_workspace(workspace, inventory)
            final_path = root / "ceph-incident-bundle-20260814T120000Z.tar.gz"

            with (
                patch("ceph_incident_bundle.collect.bundle.os.O_NOFOLLOW", 0),
                self.assertRaises(BundlePublicationError),
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

        self.assertFalse(final_path.exists())
        self.assertEqual(candidates, [])

    def test_prior_problem_is_recorded_as_partial_without_changing_delivery_status(self) -> None:
        inventory = b"[common]\n[nodes]\nnode-a = node-a.example\n"
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

    def test_archive_construction_failure_removes_owned_incomplete_work(self) -> None:
        inventory = b"[common]\n[nodes]\nnode-a = node-a.example\n"
        started_at = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            self._write_workspace(workspace, inventory)
            final_path = root / "ceph-incident-bundle-20260814T120000Z.tar.gz"
            outside = root / "outside-sentinel"
            outside.write_bytes(b"unchanged")

            with (
                patch(
                    "tarfile.TarFile.addfile",
                    side_effect=OSError("injected archive construction failure"),
                ),
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
            outside_bytes = outside.read_bytes()
            workspace_exists = workspace.exists()
            final_exists = final_path.exists()

        self.assertIn("injected archive construction failure", str(caught.exception))
        self.assertFalse(workspace_exists)
        self.assertFalse(final_exists)
        self.assertEqual(candidates, [])
        self.assertEqual(outside_bytes, b"unchanged")

    def test_archive_close_failure_removes_owned_incomplete_work(self) -> None:
        inventory = b"[common]\n[nodes]\nnode-a = node-a.example\n"
        started_at = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
        real_close = tarfile.TarFile.close
        close_failure_seen = False

        def close_then_fail(archive: tarfile.TarFile) -> None:
            nonlocal close_failure_seen
            real_close(archive)
            if not close_failure_seen:
                close_failure_seen = True
                raise OSError("injected archive close failure")

        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            self._write_workspace(workspace, inventory)
            final_path = root / "ceph-incident-bundle-20260814T120000Z.tar.gz"

            with (
                patch("tarfile.TarFile.close", new=close_then_fail),
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

        self.assertTrue(close_failure_seen)
        self.assertIn("injected archive close failure", str(caught.exception))
        self.assertFalse(final_exists)
        self.assertEqual(candidates, [])

    @staticmethod
    def _write_workspace(workspace: Path, inventory: bytes) -> None:
        workspace.mkdir()
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
