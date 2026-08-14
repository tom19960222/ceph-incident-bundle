import io
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from contextlib import redirect_stderr

from ceph_incident_bundle.collect.node import collect_node
from ceph_incident_bundle.inventory import TargetNode


class TargetNodeTests(unittest.TestCase):
    def test_one_ssh_process_transfers_unchanged_source_and_admits_archive(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self._write_fake_ssh(fake_bin / "ssh")
            record = root / "record"
            staging = root / "private" / "node-a"
            contribution = root / "admitted" / "node-a"
            staging.parent.mkdir()
            contribution.parent.mkdir()
            environment = {
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                "FAKE_SSH_RECORD": str(record),
                "FAKE_SSH_DIAGNOSTIC": "1",
            }
            rendered = io.StringIO()

            with patch.dict(os.environ, environment), redirect_stderr(rendered):
                problems = collect_node(
                    TargetNode("node-a", "node-a.example.test"),
                    ssh_user="root",
                    since_seconds=86400,
                    probe_timeout_seconds=1800,
                    ssh_connect_timeout_seconds=15,
                    ceph_allowed=False,
                    staging_directory=staging,
                    contribution_directory=contribution,
                )

            argv = json.loads((record / "argv.json").read_text(encoding="utf-8"))
            transferred = (record / "stdin.py").read_bytes()
            expected_source = (
                Path(__file__).parents[2]
                / "src"
                / "ceph_incident_bundle"
                / "remote_collector.py"
            ).read_bytes()
            process_count = (record / "count").read_text(encoding="ascii")
            hostname_stdout = (
                contribution / "node/probes/hostname/stdout"
            ).read_bytes()
            files_is_directory = (contribution / "node/files").is_dir()

        self.assertEqual(problems, [])
        self.assertEqual(
            argv,
            [
                "-T",
                "-o",
                "BatchMode=yes",
                "-o",
                "ConnectTimeout=15",
                "root@node-a.example.test",
                "python3",
                "-",
                "--since-seconds",
                "86400",
                "--probe-timeout-seconds",
                "1800",
            ],
        )
        self.assertEqual(transferred, expected_source)
        self.assertEqual(process_count, "1\n")
        self.assertTrue(hostname_stdout)
        self.assertTrue(files_is_directory)
        self.assertEqual(rendered.getvalue(), "[node-a] remote\\x00\\xff\n")

    def test_complete_archive_is_kept_when_ssh_exits_nonzero(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self._write_fake_ssh(fake_bin / "ssh")
            staging = root / "private" / "node-a"
            contribution = root / "admitted" / "node-a"
            staging.parent.mkdir()
            contribution.parent.mkdir()
            environment = {
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                "FAKE_SSH_RECORD": str(root / "record"),
                "FAKE_SSH_EXIT": "9",
                "FAKE_SSH_LARGE_DIAGNOSTIC": "1",
            }
            rendered = io.StringIO()

            with patch.dict(os.environ, environment), redirect_stderr(rendered):
                problems = collect_node(
                    TargetNode("node-a", "192.0.2.10"),
                    ssh_user="root",
                    since_seconds=60,
                    probe_timeout_seconds=0,
                    ssh_connect_timeout_seconds=0,
                    ceph_allowed=False,
                    staging_directory=staging,
                    contribution_directory=contribution,
                )

            admitted = (contribution / "node/probes/hostname/stdout").is_file()

        self.assertTrue(admitted)
        self.assertEqual(len(problems), 1)
        self.assertIn("status 9", problems[0])
        self.assertTrue(rendered.getvalue().startswith("[node-a] " + "x" * 100))

    def test_corrupt_ssh_stdout_never_creates_a_contribution(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self._write_fake_ssh(fake_bin / "ssh")
            staging = root / "private" / "node-a"
            contribution = root / "admitted" / "node-a"
            staging.parent.mkdir()
            contribution.parent.mkdir()
            environment = {
                "PATH": f"{fake_bin}{os.pathsep}{os.environ['PATH']}",
                "FAKE_SSH_RECORD": str(root / "record"),
                "FAKE_SSH_CORRUPT": "1",
            }

            with patch.dict(os.environ, environment):
                problems = collect_node(
                    TargetNode("node-a", "node-a.example.test"),
                    ssh_user="root",
                    since_seconds=60,
                    probe_timeout_seconds=30,
                    ssh_connect_timeout_seconds=15,
                    ceph_allowed=False,
                    staging_directory=staging,
                    contribution_directory=contribution,
                )

            contribution_exists = contribution.exists()

        self.assertFalse(contribution_exists)
        self.assertEqual(len(problems), 1)
        self.assertIn("Archive rejected", problems[0])

    @staticmethod
    def _write_fake_ssh(path: Path) -> None:
        path.write_text(
            f"""#!{os.environ.get('PYTHON', os.sys.executable)}
import json
import os
from pathlib import Path
import subprocess
import sys

record = Path(os.environ["FAKE_SSH_RECORD"])
record.mkdir()
(record / "argv.json").write_text(json.dumps(sys.argv[1:]), encoding="utf-8")
(record / "count").write_text("1\\n", encoding="ascii")
source = sys.stdin.buffer.read()
(record / "stdin.py").write_bytes(source)
if os.environ.get("FAKE_SSH_CORRUPT"):
    sys.stdout.buffer.write(b"not-an-archive")
    raise SystemExit(0)
remote_start = sys.argv.index("python3") + 1
completed = subprocess.run(
    [sys.executable, *sys.argv[remote_start:]],
    input=source,
    stdout=subprocess.PIPE,
    stderr=subprocess.PIPE,
    check=False,
)
sys.stdout.buffer.write(completed.stdout)
if os.environ.get("FAKE_SSH_LARGE_DIAGNOSTIC"):
    os.write(2, b"x" * 200000 + b"\\n")
elif os.environ.get("FAKE_SSH_DIAGNOSTIC"):
    os.write(2, b"remote\\x00\\xff\\n")
sys.stderr.buffer.write(completed.stderr)
raise SystemExit(int(os.environ.get("FAKE_SSH_EXIT", completed.returncode)))
""",
            encoding="utf-8",
        )
        path.chmod(0o755)


if __name__ == "__main__":
    unittest.main()
