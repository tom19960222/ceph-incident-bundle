import errno
import gzip
import io
from pathlib import Path
import tarfile
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from ceph_incident_bundle.collect.node_archive import (
    ArchiveRejected,
    admit_archive,
)


def write_archive(
    path: Path,
    members: list[tuple[str, bytes | None]],
    *,
    mode: str = "w:gz",
    archive_format: int | None = None,
) -> None:
    open_arguments = {}
    if archive_format is not None:
        open_arguments["format"] = archive_format
    with tarfile.open(path, mode, **open_arguments) as archive:
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
    def test_missing_archive_retains_its_file_system_error(self) -> None:
        with TemporaryDirectory() as directory:
            workspace = Path(directory)
            staging = workspace / "private" / "node-a"
            staging.mkdir(parents=True)
            archive = staging / "missing.tar.gz"
            extraction = staging / "extracted"
            contribution = workspace / "admitted" / "node-a"
            contribution.parent.mkdir()

            with self.assertRaises(FileNotFoundError):
                admit_archive(
                    archive, extraction, contribution, ceph_allowed=False
                )

            self.assertFalse(extraction.exists())
            self.assertFalse(contribution.exists())

    def test_invalid_contribution_parent_retains_its_file_system_error(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            workspace = Path(directory)
            staging = workspace / "private" / "node-a"
            staging.mkdir(parents=True)
            archive = staging / "received.tar.gz"
            extraction = staging / "extracted"
            contribution_parent = workspace / "admitted"
            contribution_parent.write_bytes(b"not a directory")
            contribution = contribution_parent / "node-a"
            write_archive(archive, BASE_MEMBERS)

            with self.assertRaises(NotADirectoryError) as caught:
                admit_archive(
                    archive, extraction, contribution, ceph_allowed=False
                )

            self.assertEqual(caught.exception.errno, errno.ENOTDIR)
            self.assertTrue(extraction.exists())
            self.assertEqual(contribution_parent.read_bytes(), b"not a directory")

    def test_initial_tar_open_retains_its_file_system_error(self) -> None:
        def fail_with_tar_read_error(*args, **kwargs):
            file_error = OSError(errno.EIO, "injected archive read failure")
            raise tarfile.ReadError("not a gzip file") from file_error

        with TemporaryDirectory() as directory:
            workspace = Path(directory)
            staging = workspace / "private" / "node-a"
            staging.mkdir(parents=True)
            archive = staging / "received.tar.gz"
            extraction = staging / "extracted"
            contribution = workspace / "admitted" / "node-a"
            contribution.parent.mkdir()
            write_archive(archive, BASE_MEMBERS)

            with (
                patch(
                    "ceph_incident_bundle.collect.node_archive.tarfile.open",
                    new=fail_with_tar_read_error,
                ),
                self.assertRaises(OSError) as caught,
            ):
                admit_archive(
                    archive, extraction, contribution, ceph_allowed=False
                )

            self.assertEqual(caught.exception.errno, errno.EIO)
            self.assertFalse(extraction.exists())
            self.assertFalse(contribution.exists())

    def test_extraction_write_retains_its_file_system_error(self) -> None:
        real_open = Path.open
        write_error = OSError(errno.ENOSPC, "injected extraction write failure")

        class FailingWriteStream:
            def __init__(self, stream):
                self.stream = stream

            def __enter__(self):
                self.stream.__enter__()
                return self

            def __exit__(self, exception_type, exception, traceback):
                return self.stream.__exit__(
                    exception_type, exception, traceback
                )

            def write(self, contents):
                raise write_error

        with TemporaryDirectory() as directory:
            workspace = Path(directory)
            staging = workspace / "private" / "node-a"
            staging.mkdir(parents=True)
            archive = staging / "received.tar.gz"
            extraction = staging / "extracted"
            contribution = workspace / "admitted" / "node-a"
            contribution.parent.mkdir()
            outside_sentinel = workspace / "outside-sentinel"
            outside_sentinel.write_bytes(b"unchanged")
            evidence = extraction / "node" / "files" / "evidence"
            write_archive(
                archive, [*BASE_MEMBERS, ("node/files/evidence", b"private")]
            )

            def fail_evidence_write(path: Path, *args, **kwargs):
                mode = args[0] if args else kwargs.get("mode", "r")
                opened = real_open(path, *args, **kwargs)
                if path == evidence and mode == "xb":
                    return FailingWriteStream(opened)
                return opened

            with (
                patch("pathlib.Path.open", new=fail_evidence_write),
                self.assertRaises(OSError) as caught,
            ):
                admit_archive(
                    archive, extraction, contribution, ceph_allowed=False
                )

            self.assertIs(caught.exception, write_error)
            self.assertEqual(caught.exception.errno, errno.ENOSPC)
            self.assertTrue(extraction.is_dir())
            self.assertEqual(evidence.read_bytes(), b"")
            self.assertFalse(contribution.exists())
            self.assertEqual(outside_sentinel.read_bytes(), b"unchanged")

    def test_final_promotion_retains_its_file_system_error(self) -> None:
        promotion_error = OSError(errno.EIO, "injected promotion failure")

        with TemporaryDirectory() as directory:
            workspace = Path(directory)
            staging = workspace / "private" / "node-a"
            staging.mkdir(parents=True)
            archive = staging / "received.tar.gz"
            extraction = staging / "extracted"
            contribution = workspace / "admitted" / "node-a"
            contribution.parent.mkdir()
            outside_sentinel = workspace / "outside-sentinel"
            outside_sentinel.write_bytes(b"unchanged")
            write_archive(archive, BASE_MEMBERS)

            with (
                patch(
                    "ceph_incident_bundle.collect.node_archive.os.rename",
                    side_effect=promotion_error,
                ),
                self.assertRaises(OSError) as caught,
            ):
                admit_archive(
                    archive, extraction, contribution, ceph_allowed=False
                )

            self.assertIs(caught.exception, promotion_error)
            self.assertEqual(caught.exception.errno, errno.EIO)
            self.assertTrue((extraction / "node/probes").is_dir())
            self.assertFalse(contribution.exists())
            self.assertEqual(outside_sentinel.read_bytes(), b"unchanged")

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

    def test_ceph_directory_is_permitted_not_required_when_allowed(self) -> None:
        # Even when the workstation allows ceph evidence, a Target Node that
        # offers only ordinary node evidence must still be admitted in full:
        # ``ceph/`` is a permission, not a requirement.
        with TemporaryDirectory() as directory:
            workspace = Path(directory)
            staging = workspace / "private" / "node-a"
            staging.mkdir(parents=True)
            archive = staging / "received.tar.gz"
            extraction = staging / "extracted"
            contribution = workspace / "admitted" / "node-a"
            contribution.parent.mkdir()
            write_archive(archive, BASE_MEMBERS)

            admit_archive(archive, extraction, contribution, ceph_allowed=True)

            self.assertFalse(extraction.exists())
            self.assertTrue((contribution / "node/files").is_dir())
            self.assertTrue((contribution / "node/probes").is_dir())
            self.assertFalse((contribution / "ceph").exists())

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

    def test_gnu_long_name_finds_the_first_tar_eof(self) -> None:
        long_component = "g" * 120
        with TemporaryDirectory() as directory:
            workspace = Path(directory)
            staging = workspace / "private" / "node-a"
            staging.mkdir(parents=True)
            archive = staging / "received.tar.gz"
            extraction = staging / "extracted"
            contribution = workspace / "admitted" / "node-a"
            contribution.parent.mkdir()
            write_archive(
                archive,
                [*BASE_MEMBERS, (f"node/files/{long_component}", b"gnu\n")],
                archive_format=tarfile.GNU_FORMAT,
            )

            admit_archive(archive, extraction, contribution, ceph_allowed=False)

            self.assertEqual(
                (contribution / "node/files" / long_component).read_bytes(),
                b"gnu\n",
            )

    def test_highly_compressible_archive_is_admitted_without_a_total_cap(self) -> None:
        evidence = b"\0" * (3 * 1024 * 1024 + 17)
        with TemporaryDirectory() as directory:
            workspace = Path(directory)
            staging = workspace / "private" / "node-a"
            staging.mkdir(parents=True)
            archive = staging / "received.tar.gz"
            extraction = staging / "extracted"
            contribution = workspace / "admitted" / "node-a"
            contribution.parent.mkdir()
            write_archive(
                archive, [*BASE_MEMBERS, ("node/files/compressible", evidence)]
            )

            admit_archive(archive, extraction, contribution, ceph_allowed=False)

            self.assertEqual(
                (contribution / "node/files/compressible").read_bytes(), evidence
            )

    def test_invalid_tar_framing_is_reported_as_archive_rejection(self) -> None:
        invalid_tar = b"not a tar header".ljust(512, b"x") + b"\0" * 1024
        with TemporaryDirectory() as directory:
            workspace = Path(directory)
            staging = workspace / "private" / "node-a"
            staging.mkdir(parents=True)
            archive = staging / "received.tar.gz"
            archive.write_bytes(gzip.compress(invalid_tar))
            extraction = staging / "extracted"
            contribution = workspace / "admitted" / "node-a"
            contribution.parent.mkdir()

            with self.assertRaises(ArchiveRejected):
                admit_archive(
                    archive, extraction, contribution, ceph_allowed=False
                )

            self.assertFalse(extraction.exists())
            self.assertFalse(contribution.exists())

    def test_hostile_pax_size_is_rejected_before_extraction(self) -> None:
        with TemporaryDirectory() as directory:
            workspace = Path(directory)
            staging = workspace / "private" / "node-a"
            staging.mkdir(parents=True)
            archive = staging / "received.tar.gz"
            extraction = staging / "extracted"
            contribution = workspace / "admitted" / "node-a"
            contribution.parent.mkdir()
            with tarfile.open(
                archive, "w:gz", format=tarfile.PAX_FORMAT
            ) as opened:
                for name, _contents in BASE_MEMBERS:
                    member = tarfile.TarInfo(name)
                    member.type = tarfile.DIRTYPE
                    opened.addfile(member)
                hostile = tarfile.TarInfo("node/files/hostile-size")
                hostile.size = 1
                hostile.pax_headers = {"size": "999999999"}
                opened.addfile(hostile, io.BytesIO(b"x"))

            with self.assertRaises(ArchiveRejected):
                admit_archive(
                    archive, extraction, contribution, ceph_allowed=False
                )

            self.assertFalse(extraction.exists())
            self.assertFalse(contribution.exists())

    def test_missing_or_malformed_cpython_member_offset_is_rejected(self) -> None:
        original_open = tarfile.open
        for label, replacement in (("missing", None), ("malformed", "512")):
            with self.subTest(label=label), TemporaryDirectory() as directory:
                workspace = Path(directory)
                staging = workspace / "private" / "node-a"
                staging.mkdir(parents=True)
                archive = staging / "received.tar.gz"
                extraction = staging / "extracted"
                contribution = workspace / "admitted" / "node-a"
                contribution.parent.mkdir()
                write_archive(archive, BASE_MEMBERS)

                def open_with_changed_offset(*args, **kwargs):
                    opened = original_open(*args, **kwargs)
                    original_getmembers = opened.getmembers

                    def changed_members():
                        members = original_getmembers()
                        if replacement is None:
                            del members[-1].offset_data
                        else:
                            members[-1].offset_data = replacement
                        return members

                    opened.getmembers = changed_members
                    return opened

                with patch(
                    "ceph_incident_bundle.collect.node_archive.tarfile.open",
                    side_effect=open_with_changed_offset,
                ):
                    with self.assertRaises(ArchiveRejected):
                        admit_archive(
                            archive, extraction, contribution, ceph_allowed=False
                        )

                self.assertFalse(extraction.exists())
                self.assertFalse(contribution.exists())

    def test_missing_or_malformed_cpython_sparse_check_is_rejected(self) -> None:
        for label, replacement in (
            ("missing", None),
            ("malformed", lambda _member: "not a boolean"),
        ):
            with self.subTest(label=label), TemporaryDirectory() as directory:
                workspace = Path(directory)
                staging = workspace / "private" / "node-a"
                staging.mkdir(parents=True)
                archive = staging / "received.tar.gz"
                extraction = staging / "extracted"
                contribution = workspace / "admitted" / "node-a"
                contribution.parent.mkdir()
                write_archive(archive, BASE_MEMBERS)

                with patch.object(tarfile.TarInfo, "issparse", replacement):
                    with self.assertRaises(ArchiveRejected):
                        admit_archive(
                            archive, extraction, contribution, ceph_allowed=False
                        )

                self.assertFalse(extraction.exists())
                self.assertFalse(contribution.exists())

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
            # ``ceph/`` present without its required ``ceph/probes`` is still
            # rejected even though the workstation allows ceph evidence: once
            # offered, ``ceph/`` must be structurally complete.
            ([*BASE_MEMBERS, ("ceph", None)], True),
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

    def test_existing_contribution_fails_real_promotion_without_replacement(self) -> None:
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

            with self.assertRaises(OSError):
                admit_archive(archive, extraction, contribution, ceph_allowed=False)

            self.assertEqual(sentinel.read_bytes(), b"unchanged")
            self.assertTrue(extraction.exists())

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
