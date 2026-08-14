import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import unittest


REMOTE_COLLECTOR = (
    Path(__file__).parents[2]
    / "src"
    / "ceph_incident_bundle"
    / "remote_collector.py"
)


class RemoteCollectorTests(unittest.TestCase):
    def test_hostname_probe_is_streamed_as_a_complete_node_archive(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(REMOTE_COLLECTOR),
                "--since-seconds",
                "86400",
                "--probe-timeout-seconds",
                "30",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

        with TemporaryDirectory() as directory:
            archive = Path(directory) / "node.tar.gz"
            archive.write_bytes(completed.stdout)
            with tarfile.open(archive, "r:gz") as opened:
                names = {member.name for member in opened.getmembers()}
                stdout_file = opened.extractfile("node/probes/hostname/stdout")
                stderr_file = opened.extractfile("node/probes/hostname/stderr")
                result_file = opened.extractfile("node/probes/hostname/result.json")
                assert stdout_file is not None
                assert stderr_file is not None
                assert result_file is not None
                stdout = stdout_file.read()
                stderr = stderr_file.read()
                result = json.load(result_file)

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(completed.stderr, b"")
        self.assertIn("node", names)
        self.assertIn("node/probes", names)
        self.assertIn("node/files", names)
        self.assertTrue(stdout)
        self.assertEqual(stderr, b"")
        self.assertEqual(set(result), {
            "argv",
            "started_at",
            "finished_at",
            "outcome",
            "exit_code",
            "error",
        })
        self.assertEqual(result["argv"], ["hostname"])
        self.assertEqual(result["outcome"], "exited")
        self.assertEqual(result["exit_code"], 0)
        self.assertIsNone(result["error"])
        self.assertRegex(result["started_at"], r"^\d{4}-\d\d-\d\dT.*Z$")
        self.assertRegex(result["finished_at"], r"^\d{4}-\d\d-\d\dT.*Z$")

    def test_selected_ceph_source_includes_the_authorized_empty_ceph_shape(self) -> None:
        completed = subprocess.run(
            [
                sys.executable,
                str(REMOTE_COLLECTOR),
                "--since-seconds",
                "60",
                "--probe-timeout-seconds",
                "0",
                "--collect-ceph",
            ],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )
        with TemporaryDirectory() as directory:
            archive = Path(directory) / "node.tar.gz"
            archive.write_bytes(completed.stdout)
            with tarfile.open(archive, "r:gz") as opened:
                names = {member.name for member in opened.getmembers()}

        self.assertEqual(completed.returncode, 0)
        self.assertIn("ceph", names)
        self.assertIn("ceph/probes", names)

    def test_noncanonical_or_repeated_remote_controls_are_rejected(self) -> None:
        invalid_arguments = (
            (
                "--since-seconds",
                "01",
                "--probe-timeout-seconds",
                "30",
            ),
            (
                "--since-seconds",
                "+60",
                "--probe-timeout-seconds",
                "30",
            ),
            (
                "--since-seconds",
                "60",
                "--since-seconds",
                "120",
                "--probe-timeout-seconds",
                "30",
            ),
            (
                "--since-s",
                "60",
                "--probe-timeout-seconds",
                "30",
            ),
            (
                "--since-seconds",
                "60",
                "--probe-timeout-s",
                "30",
            ),
            (
                "--since-seconds",
                "60",
                "--probe-timeout-seconds",
                "30",
                "--collect-c",
                "--collect-ceph",
            ),
        )
        for arguments in invalid_arguments:
            with self.subTest(arguments=arguments):
                completed = subprocess.run(
                    [sys.executable, str(REMOTE_COLLECTOR), *arguments],
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    check=False,
                )

                self.assertNotEqual(completed.returncode, 0)
                self.assertEqual(completed.stdout, b"")
                self.assertIn(b"error:", completed.stderr)

    def test_unrepresentable_probe_timeout_fails_before_process_or_workspace(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            process_marker = root / "hostname-started"
            hostname = fake_bin / "hostname"
            hostname.write_text(
                f"#!{sys.executable}\n"
                "from pathlib import Path\n"
                f"Path({str(process_marker)!r}).write_bytes(b'started')\n",
                encoding="utf-8",
            )
            hostname.chmod(0o755)
            remote_temp = root / "remote-temp"
            remote_temp.mkdir()
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["TMPDIR"] = str(remote_temp)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(REMOTE_COLLECTOR),
                    "--since-seconds",
                    "60",
                    "--probe-timeout-seconds",
                    "9" * 1000,
                ],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            process_started = process_marker.exists()
            residue = list(remote_temp.glob("ceph-incident-node.*"))

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(completed.stdout, b"")
        self.assertIn(b"probe timeout exceeds the supported range", completed.stderr)
        self.assertFalse(process_started)
        self.assertEqual(residue, [])

    def test_failed_hostname_still_streams_capture_and_removes_remote_workspace(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            hostname = fake_bin / "hostname"
            hostname.write_text(
                f"""#!{sys.executable}
import os
os.write(1, b"partial-hostname\\x00\\n")
os.write(2, b"hostname diagnostic\\xff\\n")
raise SystemExit(7)
""",
                encoding="utf-8",
            )
            hostname.chmod(0o755)
            remote_temp = root / "remote-temp"
            remote_temp.mkdir()
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["TMPDIR"] = str(remote_temp)

            completed = subprocess.run(
                [
                    sys.executable,
                    str(REMOTE_COLLECTOR),
                    "--since-seconds",
                    "60",
                    "--probe-timeout-seconds",
                    "30",
                ],
                env=environment,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )

            archive = root / "node.tar.gz"
            archive.write_bytes(completed.stdout)
            with tarfile.open(archive, "r:gz") as opened:
                stdout_file = opened.extractfile("node/probes/hostname/stdout")
                stderr_file = opened.extractfile("node/probes/hostname/stderr")
                result_file = opened.extractfile("node/probes/hostname/result.json")
                assert stdout_file is not None
                assert stderr_file is not None
                assert result_file is not None
                probe_stdout = stdout_file.read()
                probe_stderr = stderr_file.read()
                result = json.load(result_file)
            residue = list(remote_temp.glob("ceph-incident-node.*"))

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(probe_stdout, b"partial-hostname\x00\n")
        self.assertEqual(probe_stderr, b"hostname diagnostic\xff\n")
        self.assertEqual(result["outcome"], "exited")
        self.assertEqual(result["exit_code"], 7)
        self.assertIn(b"hostname Probe failed", completed.stderr)
        self.assertEqual(residue, [])


if __name__ == "__main__":
    unittest.main()
