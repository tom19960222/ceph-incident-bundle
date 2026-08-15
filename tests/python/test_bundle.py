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

    def test_published_mode_respects_restrictive_process_umask(self) -> None:
        inventory = b"[common]\nssh_user = root\n[nodes]\nnode-a = node-a.example\n"
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
        real_unlink = os.unlink
        candidate_failure_seen = False

        def fail_first_candidate_unlink(
            path: str,
            *,
            dir_fd: int | None = None,
        ) -> None:
            nonlocal candidate_failure_seen
            if ".candidate." in path and not candidate_failure_seen:
                candidate_failure_seen = True
                raise OSError("injected candidate cleanup failure")
            real_unlink(path, dir_fd=dir_fd)

        supported_with_failure = os.supports_dir_fd | {fail_first_candidate_unlink}

        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            self._write_workspace(workspace, inventory)
            final_path = root / "ceph-incident-bundle-20260814T120000Z.tar.gz"

            with (
                patch(
                    "ceph_incident_bundle.collect.bundle.os.unlink",
                    new=fail_first_candidate_unlink,
                ),
                patch(
                    "ceph_incident_bundle.collect.bundle.os.supports_dir_fd",
                    supported_with_failure,
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

            final_exists = final_path.exists()
            candidates = list(root.glob(".*.candidate.*"))

        self.assertTrue(candidate_failure_seen)
        self.assertFalse(final_exists)
        self.assertEqual(candidates, [])
        self.assertIn("final publication was rolled back", str(caught.exception))

    def test_prepublication_candidate_residue_is_reported_with_its_exact_path(self) -> None:
        inventory = b"[common]\nssh_user = root\n[nodes]\nnode-a = node-a.example\n"
        started_at = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
        real_unlink = os.unlink

        def refuse_candidate_unlink(
            path: str,
            *,
            dir_fd: int | None = None,
        ) -> None:
            if ".candidate." in path:
                raise OSError("injected retained candidate")
            real_unlink(path, dir_fd=dir_fd)

        supported_with_refusal = os.supports_dir_fd | {refuse_candidate_unlink}

        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            self._write_workspace(workspace, inventory)
            final_path = root / "ceph-incident-bundle-20260814T120000Z.tar.gz"

            with (
                patch("tarfile.open", side_effect=OSError("injected archive failure")),
                patch(
                    "ceph_incident_bundle.collect.bundle.os.unlink",
                    new=refuse_candidate_unlink,
                ),
                patch(
                    "ceph_incident_bundle.collect.bundle.os.supports_dir_fd",
                    supported_with_refusal,
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

    def test_symlinked_admitted_root_never_enters_a_bundle(self) -> None:
        inventory = b"[common]\nssh_user = root\n[nodes]\nnode-a = node-a.example\n"
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
            (workspace / OWNERSHIP_MARKER).write_text(
                str(workspace.resolve()) + "\n", encoding="utf-8"
            )
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

    def test_ancestor_swap_after_enumeration_fails_publication_closed(
        self,
    ) -> None:
        inventory = b"[common]\nssh_user = root\n[nodes]\nnode-a = node-a.example\n"
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
            with (
                patch("tarfile.TarFile.addfile", new=swap_then_add),
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

            outside_bytes = (outside_capture / "stdout").read_bytes()
            candidates = list(root.glob(".*.candidate.*"))

        self.assertTrue(swapped)
        self.assertFalse(final_path.exists())
        self.assertEqual(candidates, [])
        self.assertEqual(outside_bytes, b"outside\n")

    def test_output_parent_swap_cannot_redirect_candidate_or_final(self) -> None:
        inventory = b"[common]\nssh_user = root\n[nodes]\nnode-a = node-a.example\n"
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

            with (
                patch("tarfile.open", new=swap_then_open),
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
        self.assertEqual(moved_names, set())

    def test_workspace_root_swap_never_deletes_the_replacement(self) -> None:
        inventory = b"[common]\nssh_user = root\n[nodes]\nnode-a = node-a.example\n"
        started_at = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
        real_addfile = tarfile.TarFile.addfile
        swapped = False

        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            self._write_workspace(workspace, inventory)
            moved_workspace = root / "moved-workspace"
            replacement_sentinel = workspace / "replacement-sentinel"

            def swap_after_last_source(
                archive: tarfile.TarFile,
                member: tarfile.TarInfo,
                fileobj=None,
            ) -> None:
                nonlocal swapped
                real_addfile(archive, member, fileobj)
                if member.name.endswith("/probes/hostname/stdout") and not swapped:
                    workspace.rename(moved_workspace)
                    workspace.mkdir()
                    (workspace / OWNERSHIP_MARKER).write_text(
                        str(workspace.resolve()) + "\n", encoding="utf-8"
                    )
                    replacement_sentinel.write_bytes(b"replacement stays")
                    swapped = True

            final_path = root / "ceph-incident-bundle-20260814T120000Z.tar.gz"
            with patch("tarfile.TarFile.addfile", new=swap_after_last_source):
                cleanup_problem = publish_bundle(
                    workspace,
                    final_path,
                    collector_version="0.1.0",
                    started_at=started_at,
                    since="24h",
                    prior_partial=False,
                )

            replacement_bytes = replacement_sentinel.read_bytes()
            moved_workspace_exists = moved_workspace.exists()
            with tarfile.open(final_path, "r:gz") as archive:
                metadata_file = archive.extractfile(
                    "ceph-incident-bundle-20260814T120000Z/collection.json"
                )
                assert metadata_file is not None
                outcome = json.load(metadata_file)["outcome"]

        self.assertTrue(swapped)
        self.assertEqual(replacement_bytes, b"replacement stays")
        self.assertTrue(moved_workspace_exists)
        self.assertIsNotNone(cleanup_problem)
        assert cleanup_problem is not None
        self.assertIn("refusing to clean", cleanup_problem)
        self.assertEqual(outcome, "partial")

    def test_workspace_swap_at_cleanup_entry_never_deletes_replacement(
        self,
    ) -> None:
        inventory = b"[common]\nssh_user = root\n[nodes]\nnode-a = node-a.example\n"
        started_at = datetime(2026, 8, 14, 12, 0, 0, tzinfo=timezone.utc)
        real_listdir = os.listdir
        swapped = False

        with TemporaryDirectory() as directory:
            root = Path(directory)
            workspace = root / "workspace"
            self._write_workspace(workspace, inventory)
            workspace_identity = (
                workspace.stat().st_dev,
                workspace.stat().st_ino,
            )
            moved_workspace = root / "moved-workspace"
            replacement_sentinel = workspace / "replacement-sentinel"

            def swap_when_cleanup_lists_root(directory_descriptor: int):
                nonlocal swapped
                descriptor_facts = os.fstat(directory_descriptor)
                if (
                    (descriptor_facts.st_dev, descriptor_facts.st_ino)
                    == workspace_identity
                    and not swapped
                ):
                    workspace.rename(moved_workspace)
                    workspace.mkdir()
                    (workspace / OWNERSHIP_MARKER).write_text(
                        str(workspace.resolve()) + "\n", encoding="utf-8"
                    )
                    replacement_sentinel.write_bytes(b"replacement stays")
                    swapped = True
                return real_listdir(directory_descriptor)

            supported_with_swap = os.supports_fd | {
                swap_when_cleanup_lists_root
            }
            final_path = root / "ceph-incident-bundle-20260814T120000Z.tar.gz"
            with (
                patch(
                    "ceph_incident_bundle.collect.bundle.os.listdir",
                    new=swap_when_cleanup_lists_root,
                ),
                patch(
                    "ceph_incident_bundle.collect.bundle.os.supports_fd",
                    supported_with_swap,
                ),
            ):
                cleanup_problem = publish_bundle(
                    workspace,
                    final_path,
                    collector_version="0.1.0",
                    started_at=started_at,
                    since="24h",
                    prior_partial=False,
                )

            replacement_bytes = replacement_sentinel.read_bytes()
            moved_workspace_exists = moved_workspace.exists()

        self.assertTrue(swapped)
        self.assertEqual(replacement_bytes, b"replacement stays")
        self.assertTrue(moved_workspace_exists)
        self.assertIsNotNone(cleanup_problem)
        assert cleanup_problem is not None
        self.assertIn("refusing to clean", cleanup_problem)

    def test_missing_posix_no_follow_support_fails_closed(self) -> None:
        inventory = b"[common]\nssh_user = root\n[nodes]\nnode-a = node-a.example\n"
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
