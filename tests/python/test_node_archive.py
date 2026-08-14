import gzip
import io
from pathlib import Path
import tarfile
from tempfile import TemporaryDirectory
import unittest

from ceph_incident_bundle.collect.node_archive import (
    ArchiveRejected,
    admit_archive,
)


def write_archive(
    path: Path,
    members: list[tuple[str, bytes | None]],
    *,
    mode: str = "w:gz",
) -> None:
    with tarfile.open(path, mode) as archive:
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
            tarfile.GNUTYPE_SPARSE,
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

    def test_zero_payload_blocks_are_not_mistaken_for_tar_eof(self) -> None:
        members = [
            *BASE_MEMBERS,
            ("node/files/zero-blocks", b"\0" * 1024),
            ("node/files/after-zero-blocks", b"still evidence\n"),
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

            admit_archive(archive, extraction, contribution, ceph_allowed=False)

            self.assertEqual(
                (contribution / "node/files/zero-blocks").read_bytes(),
                b"\0" * 1024,
            )
            self.assertEqual(
                (contribution / "node/files/after-zero-blocks").read_bytes(),
                b"still evidence\n",
            )

    def test_pax_and_payload_block_boundaries_find_the_first_tar_eof(self) -> None:
        long_component = "e" * 120
        members = [
            *BASE_MEMBERS,
            (f"node/files/{long_component}", b"evidence\n"),
            ("node/files/one-byte", b"x"),
            ("node/files/513-bytes", b"y" * 513),
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

            admit_archive(archive, extraction, contribution, ceph_allowed=False)

            self.assertEqual(
                (contribution / "node/files" / long_component).read_bytes(),
                b"evidence\n",
            )
            self.assertEqual(
                (contribution / "node/files/one-byte").read_bytes(), b"x"
            )
            self.assertEqual(
                (contribution / "node/files/513-bytes").read_bytes(), b"y" * 513
            )

    def test_exact_tar_eof_and_extra_zero_padding_are_accepted(self) -> None:
        with TemporaryDirectory() as directory:
            workspace = Path(directory)
            uncompressed = workspace / "ordinary.tar"
            write_archive(uncompressed, BASE_MEMBERS, mode="w")
            ordinary_bytes = uncompressed.read_bytes()
            first_eof_end = len(BASE_MEMBERS) * 512 + 1024
            for label, tar_bytes in (
                ("exact-eof", ordinary_bytes[:first_eof_end]),
                (
                    "extra-zero-padding",
                    ordinary_bytes[:first_eof_end] + b"\0" * 512,
                ),
            ):
                with self.subTest(label=label):
                    staging = workspace / label
                    staging.mkdir()
                    archive = staging / "received.tar.gz"
                    archive.write_bytes(gzip.compress(tar_bytes))
                    extraction = staging / "extracted"
                    contribution = workspace / "admitted" / label
                    contribution.parent.mkdir(exist_ok=True)

                    admit_archive(
                        archive, extraction, contribution, ceph_allowed=False
                    )

                    self.assertTrue((contribution / "node/probes").is_dir())
                    self.assertTrue((contribution / "node/files").is_dir())

    def test_a_second_gzip_member_or_raw_trailing_bytes_are_rejected(self) -> None:
        with TemporaryDirectory() as directory:
            workspace = Path(directory)
            first = workspace / "first.tar.gz"
            write_archive(first, BASE_MEMBERS)
            for label, trailing_bytes in (
                ("empty-member", gzip.compress(b"")),
                ("zero-only-member", gzip.compress(b"\0" * 1024)),
                ("raw-zero-tail", b"\0"),
            ):
                with self.subTest(label=label):
                    staging = workspace / label
                    staging.mkdir()
                    archive = staging / "received.tar.gz"
                    archive.write_bytes(
                        first.read_bytes() + trailing_bytes
                    )
                    extraction = staging / "extracted"
                    contribution = workspace / "admitted" / label
                    contribution.parent.mkdir(exist_ok=True)

                    with self.assertRaises(ArchiveRejected):
                        admit_archive(
                            archive, extraction, contribution, ceph_allowed=False
                        )

                    self.assertFalse(extraction.exists())
                    self.assertFalse(contribution.exists())

    def test_one_gzip_member_cannot_contain_a_second_tar_or_nonzero_tail(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            workspace = Path(directory)
            first = workspace / "first.tar"
            second = workspace / "second.tar"
            write_archive(first, BASE_MEMBERS, mode="w")
            write_archive(second, BASE_MEMBERS, mode="w")
            first_bytes = first.read_bytes()
            for label, uncompressed in (
                ("second-tar", first_bytes + second.read_bytes()),
                ("nonzero-tail", first_bytes + b"not padding"),
            ):
                with self.subTest(label=label):
                    staging = workspace / label
                    staging.mkdir()
                    archive = staging / "received.tar.gz"
                    archive.write_bytes(gzip.compress(uncompressed))
                    extraction = staging / "extracted"
                    contribution = workspace / "admitted" / label
                    contribution.parent.mkdir(exist_ok=True)

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
