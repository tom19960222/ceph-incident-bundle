import json
import os
from pathlib import Path
import subprocess
import sys
import tarfile
from tempfile import TemporaryDirectory
import unittest

from ceph_incident_bundle.remote_collector import NODE_PROBE_CATALOG


REMOTE_COLLECTOR = (
    Path(__file__).parents[2]
    / "src"
    / "ceph_incident_bundle"
    / "remote_collector.py"
)


class RemoteCollectorTests(unittest.TestCase):
    @staticmethod
    def write_successful_node_probe_commands(directory: Path) -> None:
        script = f"""#!{sys.executable}
import json
import os
from pathlib import Path
import sys
event_log = os.environ.get("PROBE_EVENT_LOG")
if event_log:
    with Path(event_log).open("a", encoding="utf-8") as events:
        events.write(json.dumps([os.path.basename(__file__), *sys.argv[1:]]) + "\\n")
print(os.path.basename(__file__))
"""
        for command in (
            "hostname",
            "date",
            "uname",
            "uptime",
            "lscpu",
            "free",
            "ps",
            "df",
            "lsblk",
            "iostat",
            "pvs",
            "vgs",
            "lvs",
            "ip",
            "dmesg",
            "systemctl",
            "podman",
            "docker",
            "chronyc",
            "ntpq",
            "timedatectl",
        ):
            executable = directory / command
            executable.write_text(script, encoding="utf-8")
            executable.chmod(0o755)

    def run_remote_collector(
        self, *arguments: str, environment: dict[str, str] | None = None
    ) -> subprocess.CompletedProcess[bytes]:
        return subprocess.run(
            [sys.executable, str(REMOTE_COLLECTOR), *arguments],
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            check=False,
        )

    def read_hostname_capture(
        self, archive_bytes: bytes, root: Path
    ) -> tuple[set[str], bytes, bytes, dict[str, object]]:
        archive = root / "node.tar.gz"
        archive.write_bytes(archive_bytes)
        with tarfile.open(archive, "r:gz") as opened:
            names = {member.name for member in opened.getmembers()}
            stdout_file = opened.extractfile("node/probes/hostname/stdout")
            stderr_file = opened.extractfile("node/probes/hostname/stderr")
            result_file = opened.extractfile("node/probes/hostname/result.json")
            assert stdout_file is not None
            assert stderr_file is not None
            assert result_file is not None
            return (
                names,
                stdout_file.read(),
                stderr_file.read(),
                json.load(result_file),
            )

    @staticmethod
    def expected_node_probe_catalog() -> tuple[tuple[str, str, tuple[str, ...]], ...]:
        """Return the hand-written #90 contract, independent of production."""
        expected = (
            ("hostname", "node", ("hostname",)),
            (
                "current-utc",
                "node",
                ("date", "-u", "+%Y-%m-%dT%H:%M:%SZ"),
            ),
            ("uname", "node", ("uname", "-a")),
            ("uptime", "node", ("uptime",)),
            ("lscpu", "node", ("lscpu",)),
            ("free", "node", ("free", "-h")),
            ("processes", "node", ("ps", "auxfww")),
            ("df", "node", ("df", "-hT")),
            (
                "lsblk",
                "node",
                (
                    "lsblk",
                    "-a",
                    "-o",
                    "NAME,MAJ:MIN,SIZE,TYPE,FSTYPE,MOUNTPOINT,MODEL,SERIAL",
                ),
            ),
            ("iostat", "node", ("iostat", "-xz", "1", "3")),
            ("pvs", "node", ("pvs", "--noheadings", "--separator", " ")),
            ("vgs", "node", ("vgs", "--noheadings", "--separator", " ")),
            ("lvs", "node", ("lvs", "--noheadings", "--separator", " ")),
            ("ip-address", "node", ("ip", "addr", "show")),
            ("dmesg", "node", ("dmesg", "-T")),
            (
                "failed-units",
                "node",
                ("systemctl", "--failed", "--no-pager", "--plain"),
            ),
            ("podman-ps", "node", ("podman", "ps", "-a")),
            ("docker-ps", "node", ("docker", "ps", "-a")),
            ("chronyc-tracking", "node", ("chronyc", "tracking")),
            ("chronyc-sources", "node", ("chronyc", "sources", "-v")),
            ("ntpq-peers", "node", ("ntpq", "-pn")),
            ("timedatectl-status", "node", ("timedatectl", "status")),
            (
                "timedatectl-show-timesync",
                "node",
                ("timedatectl", "show-timesync", "--all"),
            ),
            (
                "timedatectl-timesync-status",
                "node",
                ("timedatectl", "timesync-status"),
            ),
            (
                "systemd-timesyncd-status",
                "node",
                (
                    "systemctl",
                    "status",
                    "systemd-timesyncd",
                    "--no-pager",
                    "--plain",
                ),
            ),
        )
        return expected

    def test_node_probe_catalog_is_exactly_the_non_journal_baseline_and_time_catalog(
        self,
    ) -> None:
        self.assertEqual(NODE_PROBE_CATALOG, self.expected_node_probe_catalog())

    def test_hostname_probe_is_streamed_as_a_complete_node_archive(self) -> None:
        with TemporaryDirectory() as command_directory:
            fake_bin = Path(command_directory)
            self.write_successful_node_probe_commands(fake_bin)
            environment = os.environ.copy()
            environment["PATH"] = str(fake_bin)
            completed = subprocess.run(
                [
                    sys.executable,
                    str(REMOTE_COLLECTOR),
                    "--since-seconds",
                    "86400",
                    "--probe-timeout-seconds",
                    "30",
                ],
                env=environment,
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

    def test_external_subprocess_runs_the_full_catalog_in_documented_order(
        self,
    ) -> None:
        expected_catalog = self.expected_node_probe_catalog()
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self.write_successful_node_probe_commands(fake_bin)
            event_log = root / "probe-events.jsonl"
            environment = os.environ.copy()
            environment["PATH"] = str(fake_bin)
            environment["PROBE_EVENT_LOG"] = str(event_log)

            completed = self.run_remote_collector(
                "--since-seconds",
                "60",
                "--probe-timeout-seconds",
                "30",
                environment=environment,
            )

            executed = tuple(
                tuple(json.loads(line))
                for line in event_log.read_text(encoding="utf-8").splitlines()
            )
            archive = root / "node.tar.gz"
            archive.write_bytes(completed.stdout)
            with tarfile.open(archive, "r:gz") as opened:
                member_names = {member.name for member in opened.getmembers()}

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(
            executed, tuple(argv for _, _, argv in expected_catalog)
        )
        for probe_name, _, _ in expected_catalog:
            self.assertIn(f"node/probes/{probe_name}/result.json", member_names)

    def test_selected_ceph_source_includes_the_authorized_empty_ceph_shape(self) -> None:
        with TemporaryDirectory() as command_directory:
            fake_bin = Path(command_directory)
            self.write_successful_node_probe_commands(fake_bin)
            environment = os.environ.copy()
            environment["PATH"] = str(fake_bin)
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
                env=environment,
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
            self.write_successful_node_probe_commands(fake_bin)
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

    def test_missing_probe_is_captured_with_empty_raw_streams_and_a_concrete_diagnostic(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self.write_successful_node_probe_commands(fake_bin)
            hostname = fake_bin / "hostname"
            hostname.write_bytes(b"not executable")
            hostname.chmod(0o644)
            date = fake_bin / "date"
            date.write_text(
                f"""#!{sys.executable}
print("2026-08-21T00:00:00Z")
""",
                encoding="utf-8",
            )
            date.chmod(0o755)
            remote_temp = root / "remote-temp"
            remote_temp.mkdir()
            environment = os.environ.copy()
            environment["PATH"] = str(fake_bin)
            environment["TMPDIR"] = str(remote_temp)

            completed = self.run_remote_collector(
                "--since-seconds",
                "60",
                "--probe-timeout-seconds",
                "30",
                environment=environment,
            )
            names, probe_stdout, probe_stderr, result = self.read_hostname_capture(
                completed.stdout, root
            )
            archive = root / "node.tar.gz"
            with tarfile.open(archive, "r:gz") as opened:
                current_utc_stdout_file = opened.extractfile(
                    "node/probes/current-utc/stdout"
                )
                current_utc_result_file = opened.extractfile(
                    "node/probes/current-utc/result.json"
                )
                assert current_utc_stdout_file is not None
                assert current_utc_result_file is not None
                current_utc_stdout = current_utc_stdout_file.read()
                current_utc_result = json.load(current_utc_result_file)
            residue = list(remote_temp.glob("ceph-incident-node.*"))

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(probe_stdout, b"")
        self.assertEqual(probe_stderr, b"")
        self.assertEqual(
            set(result),
            {"argv", "started_at", "finished_at", "outcome", "exit_code", "error"},
        )
        self.assertEqual(result["argv"], ["hostname"])
        self.assertEqual(result["outcome"], "failed_to_start")
        self.assertIsNone(result["exit_code"])
        self.assertEqual(result["error"]["kind"], "PermissionError")
        self.assertIn("Permission denied", result["error"]["message"])
        self.assertIn(b"PermissionError: ", completed.stderr)
        capture_members = {
            name
            for name in names
            if name == "node/probes/hostname"
            or name.startswith("node/probes/hostname/")
        }
        self.assertEqual(
            capture_members,
            {
                "node/probes/hostname",
                "node/probes/hostname/stdout",
                "node/probes/hostname/stderr",
                "node/probes/hostname/result.json",
            },
        )
        self.assertEqual(current_utc_stdout, b"2026-08-21T00:00:00Z\n")
        self.assertEqual(current_utc_result["outcome"], "exited")
        self.assertEqual(current_utc_result["exit_code"], 0)
        self.assertEqual(residue, [])

    def test_timeout_preserves_partial_streams_and_records_a_timeout_error(
        self,
    ) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self.write_successful_node_probe_commands(fake_bin)
            hostname = fake_bin / "hostname"
            hostname.write_text(
                f"""#!{sys.executable}
import os
import time
os.write(1, b"before-timeout\\x00\\xff")
os.write(2, b"still-running\\x00\\xfe")
time.sleep(30)
""",
                encoding="utf-8",
            )
            hostname.chmod(0o755)
            remote_temp = root / "remote-temp"
            remote_temp.mkdir()
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"
            environment["TMPDIR"] = str(remote_temp)

            completed = self.run_remote_collector(
                "--since-seconds",
                "60",
                "--probe-timeout-seconds",
                "1",
                environment=environment,
            )
            _, probe_stdout, probe_stderr, result = self.read_hostname_capture(
                completed.stdout, root
            )
            archive = root / "node.tar.gz"
            with tarfile.open(archive, "r:gz") as opened:
                current_utc_stdout_file = opened.extractfile(
                    "node/probes/current-utc/stdout"
                )
                current_utc_result_file = opened.extractfile(
                    "node/probes/current-utc/result.json"
                )
                assert current_utc_stdout_file is not None
                assert current_utc_result_file is not None
                current_utc_stdout = current_utc_stdout_file.read()
                current_utc_result = json.load(current_utc_result_file)
            residue = list(remote_temp.glob("ceph-incident-node.*"))

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(probe_stdout, b"before-timeout\x00\xff")
        self.assertEqual(probe_stderr, b"still-running\x00\xfe")
        self.assertEqual(result["outcome"], "timed_out")
        self.assertIsNone(result["exit_code"])
        self.assertEqual(
            result["error"],
            {"kind": "timeout", "message": "hostname exceeded 1 seconds"},
        )
        self.assertIn(b"timeout: hostname exceeded 1 seconds", completed.stderr)
        self.assertTrue(current_utc_stdout)
        self.assertEqual(current_utc_result["outcome"], "exited")
        self.assertEqual(current_utc_result["exit_code"], 0)
        self.assertEqual(residue, [])

    def test_zero_probe_timeout_disables_only_that_probe_timeout(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            self.write_successful_node_probe_commands(fake_bin)
            hostname = fake_bin / "hostname"
            hostname.write_text(
                f"""#!{sys.executable}
import time
time.sleep(1.1)
print("hostname after delay")
""",
                encoding="utf-8",
            )
            hostname.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"

            completed = self.run_remote_collector(
                "--since-seconds",
                "60",
                "--probe-timeout-seconds",
                "0",
                environment=environment,
            )
            _, probe_stdout, probe_stderr, result = self.read_hostname_capture(
                completed.stdout, root
            )

        self.assertEqual(completed.returncode, 0)
        self.assertEqual(probe_stdout, b"hostname after delay\n")
        self.assertEqual(probe_stderr, b"")
        self.assertEqual(result["outcome"], "exited")
        self.assertEqual(result["exit_code"], 0)
        self.assertIsNone(result["error"])

    def test_failed_probe_does_not_stop_the_next_fixed_probe(self) -> None:
        with TemporaryDirectory() as directory:
            root = Path(directory)
            fake_bin = root / "bin"
            fake_bin.mkdir()
            hostname = fake_bin / "hostname"
            hostname.write_text(
                f"""#!{sys.executable}
import os
os.write(1, b"failed hostname")
raise SystemExit(7)
""",
                encoding="utf-8",
            )
            hostname.chmod(0o755)
            date = fake_bin / "date"
            date.write_text(
                f"""#!{sys.executable}
import os
os.write(1, b"2026-08-21T00:00:00Z\\n")
""",
                encoding="utf-8",
            )
            date.chmod(0o755)
            environment = os.environ.copy()
            environment["PATH"] = f"{fake_bin}{os.pathsep}{environment['PATH']}"

            completed = self.run_remote_collector(
                "--since-seconds",
                "60",
                "--probe-timeout-seconds",
                "30",
                environment=environment,
            )
            archive = root / "node.tar.gz"
            archive.write_bytes(completed.stdout)
            with tarfile.open(archive, "r:gz") as opened:
                hostname_result_file = opened.extractfile(
                    "node/probes/hostname/result.json"
                )
                current_utc_stdout_file = opened.extractfile(
                    "node/probes/current-utc/stdout"
                )
                current_utc_result_file = opened.extractfile(
                    "node/probes/current-utc/result.json"
                )
                assert hostname_result_file is not None
                assert current_utc_stdout_file is not None
                assert current_utc_result_file is not None
                hostname_result = json.load(hostname_result_file)
                current_utc_stdout = current_utc_stdout_file.read()
                current_utc_result = json.load(current_utc_result_file)

        self.assertNotEqual(completed.returncode, 0)
        self.assertEqual(hostname_result["outcome"], "exited")
        self.assertEqual(hostname_result["exit_code"], 7)
        self.assertEqual(current_utc_stdout, b"2026-08-21T00:00:00Z\n")
        self.assertEqual(current_utc_result["outcome"], "exited")
        self.assertEqual(current_utc_result["exit_code"], 0)
        self.assertIn(b"hostname Probe failed", completed.stderr)


if __name__ == "__main__":
    unittest.main()
