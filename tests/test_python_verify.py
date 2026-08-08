from __future__ import annotations

import gzip
import io
import os
import shutil
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import ceph_incident_bundle as bundle_entry


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "ceph_incident_bundle.py"


class VerifyCliTests(unittest.TestCase):
    def run_cli(
        self,
        *arguments: str,
        environment: dict[str, str] | None = None,
        working_directory: Path | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ENTRYPOINT), *arguments],
            cwd=working_directory or ROOT,
            env={**os.environ, **(environment or {})},
            text=True,
            capture_output=True,
            check=False,
        )

    def make_valid_bundle_dir(self, root: Path) -> None:
        (root / "cluster" / "ceph").mkdir(parents=True)
        (root / "nodes" / "monitor01" / "system").mkdir(parents=True)
        (root / "manifest.jsonl").write_text(
            '{"bundle":"ceph-incident"}\n', encoding="utf-8"
        )
        (root / "summary.txt").write_text("summary\n", encoding="utf-8")
        (root / "README-FIRST.txt").write_text("read me first\n", encoding="utf-8")
        (root / "cluster" / "ceph" / "status.txt").write_text(
            "ok\n", encoding="utf-8"
        )
        (root / "nodes" / "monitor01" / "system" / "hostname.txt").write_text(
            "monitor01\n", encoding="utf-8"
        )

    def make_bundle_archive(self, source: Path, archive: Path) -> None:
        with tarfile.open(archive, "w:gz") as tar:
            tar.add(source, arcname=".")

    def assert_rejected_after_removing(
        self, relative_path: Path, expected_error: str
    ) -> None:
        for target_kind in ("directory", "archive"):
            with self.subTest(
                relative_path=relative_path, target_kind=target_kind
            ):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    temporary_root = Path(temporary_directory)
                    bundle = temporary_root / "incident"
                    self.make_valid_bundle_dir(bundle)
                    (bundle / relative_path).unlink()
                    target = bundle
                    if target_kind == "archive":
                        target = temporary_root / "incident.tar.gz"
                        self.make_bundle_archive(bundle, target)

                    result = self.run_cli("verify", str(target))

                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn(expected_error, result.stderr)

    def assert_content_rejected_for_directory_and_archive(
        self, relative_path: Path, payload: bytes, expected_error: str
    ) -> None:
        for target_kind in ("directory", "archive"):
            with self.subTest(path=relative_path, target_kind=target_kind):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    temporary_root = Path(temporary_directory)
                    bundle = temporary_root / "incident"
                    self.make_valid_bundle_dir(bundle)
                    destination = bundle / relative_path
                    destination.parent.mkdir(parents=True, exist_ok=True)
                    destination.write_bytes(payload)
                    target = bundle
                    if target_kind == "archive":
                        target = temporary_root / "incident.tar.gz"
                        self.make_bundle_archive(bundle, target)

                    result = self.run_cli("verify", str(target))

                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn(expected_error, result.stderr)

    def test_wrong_invocation_prints_usage_to_stderr(self) -> None:
        result = self.run_cli("verify")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("Usage:", result.stderr)

    def test_extra_argument_prints_usage_to_stderr(self) -> None:
        result = self.run_cli("verify", "bundle.tar.gz", "extra")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("Usage:", result.stderr)

    def test_invalid_command_and_targets_fail_closed(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            wrong_suffix = temporary_root / "bundle.tgz"
            wrong_suffix.write_bytes(b"not a supported bundle\n")
            cases = (
                (("collect", "anything"), "Usage:"),
                (("verify", str(temporary_root / "missing")), "expected a directory"),
                (("verify", str(wrong_suffix)), "expected a directory"),
            )
            for arguments, expected_error in cases:
                with self.subTest(arguments=arguments):
                    result = self.run_cli(*arguments)

                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stdout, "")
                    self.assertIn(expected_error, result.stderr)

    def test_valid_minimal_directory_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            bundle = Path(temporary_directory) / "incident"
            self.make_valid_bundle_dir(bundle)

            result = self.run_cli("verify", str(bundle))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"VERIFY PASS: {bundle}\n")
        self.assertEqual(result.stderr, "")

    def test_valid_minimal_archive_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            bundle = temporary_root / "incident"
            archive = temporary_root / "incident.tar.gz"
            self.make_valid_bundle_dir(bundle)
            self.make_bundle_archive(bundle, archive)

            result = self.run_cli("verify", str(archive))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"VERIFY PASS: {archive}\n")
        self.assertEqual(result.stderr, "")

    def test_missing_required_content_is_rejected_for_directory_and_archive(self) -> None:
        cases = (
            (Path("README-FIRST.txt"), "README-FIRST.txt"),
            (Path("summary.txt"), "summary.txt"),
            (Path("manifest.jsonl"), "manifest.jsonl"),
            (Path("cluster/ceph/status.txt"), "cluster/ artifact"),
            (Path("nodes/monitor01/system/hostname.txt"), "nodes/ artifact"),
        )
        for missing_path, expected_error in cases:
            self.assert_rejected_after_removing(missing_path, expected_error)

    def test_corrupt_and_truncated_archives_are_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            corrupt_archive = temporary_root / "corrupt.tar.gz"
            corrupt_archive.write_bytes(b"not a tar.gz\n")

            bundle = temporary_root / "incident"
            complete_archive = temporary_root / "complete.tar.gz"
            truncated_archive = temporary_root / "truncated.tar.gz"
            self.make_valid_bundle_dir(bundle)
            self.make_bundle_archive(bundle, complete_archive)
            complete_bytes = complete_archive.read_bytes()
            truncated_archive.write_bytes(complete_bytes[: len(complete_bytes) // 2])

            for archive in (corrupt_archive, truncated_archive):
                with self.subTest(archive=archive.name):
                    result = self.run_cli("verify", str(archive))

                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stdout, "")
                    self.assertIn("invalid archive", result.stderr)

    def test_archive_with_truncated_gzip_tail_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            bundle = temporary_root / "incident"
            complete_archive = temporary_root / "complete.tar.gz"
            self.make_valid_bundle_dir(bundle)
            self.make_bundle_archive(bundle, complete_archive)
            complete_bytes = complete_archive.read_bytes()

            for removed_bytes in (1, 8, 16):
                with self.subTest(removed_bytes=removed_bytes):
                    truncated_archive = temporary_root / f"tail-{removed_bytes}.tar.gz"
                    truncated_archive.write_bytes(complete_bytes[:-removed_bytes])

                    result = self.run_cli("verify", str(truncated_archive))

                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stdout, "")
                    self.assertIn("invalid archive", result.stderr)

    def test_archive_with_invalid_deflate_body_is_rejected_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            bundle = temporary_root / "incident"
            self.make_valid_bundle_dir(bundle)

            tar_stream = io.BytesIO()
            with tarfile.open(fileobj=tar_stream, mode="w") as output:
                output.add(bundle, arcname=".")
            corrupt_bytes = bytearray(gzip.compress(tar_stream.getvalue()))
            corrupt_bytes[10] |= 0x06  # Deflate BTYPE=3 is reserved/invalid.
            corrupt_archive = temporary_root / "invalid-deflate.tar.gz"
            corrupt_archive.write_bytes(corrupt_bytes)

            result = self.run_cli("verify", str(corrupt_archive))

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("VERIFY FAIL: invalid archive", result.stderr)
        self.assertNotIn("Traceback", result.stderr)

    def test_unsafe_archive_members_are_rejected_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            bundle = temporary_root / "incident"
            verifier_tmp = temporary_root / "verifier-tmp"
            verifier_tmp.mkdir()
            process_cwd = temporary_root / "process-cwd"
            process_cwd.mkdir()
            self.make_valid_bundle_dir(bundle)

            absolute_escape = temporary_root / "absolute-escape.txt"
            cases = (
                ("traversal", "../escape.txt", tarfile.REGTYPE, "unsafe archive member"),
                (
                    "absolute",
                    str(absolute_escape),
                    tarfile.REGTYPE,
                    "unsafe archive member",
                ),
                (
                    "symlink",
                    "nodes/link",
                    tarfile.SYMTYPE,
                    "archive contains symlink member",
                ),
                (
                    "hardlink",
                    "nodes/hardlink",
                    tarfile.LNKTYPE,
                    "archive contains hardlink member",
                ),
                (
                    "fifo",
                    "nodes/monitor01/system/pipe",
                    tarfile.FIFOTYPE,
                    "non-file/non-directory",
                ),
                (
                    "device",
                    "nodes/monitor01/system/device",
                    tarfile.CHRTYPE,
                    "non-file/non-directory",
                ),
                (
                    "duplicate",
                    "./manifest.jsonl",
                    tarfile.REGTYPE,
                    "duplicate archive member",
                ),
                (
                    "normalised-collision",
                    "cluster//ceph/status.txt",
                    tarfile.REGTYPE,
                    "duplicate archive member",
                ),
            )
            for case_name, member_name, member_type, expected_error in cases:
                with self.subTest(case=case_name):
                    archive = temporary_root / f"{case_name}.tar.gz"
                    with tarfile.open(archive, "w:gz") as output:
                        output.add(bundle, arcname=".")
                        member = tarfile.TarInfo(member_name)
                        member.type = member_type
                        if member_type == tarfile.REGTYPE:
                            payload = b"must not be written\n"
                            member.size = len(payload)
                            output.addfile(member, io.BytesIO(payload))
                        else:
                            if member_type in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                                member.linkname = "/etc/passwd"
                            if member_type == tarfile.CHRTYPE:
                                member.devmajor = 1
                                member.devminor = 3
                            output.addfile(member)

                    result = self.run_cli(
                        "verify",
                        str(archive),
                        environment={"TMPDIR": str(verifier_tmp)},
                        working_directory=process_cwd,
                    )

                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stdout, "")
                    self.assertIn(expected_error, result.stderr)
                    self.assertFalse((temporary_root / "escape.txt").exists())
                    self.assertFalse(absolute_escape.exists())
                    self.assertEqual(list(verifier_tmp.iterdir()), [])
                    self.assertEqual(list(process_cwd.iterdir()), [])

    def test_secret_paths_are_rejected_for_directory_and_archive(self) -> None:
        # Each pattern is named in the failure, not merely counted: a reader has
        # to learn which path made verification fail.  A secret directory is
        # named as the directory, because that is what may not be in a bundle.
        for relative_path, named in (
            (Path("nodes/monitor01/keyring"), "nodes/monitor01/keyring"),
            (Path("nodes/monitor01/.ssh/config"), "nodes/monitor01/.ssh"),
            (Path("nodes/monitor01/id_ed25519"), "nodes/monitor01/id_ed25519"),
            (Path("nodes/monitor01/private_key"), "nodes/monitor01/private_key"),
            (Path("nodes/monitor01/client.pem"), "nodes/monitor01/client.pem"),
        ):
            self.assert_content_rejected_for_directory_and_archive(
                relative_path, b"not secret by content\n", f"forbidden path: {named}"
            )

    def test_private_key_and_ceph_key_content_are_rejected(self) -> None:
        for payload in (
            b"-----BEGIN OPENSSH PRIVATE KEY-----\nmaterial\n",
            b"key = AQB12345678901234567890==\n",
            b"key\v=\vAQB12345678901234567890==\n",
            b"key"
            + b" " * (1024 * 1024 + 1024)
            + b"=AQB12345678901234567890==\n",
            b"-----BEGIN"
            + b"A" * (1024 * 1024 + 1024)
            + b"PRIVATE KEY-----\n",
            b"-----BEGIN OPENSSH PRIVATE KEY-----\n"
            + b"x" * (2 * 1024 * 1024),
        ):
            self.assert_content_rejected_for_directory_and_archive(
                Path("nodes/monitor01/system/leak.txt"),
                payload,
                "unredacted PRIVATE KEY / key material",
            )

    def test_structural_payload_cap_bounds_directory_and_archive_expansion(
        self,
    ) -> None:
        for target_kind in ("directory", "archive"):
            with self.subTest(target_kind=target_kind):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    temporary_root = Path(temporary_directory)
                    bundle = temporary_root / "incident"
                    self.make_valid_bundle_dir(bundle)
                    (bundle / "nodes/monitor01/system/large.txt").write_bytes(
                        b"x" * (64 * 1024)
                    )
                    target = bundle
                    if target_kind == "archive":
                        target = temporary_root / "incident.tar.gz"
                        self.make_bundle_archive(bundle, target)

                    result = self.run_cli(
                        "verify",
                        str(target),
                        environment={
                            "CEPH_INCIDENT_TEST_BUNDLE_SAFETY_CAP_BYTES": "32768"
                        },
                    )

                self.assertEqual(result.returncode, 1)
                self.assertIn("structural payload cap", result.stderr)

    def test_archive_missing_tar_end_markers_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            bundle = temporary_root / "incident"
            complete = temporary_root / "complete.tar.gz"
            truncated = temporary_root / "missing-eoa.tar.gz"
            self.make_valid_bundle_dir(bundle)
            self.make_bundle_archive(bundle, complete)
            tar_payload = gzip.decompress(complete.read_bytes())
            blocks = [
                tar_payload[index : index + 512]
                for index in range(0, len(tar_payload), 512)
            ]
            last_nonzero = max(index for index, block in enumerate(blocks) if any(block))
            truncated.write_bytes(
                gzip.compress(
                    b"".join(blocks[: last_nonzero + 1]) + b"\0" * 512,
                    mtime=0,
                )
            )

            result = self.run_cli("verify", str(truncated))

        self.assertEqual(result.returncode, 1)
        self.assertIn("missing tar end markers", result.stderr)

    def test_tar_trailer_is_checked_with_bounded_reads(self) -> None:
        class BoundedZeroTrailer:
            def __init__(self) -> None:
                self.remaining = 3 * 1024 * 1024 + 1024
                self.read_sizes: list[int] = []

            def seek(self, offset: int) -> int:
                self.offset = offset
                return offset

            def read(self, size: int = -1) -> bytes:
                if size < 0:
                    raise AssertionError("unbounded trailer read")
                self.read_sizes.append(size)
                count = min(size, self.remaining)
                self.remaining -= count
                return b"\0" * count

        trailer = BoundedZeroTrailer()

        bundle_entry._verify_zero_tar_trailer(trailer, 4096)

        self.assertEqual(trailer.offset, 4096)
        self.assertGreater(len(trailer.read_sizes), 1)
        self.assertEqual(set(trailer.read_sizes), {1024 * 1024})

    def test_archive_content_scan_does_not_cache_the_member_table(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            bundle = temporary_root / "incident"
            archive_path = temporary_root / "incident.tar.gz"
            self.make_valid_bundle_dir(bundle)
            for index in range(64):
                (bundle / "nodes/monitor01/system" / f"evidence-{index}.txt").write_text(
                    "safe evidence\n", encoding="utf-8"
                )
            self.make_bundle_archive(bundle, archive_path)
            opened_archives: list[tarfile.TarFile] = []
            real_open = tarfile.open

            def observed_open(*args: object, **kwargs: object) -> tarfile.TarFile:
                opened = real_open(*args, **kwargs)
                opened_archives.append(opened)
                return opened

            with mock.patch.object(
                bundle_entry.tarfile, "open", side_effect=observed_open
            ):
                bundle_entry._verify_content_safety(archive_path)

        self.assertEqual(len(opened_archives), 1)
        self.assertEqual(opened_archives[0].members, [])

    def test_archive_file_ancestor_collision_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            bundle = temporary_root / "incident"
            archive_path = temporary_root / "hierarchy.tar.gz"
            self.make_valid_bundle_dir(bundle)
            with tarfile.open(archive_path, "w:gz") as archive:
                archive.add(bundle, arcname=".")
                for name in ("rogue", "rogue/child"):
                    member = tarfile.TarInfo(name)
                    member.size = 1
                    archive.addfile(member, io.BytesIO(b"x"))

            result = self.run_cli("verify", str(archive_path))

        self.assertEqual(result.returncode, 1)
        self.assertIn("hierarchy collision", result.stderr)

    def test_archive_regular_root_member_is_rejected_as_a_file_ancestor(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            bundle = temporary_root / "incident"
            archive_path = temporary_root / "file-root.tar.gz"
            self.make_valid_bundle_dir(bundle)
            with tarfile.open(archive_path, "w:gz") as archive:
                root_member = tarfile.TarInfo(".")
                root_member.size = 0
                archive.addfile(root_member, io.BytesIO())
                for path in sorted(bundle.rglob("*")):
                    archive.add(
                        path,
                        arcname=path.relative_to(bundle).as_posix(),
                        recursive=False,
                    )

            result = self.run_cli("verify", str(archive_path))

        self.assertEqual(result.returncode, 1)
        self.assertIn("hierarchy collision", result.stderr)

    def test_directory_symlink_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            bundle = Path(temporary_directory) / "incident"
            self.make_valid_bundle_dir(bundle)
            (bundle / "nodes" / "monitor01" / "system" / "outside-link").symlink_to(
                "/etc/passwd"
            )

            result = self.run_cli("verify", str(bundle))

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("symlink", result.stderr)

    def test_directory_special_file_is_rejected(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            bundle = Path(temporary_directory) / "incident"
            self.make_valid_bundle_dir(bundle)
            os.mkfifo(bundle / "nodes" / "monitor01" / "system" / "pipe")

            result = self.run_cli("verify", str(bundle))

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("non-file/non-directory", result.stderr)

    @unittest.skipIf(os.geteuid() == 0, "root can read mode-000 directories")
    def test_unreadable_directory_is_rejected_without_traceback(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            bundle = Path(temporary_directory) / "incident"
            self.make_valid_bundle_dir(bundle)
            unreadable = bundle / "nodes" / "monitor01" / "unreadable"
            unreadable.mkdir()
            unreadable.chmod(0)
            try:
                result = self.run_cli("verify", str(bundle))
            finally:
                unreadable.chmod(0o700)

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("cannot read bundle directory", result.stderr)
        self.assertNotIn("Traceback", result.stderr)


class ProductionModuleBoundaryTests(unittest.TestCase):
    def test_runtime_has_exactly_three_top_level_modules(self) -> None:
        production_modules = sorted(path.name for path in ROOT.glob("*.py"))

        self.assertEqual(
            production_modules,
            [
                "ceph_incident_bundle.py",
                "ceph_incident_collectors.py",
                "ceph_incident_node.py",
            ],
            "ADR 0001 requires exactly three top-level production Python modules",
        )


class RepositoryGateTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("git"), "git is required for repository gates")
    def test_local_connection_note_and_agent_worktrees_are_repo_ignored(self) -> None:
        paths = ".claude/worktrees/review/\nCEPH-LAB-CONNECTION.md\n"
        result = subprocess.run(
            ["git", "check-ignore", "-v", "--stdin"],
            cwd=ROOT,
            input=paths,
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        ignored = result.stdout.splitlines()
        self.assertEqual(len(ignored), 2, result.stdout)
        self.assertTrue(
            all(line.startswith(".gitignore:") for line in ignored), result.stdout
        )
        self.assertTrue(ignored[0].endswith("\t.claude/worktrees/review/"))
        self.assertTrue(ignored[1].endswith("\tCEPH-LAB-CONNECTION.md"))

        tracked = subprocess.run(
            ["git", "ls-files", "--", ".claude/worktrees", "CEPH-LAB-CONNECTION.md"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(tracked.returncode, 0, tracked.stderr)
        self.assertEqual(tracked.stdout, "")

    @unittest.skipUnless(shutil.which("make"), "make is required for repository gates")
    def test_python_gate_rejects_an_interpreter_below_311(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            fake_python = Path(temporary_directory) / "python-below-311"
            fake_python.write_text(
                f"#!{sys.executable}\n"
                "import os\n"
                "import sys\n"
                "if len(sys.argv) != 3 or sys.argv[1] != '-c':\n"
                "    raise SystemExit('expected Python -c invocation')\n"
                "real_python = os.environ['REAL_PYTHON']\n"
                "spoof = \"import sys; sys.version_info = (3, 10, 0, 'final', 0); \"\n"
                "os.execv(real_python, [real_python, '-c', spoof + sys.argv[2]])\n",
                encoding="utf-8",
            )
            fake_python.chmod(0o755)

            result = subprocess.run(
                ["make", "check-python", f"PYTHON={fake_python}"],
                cwd=ROOT,
                env={**os.environ, "REAL_PYTHON": sys.executable},
                text=True,
                capture_output=True,
                check=False,
            )

        self.assertNotEqual(result.returncode, 0)
        self.assertIn("Python 3.11", result.stdout + result.stderr)

        supported = subprocess.run(
            ["make", "check-python", f"PYTHON={sys.executable}"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(supported.returncode, 0, supported.stderr)

        test_python_dry_run = subprocess.run(
            ["make", "-n", "test-python", f"PYTHON={sys.executable}"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(test_python_dry_run.returncode, 0, test_python_dry_run.stderr)
        self.assertIn("version_info < (3, 11)", test_python_dry_run.stdout)
        # The sharded runner spawns its own interpreters, so the gate only means
        # something if the checked interpreter is the one it is handed.
        self.assertIn("run-python-tests.sh", test_python_dry_run.stdout)
        self.assertIn(f'PYTHON="{sys.executable}"', test_python_dry_run.stdout)

        validate_dry_run = subprocess.run(
            ["make", "-n", "-j4", "validate", f"PYTHON={sys.executable}"],
            cwd=ROOT,
            text=True,
            capture_output=True,
            check=False,
        )
        self.assertEqual(validate_dry_run.returncode, 0, validate_dry_run.stderr)
        first_command = validate_dry_run.stdout.splitlines()[0]
        self.assertIn("version_info < (3, 11)", first_command)


if __name__ == "__main__":
    unittest.main()
