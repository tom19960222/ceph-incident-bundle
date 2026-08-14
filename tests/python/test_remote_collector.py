import json
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


if __name__ == "__main__":
    unittest.main()
