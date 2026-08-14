import io
from pathlib import Path
import tarfile
from tempfile import TemporaryDirectory
import unittest

from ceph_incident_bundle.collect.node_archive import (
    ArchiveRejected,
    admit_archive,
)


def write_archive(path: Path, members: list[tuple[str, bytes | None]]) -> None:
    with tarfile.open(path, "w:gz") as archive:
        for name, contents in members:
            member = tarfile.TarInfo(name)
            if contents is None:
                member.type = tarfile.DIRTYPE
                member.mode = 0o700
                archive.addfile(member)
            else:
                member.size = len(contents)
                member.mode = 0o600
                archive.addfile(member, io.BytesIO(contents))


BASE_MEMBERS = [
    ("node", None),
    ("node/probes", None),
    ("node/files", None),
]


class NodeArchiveAdmissionTests(unittest.TestCase):
    def test_complete_archive_is_privately_extracted_then_promoted(self) -> None:
        members = [
            ("node", None),
            ("node/probes", None),
            ("node/probes/hostname", None),
            ("node/probes/hostname/stdout", b"node-a\n"),
            ("node/probes/hostname/stderr", b""),
            ("node/probes/hostname/result.json", b"{}\n"),
            ("node/files", None),
            ("ceph", None),
            ("ceph/probes", None),
        ]
        with TemporaryDirectory() as directory:
            workspace = Path(directory)
            staging = workspace / "private" / "node-a"
            staging.mkdir(parents=True)
            archive = staging / "received.tar.gz"
            extraction = staging / "extracted"
            contribution = workspace / "admitted" / "node-a"
            contribution.parent.mkdir()
            write_archive(archive, members)

            admit_archive(archive, extraction, contribution, ceph_allowed=True)

            self.assertFalse(extraction.exists())
            self.assertEqual(
                (contribution / "node/probes/hostname/stdout").read_bytes(),
                b"node-a\n",
            )
            self.assertTrue((contribution / "node/files").is_dir())
            self.assertTrue((contribution / "ceph/probes").is_dir())

    def test_every_unsafe_archive_is_rejected_before_extraction(self) -> None:
        hostile_members = {
            "absolute": [("/escape", b"x")],
            "traversal": [("node/files/../../../../outside-sentinel", b"x")],
            "dot component": [("node/files/./escape", b"x")],
            "empty component": [("node/files//escape", b"x")],
            "backslash": [(r"node/files\escape", b"x")],
            "unknown root": [("other", None)],
            "duplicate": [("node/files/evidence", b"a"), ("node/files/evidence", b"b")],
            "case collision": [("node/files/Evidence", b"a"), ("node/files/evidence", b"b")],
            "nfc collision": [("node/files/é", b"a"), ("node/files/é", b"b")],
            "file ancestor": [("node/files/log", b"a"), ("node/files/log/child", b"b")],
            "absent ancestor": [("node/files/log/child", b"b")],
        }
        for label, extra_members in hostile_members.items():
            with self.subTest(label=label), TemporaryDirectory() as directory:
                workspace = Path(directory)
                staging = workspace / "private" / "node-a"
                staging.mkdir(parents=True)
                archive = staging / "received.tar.gz"
                extraction = staging / "extracted"
                contribution = workspace / "admitted" / "node-a"
                contribution.parent.mkdir()
                outside_sentinel = workspace / "outside-sentinel"
                outside_sentinel.write_bytes(b"unchanged")
                write_archive(archive, [*BASE_MEMBERS, *extra_members])

                with self.assertRaises(ArchiveRejected):
                    admit_archive(
                        archive, extraction, contribution, ceph_allowed=False
                    )

                self.assertFalse(extraction.exists())
                self.assertFalse(contribution.exists())
                self.assertEqual(outside_sentinel.read_bytes(), b"unchanged")

    def test_links_devices_fifos_and_other_special_members_are_rejected(self) -> None:
        special_types = (
            tarfile.SYMTYPE,
            tarfile.LNKTYPE,
            tarfile.CHRTYPE,
            tarfile.BLKTYPE,
            tarfile.FIFOTYPE,
            b"Z",
        )
        for member_type in special_types:
            with self.subTest(member_type=member_type), TemporaryDirectory() as directory:
                workspace = Path(directory)
                staging = workspace / "private" / "node-a"
                staging.mkdir(parents=True)
                archive = staging / "received.tar.gz"
                extraction = staging / "extracted"
                contribution = workspace / "admitted" / "node-a"
                contribution.parent.mkdir()
                with tarfile.open(archive, "w:gz") as opened:
                    for name, contents in BASE_MEMBERS:
                        member = tarfile.TarInfo(name)
                        member.type = tarfile.DIRTYPE
                        opened.addfile(member)
                    special = tarfile.TarInfo("node/files/special")
                    special.type = member_type
                    special.linkname = "node/files/target"
                    opened.addfile(special)

                with self.assertRaises(ArchiveRejected):
                    admit_archive(
                        archive, extraction, contribution, ceph_allowed=False
                    )

                self.assertFalse(extraction.exists())
                self.assertFalse(contribution.exists())

    def test_required_shape_and_ceph_authorization_are_fail_closed(self) -> None:
        cases = (
            ([("node", None), ("node/probes", None)], False),
            ([*BASE_MEMBERS, ("ceph", None), ("ceph/probes", None)], False),
            (BASE_MEMBERS, True),
        )
        for members, ceph_allowed in cases:
            with self.subTest(ceph_allowed=ceph_allowed, members=members), TemporaryDirectory() as directory:
                workspace = Path(directory)
                staging = workspace / "private" / "node-a"
                staging.mkdir(parents=True)
                archive = staging / "received.tar.gz"
                extraction = staging / "extracted"
                contribution = workspace / "admitted" / "node-a"
                contribution.parent.mkdir()
                write_archive(archive, members)

                with self.assertRaises(ArchiveRejected):
                    admit_archive(
                        archive, extraction, contribution, ceph_allowed=ceph_allowed
                    )

                self.assertFalse(extraction.exists())
                self.assertFalse(contribution.exists())

    def test_corrupt_and_truncated_streams_are_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            workspace = Path(directory)
            admitted = workspace / "admitted"
            admitted.mkdir()
            for label, archive_bytes in (
                ("corrupt", b"not a gzip archive"),
                ("truncated", self._truncated_archive(workspace)),
                ("concatenated", self._concatenated_archives(workspace)),
            ):
                with self.subTest(label=label):
                    staging = workspace / label
                    staging.mkdir()
                    archive = staging / "received.tar.gz"
                    archive.write_bytes(archive_bytes)
                    extraction = staging / "extracted"
                    contribution = admitted / label

                    with self.assertRaises(ArchiveRejected):
                        admit_archive(
                            archive, extraction, contribution, ceph_allowed=False
                        )

                    self.assertFalse(extraction.exists())
                    self.assertFalse(contribution.exists())

    def test_existing_contribution_is_never_replaced(self) -> None:
        with TemporaryDirectory() as directory:
            workspace = Path(directory)
            staging = workspace / "private" / "node-a"
            staging.mkdir(parents=True)
            archive = staging / "received.tar.gz"
            extraction = staging / "extracted"
            contribution = workspace / "admitted" / "node-a"
            contribution.mkdir(parents=True)
            sentinel = contribution / "sentinel"
            sentinel.write_bytes(b"unchanged")
            write_archive(archive, BASE_MEMBERS)

            with self.assertRaises(ArchiveRejected):
                admit_archive(archive, extraction, contribution, ceph_allowed=False)

            self.assertEqual(sentinel.read_bytes(), b"unchanged")
            self.assertFalse(extraction.exists())

    @staticmethod
    def _truncated_archive(workspace: Path) -> bytes:
        complete = workspace / "complete.tar.gz"
        write_archive(complete, BASE_MEMBERS)
        return complete.read_bytes()[:-8]

    @staticmethod
    def _concatenated_archives(workspace: Path) -> bytes:
        first = workspace / "first.tar.gz"
        second = workspace / "second.tar.gz"
        write_archive(first, BASE_MEMBERS)
        write_archive(second, BASE_MEMBERS)
        return first.read_bytes() + second.read_bytes()


if __name__ == "__main__":
    unittest.main()
