from __future__ import annotations

import io
import os
import subprocess
import sys
import tarfile
import tempfile
import unittest
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
ENTRYPOINT = ROOT / "ceph_incident_bundle.py"


class VerifyCliTests(unittest.TestCase):
    def run_cli(
        self, *arguments: str, environment: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [sys.executable, str(ENTRYPOINT), *arguments],
            cwd=ROOT,
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

    def test_wrong_invocation_prints_usage_to_stderr(self) -> None:
        result = self.run_cli("verify")

        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertIn("Usage:", result.stderr)

    def test_valid_shell_compatible_directory_passes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            bundle = Path(temporary_directory) / "incident"
            self.make_valid_bundle_dir(bundle)

            result = self.run_cli("verify", str(bundle))

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, f"VERIFY PASS: {bundle}\n")
        self.assertEqual(result.stderr, "")

    def test_valid_shell_compatible_archive_passes(self) -> None:
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

    def test_missing_required_metadata_is_rejected_for_directory_and_archive(self) -> None:
        for missing_name in ("README-FIRST.txt", "summary.txt", "manifest.jsonl"):
            for target_kind in ("directory", "archive"):
                with self.subTest(missing_name=missing_name, target_kind=target_kind):
                    with tempfile.TemporaryDirectory() as temporary_directory:
                        temporary_root = Path(temporary_directory)
                        bundle = temporary_root / "incident"
                        self.make_valid_bundle_dir(bundle)
                        (bundle / missing_name).unlink()
                        target = bundle
                        if target_kind == "archive":
                            target = temporary_root / "incident.tar.gz"
                            self.make_bundle_archive(bundle, target)

                        result = self.run_cli("verify", str(target))

                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stdout, "")
                    self.assertIn(missing_name, result.stderr)

    def test_missing_cluster_evidence_is_rejected_for_directory_and_archive(self) -> None:
        for target_kind in ("directory", "archive"):
            with self.subTest(target_kind=target_kind):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    temporary_root = Path(temporary_directory)
                    bundle = temporary_root / "incident"
                    self.make_valid_bundle_dir(bundle)
                    (bundle / "cluster" / "ceph" / "status.txt").unlink()
                    target = bundle
                    if target_kind == "archive":
                        target = temporary_root / "incident.tar.gz"
                        self.make_bundle_archive(bundle, target)

                    result = self.run_cli("verify", str(target))

                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn("cluster/ artifact", result.stderr)

    def test_missing_nodes_evidence_is_rejected_for_directory_and_archive(self) -> None:
        for target_kind in ("directory", "archive"):
            with self.subTest(target_kind=target_kind):
                with tempfile.TemporaryDirectory() as temporary_directory:
                    temporary_root = Path(temporary_directory)
                    bundle = temporary_root / "incident"
                    self.make_valid_bundle_dir(bundle)
                    (
                        bundle / "nodes" / "monitor01" / "system" / "hostname.txt"
                    ).unlink()
                    target = bundle
                    if target_kind == "archive":
                        target = temporary_root / "incident.tar.gz"
                        self.make_bundle_archive(bundle, target)

                    result = self.run_cli("verify", str(target))

                self.assertEqual(result.returncode, 1)
                self.assertEqual(result.stdout, "")
                self.assertIn("nodes/ artifact", result.stderr)

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

    def test_unsafe_archive_members_are_rejected_without_writes(self) -> None:
        with tempfile.TemporaryDirectory() as temporary_directory:
            temporary_root = Path(temporary_directory)
            bundle = temporary_root / "incident"
            verifier_tmp = temporary_root / "verifier-tmp"
            verifier_tmp.mkdir()
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
                ("symlink", "nodes/link", tarfile.SYMTYPE, "symlink"),
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
                            member.linkname = "/etc/passwd"
                            output.addfile(member)

                    result = self.run_cli(
                        "verify", str(archive), environment={"TMPDIR": str(verifier_tmp)}
                    )

                    self.assertEqual(result.returncode, 1)
                    self.assertEqual(result.stdout, "")
                    self.assertIn(expected_error, result.stderr)
                    self.assertFalse((temporary_root / "escape.txt").exists())
                    self.assertFalse(absolute_escape.exists())

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


class ProductionModuleBoundaryTests(unittest.TestCase):
    def test_runtime_is_exactly_three_cohesive_modules(self) -> None:
        production_modules = sorted(
            path.name for path in ROOT.glob("ceph_incident_*.py")
        )

        self.assertEqual(
            production_modules,
            [
                "ceph_incident_bundle.py",
                "ceph_incident_collectors.py",
                "ceph_incident_node.py",
            ],
        )


if __name__ == "__main__":
    unittest.main()
